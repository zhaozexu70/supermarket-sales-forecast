# -*- coding: utf-8 -*-
"""
预测结果展示（设计说明 8.6）：读取预测任务和明细，给结果页的图表和表格准备数据，导出 Excel，历史记录和删除。
这里只读写 forecast_task、forecast_detail 两张表和月度表，不做任何预测计算；页面上不提供任何准确度方面的指标
"""
from io import BytesIO

import pandas as pd
from sqlalchemy import text

from app.constants import CATEGORIES, CATEGORY_SUBS, REGIONS
from app.db import fetch_all, fetch_one, get_engine
from app.services import state_service
from app.services.forecast_service import ALL, LEVEL_SUB, RUNNING, year_versions

LOWER_NOTE = "下限按 0 显示（销量不能为负）"


def not_negative(value):
    """显示用：销量、进货量不能是负数，小于 0 的按 0 显示。只影响显示，数据库里存的数不改"""
    return max(value, 0)


EXPORT_COLUMNS = {"region": "地区", "category": "类别", "sub_category": "子类别", "ym": "预测月份",
                  "forecast_qty": "预测销量", "lower_qty": "预测下限", "upper_qty": "预测上限",
                  "actual_qty": "实际销量", "safety_stock": "安全库存", "suggested_purchase": "建议进货量"}


# ---------------- 任务 ----------------
def get_task(task_id):
    """一个预测任务，带上操作人的用户名和计算用了多少秒。不存在时返回 None"""
    task = fetch_one("SELECT t.*, u.username FROM forecast_task t LEFT JOIN sys_user u ON u.id = t.user_id "
                     "WHERE t.id = :id", {"id": task_id})
    return add_seconds(task) if task else None


def add_seconds(task):
    done = task["finished_at"]
    task["seconds"] = int((done - task["created_at"]).total_seconds()) if done else None
    task["service_percent"] = round(float(task["service_level"]) * 100)
    return task


def list_tasks(limit=100):
    """历史预测任务，新的在前"""
    rows = fetch_all("SELECT t.*, u.username FROM forecast_task t LEFT JOIN sys_user u ON u.id = t.user_id "
                     "ORDER BY t.id DESC LIMIT :n", {"n": limit})
    return [add_seconds(r) for r in rows]


def delete_task(task_id):
    """删除一个预测任务和它的明细。正在运行的不能删。返回 True 删了，False 正在运行，None 不存在"""
    with get_engine().begin() as conn:
        row = conn.execute(text("SELECT status FROM forecast_task WHERE id = :id FOR UPDATE"), {"id": task_id}).first()
        if row is None:
            return None
        if row[0] == RUNNING:
            return False
        conn.execute(text("DELETE FROM forecast_detail WHERE task_id = :id"), {"id": task_id})
        conn.execute(text("DELETE FROM forecast_task WHERE id = :id"), {"id": task_id})
    return True


def data_changed(task):
    """预测之后，相关年份的订单有没有再变过（比较预测时记下的各年份数据版本号和现在的）"""
    return task["year_versions"] != year_versions(task["history_start"], task["target_year"])


def actual_note(task):
    """
    预测年份有订单、但这次预测没有填实际销量时，给一句说明；其他情况返回空字符串。
    预测年份一条订单都没有时（比如预测明年）不用说明
    """
    has_actual = fetch_one("SELECT COUNT(*) AS n FROM forecast_detail WHERE task_id = :id AND actual_qty IS NOT NULL",
                           {"id": task["id"]})["n"] > 0
    has_orders = any(y["year"] == task["target_year"] for y in state_service.year_status())
    return "该年数据有更新尚未处理，未显示实际值" if has_orders and not has_actual else ""


# ---------------- 一个任务里有哪些序列 ----------------
def series_options(task):
    """结果页上切换序列用的选项：地区、类别，以及每个类别下有哪些子类别（类别层级时没有子类别）"""
    regions = REGIONS if task["region"] == ALL else [task["region"]]
    categories = CATEGORIES if task["category"] == ALL else [task["category"]]
    subs = {}
    if task["level_name"] == LEVEL_SUB:
        wanted = task["sub_category"]
        subs = {c: (CATEGORY_SUBS[c] if wanted in (None, ALL) else [wanted]) for c in categories}
    return {"regions": regions, "categories": categories, "subs": subs}


def pick_series(task, args):
    """从网址参数里取要看哪条序列；没给或者不在这个任务里的，用第一条"""
    options = series_options(task)
    region = args.get("region") if args.get("region") in options["regions"] else options["regions"][0]
    category = args.get("category") if args.get("category") in options["categories"] else options["categories"][0]
    sub = None
    if options["subs"]:
        names = options["subs"][category]
        sub = args.get("sub_category") if args.get("sub_category") in names else names[0]
    return region, category, sub


def series_data(task, region, category, sub):
    """一条序列的全部数据：历史每月销量（画实线用）和预测年份 12 个月的预测结果（画图、填表用）"""
    span = {"a": f"{task['history_start']}-01", "b": f"{task['history_end']}-12", "region": region}
    if sub:
        history = fetch_all("SELECT ym, quantity FROM monthly_sales WHERE region = :region AND sub_category = :k "
                            "AND ym BETWEEN :a AND :b ORDER BY ym", {**span, "k": sub})
    else:
        history = fetch_all("SELECT ym, quantity FROM monthly_sales_category WHERE region = :region "
                            "AND category = :k AND ym BETWEEN :a AND :b ORDER BY ym", {**span, "k": category})
    rows = fetch_all(
        "SELECT ym, forecast_qty, lower_qty, upper_qty, actual_qty, safety_stock, suggested_purchase "
        "FROM forecast_detail WHERE task_id = :id AND region = :region AND category = :category "
        "AND sub_category <=> :sub ORDER BY ym",                 # <=> ：两边都是空也算相等
        {"id": task["id"], "region": region, "category": category, "sub": sub})
    return {
        "title": " · ".join(x for x in (region, category, sub) if x),
        "region": region, "category": category, "sub_category": sub,
        "history": {"months": [h["ym"] for h in history], "quantity": [int(h["quantity"]) for h in history]},
        "rows": [{"ym": r["ym"], "forecast": not_negative(float(r["forecast_qty"])),
                  "lower": not_negative(float(r["lower_qty"])), "upper": not_negative(float(r["upper_qty"])),
                  "actual": r["actual_qty"], "safety_stock": float(r["safety_stock"]),
                  "purchase": not_negative(r["suggested_purchase"])} for r in rows],
        "has_actual": any(r["actual_qty"] is not None for r in rows),
        # 这条序列里有没有哪个月的区间下限是 0（算出来不大于 0 的都按 0）。有的话页面上加一句说明
        "lower_note": LOWER_NOTE if any(float(r["lower_qty"]) <= 0 for r in rows) else "",
    }


# ---------------- 说明文字、导出 ----------------
def settings_text(task):
    """本次预测的设置，一项一项列出来，页面顶部和导出文件里都用"""
    items = [("预测年份", f"{task['target_year']} 年"),
             ("历史数据", f"{task['history_start']}–{task['history_end']} 年"),
             ("地区", task["region"]), ("类别", task["category"]), ("预测层级", task["level_name"])]
    if task["level_name"] == LEVEL_SUB:
        items.append(("子类别", task["sub_category"] or ALL))
    items += [("当前库存", f"{task['inventory']} 件"), ("补货周期", f"{task['lead_time']} 个月"),
              ("服务水平", f"{task['service_percent']}%")]
    return items


def purchase_text(task):
    """一句话说明建议进货量是怎么算的，写出补货周期是几个月"""
    n, stock = task["lead_time"], task["inventory"]
    if n == 1:
        return (f"建议进货量：补货周期 1 个月，每月进一次货。第 1 个月 = 当月预测销量 + 安全库存 − 当前库存（{stock} 件），"
                "之后每月 = 当月预测销量；都向上取整。")
    return (f"建议进货量：补货周期 {n} 个月，每 {n} 个月进一次货，一次进够接下来 {n} 个月的量。"
            f"第 1 次 = 前 {n} 个月预测销量之和 + 安全库存 − 当前库存（{stock} 件），"
            f"之后每次 = 接下来 {n} 个月预测销量之和；都向上取整，不进货的月份为 0。")


def result_xlsx(task):
    """导出这个任务的全部预测结果：一个工作表是明细，一个工作表是本次预测的设置"""
    fields = list(EXPORT_COLUMNS)
    rows = fetch_all(f"SELECT {', '.join(fields)} FROM forecast_detail WHERE task_id = :id ORDER BY id",
                     {"id": task["id"]})
    df = pd.DataFrame(rows, columns=fields).rename(columns=EXPORT_COLUMNS)
    for c in ("预测销量", "预测下限", "预测上限", "安全库存"):
        df[c] = df[c].astype(float)
    has_zero_lower = bool((df["预测下限"] <= 0).any())
    for c in ("预测销量", "预测下限", "预测上限", "建议进货量"):          # 和页面一样：负数按 0 显示
        df[c] = df[c].clip(lower=0)
    if task["level_name"] != LEVEL_SUB:
        df = df.drop(columns=["子类别"])
    notes = [("预测时间", task["created_at"].strftime("%Y-%m-%d %H:%M:%S")), ("进货量算法", purchase_text(task))]
    if has_zero_lower:
        notes.append(("预测下限", LOWER_NOTE))
    settings = pd.DataFrame(settings_text(task) + notes, columns=["项目", "内容"])
    buf = BytesIO()
    with pd.ExcelWriter(buf) as xw:
        df.to_excel(xw, sheet_name="预测结果", index=False)
        settings.to_excel(xw, sheet_name="预测设置", index=False)
        xw.sheets["预测设置"].column_dimensions["B"].width = 90
    buf.seek(0)
    return buf
