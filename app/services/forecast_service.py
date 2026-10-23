# -*- coding: utf-8 -*-
"""
数据预测（设计说明 8.5、第 9 节、14.2）：检查预测设置 → 新建预测任务 → 逐条序列计算 → 保存结果。
每条序列怎么算在 src/forecast_worker.py 里（它调用 forecast_core.py，和实验用的是同一批函数）；
这个文件负责准备数据、把多条序列分给几个进程同时算、更新进度、读写数据库
"""
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime

from flask import current_app
from sqlalchemy import text

from app.constants import CATEGORIES, CATEGORY_SUBS, REGIONS
from app.db import execute, fetch_all, fetch_one, get_engine
from app.services import state_service
from config import Z_TABLE
from forecast_worker import STEPS, forecast_series

LEVEL_CAT, LEVEL_SUB = "类别", "子类别"
ALL = "全部"
RUNNING, DONE, FAILED = "运行中", "已完成", "失败"
MIN_YEARS = 3                    # 历史数据至少 3 年
MAX_WORKERS = 4                  # 最多同时开几个进程
DETAIL_FIELDS = ["task_id", "region", "category", "sub_category", "ym", "forecast_qty", "lower_qty",
                 "upper_qty", "actual_qty", "safety_stock", "suggested_purchase"]


class ForecastError(Exception):
    """预测设置不合要求，或者某条序列算不出来。原因直接显示给用户"""


class NeedProcess(ForecastError):
    """历史年份里有还没处理的年份：页面上除了原因，还要给一个去数据处理的按钮"""


class TaskRunning(ForecastError):
    """已经有一个预测任务在运行。task_id 是正在运行的那个任务"""
    def __init__(self, task_id):
        super().__init__(f"已有一个预测任务正在运行（任务 {task_id}），同一时间只能运行一个，请等它完成后再试")
        self.task_id = task_id


# ---------------- 预测设置 ----------------
def year_options():
    """
    可以预测哪些年份、各自可以从哪一年开始用历史数据：{预测年份: [可选的历史起始年]}
    规则：历史年份到预测年份的前一年为止，必须连续、每一年都是完整年份（12 个月都有订单），至少 3 年
    """
    complete = {y["year"] for y in state_service.year_status() if y["complete"]}
    options = {}
    for target in sorted(y + 1 for y in complete):
        starts, year = [], target - 1
        while year in complete:                              # 从前一年往回数，数到不完整的年份为止
            if target - year >= MIN_YEARS:
                starts.append(year)
            year -= 1
        if starts:
            options[target] = sorted(starts)
    return options


def to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def check_settings(form):
    """检查并整理预测设置，返回整理好的设置；有问题就抛出 ForecastError"""
    target, start = to_int(form.get("target_year")), to_int(form.get("history_start"))
    options = year_options()
    if target not in options:
        raise ForecastError("这个年份不能预测：它前面要有连续 3 年完整的数据")
    if start not in options[target]:
        raise ForecastError(f"预测 {target} 年时，历史起始年只能选 {'、'.join(map(str, options[target]))}")
    status = {y["year"]: y for y in state_service.year_status()}
    stale = [y for y in range(start, target) if not status[y]["processed"]]
    if stale:
        raise NeedProcess(f"所选年份的数据有更新或尚未处理，请先到【数据处理】处理 {stale[0]}–{stale[-1]} 年")

    region, category = form.get("region") or ALL, form.get("category") or ALL
    level = LEVEL_SUB if form.get("level") == LEVEL_SUB else LEVEL_CAT
    if region != ALL and region not in REGIONS:
        raise ForecastError("地区不在可选范围内")
    if category != ALL and category not in CATEGORIES:
        raise ForecastError("类别不在可选范围内")
    sub = None
    if level == LEVEL_SUB:                                   # 子类别只在选了具体类别时才能指定
        sub = form.get("sub_category") or ALL
        if sub != ALL and (category == ALL or sub not in CATEGORY_SUBS[category]):
            raise ForecastError("子类别不属于所选的类别")

    lead_time = to_int(form.get("lead_time"), 1)
    if lead_time not in (1, 2, 3):
        raise ForecastError("补货周期只能是 1、2、3 个月")
    try:
        service_level = round(float(form.get("service_level") or 0.95), 2)
    except (TypeError, ValueError):
        service_level = None
    if service_level not in Z_TABLE:
        raise ForecastError("服务水平只能是 90%、95%、99%")
    inventory = to_int(form.get("inventory") or 0)
    if inventory is None or inventory < 0:
        raise ForecastError("当前库存应为不小于 0 的整数")
    # 只预测一条序列时才能填库存：多条序列不能共用一个库存数，其他情况一律按 0 算
    single = region != ALL and category != ALL and (level == LEVEL_CAT or sub != ALL)
    return {"history_start": start, "history_end": target - 1, "target_year": target, "region": region,
            "category": category, "level_name": level, "sub_category": sub,
            "inventory": inventory if single else 0, "lead_time": lead_time, "service_level": service_level}


def year_versions(start, target):
    """预测时各相关年份的数据版本号，写成 2021:1,2022:1,...。预测年份没有数据时记 0"""
    state = {r["year"]: r["data_version"] for r in fetch_all("SELECT `year`, data_version FROM year_state")}
    return ",".join(f"{y}:{state.get(y, 0)}" for y in range(start, target + 1))


def insert_task(s, user_id):
    """按整理好的设置新建一条“运行中”的预测任务，返回任务号"""
    regions = REGIONS if s["region"] == ALL else [s["region"]]
    categories = CATEGORIES if s["category"] == ALL else [s["category"]]
    row = {**s, "status": RUNNING, "progress_done": 0, "progress_total": len(regions) * len(categories),
           "user_id": user_id, "year_versions": year_versions(s["history_start"], s["target_year"])}
    with get_engine().begin() as conn:
        return conn.execute(text(f"INSERT INTO forecast_task ({', '.join(row)}) "
                                 f"VALUES ({', '.join(':' + k for k in row)})"), row).lastrowid


def create_task(form, user_id):
    """检查设置，新建一条“运行中”的预测任务，返回任务号。计算由 run_task 来做"""
    return insert_task(check_settings(form), user_id)


# ---------------- 提交预测：复用已有结果，或者在后台开始计算 ----------------
START_LOCK = threading.Lock()


def running_task():
    """正在运行的预测任务，没有就返回 None"""
    return fetch_one("SELECT id FROM forecast_task WHERE status = :r ORDER BY id LIMIT 1", {"r": RUNNING})


def find_reusable(s):
    """
    找一个可以直接拿来用的已完成任务：设置完全相同，而且相关年份的数据版本号和那次预测时一样（数据没变过）。
    另外，预测年份现在能不能取到实际销量，也要和那次一样。找不到返回 None
    """
    old = fetch_one(
        "SELECT id FROM forecast_task WHERE status = :done AND history_start = :history_start "
        "AND target_year = :target_year AND region = :region AND category = :category AND level_name = :level_name "
        "AND sub_category <=> :sub_category AND inventory = :inventory AND lead_time = :lead_time "
        "AND service_level = :service_level AND year_versions = :versions ORDER BY id DESC LIMIT 1",
        {**s, "done": DONE, "versions": year_versions(s["history_start"], s["target_year"])})
    if old is None:
        return None
    had_actual = fetch_one("SELECT COUNT(*) AS n FROM forecast_detail WHERE task_id = :id AND actual_qty IS NOT NULL",
                           {"id": old["id"]})["n"] > 0
    has_actual_now = bool(actual_values({"target_year": s["target_year"], "level_name": s["level_name"]}))
    return old["id"] if had_actual == has_actual_now else None


def run_in_background(task_id):
    """
    在后台线程里执行预测，提交预测的那个请求不用等它算完。
    线程里要自己进入应用上下文才能用数据库；出任何错都在 run_task 里记成“失败”，这里再兜一层底
    """
    app = current_app._get_current_object()

    def work():
        with app.app_context():
            try:
                run_task(task_id)
            except Exception as e:                       # run_task 已经把任务标为失败并记了日志
                task = fetch_one("SELECT status FROM forecast_task WHERE id = :id", {"id": task_id})
                if task and task["status"] == RUNNING:   # 万一连标记失败这一步都没做成，这里补上
                    fail_task(task_id, f"{type(e).__name__}: {e}")
    threading.Thread(target=work, name=f"forecast-{task_id}", daemon=True).start()


def start(form, user_id, force=False):
    """
    提交一次预测，返回 (任务号, 是不是复用了以前的结果)。
    设置和数据都没变时直接用以前的结果（force=True 时不复用，一定重新算）；
    否则新建任务并在后台开始计算。已经有任务在运行时抛出 TaskRunning
    """
    s = check_settings(form)
    if not force:
        old = find_reusable(s)
        if old:
            return old, True
    with START_LOCK:                                     # “看有没有在跑的”和“新建任务”要连在一起做，不能被插队
        running = running_task()
        if running:
            raise TaskRunning(running["id"])
        task_id = insert_task(s, user_id)
    run_in_background(task_id)
    return task_id, False


def settings_of(task):
    """把一个已有任务的设置还原成提交时的样子，用来“重新计算”"""
    return {"target_year": task["target_year"], "history_start": task["history_start"], "region": task["region"],
            "category": task["category"], "level": task["level_name"], "sub_category": task["sub_category"],
            "inventory": task["inventory"], "lead_time": task["lead_time"],
            "service_level": float(task["service_level"])}


# ---------------- 准备数据 ----------------
def load_history(table, key, start, end):
    """从月度表读历史销量：{(地区, 类别或子类别): [每个月的销量]}，月份从 start 年 1 月到 end 年 12 月"""
    rows = fetch_all(f"SELECT region, {key} AS k, quantity FROM {table} WHERE ym BETWEEN :a AND :b "
                     f"ORDER BY region, {key}, ym", {"a": f"{start}-01", "b": f"{end}-12"})
    series = {}
    for r in rows:
        series.setdefault((r["region"], r["k"]), []).append(int(r["quantity"]))
    return series


def build_jobs(task):
    """把一个预测任务拆成若干条“地区 × 类别”序列，每条是一个只含普通数据的字典，可以交给别的进程"""
    start, end = task["history_start"], task["history_end"]
    months = (end - start + 1) * 12
    by_cat = load_history("monthly_sales_category", "category", start, end)
    by_sub = load_history("monthly_sales", "sub_category", start, end) if task["level_name"] == LEVEL_SUB else {}
    jobs = []
    for region in (REGIONS if task["region"] == ALL else [task["region"]]):
        for category in (CATEGORIES if task["category"] == ALL else [task["category"]]):
            y = by_cat.get((region, category), [])
            subs = {s: by_sub.get((region, s), []) for s in CATEGORY_SUBS[category]} if by_sub else {}
            if len(y) != months or any(len(v) != months for v in subs.values()):
                raise ForecastError(f"月度表里 {region}-{category} 的数据不完整，请先到【数据处理】重新处理 "
                                    f"{start}–{end} 年")
            wanted = task["sub_category"]
            jobs.append({"region": region, "category": category, "start_ym": f"{start}-01", "y": y, "subs": subs,
                         "z": Z_TABLE[float(task["service_level"])], "lead_time": task["lead_time"],
                         "inventory": task["inventory"] if task["level_name"] == LEVEL_CAT else 0,
                         "sub_inventory": {wanted: task["inventory"]} if wanted and wanted != ALL else {}})
    return jobs


# ---------------- 计算：一条一条算，或者几个进程同时算 ----------------
def worker_count(n_jobs):
    """开几个进程：CPU 核数减 1，最多 4 个，至少 1 个，也不超过序列条数"""
    return max(1, min(MAX_WORKERS, (os.cpu_count() or 2) - 1, n_jobs))


def run_serial(jobs, on_done):
    results = []
    for job in jobs:
        results.append(forecast_series(job))
        on_done(len(results))
    return results


def run_parallel(jobs, on_done, workers):
    """
    用进程池同时算几条序列。每条序列的算法和串行时完全一样，只是谁先算完不一定，
    所以结果按原来的顺序放回去。子进程里不连数据库，进度由这边（主进程）来更新
    """
    saved = {k: os.environ.get(k) for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS")}
    os.environ.update({k: "1" for k in saved})     # 子进程会继承这两个设置：每个进程只用 1 个线程，不互相抢 CPU
    try:
        results = [None] * len(jobs)
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(forecast_series, job): i for i, job in enumerate(jobs)}
            for done, future in enumerate(as_completed(futures), start=1):
                results[futures[future]] = future.result()
                on_done(done)
        return results
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})


def compute(jobs, on_done=lambda n: None, parallel=True):
    """算出所有序列的结果，返回 (结果列表, 用的方式)。进程池出了问题就退回到一条一条算，并记进日志"""
    workers = worker_count(len(jobs))
    if parallel and workers > 1:
        try:
            return run_parallel(jobs, on_done, workers), f"并行（{workers} 个进程）"
        except Exception as e:                       # 进程池本身起不来或中途坏掉，不是某条序列算不出来
            current_app.logger.warning("并行预测失败，改为逐条计算：%s: %s", type(e).__name__, e)
            on_done(0)
    return run_serial(jobs, on_done), "串行"


# ---------------- 保存结果 ----------------
def actual_values(task):
    """预测年份的实际销量：{(地区, 类别, 子类别或 None, 年月): 销量}。只有这一年“已处理”时才取，否则为空"""
    year = task["target_year"]
    status = {y["year"]: y for y in state_service.year_status()}
    if year not in status or not status[year]["processed"]:
        return {}
    span = {"a": f"{year}-01", "b": f"{year}-12"}
    if task["level_name"] == LEVEL_SUB:
        rows = fetch_all("SELECT region, category, sub_category, ym, quantity FROM monthly_sales "
                         "WHERE ym BETWEEN :a AND :b", span)
    else:
        rows = fetch_all("SELECT region, category, NULL AS sub_category, ym, quantity FROM monthly_sales_category "
                         "WHERE ym BETWEEN :a AND :b", span)
    return {(r["region"], r["category"], r["sub_category"], r["ym"]): int(r["quantity"]) for r in rows}


def detail_rows(task, results):
    """把计算结果整理成 forecast_detail 的行：类别层级每条序列 12 行；子类别层级每个子类别 12 行"""
    actual = actual_values(task)
    months = [f"{task['target_year']}-{m:02d}" for m in range(1, STEPS + 1)]
    rows = []
    for r in results:
        if task["level_name"] == LEVEL_CAT:
            parts = [(None, r)]
        else:
            wanted = task["sub_category"]
            parts = [(name, r["subs"][name]) for name in CATEGORY_SUBS[r["category"]]
                     if wanted in (None, ALL) or name == wanted]
        for sub, p in parts:
            for i, ym in enumerate(months):
                rows.append({"task_id": task["id"], "region": r["region"], "category": r["category"],
                             "sub_category": sub, "ym": ym, "forecast_qty": round(p["forecast"][i], 2),
                             "lower_qty": round(p["lower"][i], 2), "upper_qty": round(p["upper"][i], 2),
                             "actual_qty": actual.get((r["region"], r["category"], sub, ym)),
                             "safety_stock": round(p["safety_stock"], 2), "suggested_purchase": p["plan"][i]})
    return rows


def fail_task(task_id, reason):
    execute("UPDATE forecast_task SET status = :s, error_msg = :e, finished_at = :t WHERE id = :id",
            {"s": FAILED, "e": str(reason)[:500], "t": datetime.now().replace(microsecond=0), "id": task_id})


def run_task(task_id, parallel=True):
    """
    执行一个预测任务。成功：结果写进 forecast_detail，任务标为“已完成”；
    出任何错：任务标为“失败”并记下原因，不留下半截结果。返回 (各序列的结果, 计算方式, 计算用了多少秒)
    """
    task = fetch_one("SELECT * FROM forecast_task WHERE id = :id", {"id": task_id})
    try:
        jobs = build_jobs(task)
        started = time.perf_counter()
        results, how = compute(
            jobs, lambda n: execute("UPDATE forecast_task SET progress_done = :n WHERE id = :id",
                                    {"n": n, "id": task_id}), parallel)
        seconds = round(time.perf_counter() - started, 2)
        failed = [r for r in results if "error" in r]
        if failed:
            raise ForecastError(f"{failed[0]['region']}-{failed[0]['category']} 预测失败：{failed[0]['error']}")
        rows = detail_rows(task, results)
        sql = text(f"INSERT INTO forecast_detail ({', '.join(DETAIL_FIELDS)}) "
                   f"VALUES ({', '.join(':' + f for f in DETAIL_FIELDS)})")
        with get_engine().begin() as conn:               # 明细和“已完成”的状态一起写，要么都有要么都没有
            for i in range(0, len(rows), 1000):
                conn.execute(sql, rows[i:i + 1000])
            conn.execute(text("UPDATE forecast_task SET status = :s, progress_done = progress_total, "
                              "finished_at = :t WHERE id = :id"),
                         {"s": DONE, "t": datetime.now().replace(microsecond=0), "id": task_id})
        current_app.logger.info("预测任务 %s 完成：%s 条序列，%s，%s 秒", task_id, len(jobs), how, seconds)
        return results, how, seconds
    except Exception as e:
        fail_task(task_id, e if isinstance(e, ForecastError) else f"{type(e).__name__}: {e}")
        current_app.logger.exception("预测任务 %s 失败", task_id)
        raise


def mark_interrupted():
    """网站启动时调用：上次没算完就被关掉的任务还标着“运行中”，改成“失败”，免得页面一直显示在算"""
    return execute("UPDATE forecast_task SET status = :f, error_msg = '系统重启中断', finished_at = :t "
                   "WHERE status = :r", {"f": FAILED, "r": RUNNING, "t": datetime.now().replace(microsecond=0)})
