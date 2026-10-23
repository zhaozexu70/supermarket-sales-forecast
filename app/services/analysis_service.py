# -*- coding: utf-8 -*-
"""
销售数据分析展示（设计说明 8.4）：给 8 张图表准备数据。
数据都来自处理后的月度汇总表 monthly_sales（地区 × 子类别 × 月份），按年份范围、地区、类别筛选。
用户选的条件一律走参数绑定（:name），不拼进 SQL
"""
from statistics import median

from app.constants import CATEGORIES, CATEGORY_SUBS, REGIONS
from app.db import fetch_all
from app.services import state_service

# 四象限：按销量、利润率是否不低于中位数来分，每个象限一句进货建议
QUADRANTS = {
    (True, True): ("高销量高利润", "重点保障", "卖得多、利润率也高，优先保证不断货"),
    (True, False): ("高销量低利润", "控制折扣", "卖得多但利润率低，进货量照常，重点是少打折"),
    (False, True): ("低销量高利润", "适度备货", "卖得不多但利润率高，保持适量库存"),
    (False, False): ("低销量低利润", "减少进货", "卖得少、利润率也低，减少进货量"),
}


# ---------------- 筛选条件 ----------------
def available_years():
    """可以选的年份：月度表里有数据的年份，加上有订单但还没处理的年份"""
    rows = fetch_all("SELECT DISTINCT LEFT(ym, 4) AS y FROM monthly_sales")
    years = {int(r["y"]) for r in rows} | {y["year"] for y in state_service.year_status()}
    return sorted(years)


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def clean_filters(args):
    """整理筛选条件：年份不在可选范围内就用最早、最晚的年份；地区、类别取值不对的当作没选"""
    years = available_years()
    start, end = to_int(args.get("start")), to_int(args.get("end"))
    start = start if start in years else (years[0] if years else None)
    end = end if end in years else (years[-1] if years else None)
    if years and start > end:
        start, end = end, start
    region, category = args.get("region", ""), args.get("category", "")
    return {"start": start, "end": end, "years": years,
            "region": region if region in REGIONS else "",
            "category": category if category in CATEGORIES else ""}


def stale_years(f):
    """所选范围里，数据有更新或还没处理的年份"""
    if f["start"] is None:
        return []
    return [y["year"] for y in state_service.year_status()
            if f["start"] <= y["year"] <= f["end"] and not y["processed"]]


def query(f, select, tail):
    """在月度表上按筛选条件查询。select、tail 是程序里写死的片段，用户选的值都在参数里"""
    conds, params = ["ym BETWEEN :a AND :b"], {"a": f"{f['start']}-01", "b": f"{f['end']}-12"}
    for key in ("region", "category"):
        if f[key]:
            conds.append(f"{key} = :{key}")
            params[key] = f[key]
    return fetch_all(f"SELECT {select} FROM monthly_sales WHERE {' AND '.join(conds)} {tail}", params)


def percent(part, whole):
    return round(float(part) / float(whole) * 100, 1) if whole else 0.0


# ---------------- 8 张图的数据 ----------------
def trend(f):
    """月度走势：每个月的销售额和销量"""
    rows = query(f, "ym, SUM(sales) AS sales, SUM(quantity) AS quantity", "GROUP BY ym ORDER BY ym")
    return {"months": [r["ym"] for r in rows], "sales": [float(r["sales"]) for r in rows],
            "quantity": [int(r["quantity"]) for r in rows]}


def season(f):
    """淡旺季：1–12 月各占所选年份总销量的百分之几"""
    rows = query(f, "RIGHT(ym, 2) AS m, SUM(quantity) AS quantity", "GROUP BY m")
    by_month = {int(r["m"]): int(r["quantity"]) for r in rows}
    total = sum(by_month.values())
    quantity = [by_month.get(m, 0) for m in range(1, 13)]
    return {"months": [f"{m}月" for m in range(1, 13)], "quantity": quantity,
            "share": [percent(q, total) for q in quantity]}


def yoy(f):
    """各年同期对比：每年一条线，横轴是 1–12 月，纵轴是销量。没有数据的月份是空的，不画成 0"""
    rows = query(f, "LEFT(ym, 4) AS y, RIGHT(ym, 2) AS m, SUM(quantity) AS quantity", "GROUP BY y, m ORDER BY y, m")
    lines = {}
    for r in rows:
        lines.setdefault(r["y"], [None] * 12)[int(r["m"]) - 1] = int(r["quantity"])
    return {"months": [f"{m}月" for m in range(1, 13)],
            "years": [{"year": y, "quantity": q} for y, q in lines.items()]}


def annual(f):
    """年度增长：每年的销售额、利润，以及销售额比上一年增长百分之几（两年都有 12 个月才算）"""
    rows = query(f, "LEFT(ym, 4) AS y, SUM(sales) AS sales, SUM(profit) AS profit, COUNT(DISTINCT ym) AS months",
                 "GROUP BY y ORDER BY y")
    result, prev = [], None
    for r in rows:
        year, sales = int(r["y"]), float(r["sales"])
        can_compare = prev and prev["year"] == year - 1 and prev["months"] == 12 and r["months"] == 12
        result.append({"year": year, "sales": sales, "profit": float(r["profit"]), "months": r["months"],
                       "growth": round((sales - prev["sales"]) / prev["sales"] * 100, 1) if can_compare else None})
        prev = result[-1]
    return {"years": result}


def region(f):
    """地区对比：各地区的销售额、利润，以及销售额占比"""
    rows = {r["region"]: r for r in query(f, "region, SUM(sales) AS sales, SUM(profit) AS profit", "GROUP BY region")}
    total = sum(float(r["sales"]) for r in rows.values())
    return {"regions": [{"region": name, "sales": float(rows[name]["sales"]), "profit": float(rows[name]["profit"]),
                         "share": percent(rows[name]["sales"], total)} for name in REGIONS if name in rows]}


def subcategory(f):
    """品类贡献：各子类别的销售额、利润，按销售额从高到低"""
    rows = query(f, "category, sub_category, SUM(sales) AS sales, SUM(profit) AS profit",
                 "GROUP BY category, sub_category ORDER BY sales DESC")
    return {"subs": [{"category": r["category"], "sub_category": r["sub_category"], "sales": float(r["sales"]),
                      "profit": float(r["profit"])} for r in rows]}


def heatmap(f):
    """地区 × 子类别热力图：每格是销量"""
    rows = query(f, "region, sub_category, SUM(quantity) AS quantity", "GROUP BY region, sub_category")
    regions = [r for r in REGIONS if not f["region"] or r == f["region"]]
    subs = [s for c in CATEGORIES if not f["category"] or c == f["category"] for s in CATEGORY_SUBS[c]]
    cells = {(r["region"], r["sub_category"]): int(r["quantity"]) for r in rows}
    return {"regions": regions, "subs": subs,
            "cells": [[x, y, cells.get((r, s), 0)] for y, r in enumerate(regions) for x, s in enumerate(subs)]}


def quadrant(f):
    """
    品类四象限：每个子类别一个点，横轴是销量，纵轴是利润率（利润 ÷ 销售额）。
    以销量、利润率各自的中位数为界分成四个象限，每个象限给一句进货建议
    """
    rows = query(f, "category, sub_category, SUM(quantity) AS quantity, SUM(sales) AS sales, SUM(profit) AS profit",
                 "GROUP BY category, sub_category HAVING SUM(sales) > 0 ORDER BY category, sub_category")
    points = [{"category": r["category"], "sub_category": r["sub_category"], "quantity": int(r["quantity"]),
               "profit_rate": percent(r["profit"], r["sales"])} for r in rows]
    if not points:
        return {"points": [], "median_quantity": 0, "median_rate": 0, "groups": []}
    mid_q, mid_r = median(p["quantity"] for p in points), median(p["profit_rate"] for p in points)
    for p in points:
        p["quadrant"], p["advice"], _ = QUADRANTS[(p["quantity"] >= mid_q, p["profit_rate"] >= mid_r)]
    groups = [{"quadrant": name, "advice": advice, "why": why,
               "subs": [p["sub_category"] for p in points if p["quadrant"] == name]}
              for name, advice, why in QUADRANTS.values()]
    return {"points": points, "median_quantity": mid_q, "median_rate": round(mid_r, 1), "groups": groups}


CHARTS = {"trend": trend, "season": season, "yoy": yoy, "annual": annual, "region": region,
          "subcategory": subcategory, "heatmap": heatmap, "quadrant": quadrant}
