# -*- coding: utf-8 -*-
"""数据导入和展示：首页概览、订单查询、导出、表格导入（上传 → 预览 → 确认）、对话框录入、导入记录"""
from datetime import datetime

from flask import (Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file,
                   session, url_for)

from app.constants import (CATEGORIES, CATEGORY_SUBS, PROVINCES, REGIONS, SEGMENTS, SHIP_MODES, SOURCES,
                           TEMPLATE_COLUMNS)
from app.services import export_service, import_service, order_service, state_service

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHOW_FAILED, SHOW_SKIPPED = 200, 50        # 页面上最多列出多少条失败、跳过的行，完整的在下载文件里

bp = Blueprint("data", __name__)


@bp.route("/")
def index():
    """首页：数据概览"""
    return render_template("index.html", info=order_service.overview(),
                           years=state_service.year_status(), chart=order_service.monthly_sales())


@bp.route("/data")
def orders():
    """订单查询：筛选条件、排序、页码都在网址里，刷新或把网址发给别人看到的是同一页"""
    f = order_service.clean_filters(request.args)
    rows, total, page, pages = order_service.query_orders(f, request.args.get("page", 1, type=int))

    def link(**change):
        """在当前条件的基础上改几项，生成新的网址（空的条件不写进网址）"""
        args = {**f, "page": page, **change}
        return url_for("data.orders", **{k: v for k, v in args.items() if v not in ("", None)})

    return render_template("orders.html", f=f, rows=rows, total=total, page=page, pages=pages, link=link,
                           export_url=url_for("data.export", **{k: v for k, v in f.items() if v}),
                           regions=REGIONS, categories=CATEGORIES, category_subs=CATEGORY_SUBS,
                           sources=SOURCES, ship_modes=SHIP_MODES, segments=SEGMENTS, provinces=PROVINCES)


@bp.route("/data/export")
def export():
    """按当前筛选条件导出 Excel"""
    f = order_service.clean_filters(request.args)
    buf, _ = export_service.orders_to_xlsx(f)
    name = f"订单导出_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    return send_file(buf, as_attachment=True, download_name=name, mimetype=XLSX)


# ---------------- 表格导入：上传 → 预览 → 确认 → 结果 ----------------
@bp.route("/data/import/template")
def import_template():
    """下载导入模板"""
    return send_file(import_service.template_xlsx(), as_attachment=True, download_name="订单导入模板.xlsx",
                     mimetype=XLSX)


def pending_file():
    """这个用户上传了、还没确认的文件：(路径, 原文件名)。没有就返回 (None, "")"""
    pending = session.get("pending_import") or {}
    return import_service.temp_path(pending.get("token")), pending.get("name", "")


def drop_pending():
    pending = session.pop("pending_import", None)
    if pending:
        import_service.remove_temp(pending.get("token"))


@bp.route("/data/import", methods=["GET", "POST"])
def import_page():
    """GET：上传页面。POST：收下文件，存到临时文件夹，转到预览页。这一步不写数据库"""
    if request.method == "POST":
        drop_pending()                                    # 之前传了没确认的文件作废
        file = request.files.get("file")
        if file is None or not file.filename:
            flash("请先选择要上传的文件", "warning")
            return redirect(url_for("data.import_page"))
        try:
            token, name = import_service.save_upload(file)
        except import_service.FileRejected as e:
            flash(str(e), "danger")
            return redirect(url_for("data.import_page"))
        session["pending_import"] = {"token": token, "name": name}
        return redirect(url_for("data.import_preview"))
    return render_template("import.html", columns=TEMPLATE_COLUMNS)


@bp.route("/data/import/preview")
def import_preview():
    """预览：校验上传的文件，显示将导入、失败、跳过各多少行。只读数据库，不写"""
    path, name = pending_file()
    if path is None:
        flash("没有待确认的文件，请重新上传", "warning")
        return redirect(url_for("data.import_page"))
    try:
        result = import_service.preview_file(path)
    except import_service.FileRejected as e:
        drop_pending()
        flash(str(e), "danger")
        return redirect(url_for("data.import_page"))
    return render_template("import_preview.html", name=name, total=result["total"], n_ok=len(result["rows"]),
                           failed=result["failed"], skipped=result["skipped"], columns=TEMPLATE_COLUMNS,
                           rows=import_service.preview_rows(result["rows"]),
                           token=session["pending_import"]["token"],
                           show_failed=SHOW_FAILED, show_skipped=SHOW_SKIPPED)


@bp.route("/data/import/preview/problems")
def import_preview_problems():
    """预览阶段下载失败明细"""
    path, _ = pending_file()
    if path is None:
        abort(404)
    result = import_service.preview_file(path)
    return send_file(import_service.problems_xlsx(result["failed"], result["skipped"]), as_attachment=True,
                     download_name="导入失败明细.xlsx", mimetype=XLSX)


@bp.route("/data/import/confirm", methods=["POST"])
def import_confirm():
    """确认导入：重新校验一遍再写入，写完删掉临时文件"""
    path, name = pending_file()
    if path is None or request.form.get("token") != session["pending_import"]["token"]:
        flash("这份文件已经处理过或已失效，请重新上传", "warning")
        return redirect(url_for("data.import_page"))
    try:
        batch_id, _ = import_service.import_file(path, name, g.user["id"])
    except import_service.FileRejected as e:
        batch_id = None
        flash(str(e), "danger")
    finally:
        drop_pending()
    if batch_id is None:
        flash("没有可以导入的行，数据没有变化", "warning")
        return redirect(url_for("data.import_page"))
    return redirect(url_for("data.import_result", batch_id=batch_id))


@bp.route("/data/import/cancel", methods=["POST"])
def import_cancel():
    drop_pending()
    flash("已取消，数据没有变化", "secondary")
    return redirect(url_for("data.import_page"))


@bp.route("/data/import/result/<int:batch_id>")
def import_result(batch_id):
    """导入结果：成功、失败、跳过各多少行，失败明细，导入时间"""
    batch = import_service.get_batch(batch_id)
    if batch is None:
        abort(404)
    return render_template("import_result.html", batch=batch, detail=import_service.batch_detail(batch_id),
                           show_failed=SHOW_FAILED, show_skipped=SHOW_SKIPPED)


@bp.route("/data/import/result/<int:batch_id>/problems")
def import_result_problems(batch_id):
    """下载这一批的失败明细"""
    detail = import_service.batch_detail(batch_id)
    if detail is None:
        abort(404)
    return send_file(import_service.problems_xlsx(detail["failed"], detail["skipped"]), as_attachment=True,
                     download_name=f"导入失败明细_批次{batch_id}.xlsx", mimetype=XLSX)


# ---------------- 对话框录入 ----------------
@bp.route("/data/orders/new", methods=["POST"])
def order_new():
    """“新增订单”对话框提交到这里。校验不通过时返回每个字段的错误，页面在对话框里显示，不关闭对话框"""
    form = request.get_json(silent=True) or {}
    row_id, batch_id, errors = import_service.add_order(form, g.user["id"])
    if errors:
        return jsonify(ok=False, errors=errors)
    flash(f"订单已保存：行 ID {row_id}，导入批次 {batch_id}。录错了可以到“导入记录”里撤销这一条", "success")
    return jsonify(ok=True, row_id=row_id, batch_id=batch_id)


# ---------------- 导入记录、撤销、恢复 ----------------
@bp.route("/data/batches")
def batches():
    return render_template("batches.html", batches=import_service.list_batches(),
                           imported=import_service.imported_count())


def changed_message(deleted, years):
    text = f"删除了 {deleted} 条订单。"
    if years:
        text += f"受影响的年份（{'、'.join(str(y) for y in years)}）需要重新处理"
    return text


@bp.route("/data/batches/<int:batch_id>/revoke", methods=["POST"])
def batch_revoke(batch_id):
    result = import_service.revoke_batch(batch_id)
    if result is None:
        flash("这个批次不存在，或者已经撤销过了", "warning")
    else:
        flash(f"批次 {batch_id} 已撤销，" + changed_message(*result), "success")
    return redirect(url_for("data.batches"))


@bp.route("/data/batches/restore", methods=["POST"])
def batch_restore():
    deleted, years = import_service.restore_original()
    flash("已恢复原始数据，" + changed_message(deleted, years), "success")
    return redirect(url_for("data.batches"))
