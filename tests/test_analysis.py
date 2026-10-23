# -*- coding: utf-8 -*-
"""
销售数据分析展示：8 个图表数据接口。数字要和阶段一探索性分析（EDA）的结果一致，
每个数另外用 pandas 从清洗后的订单明细 orders_clean.csv 里独立算一遍来核对。只用测试库
"""
import re

import pandas as pd
import pytest

from app import constants
from app.services import analysis_service
from config import PROCESSED_DIR, TEST_DB_NAME
from conftest import log_in, require_test_db, reset_imported_data, run_sql
from step3_load_mysql import load

CHARTS = ["trend", "season", "yoy", "annual", "region", "subcategory", "heatmap", "quadrant"]


@pytest.fixture(scope="module", autouse=True)
def original_data():
    """测试库装成原始数据：月度表是 step1 的结果"""
    load(require_test_db(TEST_DB_NAME), force=True)
    yield
    reset_imported_data()


@pytest.fixture(scope="module")
def clean():
    return pd.read_csv(PROCESSED_DIR / "orders_clean.csv", encoding="utf-8-sig")


@pytest.fixture
def api(client):
    def get(name, **params):
        resp = client.get(f"/api/analysis/{name}", query_string=params)
        assert resp.status_code == 200 and resp.mimetype == "application/json"
        return resp.get_json()
    return get


# ---------------- 和阶段一 EDA 的数字一致 ----------------
def test_annual_growth(api, clean):
    """销售额逐年增长 17.1%、23.6%、28.7%（2022–2024）"""
    years = api("annual")["years"]
    assert [y["year"] for y in years] == [2021, 2022, 2023, 2024]
    assert [y["growth"] for y in years] == [None, 17.1, 23.6, 28.7]
    assert [y["months"] for y in years] == [12, 12, 12, 12]
    by_year = clean.groupby("年")["销售额"].sum()
    assert [y["growth"] for y in years][1:] == (by_year.pct_change() * 100).round(1).tolist()[1:]
    assert max(abs(y["sales"] - by_year[y["year"]]) for y in years) < 1
    assert abs(sum(y["sales"] for y in years) - 16068954) < 1


def test_region_share(api, clean):
    """华东占销售额 29.2%，中南 25.7%，西北 5.1%"""
    regions = {r["region"]: r for r in api("region")["regions"]}
    assert list(regions) == constants.REGIONS
    assert (regions["华东"]["share"], regions["中南"]["share"], regions["西北"]["share"]) == (29.2, 25.7, 5.1)
    assert round(sum(r["share"] for r in regions.values()), 1) == 100.0
    want = (clean.groupby("地区")["销售额"].sum() / clean["销售额"].sum() * 100).round(1)
    assert {name: r["share"] for name, r in regions.items()} == want.to_dict()
    profit = clean.groupby("地区")["利润"].sum()
    assert max(abs(r["profit"] - profit[name]) for name, r in regions.items()) < 1


def test_subcategory_profit(api, clean):
    """桌子四年利润约 −13.3 万；亏损的子类别只有桌子和美术"""
    subs = {s["sub_category"]: s for s in api("subcategory")["subs"]}
    assert len(subs) == 17
    assert round(subs["桌子"]["profit"] / 10000, 1) == -13.3
    assert sorted(name for name, s in subs.items() if s["profit"] < 0) == ["桌子", "美术"]
    sales = [s["sales"] for s in subs.values()]
    assert sales == sorted(sales, reverse=True)                              # 按销售额从高到低
    want = clean.groupby("子类别")[["销售额", "利润"]].sum()
    for name, s in subs.items():
        assert abs(s["profit"] - want.loc[name, "利润"]) < 1 and abs(s["sales"] - want.loc[name, "销售额"]) < 1
        assert s["category"] == next(c for c, names in constants.CATEGORY_SUBS.items() if name in names)


def test_season_share(api, clean):
    """5–6 月和 8–12 月每月约占全年销量的 10%～11%，1–4 月和 7 月约 4.5%～6%"""
    d = api("season")
    share = dict(zip(range(1, 13), d["share"]))
    assert d["months"][0] == "1月" and sum(d["quantity"]) == 37534
    assert all(10.0 <= share[m] <= 11.0 for m in (5, 6, 8, 9, 10, 11, 12))
    assert all(4.5 <= share[m] <= 6.0 for m in (1, 2, 3, 4, 7))
    want = (clean.groupby("月")["数量"].sum() / clean["数量"].sum() * 100).round(1)
    assert d["share"] == want.tolist()


def test_trend_and_yoy(api, clean):
    d = api("trend")
    assert len(d["months"]) == 48 and d["months"][0] == "2021-01" and d["months"][-1] == "2024-12"
    assert d["quantity"] == clean.groupby("年月")["数量"].sum().tolist() and sum(d["quantity"]) == 37534
    assert max(abs(a - b) for a, b in zip(d["sales"], clean.groupby("年月")["销售额"].sum())) < 1
    lines = {y["year"]: y["quantity"] for y in api("yoy")["years"]}
    assert list(lines) == ["2021", "2022", "2023", "2024"]
    assert lines["2023"] == clean[clean["年"] == 2023].groupby("月")["数量"].sum().tolist()


def test_heatmap(api, clean):
    d = api("heatmap")
    assert d["regions"] == constants.REGIONS and len(d["subs"]) == 17 and len(d["cells"]) == 102
    want = clean.groupby(["地区", "子类别"])["数量"].sum()
    for x, y, value in d["cells"]:
        assert value == want[(d["regions"][y], d["subs"][x])]


def test_quadrant(api, clean):
    d = api("quadrant")
    points = {p["sub_category"]: p for p in d["points"]}
    g = clean.groupby("子类别")[["数量", "销售额", "利润"]].sum()
    rate = (g["利润"] / g["销售额"] * 100).round(1)
    assert len(points) == 17
    for name, p in points.items():
        assert p["quantity"] == g.loc[name, "数量"] and p["profit_rate"] == rate[name]
    assert d["median_quantity"] == g["数量"].median() and d["median_rate"] == rate.median()
    # 每个点所在的象限，和它相对两个中位数的位置一致
    names = {(True, True): "高销量高利润", (True, False): "高销量低利润",
             (False, True): "低销量高利润", (False, False): "低销量低利润"}
    for p in points.values():
        key = (p["quantity"] >= d["median_quantity"], p["profit_rate"] >= d["median_rate"])
        assert p["quadrant"] == names[key]
    groups = {x["quadrant"]: x for x in d["groups"]}
    assert [groups[n]["advice"] for n in names.values()] == ["重点保障", "控制折扣", "适度备货", "减少进货"]
    assert sum(len(x["subs"]) for x in groups.values()) == 17
    assert "桌子" in groups["低销量低利润"]["subs"] and points["桌子"]["profit_rate"] < 0
    assert "椅子" in groups["高销量高利润"]["subs"]


# ---------------- 筛选条件 ----------------
def test_filters(api, clean):
    east = clean[(clean["地区"] == "华东") & (clean["类别"] == "家具")]
    d = api("trend", region="华东", category="家具")
    assert sum(d["quantity"]) == east["数量"].sum() and len(d["months"]) == 48
    d = api("trend", start=2023, end=2024, region="华东")
    part = clean[(clean["地区"] == "华东") & (clean["年"] >= 2023)]
    assert len(d["months"]) == 24 and d["months"][0] == "2023-01" and sum(d["quantity"]) == part["数量"].sum()
    assert [y["year"] for y in api("annual", start=2022, end=2023)["years"]] == [2022, 2023]
    assert [y["growth"] for y in api("annual", start=2022, end=2023)["years"]] == [None, 23.6]

    assert [r["region"] for r in api("region", region="西北")["regions"]] == ["西北"]
    assert {s["category"] for s in api("subcategory", category="技术")["subs"]} == {"技术"}
    d = api("heatmap", region="华东", category="家具")
    assert d["regions"] == ["华东"] and d["subs"] == ["书架", "桌子", "椅子", "用具"] and len(d["cells"]) == 4
    assert len(api("quadrant", category="办公用品")["points"]) == 9


def test_bad_filters_fall_back(api):
    """取值不对的条件当作没选；起始年和结束年写反了就换过来；带引号的输入不会破坏 SQL"""
    full = api("trend")
    assert api("trend", start="abc", end=1900, region="火星", category="零食") == full
    assert api("trend", start=2024, end=2021) == full
    assert api("trend", region="华东' OR '1'='1", category="x'; DROP TABLE monthly_sales; --") == full
    assert api("annual")["years"][0]["year"] == 2021


def test_unknown_chart_and_login(client, anon_client):
    assert client.get("/api/analysis/nothing").status_code == 404
    for url in ("/analysis", "/api/analysis/trend"):
        resp = anon_client.get(url)
        assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_sql_uses_bound_parameters(app, monkeypatch):
    """用户选的地区、类别、年份只出现在参数里，不出现在 SQL 文字里"""
    seen = []
    monkeypatch.setattr(analysis_service, "fetch_all", lambda sql, params=None: seen.append((sql, params)) or [])
    f = {"start": 2022, "end": 2023, "region": "华东", "category": "家具", "years": [2021, 2022, 2023, 2024]}
    with app.app_context():
        for name in CHARTS:
            analysis_service.CHARTS[name](f)
    assert len(seen) == 8
    for sql, params in seen:
        assert params == {"a": "2022-01", "b": "2023-12", "region": "华东", "category": "家具"}
        assert "华东" not in sql and "家具" not in sql and "2022" not in sql
        assert "ym BETWEEN :a AND :b AND region = :region AND category = :category" in sql


# ---------------- 页面 ----------------
def test_page(client):
    html = client.get("/analysis?start=2022&end=2024&region=华东").get_data(as_text=True)
    for name in CHARTS:
        assert f'id="chart_{name}"' in html
    assert re.findall(r"<option[^>]*>(\d{4})</option>", html.split('id="start"')[1].split("</select>")[0]) \
        == ["2021", "2022", "2023", "2024"]
    assert '"start": 2022' in html and '"end": 2024' in html and '"region": "华东"' in html
    assert 'id="clearLink"' in html and "js/analysis.js" in html and 'id="staleBanner"' not in html
    assert "/api/analysis/NAME" in html


def test_stale_banner(client):
    """所选年份里有需要重新处理的年份时，页面顶部有提示和去数据处理的按钮"""
    run_sql(["UPDATE year_state SET data_version = 2 WHERE `year` = 2023"])
    try:
        html = client.get("/analysis").get_data(as_text=True)
        assert 'id="staleBanner"' in html and "部分年份数据已更新但尚未处理（2023 年）" in html
        assert 'href="/process">去数据处理' in html
        assert 'id="staleBanner"' not in client.get("/analysis?start=2021&end=2022").get_data(as_text=True)
    finally:
        reset_imported_data()


def test_empty_database(client, api):
    """月度表是空的（比如还没处理过）时，接口不报错"""
    run_sql(["CREATE TABLE monthly_backup AS SELECT * FROM monthly_sales", "DELETE FROM monthly_sales"])
    try:
        assert api("trend")["months"] == [] and api("quadrant")["points"] == []
        assert api("annual")["years"] == [] and api("region")["regions"] == []
        assert client.get("/analysis").status_code == 200
    finally:
        run_sql(["INSERT INTO monthly_sales SELECT * FROM monthly_backup", "DROP TABLE monthly_backup"])
