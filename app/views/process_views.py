# -*- coding: utf-8 -*-
"""数据处理和展示：选年份范围、开始处理、处理摘要、月度汇总表与走势图"""
from datetime import datetime

from flask import Blueprint, flash, g, redirect, render_template, request, send_file, url_for

from app.constants import CATEGORIES, CATEGORY_SUBS, REGIONS
from app.services import process_service, state_service

bp = Blueprint("process", __name__)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@bp.route("/process")
def index():
    """上面是年份范围和各年份的状态，中间是最近一次处理的摘要，下面是处理结果（表和图）"""
    years = state_service.year_status()
    v = process_service.clean_view(request.args)
    rows, total, page, pages = process_service.monthly_rows(v, request.args.get("page", 1, type=int))
    log = process_service.get_log(request.args.get("log", type=int))

    def link(**change):
        args = {**v, "page": page, **change}
        return url_for("process.index", **{k: a for k, a in args.items() if a not in ("", None)}) + "#result"

    return render_template(
        "process.html", years=years, log=log, v=v, rows=rows, total=total, page=page, pages=pages, link=link,
        stale=[y["year"] for y in years if not y["processed"]],
        chart=process_service.monthly_chart(v), zero=process_service.zero_info(v),
        export_url=url_for("process.export", **{k: a for k, a in v.items() if a}),
        regions=REGIONS, categories=CATEGORIES, category_subs=CATEGORY_SUBS)


@bp.route("/process/run", methods=["POST"])
def run():
    """点“开始处理”提交到这里。处理完跳回页面，显示这一次的摘要"""
    start, end = request.form.get("start_year", type=int), request.form.get("end_year", type=int)
    try:
        log_id, seconds = process_service.run(start, end, g.user["id"])
    except process_service.ProcessError as e:
        flash(str(e), "danger")
        return redirect(url_for("process.index"))
    flash(f"处理完成：{start}–{end} 年，用时 {seconds} 秒", "success")
    return redirect(url_for("process.index", log=log_id))


@bp.route("/process/export")
def export():
    """按当前的层级、地区、类别导出月度汇总表"""
    v = process_service.clean_view(request.args)
    name = f"月度汇总_{v['level']}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    return send_file(process_service.monthly_xlsx(v), as_attachment=True, download_name=name, mimetype=XLSX)
