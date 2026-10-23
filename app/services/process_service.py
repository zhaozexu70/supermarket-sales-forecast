# -*- coding: utf-8 -*-
"""
数据处理和展示（设计说明 8.3）：对所选年份的订单做清洗和按月汇总，重新生成两张月度表。
清洗、汇总、补零的规则都在 src/preprocess_core.py 里，和阶段一的 step1 用的是同一套函数
"""
import time
from io import BytesIO

import pandas as pd
from sqlalchemy import text

from app.constants import (CATEGORIES, CATEGORY_SUBS, MONTHLY_COLUMNS, ORDER_COLUMNS, REGIONS,
                           TEMPLATE_COLUMNS)
from app.db import execute, fetch_all, fetch_one, write_transaction
from app.services import state_service
from preprocess_core import (add_features, drop_duplicate_rows, drop_invalid_rows, flag_outliers,
                             month_list, monthly_region_cat, monthly_region_sub, parse_types, strip_text)

PAGE_SIZE = 20
LEVEL_CAT, LEVEL_SUB = "类别", "子类别"
ALL_SUBS = [s for subs in CATEGORY_SUBS.values() for s in subs]
# 从订单表读哪些字段：原始数据的 19 个字段（数据库不存“国家”）。订单键、年月这些派生字段读出来以后重新算
RAW_FIELDS = [en for en, cn in ORDER_COLUMNS.items() if cn in TEMPLATE_COLUMNS]
# 全部组合：6 个地区 × 17 个子类别。汇总时每个组合每个月都要有一行
COMBOS = pd.DataFrame([(r, c, s) for r in REGIONS for c, subs in CATEGORY_SUBS.items() for s in subs],
                      columns=["地区", "类别", "子类别"])
SUB_FIELDS = ["region", "category", "sub_category", "ym", "sales", "quantity", "profit", "order_count"]
CAT_FIELDS = [f for f in SUB_FIELDS if f != "sub_category"]


class ProcessError(Exception):
    """不能处理时的原因，直接显示给用户"""


# ---------------- 处理 ----------------
def check_range(start_year, end_year):
    """年份范围要合理：起始年不晚于结束年，范围里每一年都有订单"""
    years = [y["year"] for y in state_service.year_status()]
    if start_year not in years or end_year not in years:
        raise ProcessError("请从下拉框里选择起始年和结束年")
    if start_year > end_year:
        raise ProcessError("起始年不能晚于结束年")
    empty = [y for y in range(start_year, end_year + 1) if y not in years]
    if empty:
        raise ProcessError(f"所选范围里 {'、'.join(map(str, empty))} 年没有任何订单，中间不能有整年空缺。"
                           "请把有数据的年份分开处理")


def insert_monthly(conn, table, fields, data):
    """把汇总结果写进月度表，每 1000 行一批"""
    rows = data.rename(columns={cn: en for en, cn in MONTHLY_COLUMNS.items()})[fields].to_dict("records")
    sql = text(f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({', '.join(':' + f for f in fields)})")
    for i in range(0, len(rows), 1000):
        conn.execute(sql, rows[i:i + 1000])


def run(start_year, end_year, user_id):
    """处理所选年份的全部订单（不分地区、类别），返回 (处理记录的编号, 用了多少秒)"""
    check_range(start_year, end_year)
    started = time.perf_counter()
    with write_transaction() as conn:                     # 处理期间不让别的请求改订单
        # 1. 读取所选年份的订单，列名改回中文
        df = pd.read_sql(text(f"SELECT {', '.join(RAW_FIELDS)} FROM sales_order "
                              "WHERE YEAR(order_date) BETWEEN :a AND :b ORDER BY row_id"),
                         conn, params={"a": start_year, "b": end_year}).rename(columns=ORDER_COLUMNS)
        log = {"start_year": start_year, "end_year": end_year, "order_rows": len(df), "user_id": user_id}
        # 2. 文本去首尾空格
        df = strip_text(df)
        # 3. 重复录入：除行 ID 外都相同的只留一条
        df, log["duplicate_rows"] = drop_duplicate_rows(df, ignore=["行 ID"])
        # 4. 日期、数值转好类型后，去掉不合规的行
        df = parse_types(df, date_format="%Y-%m-%d")
        df, log["invalid_rows"] = drop_invalid_rows(df)
        if df.empty:
            raise ProcessError("所选年份没有可以处理的订单")
        df = add_features(df)                             # 订单键、年月等派生字段
        # 5. IQR 异常值：只数有多少行，不删。上下界用所选年份的这批订单来算
        flagged = flag_outliers(df)
        log["outlier_sales"] = int(flagged["销售额_异常标记"].sum())
        log["outlier_profit"] = int(flagged["利润_异常标记"].sum())
        # 6、7. 按月汇总并补零：从所选年份的 1 月到最后一个有订单的月份，还没发生的月份不补
        months = month_list(f"{start_year}-01", df["年月"].max())
        by_sub = monthly_region_sub(df, months, COMBOS)
        by_cat = monthly_region_cat(df, months, COMBOS)
        log["month_count"] = len(months)
        log["zero_filled_sub"] = int((by_sub["订单数"] == 0).sum())
        log["zero_filled_cat"] = int((by_cat["订单数"] == 0).sum())
        # 8. 写库：删掉这些年份的旧汇总，写入新的；记一条处理记录；这些年份标为已处理
        span = {"a": f"{start_year}-01", "b": f"{end_year}-12"}
        for table, fields, data in (("monthly_sales", SUB_FIELDS, by_sub),
                                    ("monthly_sales_category", CAT_FIELDS, by_cat)):
            conn.execute(text(f"DELETE FROM {table} WHERE ym BETWEEN :a AND :b"), span)
            insert_monthly(conn, table, fields, data)
        log_id = conn.execute(text(f"INSERT INTO process_log ({', '.join(log)}) "
                                   f"VALUES ({', '.join(':' + k for k in log)})"), log).lastrowid
        state_service.mark_processed(conn, range(start_year, end_year + 1))
    seconds = round(time.perf_counter() - started, 2)     # 事务提交完才算结束，所以用时在外面另记
    execute("UPDATE process_log SET seconds = :s WHERE id = :id", {"s": seconds, "id": log_id})
    return log_id, seconds


# ---------------- 处理摘要 ----------------
def get_log(log_id=None):
    """某一次处理的记录；不指定就是最近一次。没有记录时返回 None"""
    where, params = ("WHERE p.id = :id", {"id": log_id}) if log_id else ("", {})
    return fetch_one("SELECT p.*, u.username FROM process_log p LEFT JOIN sys_user u ON u.id = p.user_id "
                     f"{where} ORDER BY p.id DESC LIMIT 1", params)


# ---------------- 处理结果：月度汇总表、走势图 ----------------
def clean_view(args):
    """查看结果时的筛选条件：层级、地区、类别、子类别。取值不在允许范围内的当作没选"""
    level = LEVEL_SUB if args.get("level") == LEVEL_SUB else LEVEL_CAT
    region, category, sub = args.get("region", ""), args.get("category", ""), args.get("sub_category", "")
    category = category if category in CATEGORIES else ""
    allowed = CATEGORY_SUBS[category] if category else ALL_SUBS
    return {"level": level, "region": region if region in REGIONS else "", "category": category,
            "sub_category": sub if level == LEVEL_SUB and sub in allowed else ""}


def source(v):
    """按筛选条件决定查哪张月度表、带什么条件。返回 (表名, 字段, WHERE 子句, 参数)"""
    table, fields = (("monthly_sales", SUB_FIELDS) if v["level"] == LEVEL_SUB
                     else ("monthly_sales_category", CAT_FIELDS))
    conds = [f"{k} = :{k}" for k in ("region", "category", "sub_category") if v[k]]
    params = {k: v[k] for k in ("region", "category", "sub_category") if v[k]}
    return table, fields, (" WHERE " + " AND ".join(conds) if conds else ""), params


def order_by(fields):
    """先按月份，再按地区、类别（、子类别）排"""
    return " ORDER BY ym, " + ", ".join(f for f in fields[:-4] if f != "ym")


def monthly_rows(v, page):
    """月度汇总表的一页。返回 (这一页的行, 总行数, 实际页码, 总页数)"""
    table, fields, where, params = source(v)
    total = fetch_one(f"SELECT COUNT(*) AS n FROM {table}{where}", params)["n"]
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(page, 1), pages)
    rows = fetch_all(f"SELECT {', '.join(fields)} FROM {table}{where}{order_by(fields)} "
                     "LIMIT :limit OFFSET :offset",
                     {**params, "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE})
    return rows, total, page, pages


def monthly_chart(v):
    """走势图的数据：符合筛选条件的每月销量合计"""
    table, _, where, params = source(v)
    rows = fetch_all(f"SELECT ym, SUM(quantity) AS quantity FROM {table}{where} GROUP BY ym ORDER BY ym", params)
    return {"months": [r["ym"] for r in rows], "quantity": [int(r["quantity"]) for r in rows]}


def zero_info(v):
    """补零情况：当前筛选条件下一共多少个格子（一个组合的一个月算一格），其中多少个是补的 0"""
    table, _, where, params = source(v)
    return fetch_one(f"SELECT COUNT(*) AS cells, COALESCE(SUM(order_count = 0), 0) AS zeros "
                     f"FROM {table}{where}", params)


def monthly_xlsx(v):
    """按当前筛选条件导出月度汇总表"""
    table, fields, where, params = source(v)
    rows = fetch_all(f"SELECT {', '.join(fields)} FROM {table}{where}{order_by(fields)}", params)
    df = pd.DataFrame(rows, columns=fields).rename(columns=MONTHLY_COLUMNS)
    for c in ("销售额", "利润"):
        df[c] = df[c].astype(float)
    buf = BytesIO()
    df.to_excel(buf, index=False, sheet_name="月度汇总")
    buf.seek(0)
    return buf
