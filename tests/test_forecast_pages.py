# -*- coding: utf-8 -*-
"""
预测页面背后的逻辑：提交预测后在后台计算、进度、结果页的数据、结果复用、同一时间只跑一个任务、
失败时显示原因、历史记录和删除、导出，以及“页面上不出现任何准确度指标”。只用测试库。
浏览器里的点击操作在 tests/test_e2e.py 里测
"""
import re
import time
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from config import TEST_DB_NAME
from conftest import log_in, require_test_db, run_sql
from step3_load_mysql import load

ONE = {"target_year": 2025, "history_start": 2022, "region": "华东", "category": "办公用品", "level": "类别"}
APP_DIR = Path(__file__).resolve().parent.parent / "app"
# 系统页面上不能出现的词：各种准确度指标的名字
BANNED = re.compile(r"R²|R\^2|\bR2\b|RMSE|WAPE|\bMAE\b|MAPE|误差")


def clear_forecasts():
    run_sql(["DELETE FROM forecast_detail", "DELETE FROM forecast_task"])


@pytest.fixture(scope="module", autouse=True)
def original_data():
    load(require_test_db(TEST_DB_NAME), force=True)
    yield
    clear_forecasts()
    load(require_test_db(TEST_DB_NAME), force=True)


@pytest.fixture(autouse=True)
def clean_tasks():
    """每个测试都从没有任何预测任务开始"""
    clear_forecasts()
    yield
    run_sql(["UPDATE year_state SET data_version = 1, processed_version = 1 WHERE `year` <= 2024",
             "DELETE FROM year_state WHERE `year` > 2024"])


@pytest.fixture
def client(app):
    return log_in(app.test_client())


def csrf(client):
    html = client.get("/forecast").get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def submit(client, base=ONE, **change):
    """相当于在对话框里点“开始预测”，返回服务器的回答"""
    return client.post("/forecast/start", json={**base, **change}, headers={"X-CSRFToken": csrf(client)}).get_json()


def wait(client, task_id, seconds=120):
    """等后台算完，返回最后的状态"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        status = client.get(f"/api/forecast/{task_id}/status").get_json()
        if status["status"] != "运行中":
            return status
        time.sleep(0.3)
    raise AssertionError(f"任务 {task_id} 等了 {seconds} 秒还没算完")


def run(client, base=ONE, **change):
    """提交并等它算完，返回任务号"""
    answer = submit(client, base, **change)
    assert answer["ok"], answer
    assert wait(client, answer["task_id"])["status"] == "已完成"
    return answer["task_id"]


def fake_running_task(done=3, total=18):
    run_sql(["INSERT INTO forecast_task (history_start, history_end, target_year, region, category, level_name, "
             f"status, progress_done, progress_total, user_id) VALUES (2021, 2024, 2025, '全部', '全部', '类别', "
             f"'运行中', {done}, {total}, 1)"])


def series(client, task_id, **params):
    return client.get(f"/api/forecast/{task_id}/series", query_string=params).get_json()


# ---------------- 数据预测页 ----------------
def test_forecast_page(client):
    html = client.get("/forecast").get_data(as_text=True)
    assert 'id="newForecastBtn"' in html and 'id="forecastModal"' in html and "js/forecast.js" in html
    assert '"options": {"2024": [2021], "2025": [2021, 2022]}' in html       # 对话框里年份的选项
    table = html.split('id="optionTable"')[1].split("</table>")[0]
    assert "2024 年" in table and "2021–2023 年" in table and "2022–2024 年" in table
    assert 'id="runningNote"' not in html and 'id="noOptions"' not in html
    for name in ("target_year", "history_start", "region", "category", "level", "sub_category", "inventory",
                 "lead_time", "service_level"):
        assert f'name="{name}"' in html


# ---------------- 提交、后台计算、结果 ----------------
def test_start_runs_in_background(client, query):
    """提交后马上得到结果页的网址，不用等算完；算完后结果页的数据和数据库里保存的一致"""
    started = time.time()
    answer = submit(client)
    assert answer["ok"] and answer["reused"] is False and answer["url"] == f"/forecast/result/{answer['task_id']}"
    assert time.time() - started < 1.5                           # 提交这一步很快就回来了，计算在后台
    task_id = answer["task_id"]
    assert wait(client, task_id) == {"status": "已完成", "done": 1, "total": 1, "error": None}

    html = client.get(answer["url"]).get_data(as_text=True)
    for text in ("预测年份", "2025 年", "2022–2024 年", "华东", "办公用品", "0 件", "1 个月", "95%"):
        assert text in html.split('id="settings"')[1].split("</div>")[0]
    assert 'id="forecastChart"' in html and 'id="forecastTable"' in html and 'id="exportBtn"' in html
    assert "补货周期 1 个月，每月进一次货" in html and 'id="reusedNote"' not in html
    assert 'id="actualNote"' not in html and 'id="changedNote"' not in html and 'id="progressCard"' not in html

    d = series(client, task_id)
    assert d["title"] == "华东 · 办公用品" and d["has_actual"] is False
    assert d["history"]["months"][0] == "2022-01" and d["history"]["months"][-1] == "2024-12"
    assert d["history"]["quantity"] == [r[0] for r in query(
        "SELECT quantity FROM monthly_sales_category WHERE region = '华东' AND category = '办公用品' "
        "AND ym >= '2022-01' ORDER BY ym")]
    saved = query("SELECT ym, forecast_qty, lower_qty, upper_qty, actual_qty, safety_stock, suggested_purchase "
                  "FROM forecast_detail WHERE task_id = %s ORDER BY ym", (task_id,))
    assert len(saved) == 12 and [r["ym"] for r in d["rows"]] == [f"2025-{m:02d}" for m in range(1, 13)]
    for row, (ym, fc, lo, hi, actual, ss, plan) in zip(d["rows"], saved):
        assert (row["forecast"], row["lower"], row["upper"], row["actual"], row["safety_stock"], row["purchase"]) \
            == (float(fc), float(lo), float(hi), actual, float(ss), plan)
        assert row["lower"] <= row["forecast"] <= row["upper"] and row["actual"] is None


def test_progress_page(client, query):
    """任务还在算的时候，结果页显示进度条和“已完成 X / Y”"""
    fake_running_task(done=3, total=18)
    task_id = query("SELECT id FROM forecast_task")[0][0]
    html = client.get(f"/forecast/result/{task_id}").get_data(as_text=True)
    assert 'id="progressCard"' in html and "已完成 3 / 18" in html and "width: 17%" in html
    assert f'data-url="/api/forecast/{task_id}/status"' in html and 'id="forecastChart"' not in html
    assert client.get(f"/api/forecast/{task_id}/status").get_json() == \
        {"status": "运行中", "done": 3, "total": 18, "error": None}
    assert client.get(f"/forecast/result/{task_id}/export").status_code == 404        # 没算完不能导出
    assert 'id="runningNote"' in client.get("/forecast").get_data(as_text=True)


def test_actual_values_shown_for_2024(client, query):
    """用 2021–2023 预测 2024：2024 年已处理，结果里有 12 个月的实际销量"""
    task_id = run(client, target_year=2024, history_start=2021)
    d = series(client, task_id)
    actual = [r[0] for r in query("SELECT quantity FROM monthly_sales_category WHERE region = '华东' "
                                  "AND category = '办公用品' AND ym LIKE '2024-%' ORDER BY ym")]
    assert d["has_actual"] and [r["actual"] for r in d["rows"]] == actual and len(actual) == 12
    assert len(d["history"]["months"]) == 36 and d["history"]["months"][-1] == "2023-12"
    assert 'id="actualNote"' not in client.get(f"/forecast/result/{task_id}").get_data(as_text=True)


def test_note_when_target_year_not_processed(client):
    """预测年份有订单但还没处理：照常预测，不显示实际值，并说明原因"""
    run_sql(["UPDATE year_state SET data_version = 2 WHERE `year` = 2024"])
    task_id = run(client, target_year=2024, history_start=2021)
    assert series(client, task_id)["has_actual"] is False
    html = client.get(f"/forecast/result/{task_id}").get_data(as_text=True)
    assert 'id="actualNote"' in html and "该年数据有更新尚未处理，未显示实际值" in html


def test_series_switching(client, query):
    """子类别层级的任务：可以按地区、类别、子类别切换；参数不对时回到第一条"""
    task_id = run(client, category="全部", level="子类别", lead_time=3)
    html = client.get(f"/forecast/result/{task_id}").get_data(as_text=True)
    assert 'id="s_sub"' in html and '"办公用品": ["信封", "器具"' in html and "每 3 个月进一次货" in html
    assert series(client, task_id)["title"] == "华东 · 办公用品 · 信封"
    d = series(client, task_id, category="家具", sub_category="桌子")
    assert d["title"] == "华东 · 家具 · 桌子" and len(d["rows"]) == 12
    assert [i for i, r in enumerate(d["rows"]) if r["purchase"] > 0] in ([0, 3, 6, 9], [0, 3, 6], [3, 6, 9], [0])
    assert all(r["purchase"] == 0 for i, r in enumerate(d["rows"]) if i % 3)       # 不进货的月份是 0
    assert d["history"]["quantity"] == [r[0] for r in query(
        "SELECT quantity FROM monthly_sales WHERE region = '华东' AND sub_category = '桌子' "
        "AND ym BETWEEN '2022-01' AND '2024-12' ORDER BY ym")]
    assert series(client, task_id, region="火星", category="家具", sub_category="纸张")["title"] == "华东 · 家具 · 书架"
    assert query("SELECT COUNT(*) FROM forecast_detail WHERE task_id = %s", (task_id,))[0][0] == 17 * 12


# ---------------- 不能预测的情况 ----------------
def test_unprocessed_history_refused(client, query):
    """历史年份里有没处理的年份：不建任务，回答里带着“去数据处理”的标记"""
    run_sql(["UPDATE year_state SET data_version = 2 WHERE `year` = 2023"])
    answer = submit(client)
    assert answer == {"ok": False, "need_process": True,
                      "message": "所选年份的数据有更新或尚未处理，请先到【数据处理】处理 2023–2023 年"}
    assert query("SELECT COUNT(*) FROM forecast_task")[0][0] == 0


def test_bad_settings_refused(client, query):
    assert submit(client, target_year=2030)["message"].startswith("这个年份不能预测")
    assert submit(client, lead_time=9) == {"ok": False, "message": "补货周期只能是 1、2、3 个月"}
    assert client.post("/forecast/start", json=ONE).status_code == 400          # 不带令牌
    assert query("SELECT COUNT(*) FROM forecast_task")[0][0] == 0


def test_one_task_at_a_time(client, query):
    """已经有任务在运行时，再提交会被拒绝，并告诉用户去看正在运行的那个"""
    fake_running_task()
    running = query("SELECT id FROM forecast_task")[0][0]
    answer = submit(client)
    assert answer["ok"] is False and answer["running_url"] == f"/forecast/result/{running}"
    assert f"已有一个预测任务正在运行（任务 {running}）" in answer["message"]
    assert query("SELECT COUNT(*) FROM forecast_task")[0][0] == 1
    # “重新计算”也一样被拒绝
    run_sql([f"UPDATE forecast_task SET status = '失败' WHERE id = {running}"])
    done = run(client)
    fake_running_task()
    html = client.post(f"/forecast/result/{done}/rerun", data={"csrf_token": csrf(client)},
                       follow_redirects=True).get_data(as_text=True)
    assert "已有一个预测任务正在运行" in html and query("SELECT COUNT(*) FROM forecast_task")[0][0] == 3


def test_second_submit_while_first_is_computing(client, query, monkeypatch):
    """第一个任务还没算完（这里让后台线程不启动来模拟），马上再提交一次：第二次被拒绝"""
    from app.services import forecast_service
    monkeypatch.setattr(forecast_service, "run_in_background", lambda task_id: None)
    first = submit(client)
    second = submit(client, lead_time=2)
    assert first["ok"] and second["ok"] is False and "正在运行" in second["message"]
    assert query("SELECT status FROM forecast_task") == (("运行中",),)


# ---------------- 结果复用 ----------------
def test_reuse_and_recalculate(client, query):
    first = run(client)
    again = submit(client)
    assert again["ok"] and again["reused"] is True and again["task_id"] == first
    assert again["url"] == f"/forecast/result/{first}?reused=1"
    assert query("SELECT COUNT(*) FROM forecast_task")[0][0] == 1                # 没有新建任务
    html = client.get(again["url"]).get_data(as_text=True)
    created = query("SELECT created_at FROM forecast_task")[0][0]
    assert 'id="reusedNote"' in html and f"数据未变化，显示 {created.month} 月 {created.day} 日的结果" in html
    assert 'id="rerunBtn"' in html

    # 点“重新计算”：一定新算一次
    resp = client.post(f"/forecast/result/{first}/rerun", data={"csrf_token": csrf(client)})
    second = int(resp.headers["Location"].rsplit("/", 1)[1])
    assert second != first and wait(client, second)["status"] == "已完成"
    assert 'id="reusedNote"' not in client.get(f"/forecast/result/{second}").get_data(as_text=True)
    assert query("SELECT forecast_qty FROM forecast_detail WHERE task_id = %s ORDER BY ym", (first,)) == \
        query("SELECT forecast_qty FROM forecast_detail WHERE task_id = %s ORDER BY ym", (second,))
    assert submit(client)["task_id"] == second                                   # 之后复用的是最新的那一次

    # 设置变了、或者数据变了，就不复用
    for change in ({"lead_time": 2}, {"inventory": 10}, {"service_level": 0.99}, {"history_start": 2021},
                   {"level": "子类别"}):
        answer = submit(client, **change)
        assert answer["reused"] is False and wait(client, answer["task_id"])["status"] == "已完成", change
    run_sql(["UPDATE year_state SET data_version = 2, processed_version = 2 WHERE `year` = 2023"])   # 2023 年的数据变过又处理了
    answer = submit(client)
    assert answer["reused"] is False and answer["task_id"] > second
    wait(client, answer["task_id"])
    assert query("SELECT year_versions FROM forecast_task WHERE id = %s", (answer["task_id"],))[0][0] \
        == "2022:1,2023:2,2024:1,2025:0"
    assert 'id="changedNote"' in client.get(f"/forecast/result/{second}").get_data(as_text=True)


def test_failed_task_is_not_reused(client, query, monkeypatch):
    from app.services import forecast_service
    monkeypatch.setattr(forecast_service, "forecast_series",
                        lambda job: {"region": job["region"], "category": job["category"], "error": "模拟的原因"})
    first = submit(client)
    assert wait(client, first["task_id"])["status"] == "失败"
    monkeypatch.undo()
    second = submit(client)
    assert second["reused"] is False and wait(client, second["task_id"])["status"] == "已完成"


# ---------------- 失败 ----------------
def test_failure_shows_reason(client, query, monkeypatch):
    from app.services import forecast_service
    monkeypatch.setattr(forecast_service, "forecast_series",
                        lambda job: {"region": job["region"], "category": job["category"],
                                     "error": "RuntimeError: 所有候选阶数都拟合失败"})
    task_id = submit(client)["task_id"]
    status = wait(client, task_id)
    assert status["status"] == "失败" and "华东-办公用品 预测失败：RuntimeError: 所有候选阶数都拟合失败" in status["error"]
    html = client.get(f"/forecast/result/{task_id}").get_data(as_text=True)
    assert 'id="failedNote"' in html and "华东-办公用品 预测失败" in html and "用同样的设置再试一次" in html
    assert 'id="forecastChart"' not in html and 'id="progressCard"' not in html
    assert query("SELECT COUNT(*) FROM forecast_detail")[0][0] == 0


def test_unexpected_error_in_background_thread(client, monkeypatch):
    """后台线程里出了没料到的错：任务也要标成失败并写明原因，不能一直显示运行中"""
    from app.services import forecast_service

    def boom(task):
        raise ZeroDivisionError("没料到的错")
    monkeypatch.setattr(forecast_service, "build_jobs", boom)
    status = wait(client, submit(client)["task_id"])
    assert status["status"] == "失败" and "ZeroDivisionError: 没料到的错" in status["error"]


# ---------------- 导出 ----------------
def test_export_matches_page(client):
    task_id = run(client, category="全部", level="子类别")
    resp = client.get(f"/forecast/result/{task_id}/export")
    assert resp.status_code == 200 and "attachment" in resp.headers["Content-Disposition"]
    sheets = pd.read_excel(BytesIO(resp.data), sheet_name=None)
    assert list(sheets) == ["预测结果", "预测设置"]
    out = sheets["预测结果"]
    assert list(out.columns) == ["地区", "类别", "子类别", "预测月份", "预测销量", "预测下限", "预测上限",
                                 "实际销量", "安全库存", "建议进货量"] and len(out) == 17 * 12
    for category, sub in (("办公用品", "纸张"), ("技术", "电话"), ("家具", "桌子")):
        page = series(client, task_id, category=category, sub_category=sub)["rows"]
        rows = out[(out["类别"] == category) & (out["子类别"] == sub)]
        assert rows["预测月份"].tolist() == [r["ym"] for r in page]
        assert rows["预测销量"].tolist() == [r["forecast"] for r in page]
        assert rows["预测下限"].tolist() == [r["lower"] for r in page]
        assert rows["预测上限"].tolist() == [r["upper"] for r in page]
        assert rows["安全库存"].tolist() == [r["safety_stock"] for r in page]
        assert rows["建议进货量"].tolist() == [r["purchase"] for r in page]
    settings = dict(sheets["预测设置"].values.tolist())
    assert settings["预测年份"] == "2025 年" and settings["历史数据"] == "2022–2024 年" and settings["子类别"] == "全部"

    task_id = run(client, target_year=2024, history_start=2021)              # 类别层级：没有子类别一列，有实际销量
    out = pd.read_excel(BytesIO(client.get(f"/forecast/result/{task_id}/export").data))
    page = series(client, task_id)["rows"]
    assert "子类别" not in out.columns and out["实际销量"].tolist() == [r["actual"] for r in page]


# ---------------- 历史记录、删除 ----------------
def test_history_and_delete(client, query):
    assert "还没有预测记录" in client.get("/forecast/history").get_data(as_text=True)
    first = run(client)
    second = run(client, lead_time=3, service_level=0.99)
    fake_running_task()
    running = query("SELECT MAX(id) FROM forecast_task")[0][0]
    html = client.get("/forecast/history").get_data(as_text=True)
    rows = re.findall(r'<tr data-task="(\d+)">(.*?)</tr>', html, re.S)
    assert [int(r[0]) for r in rows] == [running, second, first]             # 新的在前
    assert "3 个月" in rows[1][1] and "99%" in rows[1][1] and "已完成" in rows[1][1] and "delete-btn" in rows[1][1]
    assert "运行中 3 / 18" in rows[0][1] and "delete-btn" not in rows[0][1]   # 正在运行的不能删
    assert 'id="taskCount">3<' in html and 'id="deleteModal"' in html

    token = csrf(client)
    html = client.post(f"/forecast/result/{running}/delete", data={"csrf_token": token},
                       follow_redirects=True).get_data(as_text=True)
    assert "还在运行，等它结束后才能删除" in html
    html = client.post(f"/forecast/result/{first}/delete", data={"csrf_token": token},
                       follow_redirects=True).get_data(as_text=True)
    assert f"预测记录 {first} 已删除" in html
    assert query("SELECT COUNT(*) FROM forecast_detail WHERE task_id = %s", (first,))[0][0] == 0
    assert [r[0] for r in query("SELECT id FROM forecast_task ORDER BY id")] == [second, running]
    assert "不存在" in client.post(f"/forecast/result/{first}/delete", data={"csrf_token": token},
                                  follow_redirects=True).get_data(as_text=True)
    assert client.get(f"/forecast/result/{first}").status_code == 404
    assert client.post(f"/forecast/result/{second}/delete").status_code == 400     # 不带令牌


def test_login_required(anon_client):
    for url in ("/forecast", "/forecast/history", "/forecast/result/1", "/api/forecast/1/status",
                "/api/forecast/1/series", "/forecast/result/1/export"):
        resp = anon_client.get(url)
        assert resp.status_code == 302 and "/login" in resp.headers["Location"], url


# ---------------- 页面上不出现任何准确度指标 ----------------
def strip_tokens(html):
    """CSRF 令牌是一串随机字符，里面碰巧出现 MAE 之类的字母不算数，检查前先去掉"""
    html = re.sub(r'name="csrf-token" content="[^"]*"', "", html)
    return re.sub(r'name="csrf_token" value="[^"]*"', "", html)


def test_no_error_metrics_anywhere(client, monkeypatch):
    """预测相关的页面、接口、导出文件、页面脚本里，都不能出现 R²、RMSE、WAPE、MAE、MAPE、误差"""
    assert BANNED.search("测试集 RMSE 25.14") and BANNED.search("预测误差") and BANNED.search("R² = 0.75")
    assert BANNED.search("MAE 和 MAPE") and not BANNED.search("安全库存 86.77，建议进货量 174")

    done = run(client, target_year=2024, history_start=2021)                 # 有实际销量的结果
    sub = run(client, level="子类别", lead_time=2)
    fake_running_task()
    texts = {}
    for url in ("/forecast", "/forecast/history", f"/forecast/result/{done}", f"/forecast/result/{done}?reused=1",
                f"/forecast/result/{sub}", f"/api/forecast/{done}/series", f"/api/forecast/{sub}/series",
                f"/api/forecast/{done}/status", "/static/js/forecast.js", "/static/js/forecast_result.js"):
        resp = client.get(url)
        assert resp.status_code == 200, url
        texts[url] = strip_tokens(resp.get_data(as_text=True))
    running = client.get("/forecast/history").get_data(as_text=True)
    task_id = int(re.search(r'<tr data-task="(\d+)">', running).group(1))
    texts["运行中的结果页"] = strip_tokens(client.get(f"/forecast/result/{task_id}").get_data(as_text=True))
    for task in (done, sub):                                                  # 导出文件的每一个格子
        sheets = pd.read_excel(BytesIO(client.get(f"/forecast/result/{task}/export").data), sheet_name=None)
        texts[f"导出文件 {task}"] = " ".join(
            " ".join(map(str, df.columns)) + " " + " ".join(df.astype(str).values.ravel()) for df in sheets.values())
    for name in ("forecast.html", "forecast_result.html", "forecast_history.html"):   # 模板原文也查一遍
        texts[name] = (APP_DIR / "templates" / name).read_text(encoding="utf-8")
    assert len(texts) == 16
    for where, content in texts.items():
        found = BANNED.search(content)
        assert not found, f"{where} 里出现了“{found.group(0)}”"

    # 失败页显示的是失败原因，同样不带这些词
    from app.services import forecast_service
    run_sql(["UPDATE forecast_task SET status = '失败' WHERE status = '运行中'"])
    monkeypatch.setattr(forecast_service, "forecast_series",
                        lambda job: {"region": job["region"], "category": job["category"], "error": "模拟的原因"})
    failed = submit(client, lead_time=3)["task_id"]
    wait(client, failed)
    assert not BANNED.search(strip_tokens(client.get(f"/forecast/result/{failed}").get_data(as_text=True)))


# ---------------- 负数不上页面：下限按 0 显示 ----------------
def test_zero_lower_bound_has_a_note(client, query):
    """区间下限是 0 的序列，页面数据里带一句说明，导出文件里也有；下限都大于 0 的序列没有这句话"""
    task_id = run(client, region="全部", category="全部", history_start=2021)
    lows = {(r, c): float(v) for r, c, v in query(
        "SELECT region, category, MIN(lower_qty) FROM forecast_detail WHERE task_id = %s GROUP BY region, category",
        (task_id,))}
    with_zero = [k for k, v in lows.items() if v == 0]
    without = [k for k, v in lows.items() if v > 0]
    assert len(with_zero) == 9 and len(without) == 9 and min(lows.values()) == 0     # 18 条里 9 条有下限为 0 的月份
    d = series(client, task_id, region=with_zero[0][0], category=with_zero[0][1])
    assert d["lower_note"] == "下限按 0 显示（销量不能为负）" and min(r["lower"] for r in d["rows"]) == 0
    d = series(client, task_id, region=without[0][0], category=without[0][1])
    assert d["lower_note"] == "" and min(r["lower"] for r in d["rows"]) > 0
    sheets = pd.read_excel(BytesIO(client.get(f"/forecast/result/{task_id}/export").data), sheet_name=None)
    assert dict(sheets["预测设置"].values.tolist())["预测下限"] == "下限按 0 显示（销量不能为负）"
    out = sheets["预测结果"]
    assert (out[["预测销量", "预测下限", "预测上限", "建议进货量"]] >= 0).all().all() and (out["预测下限"] == 0).sum() == 52
    assert 'id="lowerNote"' in client.get(f"/forecast/result/{task_id}").get_data(as_text=True)


def test_negative_stored_values_are_shown_as_zero(client, query):
    """
    现在的模型不会算出负数。这里手工把数据库里的几个数改成负的，模拟“万一存进了负数”：
    页面和导出都显示 0 并带说明；数据库里存的数保持原样，不被改掉
    """
    task_id = run(client)
    run_sql([f"UPDATE forecast_detail SET forecast_qty = -1.25, lower_qty = -12.5, suggested_purchase = -3 "
             f"WHERE task_id = {task_id} AND ym = '2025-03'"])
    row = series(client, task_id)["rows"][2]
    assert (row["ym"], row["forecast"], row["lower"], row["purchase"]) == ("2025-03", 0, 0, 0)
    assert series(client, task_id)["lower_note"] == "下限按 0 显示（销量不能为负）"
    sheets = pd.read_excel(BytesIO(client.get(f"/forecast/result/{task_id}/export").data), sheet_name=None)
    march = sheets["预测结果"].iloc[2]
    assert (march["预测月份"], march["预测销量"], march["预测下限"], march["建议进货量"]) == ("2025-03", 0, 0, 0)
    assert "预测下限" in dict(sheets["预测设置"].values.tolist())
    stored = query("SELECT forecast_qty, lower_qty, suggested_purchase FROM forecast_detail "
                   "WHERE task_id = %s AND ym = '2025-03'", (task_id,))[0]
    assert (float(stored[0]), float(stored[1]), stored[2]) == (-1.25, -12.5, -3)     # 存的数没有被改
