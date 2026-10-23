# -*- coding: utf-8 -*-
"""
预测服务：系统算出来的预测值、安全库存、建议进货量要和阶段一实验的结果一致；
并行和串行结果相同；补货周期、服务水平、实际销量、任务失败的处理。只用测试库。
全部 18 条序列的预测比较慢，所以每种设置只算一次，几个测试共用（下面带 scope="module" 的部分）
"""
import json
import math
import os
import pickle

import numpy as np
import pandas as pd
import pytest

from app.constants import CATEGORY_SUBS, REGIONS
from config import MODEL_DIR, TABLE_DIR, TEST_DB_NAME
from conftest import require_test_db, run_sql
from forecast_core import purchase_plan, safety_stock
from step3_load_mysql import connect, load

FINAL = "组合模型"            # 实验结果表里组合模型那一列的名字
BASE = {"target_year": 2025, "history_start": 2021, "region": "全部", "category": "全部", "level": "类别"}
ONE = {**BASE, "region": "华东", "category": "办公用品"}          # 只有一条序列的设置，算得快


def clear_forecasts():
    run_sql(["DELETE FROM forecast_detail", "DELETE FROM forecast_task"])


@pytest.fixture(scope="module", autouse=True)
def ctx():
    """测试库装成原始数据；整个文件共用一个网站应用，测试代码直接调用预测服务"""
    from app import create_app
    from migrate_v2 import add_missing_columns
    load(require_test_db(TEST_DB_NAME), force=True)
    conn = connect(TEST_DB_NAME)
    with conn.cursor() as cur:
        add_missing_columns(cur)
    conn.commit()
    conn.close()
    clear_forecasts()
    app = create_app(db_name=TEST_DB_NAME)
    with app.app_context():
        yield app
    clear_forecasts()
    load(require_test_db(TEST_DB_NAME), force=True)


@pytest.fixture(scope="module")
def fs():
    from app.services import forecast_service
    return forecast_service


def run(fs, base=BASE, parallel=True, **settings):
    """新建一个预测任务并执行，返回 (任务号, 各序列的计算结果)。base 是基本设置，settings 是要改的几项"""
    task_id = fs.create_task({**base, **settings}, 1)
    results, _, _ = fs.run_task(task_id, parallel=parallel)
    return task_id, results


@pytest.fixture(scope="module")
def cat_task(fs):
    """2021–2024 预测 2025，类别层级，全部 18 条序列，并行"""
    return run(fs)


@pytest.fixture(scope="module")
def sub_task(fs):
    """同上，子类别层级"""
    return run(fs, level="子类别")


@pytest.fixture(scope="module")
def task_2024(fs):
    """2021–2023 预测 2024，类别层级，全部 18 条序列"""
    return run(fs, target_year=2024)


def details(query, task_id):
    rows = query("SELECT region, category, sub_category, ym, forecast_qty, lower_qty, upper_qty, actual_qty, "
                 "safety_stock, suggested_purchase FROM forecast_detail WHERE task_id = %s ORDER BY id", (task_id,))
    df = pd.DataFrame(rows, columns=["地区", "类别", "子类别", "预测月份", "预测销量", "预测下限", "预测上限",
                                     "实际销量", "安全库存", "建议进货量"])
    return df.astype({"预测销量": float, "预测下限": float, "预测上限": float, "安全库存": float})


def task_row(query, task_id):
    cols = ["status", "progress_done", "progress_total", "error_msg", "year_versions", "history_start",
            "history_end", "target_year", "inventory", "lead_time", "service_level", "finished_at"]
    return dict(zip(cols, query(f"SELECT {', '.join(cols)} FROM forecast_task WHERE id = %s", (task_id,))[0]))


# ---------------- 和阶段一的结果一致 ----------------
def test_forecast_consistency(query, cat_task):
    """类别层级：2025 年 1–6 月的预测值、安全库存、建议进货量和阶段一的《进货预测结果.xlsx》一致"""
    task_id, results = cat_task
    task = task_row(query, task_id)
    assert (task["status"], task["progress_done"], task["progress_total"]) == ("已完成", 18, 18)
    assert task["year_versions"] == "2021:1,2022:1,2023:1,2024:1,2025:0" and task["finished_at"] is not None
    got = details(query, task_id)
    assert len(got) == 18 * 12 and got["子类别"].isna().all()
    assert got["实际销量"].isna().all()                          # 2025 年没有订单，没有实际销量
    assert sorted(got["预测月份"].unique()) == [f"2025-{m:02d}" for m in range(1, 13)]

    stage1 = pd.read_excel(TABLE_DIR / "进货预测结果.xlsx", sheet_name="地区类别预测")
    both = stage1.merge(got, on=["地区", "类别", "预测月份"], suffixes=("_阶段一", "_系统"))
    assert len(both) == len(stage1) == 18 * 6                    # 阶段一只存了 1–6 月，这 108 行都能对上
    for column in ("预测销量", "预测下限", "预测上限", "安全库存"):
        assert (both[f"{column}_阶段一"] - both[f"{column}_系统"]).abs().max() < 0.01, column
    assert (both["建议进货量_阶段一"] == both["建议进货量_系统"]).all()

    # σ 和选出的阶数，对比阶段一保存的 model_config.json
    config = json.loads((MODEL_DIR / "model_config.json").read_text(encoding="utf-8"))["series"]
    assert [(r["region"], r["category"]) for r in results] == [(a, b) for a in REGIONS for b in CATEGORY_SUBS]
    for r in results:
        saved = config[f"{r['region']}|{r['category']}"]
        assert abs(r["sigma"] - saved["test_rmse"]) < 0.001
        assert (r["order"], r["seasonal_order"], r["trend"]) == (saved["sarima_order"], saved["seasonal_order"],
                                                                 saved["trend"])
        assert abs(r["safety_stock"] - 1.65 * r["sigma"]) < 1e-9


def test_forecast_subcategory(query, sub_task):
    """子类别层级：102 个“地区 × 子类别”的 1–6 月结果和阶段一的“地区子类别预测”一致"""
    task_id, results = sub_task
    got = details(query, task_id)
    assert len(got) == 102 * 12 and got["子类别"].notna().all()
    stage1 = pd.read_excel(TABLE_DIR / "进货预测结果.xlsx", sheet_name="地区子类别预测")
    both = stage1.merge(got, on=["地区", "类别", "子类别", "预测月份"], suffixes=("_阶段一", "_系统"))
    assert len(both) == len(stage1) == 102 * 6
    for column in ("预测销量", "预测下限", "预测上限", "安全库存"):
        assert (both[f"{column}_阶段一"] - both[f"{column}_系统"]).abs().max() < 0.01, column
    assert (both["建议进货量_阶段一"] == both["建议进货量_系统"]).all()

    shares = stage1.drop_duplicates(["地区", "子类别"]).set_index(["地区", "子类别"])["分配占比"]
    for r in results:
        assert abs(sum(s["share"] for s in r["subs"].values()) - 1) < 1e-9       # 一个类别下各子类别的占比加起来是 1
        for name, s in r["subs"].items():
            assert abs(s["share"] - shares[(r["region"], name)]) < 0.0001
            assert np.allclose(s["forecast"], np.array(r["forecast"]) * s["share"])   # 子类别预测 = 占比 × 类别预测
            assert np.allclose(s["lower"], np.array(r["lower"]) * s["share"])         # 区间按同一个占比缩放


def test_category_part_same_in_both_levels(cat_task, sub_task):
    """两个任务各算了一遍 18 条“地区 × 类别”序列（都是并行），结果应该一模一样"""
    for a, b in zip(cat_task[1], sub_task[1]):
        assert (a["region"], a["category"]) == (b["region"], b["category"])
        assert a["forecast"] == b["forecast"] and a["sigma"] == b["sigma"] and a["plan"] == b["plan"]


# ---------------- 并行和串行结果相同 ----------------
def test_parallel_equals_serial(fs, query, sub_task):
    """华东的 3 条序列一条一条算（串行），和并行任务里华东的结果比：每个数都相同"""
    task_id, serial = run(fs, parallel=False, region="华东", level="子类别")
    parallel = [r for r in sub_task[1] if r["region"] == "华东"]
    assert len(serial) == len(parallel) == 3
    for a, b in zip(serial, parallel):
        assert a["category"] == b["category"] and a["order"] == b["order"]
        assert a["sigma"] == b["sigma"] and a["forecast"] == b["forecast"]
        assert a["lower"] == b["lower"] and a["upper"] == b["upper"] and a["plan"] == b["plan"]
        assert a["subs"] == b["subs"]
    saved_serial = details(query, task_id).reset_index(drop=True)
    all_rows = details(query, sub_task[0])
    saved_parallel = all_rows[all_rows["地区"] == "华东"].reset_index(drop=True)
    assert saved_serial.equals(saved_parallel) and len(saved_serial) == 17 * 12


def test_parallel_settings(fs, monkeypatch):
    """进程数 = CPU 核数 − 1，最多 4 个，至少 1 个；交给子进程的是普通数据；子进程只用 1 个线程"""
    monkeypatch.setattr(os, "cpu_count", lambda: 20)
    assert fs.worker_count(18) == 4 and fs.worker_count(3) == 3 and fs.worker_count(1) == 1
    monkeypatch.setattr(os, "cpu_count", lambda: 2)
    assert fs.worker_count(18) == 1
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert fs.worker_count(18) == 1

    task = {"history_start": 2021, "history_end": 2024, "target_year": 2025, "region": "华东", "category": "全部",
            "level_name": "子类别", "sub_category": "全部", "inventory": 0, "lead_time": 1, "service_level": 0.95}
    jobs = fs.build_jobs(task)
    assert len(jobs) == 3 and len(jobs[0]["y"]) == 48 and len(jobs[0]["subs"]) == 9
    assert pickle.loads(pickle.dumps(jobs)) == jobs               # 能原样传给别的进程
    assert fs.forecast_series.__module__ == "forecast_worker"     # 子进程执行的函数写在模块最外层

    seen = {}

    class FakePool:                                               # 假的进程池：记下创建时的环境变量，然后直接在本进程算
        def __init__(self, max_workers):
            seen.update(workers=max_workers, omp=os.environ.get("OMP_NUM_THREADS"),
                        mkl=os.environ.get("MKL_NUM_THREADS"))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, fn, job):
            from concurrent.futures import Future
            future = Future()
            future.set_result({"region": job["region"], "category": job["category"]})
            return future

    monkeypatch.setattr(fs, "ProcessPoolExecutor", FakePool)
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.setenv("MKL_NUM_THREADS", "8")
    progress = []
    out = fs.run_parallel(jobs, progress.append, 3)
    assert seen == {"workers": 3, "omp": "1", "mkl": "1"}
    assert [r["category"] for r in out] == ["办公用品", "技术", "家具"] and progress == [1, 2, 3]
    assert "OMP_NUM_THREADS" not in os.environ and os.environ["MKL_NUM_THREADS"] == "8"   # 用完恢复原样


def test_pool_failure_falls_back_to_serial(fs, query, monkeypatch, caplog):
    """进程池出问题时改为一条一条算，结果照样正确，并在日志里记一笔"""
    def broken(jobs, on_done, workers):
        raise OSError("模拟进程池启动失败")
    monkeypatch.setattr(fs, "run_parallel", broken)
    monkeypatch.setattr(fs, "worker_count", lambda n: 2)
    with caplog.at_level("WARNING"):
        task_id, results = run(fs, parallel=True, region="西北", category="技术", level="子类别")
    assert "并行预测失败，改为逐条计算" in caplog.text and "模拟进程池启动失败" in caplog.text
    assert task_row(query, task_id)["status"] == "已完成" and len(details(query, task_id)) == 4 * 12


# ---------------- 补货周期、服务水平、库存 ----------------
def old_purchase_plan(forecast, ss, inventory=0):
    """阶段一时的写法，原样抄在这里作对照"""
    plan = []
    for i, f in enumerate(forecast):
        q = f + ss - inventory if i == 0 else f
        plan.append(int(np.ceil(max(q, 0))))
    return plan


def test_purchase_plan_lead_time_1_unchanged():
    rng = np.random.default_rng(2026)
    for _ in range(300):
        forecast = rng.uniform(0, 300, size=rng.integers(1, 13))
        ss, inventory = rng.uniform(0, 80), int(rng.integers(0, 400))
        assert purchase_plan(forecast, ss, inventory) == old_purchase_plan(forecast, ss, inventory)
        assert purchase_plan(forecast, ss, inventory, 1) == old_purchase_plan(forecast, ss, inventory)
    assert purchase_plan([10.2, 20.0, 30.5], 5.5) == [16, 20, 31]


def test_purchase_plan_longer_lead_time():
    forecast = [10.5, 20.25, 30.0, 40.0, 50.5, 60.0, 70.0, 80.0, 90.0, 100.0, 110.0, 120.3]
    # 每 3 个月进一次货：只有第 1、4、7、10 个月有数，各是接下来 3 个月的预测之和；第 1 次另加安全库存、减库存
    assert purchase_plan(forecast, 12.3, 5, 3) == [math.ceil(60.75 + 12.3 - 5), 0, 0, 151, 0, 0, 240, 0, 0, 331, 0, 0]
    assert purchase_plan(forecast, 12.3, 0, 2) == [math.ceil(30.75 + 12.3), 0, 70, 0, 111, 0, 150, 0, 190, 0, 231, 0]
    assert purchase_plan(forecast, 12.3, 1000, 3)[0] == 0                    # 库存很多时第 1 次不用进货，不会是负数
    assert sum(purchase_plan(forecast, 0, 0, 3)) >= sum(forecast) > sum(purchase_plan(forecast, 0, 0, 3)) - 4


@pytest.mark.parametrize("lead_time", [1, 2, 3])
def test_lead_time_in_service(fs, query, cat_task, lead_time):
    """华东-办公用品一条序列：预测值不受补货周期影响；安全库存 = 1.65 × σ × √L；进货量按周期合并"""
    task_id, results = run(fs, ONE, lead_time=lead_time)
    r = results[0]
    full = next(x for x in cat_task[1] if (x["region"], x["category"]) == ("华东", "办公用品"))
    assert r["forecast"] == full["forecast"] and r["sigma"] == full["sigma"]
    got = details(query, task_id)
    assert len(got) == 12 and task_row(query, task_id)["lead_time"] == lead_time
    assert abs(got["安全库存"].iloc[0] - 1.65 * r["sigma"] * math.sqrt(lead_time)) < 0.01
    plan = got["建议进货量"].tolist()
    assert [i for i, q in enumerate(plan) if q > 0] == list(range(0, 12, lead_time))
    for i in range(0, 12, lead_time):
        block = sum(r["forecast"][i:i + lead_time]) + (r["safety_stock"] if i == 0 else 0)
        assert plan[i] == math.ceil(block)
    if lead_time == 1:
        assert plan == full["plan"]                                         # 周期为 1 时和全量任务里的结果相同


@pytest.mark.parametrize("level, z", [(0.90, 1.28), (0.95, 1.65), (0.99, 2.33)])
def test_service_level(fs, query, level, z):
    task_id, results = run(fs, ONE, service_level=level)
    r = results[0]
    assert abs(r["safety_stock"] - z * r["sigma"]) < 1e-9 == safety_stock(r["sigma"], z) - r["safety_stock"] + 1e-9
    assert abs(details(query, task_id)["安全库存"].iloc[0] - z * r["sigma"]) < 0.01
    assert float(task_row(query, task_id)["service_level"]) == level


def test_inventory_only_for_single_series(fs, query, cat_task):
    """只预测一条序列时库存才起作用：第 1 个月的进货量减去库存；多条序列时库存按 0 算"""
    task_id, results = run(fs, ONE, inventory=100)
    full = next(x for x in cat_task[1] if (x["region"], x["category"]) == ("华东", "办公用品"))
    assert task_row(query, task_id)["inventory"] == 100
    assert results[0]["plan"][0] == math.ceil(full["forecast"][0] + full["safety_stock"] - 100)
    assert results[0]["plan"][1:] == full["plan"][1:]

    check = fs.check_settings
    assert check({**BASE, "inventory": 100})["inventory"] == 0                           # 全部地区
    assert check({**ONE, "category": "全部", "inventory": 100})["inventory"] == 0         # 全部类别
    assert check({**ONE, "level": "子类别", "inventory": 100})["inventory"] == 0          # 子类别没选具体哪个
    assert check({**ONE, "level": "子类别", "sub_category": "全部", "inventory": 100})["inventory"] == 0
    assert check({**ONE, "level": "子类别", "sub_category": "纸张", "inventory": 100})["inventory"] == 100

    # 只选一个子类别：只保存这一个子类别的 12 行，库存用在它身上
    task_id, results = run(fs, ONE, level="子类别", sub_category="纸张", inventory=5)
    got = details(query, task_id)
    paper = results[0]["subs"]["纸张"]
    assert len(got) == 12 and set(got["子类别"]) == {"纸张"}
    assert got["建议进货量"].iloc[0] == math.ceil(paper["forecast"][0] + paper["safety_stock"] - 5)
    assert results[0]["subs"]["信封"]["plan"][0] == math.ceil(
        results[0]["subs"]["信封"]["forecast"][0] + results[0]["subs"]["信封"]["safety_stock"])


# ---------------- 预测设置的检查 ----------------
def test_year_options(fs):
    """按现在的数据：预测 2024 时历史起始年只有 2021；预测 2025 时有 2021、2022"""
    assert fs.year_options() == {2024: [2021], 2025: [2021, 2022]}


@pytest.mark.parametrize("change, message", [
    ({"target_year": 2026}, "这个年份不能预测"),
    ({"target_year": 2023}, "这个年份不能预测"),
    ({"target_year": "abc"}, "这个年份不能预测"),
    ({"history_start": 2023}, "历史起始年只能选 2021、2022"),
    ({"target_year": 2024, "history_start": 2022}, "历史起始年只能选 2021"),
    ({"region": "火星"}, "地区不在可选范围内"),
    ({"category": "零食"}, "类别不在可选范围内"),
    ({"level": "子类别", "category": "家具", "sub_category": "纸张"}, "子类别不属于所选的类别"),
    ({"level": "子类别", "sub_category": "纸张"}, "子类别不属于所选的类别"),
    ({"lead_time": 4}, "补货周期只能是 1、2、3 个月"),
    ({"service_level": 0.8}, "服务水平只能是 90%、95%、99%"),
    ({"service_level": "很高"}, "服务水平只能是 90%、95%、99%"),
    ({"inventory": -1}, "当前库存应为不小于 0 的整数"),
    ({"inventory": "很多"}, "当前库存应为不小于 0 的整数"),
])
def test_bad_settings(fs, query, change, message):
    before = query("SELECT COUNT(*) FROM forecast_task")[0][0]
    with pytest.raises(fs.ForecastError, match=message):
        fs.create_task({**BASE, **change}, 1)
    assert query("SELECT COUNT(*) FROM forecast_task")[0][0] == before


def test_defaults(fs):
    s = fs.check_settings({"target_year": "2025", "history_start": "2022"})
    assert s == {"history_start": 2022, "history_end": 2024, "target_year": 2025, "region": "全部",
                 "category": "全部", "level_name": "类别", "sub_category": None, "inventory": 0,
                 "lead_time": 1, "service_level": 0.95}


def test_history_must_be_processed(fs, query):
    """历史年份里有需要重新处理的年份时不能预测，提示里写明是哪几年"""
    run_sql(["UPDATE year_state SET data_version = 2 WHERE `year` IN (2022, 2023)"])
    try:
        with pytest.raises(fs.ForecastError, match="请先到【数据处理】处理 2022–2023 年"):
            fs.create_task(BASE, 1)
    finally:
        run_sql(["UPDATE year_state SET data_version = 1"])


def test_shorter_history(fs, query, cat_task):
    """用 2022–2024 三年预测 2025：能算，训练段只有 24 个月，结果和用四年历史的不同"""
    task_id, results = run(fs, ONE, history_start=2022)
    task = task_row(query, task_id)
    assert (task["history_start"], task["history_end"], task["year_versions"]) == (2022, 2024, "2022:1,2023:1,2024:1,2025:0")
    full = next(x for x in cat_task[1] if (x["region"], x["category"]) == ("华东", "办公用品"))
    assert len(results[0]["forecast"]) == 12 and results[0]["forecast"] != full["forecast"]
    assert all(q >= 0 for q in results[0]["forecast"])


# ---------------- 实际销量；和实验里 2024 年测试集预测的关系 ----------------
def test_actual_values_2024(query, task_2024):
    """2021–2023 预测 2024：2024 年已处理，12 个月的实际销量都填上了，等于月度表里的数"""
    task_id, results = task_2024
    got = details(query, task_id)
    assert len(got) == 216 and got["实际销量"].notna().all()
    assert task_row(query, task_id)["year_versions"] == "2021:1,2022:1,2023:1,2024:1"
    actual = {(r, c, ym): q for r, c, ym, q in query(
        "SELECT region, category, ym, quantity FROM monthly_sales_category WHERE ym LIKE '2024-%'")}
    for row in got.itertuples():
        assert row.实际销量 == actual[(row.地区, row.类别, row.预测月份)]
    assert got["实际销量"].sum() == sum(actual.values())


def test_2024_forecast_vs_experiment(task_2024):
    """
    系统预测的 2024 年，和实验里测试集（2021–2023 训练）的预测不一定相同：
    实验是在 36 个月上选阶；系统要留最后 12 个月做回测，所以是在前 24 个月上选阶，再用 36 个月重新估计。
    选出的阶数相同的序列，两边的预测应该相同；阶数不同的序列才有差别
    """
    config = json.loads((MODEL_DIR / "model_config.json").read_text(encoding="utf-8"))["series"]
    experiment = pd.read_excel(TABLE_DIR / "模型评估结果.xlsx", sheet_name="测试期预测明细")
    same, different = 0, 0
    for r in task_2024[1]:
        saved = config[f"{r['region']}|{r['category']}"]
        want = experiment[(experiment["地区"] == r["region"]) & (experiment["类别"] == r["category"])]
        gap = np.abs(np.array(r["forecast"]) - want.sort_values("年月")[FINAL].values).max()
        if (r["order"], r["seasonal_order"], r["trend"]) == (saved["sarima_order"], saved["seasonal_order"],
                                                             saved["trend"]):
            same += 1
            assert gap < 0.5                       # 阶数相同：只差数值计算上的一点点（Excel 里保留了 2 位小数）
        else:
            different += 1
    assert same + different == 18 and same >= 1


def test_no_actual_when_target_year_not_processed(fs, query):
    """预测年份有订单但数据有更新还没处理：照常预测，实际销量留空"""
    run_sql(["UPDATE year_state SET data_version = 2 WHERE `year` = 2024"])
    try:
        task_id, _ = run(fs, ONE, target_year=2024)
        got = details(query, task_id)
        assert len(got) == 12 and got["实际销量"].isna().all()
        assert task_row(query, task_id)["year_versions"] == "2021:1,2022:1,2023:1,2024:2"
    finally:
        run_sql(["UPDATE year_state SET data_version = 1"])


# ---------------- 失败的处理 ----------------
def test_failing_series_marks_task_failed(fs, query, monkeypatch):
    """有一条序列算不出来：整个任务标为失败并写明原因，不留下半截结果"""
    real = fs.forecast_series

    def sometimes_fails(job):
        if job["category"] == "技术":
            return {"region": job["region"], "category": job["category"], "error": "RuntimeError: 所有候选阶数都拟合失败"}
        return real(job)
    monkeypatch.setattr(fs, "forecast_series", sometimes_fails)
    task_id = fs.create_task({**BASE, "region": "西北"}, 1)
    with pytest.raises(fs.ForecastError, match="西北-技术 预测失败"):
        fs.run_task(task_id, parallel=False)
    task = task_row(query, task_id)
    assert task["status"] == "失败" and "西北-技术 预测失败：RuntimeError: 所有候选阶数都拟合失败" in task["error_msg"]
    assert task["finished_at"] is not None and len(details(query, task_id)) == 0


def test_worker_reports_errors_instead_of_crashing(fs):
    """真的让模型出错（序列太短）：计算函数不抛异常，而是把原因带回来"""
    out = fs.forecast_series({"region": "华东", "category": "技术", "start_ym": "2024-01", "y": [1.0] * 14,
                              "z": 1.65, "lead_time": 1})
    assert out["region"] == "华东" and "error" in out and "forecast" not in out


def test_incomplete_monthly_data(fs, query):
    """月度表里缺月份（比如被别的操作删掉了）：任务失败，提示去重新处理"""
    run_sql(["DELETE FROM monthly_sales_category WHERE region = '西北' AND category = '家具' AND ym = '2023-05'"])
    try:
        task_id = fs.create_task({**BASE, "region": "西北", "category": "家具"}, 1)
        with pytest.raises(fs.ForecastError, match="西北-家具 的数据不完整"):
            fs.run_task(task_id)
        assert task_row(query, task_id)["status"] == "失败"
    finally:
        load(require_test_db(TEST_DB_NAME), force=True)


def test_interrupted_tasks_marked_failed_at_startup(fs, query):
    """网站上次没算完就被关掉，任务还标着运行中：下次启动时改成失败"""
    from app import create_app
    task_id = fs.create_task(BASE, 1)
    assert task_row(query, task_id)["status"] == "运行中"
    create_app(db_name=TEST_DB_NAME)                              # 相当于重新启动网站
    task = task_row(query, task_id)
    assert (task["status"], task["error_msg"]) == ("失败", "系统重启中断") and task["finished_at"] is not None
    done = query("SELECT COUNT(*) FROM forecast_task WHERE status = '已完成'")[0][0]
    assert done > 0                                               # 已完成的任务不受影响


# ---------------- 不出现负数 ----------------
def test_no_negative_values(query, cat_task, sub_task, task_2024):
    """
    18 条类别序列、102 条子类别序列（预测 2025），以及 18 条类别序列（预测 2024）：
    预测值、区间上下限、建议进货量都不小于 0。原因是 forecast_core 里把算出来的负数截成了 0（阶段一就是这样）
    """
    counted = {}
    for name, (task_id, results) in (("类别 2025", cat_task), ("子类别 2025", sub_task), ("类别 2024", task_2024)):
        parts = [s for r in results for s in r["subs"].values()] if name.startswith("子类别") else results
        values = {k: [v for p in parts for v in p[k]] for k in ("forecast", "lower", "upper", "plan")}
        for key, numbers in values.items():
            assert min(numbers) >= 0, (name, key)
        assert all(lo <= fc <= up for lo, fc, up in zip(values["lower"], values["forecast"], values["upper"]))
        counted[name] = (len(parts), sum(v == 0 for v in values["lower"]), sum(v == 0 for v in values["forecast"]))
        low = query("SELECT MIN(forecast_qty), MIN(lower_qty), MIN(upper_qty), MIN(suggested_purchase), "
                    "MIN(safety_stock) FROM forecast_detail WHERE task_id = %s", (task_id,))[0]
        assert all(float(v) >= 0 for v in low), (name, low)                       # 存进数据库的也一样
    # (序列条数, 区间下限为 0 的月份数, 预测值为 0 的月份数)，2026-10-14 实际运行得到的数字
    assert counted == {"类别 2025": (18, 52, 0), "子类别 2025": (102, 283, 0), "类别 2024": (18, 49, 2)}


def test_purchase_plan_never_negative():
    """库存再多、预测值再小，建议进货量最小也是 0，不会是负数"""
    rng = np.random.default_rng(7)
    for _ in range(500):
        forecast = rng.uniform(0, 50, size=12)
        for lead_time in (1, 2, 3):
            plan = purchase_plan(forecast, rng.uniform(0, 30), int(rng.integers(0, 5000)), lead_time)
            assert min(plan) >= 0 and all(isinstance(q, int) for q in plan)
    assert purchase_plan([0.0, 0.0, 0.0], 0.0, 100) == [0, 0, 0]
