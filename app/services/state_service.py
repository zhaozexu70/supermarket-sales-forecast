# -*- coding: utf-8 -*-
"""各年份的数据版本与处理状态（设计说明第 7 节）"""
from sqlalchemy import text

from app.db import fetch_all

PROCESSED, CHANGED, NOT_YET = "已处理", "数据有更新，需重新处理", "尚未处理"


def year_status():
    """
    每个有订单的年份一行：订单行数、有订单的月份数、是否完整年份、处理状态
    已处理的条件：year_state 里这一年的 processed_version 等于 data_version
    """
    years = fetch_all("SELECT YEAR(order_date) AS y, COUNT(*) AS n_rows, COUNT(DISTINCT ym) AS n_months "
                      "FROM sales_order GROUP BY YEAR(order_date) ORDER BY y")
    state = {r["year"]: r for r in fetch_all("SELECT `year`, data_version, processed_version FROM year_state")}
    result = []
    for r in years:
        s = state.get(r["y"])
        if s is None or s["processed_version"] is None:
            status = NOT_YET
        elif s["processed_version"] == s["data_version"]:
            status = PROCESSED
        else:
            status = CHANGED
        result.append({"year": r["y"], "rows": r["n_rows"], "months": r["n_months"],
                       "complete": r["n_months"] == 12, "status": status,
                       "processed": status == PROCESSED})
    return result


def bump_years(conn, years):
    """
    这些年份的订单有增加或删除：数据版本号加 1，这一年就变成“需重新处理”。
    第一次出现的年份新插一行，版本号是 1，还没处理过。conn 是调用方的事务，和订单的改动一起提交
    """
    for y in years:
        conn.execute(text("INSERT INTO year_state (`year`, data_version, processed_version) "
                          "VALUES (:y, 1, NULL) ON DUPLICATE KEY UPDATE data_version = data_version + 1"),
                     {"y": int(y)})


def mark_processed(conn, years):
    """这些年份刚处理完：把“处理时的版本号”设成现在的数据版本号，两个数相等就是“已处理”"""
    for y in years:
        conn.execute(text("INSERT INTO year_state (`year`, data_version, processed_version) VALUES (:y, 1, 1) "
                          "ON DUPLICATE KEY UPDATE processed_version = data_version"), {"y": int(y)})
