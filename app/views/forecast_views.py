# -*- coding: utf-8 -*-
"""数据预测、预测结果展示：预测设置对话框、后台计算与进度、结果的图和表、历史记录、导出"""
from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

from app.constants import CATEGORIES, CATEGORY_SUBS, REGIONS
from app.services import forecast_result_service as results
from app.services import forecast_service, state_service

bp = Blueprint("forecast", __name__)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def task_or_404(task_id):
    task = results.get_task(task_id)
    if task is None:
        abort(404)
    return task


# ---------------- 数据预测 ----------------
@bp.route("/forecast")
def index():
    """数据预测页：说明、可以预测的年份、“新建预测”按钮和预测设置对话框"""
    running = forecast_service.running_task()
    return render_template("forecast.html", options=forecast_service.year_options(),
                           years=state_service.year_status(), running=running,
                           regions=REGIONS, categories=CATEGORIES, category_subs=CATEGORY_SUBS)


@bp.route("/forecast/start", methods=["POST"])
def start():
    """
    对话框点“开始预测”提交到这里，返回 JSON。
    成功：给出结果页的网址（新任务在后台计算，结果页上显示进度；设置和数据都没变时直接用以前的结果）
    不成功：给出原因；历史年份没处理时带上 need_process，已有任务在运行时带上那个任务的网址
    """
    form = request.get_json(silent=True) or {}
    try:
        task_id, reused = forecast_service.start(form, g.user["id"], force=bool(form.get("force")))
    except forecast_service.NeedProcess as e:
        return jsonify(ok=False, message=str(e), need_process=True)
    except forecast_service.TaskRunning as e:
        return jsonify(ok=False, message=str(e), running_url=url_for("forecast.result", task_id=e.task_id))
    except forecast_service.ForecastError as e:
        return jsonify(ok=False, message=str(e))
    return jsonify(ok=True, task_id=task_id, reused=reused,
                   url=url_for("forecast.result", task_id=task_id, reused=1 if reused else None))


@bp.route("/forecast/result/<int:task_id>/rerun", methods=["POST"])
def rerun(task_id):
    """“重新计算”：用这个任务的设置再算一次，不复用以前的结果"""
    task = task_or_404(task_id)
    try:
        new_id, _ = forecast_service.start(forecast_service.settings_of(task), g.user["id"], force=True)
    except forecast_service.ForecastError as e:
        flash(str(e), "warning")
        return redirect(url_for("forecast.result", task_id=task_id))
    return redirect(url_for("forecast.result", task_id=new_id))


# ---------------- 预测结果 ----------------
@bp.route("/forecast/result/<int:task_id>")
def result(task_id):
    """结果页。任务还在算：显示进度；失败：显示原因；算完：显示图和表"""
    task = task_or_404(task_id)
    done = task["status"] == forecast_service.DONE
    return render_template(
        "forecast_result.html", task=task, settings=results.settings_text(task),
        reused=done and request.args.get("reused") == "1",
        changed=done and results.data_changed(task), note=results.actual_note(task) if done else "",
        options=results.series_options(task), current=results.pick_series(task, request.args),
        purchase_text=results.purchase_text(task))


@bp.route("/api/forecast/<int:task_id>/status")
def status(task_id):
    """进度接口：结果页每 2 秒来问一次"""
    task = task_or_404(task_id)
    return jsonify(status=task["status"], done=task["progress_done"], total=task["progress_total"],
                   error=task["error_msg"])


@bp.route("/api/forecast/<int:task_id>/series")
def series(task_id):
    """一条序列的历史销量和预测结果，结果页切换地区、类别、子类别时来取"""
    task = task_or_404(task_id)
    return jsonify(results.series_data(task, *results.pick_series(task, request.args)))


@bp.route("/forecast/result/<int:task_id>/export")
def export(task_id):
    task = task_or_404(task_id)
    if task["status"] != forecast_service.DONE:
        abort(404)
    name = f"预测结果_{task['target_year']}年_任务{task_id}.xlsx"
    return send_file(results.result_xlsx(task), as_attachment=True, download_name=name, mimetype=XLSX)


@bp.route("/forecast/history")
def history():
    """历史预测记录"""
    return render_template("forecast_history.html", tasks=results.list_tasks())


@bp.route("/forecast/result/<int:task_id>/delete", methods=["POST"])
def delete(task_id):
    outcome = results.delete_task(task_id)
    if outcome is None:
        flash("这条预测记录不存在，可能已经删除了", "warning")
    elif outcome is False:
        flash("这个预测任务还在运行，等它结束后才能删除", "warning")
    else:
        flash(f"预测记录 {task_id} 已删除", "success")
    return redirect(url_for("forecast.history"))
