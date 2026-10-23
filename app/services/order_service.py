# -*- coding: utf-8 -*-
"""首页的概览数字，以及订单查询（筛选、搜索、排序、分页）"""
from datetime import datetime

from app.constants import CATEGORIES, CATEGORY_SUBS, REGIONS, SOURCES
from app.db import fetch_all, fetch_one

PAGE_SIZE = 20
# 允许排序的列（白名单）：网址里的名字 -> 数据库字段。不在这里面的一律按订单日期排
SORT_COLUMNS = {"order_date": "order_date", "sales": "sales", "profit": "profit"}
ALL_SUBS = [s for subs in CATEGORY_SUBS.values() for s in subs]


# ---------------- 首页概览 ----------------
def overview():
    """订单行数、订单数（按订单键去重）、时间范围、各来源条数"""
    total = fetch_one("SELECT COUNT(*) AS n_rows, COUNT(DISTINCT order_key) AS n_orders, "
                      "MIN(order_date) AS first_day, MAX(order_date) AS last_day FROM sales_order")
    by_source = {r["source"]: r["n"] for r in
                 fetch_all("SELECT source, COUNT(*) AS n FROM sales_order GROUP BY source")}
    total["sources"] = [(name, by_source.get(name, 0)) for name in SOURCES]
    return total


def monthly_sales():
    """每个月的销售额，给首页的走势图用"""
    rows = fetch_all("SELECT ym, SUM(sales) AS sales FROM sales_order GROUP BY ym ORDER BY ym")
    return {"months": [r["ym"] for r in rows], "sales": [float(r["sales"]) for r in rows]}


# ---------------- 订单查询 ----------------
def parse_date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return ""


def clean_filters(args):
    """
    把网址里的筛选条件整理成固定的几项。取值不在允许范围内的直接丢掉，
    所以后面拼 SQL 时用到的列名、排序方向都只可能是白名单里的值
    """
    category = args.get("category", "")
    category = category if category in CATEGORIES else ""
    sub = args.get("sub_category", "")
    allowed_subs = CATEGORY_SUBS[category] if category else ALL_SUBS     # 子类别要属于所选类别
    region, source = args.get("region", ""), args.get("source", "")
    sort = args.get("sort", "")
    return {
        "date_from": parse_date(args.get("date_from")),
        "date_to": parse_date(args.get("date_to")),
        "region": region if region in REGIONS else "",
        "category": category,
        "sub_category": sub if sub in allowed_subs else "",
        "source": source if source in SOURCES else "",
        "q": (args.get("q") or "").strip()[:50],
        "sort": sort if sort in SORT_COLUMNS else "order_date",
        "dir": "asc" if args.get("dir") == "asc" else "desc",
    }


def build_where(f):
    """按筛选条件生成 WHERE 子句。用户输入的值全部走参数绑定（:name），不拼进 SQL 字符串"""
    conds, params = [], {}
    for key, sql in [("date_from", "order_date >= :date_from"), ("date_to", "order_date <= :date_to"),
                     ("region", "region = :region"), ("category", "category = :category"),
                     ("sub_category", "sub_category = :sub_category"), ("source", "source = :source"),
                     ("q", "LOCATE(:q, product_name) > 0")]:      # 商品名里包含这段文字
        if f[key]:
            conds.append(sql)
            params[key] = f[key]
    return (" WHERE " + " AND ".join(conds) if conds else ""), params


def order_by(f):
    return f" ORDER BY {SORT_COLUMNS[f['sort']]} {'ASC' if f['dir'] == 'asc' else 'DESC'}, row_id"


def query_orders(f, page):
    """返回 (这一页的订单, 符合条件的总条数, 实际页码, 总页数)"""
    where, params = build_where(f)
    total = fetch_one("SELECT COUNT(*) AS n FROM sales_order" + where, params)["n"]
    pages = max(1, -(-total // PAGE_SIZE))          # 向上取整
    page = min(max(page, 1), pages)
    rows = fetch_all(
        "SELECT row_id, order_date, order_id, customer_name, region, province, city, category, "
        "sub_category, product_name, sales, quantity, discount, profit, source FROM sales_order"
        + where + order_by(f) + " LIMIT :limit OFFSET :offset",
        {**params, "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE})
    return rows, total, page, pages
