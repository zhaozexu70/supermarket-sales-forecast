# -*- coding: utf-8 -*-
"""首页概览、订单查询（筛选、联动、搜索、分页、排序）、导出。只用测试库，里面装的是 9959 条原始订单"""
import re
from io import BytesIO

import pandas as pd
import pytest

from app import constants
from app.services import order_service
from config import PROCESSED_DIR
from conftest import require_test_db
from step3_load_mysql import connect, load


@pytest.fixture(scope="module", autouse=True)
def original_data():
    """这个文件里的测试开始前，把测试库重新装成原始数据"""
    from config import TEST_DB_NAME
    load(require_test_db(TEST_DB_NAME), force=True)


@pytest.fixture(scope="module")
def clean():
    """清洗后的订单明细，用 pandas 独立算一遍，和页面上的数字对比"""
    return pd.read_csv(PROCESSED_DIR / "orders_clean.csv", encoding="utf-8-sig")


@pytest.fixture
def imported_order(db_name):
    """临时加 1 条“表格导入”的 2025 年订单，测试完删掉"""
    def run(sql):
        conn = connect(require_test_db(db_name))
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        conn.close()
    run("INSERT INTO sales_order (row_id, order_id, order_key, order_date, ship_date, customer_id, "
        "customer_name, region, province, city, product_id, category, sub_category, product_name, "
        "sales, quantity, discount, profit, ym, source, batch_id) VALUES (10001, 'T-1', "
        "'T-1|20250105|C-1', '2025-01-05', '2025-01-06', 'C-1', '测试客户', '华东', '上海', '上海', "
        "'P-1', '办公用品', '纸张', '测试专用纸张', 100, 2, 0, 10, '2025-01', '表格导入', 1)")
    yield
    run("DELETE FROM sales_order WHERE row_id = 10001")


def total_of(client, url):
    html = client.get(url).get_data(as_text=True)
    return int(re.search(r'id="totalCount">(\d+)<', html).group(1))


def first_column(client, url, col):
    """取结果表格某一列的全部值（col 从 0 开始数）"""
    html = client.get(url).get_data(as_text=True)
    body = html.split("<tbody>")[1].split("</tbody>")[0]
    rows = re.findall(r"<tr>(.*?)</tr>", body, re.S)
    return [re.sub(r"<[^>]+>", "", re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)[col]).strip() for r in rows]


# ---------------- 首页 ----------------
def test_overview_numbers(client, clean):
    html = client.get("/").get_data(as_text=True)
    rows_by_year = clean.groupby("年").size()
    assert rows_by_year.sum() == 9959 and list(rows_by_year.index) == [2021, 2022, 2023, 2024]
    assert 'id="statRows">9959<' in html
    assert 'id="statOrders">4963<' in html
    assert "2021-01-01 至 2024-12-30" in html
    assert "原始数据 <b>9959</b>" in html and "表格导入 <b>0</b>" in html and "手工录入 <b>0</b>" in html
    for year, rows in rows_by_year.items():
        assert f"<td>{year}</td><td>{rows}</td>" in html
    assert html.count("text-bg-success") == 4 and html.count(">已处理<") == 4     # 四年都已处理
    assert "vendor/echarts/echarts.min.js" in html and '"2021-01"' in html and '"2024-12"' in html
    for name in ["数据导入和展示", "数据处理和展示", "销售分析", "数据预测", "预测结果"]:
        assert f"<b>{name}</b>" in html                                           # 功能入口


def test_overview_matches_csv(app, clean):
    with app.app_context():
        info, chart = order_service.overview(), order_service.monthly_sales()
    assert info["n_rows"] == len(clean) == 9959
    assert info["n_orders"] == clean["订单键"].nunique() == 4963
    assert len(chart["months"]) == 48
    by_month = clean.groupby("年月")["销售额"].sum()
    assert max(abs(a - b) for a, b in zip(chart["sales"], by_month.values)) < 1   # 只差金额的小数取舍


def test_overview_with_imported_year(client, imported_order):
    html = client.get("/").get_data(as_text=True)
    assert 'id="statRows">9960<' in html and "表格导入 <b>1</b>" in html
    assert "<td>2025</td><td>1</td>" in html and "尚未处理" in html and "（不完整）" in html
    assert "至 2025-01-05" in html


# ---------------- 筛选 ----------------
def test_no_filter(client):
    assert total_of(client, "/data") == 9959


@pytest.mark.parametrize("region", constants.REGIONS)
def test_filter_region(client, clean, region):
    assert total_of(client, f"/data?region={region}") == (clean["地区"] == region).sum()


@pytest.mark.parametrize("category", constants.CATEGORIES)
def test_filter_category(client, clean, category):
    assert total_of(client, f"/data?category={category}") == (clean["类别"] == category).sum()


def test_filter_combined(client, clean):
    """地区 + 类别 + 子类别 + 日期范围一起用"""
    d = clean[(clean["地区"] == "华东") & (clean["类别"] == "家具")]
    assert total_of(client, "/data?region=华东&category=家具") == len(d) > 0
    d2 = d[(d["子类别"] == "桌子") & (d["订单日期"] >= "2023-01-01") & (d["订单日期"] <= "2023-12-31")]
    url = "/data?region=华东&category=家具&sub_category=桌子&date_from=2023-01-01&date_to=2023-12-31"
    assert total_of(client, url) == len(d2) > 0
    assert set(first_column(client, url, 7)) == {"桌子"}
    assert all("2023-01-01" <= day <= "2023-12-31" for day in first_column(client, url, 0))


def test_filter_date_edges(client, clean):
    assert total_of(client, "/data?date_from=2024-12-30") == (clean["订单日期"] >= "2024-12-30").sum() > 0
    assert total_of(client, "/data?date_to=2021-01-01") == (clean["订单日期"] <= "2021-01-01").sum() > 0
    assert total_of(client, "/data?date_from=不是日期") == 9959            # 不合规的日期被忽略


def test_filter_source(client, imported_order):
    assert total_of(client, "/data?source=原始数据") == 9959
    assert total_of(client, "/data?source=表格导入") == 1
    assert total_of(client, "/data?source=手工录入") == 0
    assert first_column(client, "/data?source=表格导入", 13) == ["表格导入"]


def test_invalid_values_ignored(client):
    """不在允许范围内的取值当作没选"""
    assert total_of(client, "/data?region=火星&category=零食&source=不知道") == 9959


# ---------------- 子类别随类别联动 ----------------
def test_subcategory_must_belong_to_category(app, client, clean):
    with app.test_request_context():
        f = order_service.clean_filters({"category": "家具", "sub_category": "桌子"})
        assert f["sub_category"] == "桌子"
        f = order_service.clean_filters({"category": "家具", "sub_category": "纸张"})   # 纸张不属于家具
        assert f["sub_category"] == ""
        assert order_service.clean_filters({"sub_category": "纸张"})["sub_category"] == "纸张"
    assert total_of(client, "/data?category=家具&sub_category=纸张") == (clean["类别"] == "家具").sum()
    assert total_of(client, "/data?sub_category=纸张") == (clean["子类别"] == "纸张").sum()


def test_subcategory_options_in_page(client):
    html = client.get("/data?category=家具&sub_category=桌子").get_data(as_text=True)
    assert 'data-selected="桌子"' in html
    assert '"家具": ["书架", "桌子", "椅子", "用具"]' in html           # 联动用的对照表写在页面里


# ---------------- 商品名搜索 ----------------
def test_search_product_name(client, clean):
    assert total_of(client, "/data?q=订书机") == clean["产品名称"].str.contains("订书机", regex=False).sum() > 0
    assert all("订书机" in name for name in first_column(client, "/data?q=订书机", 8))
    assert total_of(client, "/data?q=世界上没有这种商品") == 0
    assert "没有符合条件的订单" in client.get("/data?q=世界上没有这种商品").get_data(as_text=True)


def test_search_special_characters(client):
    """% 和 _ 按普通文字处理；带引号的输入不会破坏 SQL"""
    assert total_of(client, "/data?q=%25") < 9959                # %25 是网址里的 %
    assert total_of(client, "/data?q=' OR '1'='1") == 0
    assert total_of(client, "/data?q=x'; DROP TABLE sales_order; --") == 0
    assert total_of(client, "/data") == 9959


# ---------------- 分页 ----------------
def test_paging(client):
    html = client.get("/data").get_data(as_text=True)
    assert "第 1 / 498 页" in html                               # 9959 ÷ 20 向上取整 = 498
    assert len(first_column(client, "/data", 0)) == 20
    assert len(first_column(client, "/data?page=498", 0)) == 19  # 最后一页剩 19 条
    assert "第 498 / 498 页" in client.get("/data?page=9999").get_data(as_text=True)   # 超出范围回到最后一页
    assert "第 1 / 498 页" in client.get("/data?page=-3").get_data(as_text=True)
    assert "第 1 / 498 页" in client.get("/data?page=abc").get_data(as_text=True)


def test_pages_do_not_overlap(app):
    with app.app_context():
        f = order_service.clean_filters({"region": "西北"})
        ids, page, pages = [], 1, 1
        while page <= pages:
            rows, total, _, pages = order_service.query_orders(f, page)
            ids += [r["row_id"] for r in rows]
            page += 1
    assert len(ids) == len(set(ids)) == total                    # 翻完所有页，不重不漏


def test_filters_stay_in_links(client):
    html = client.get("/data?region=华东&category=家具&sort=sales&dir=asc&page=2").get_data(as_text=True)
    pager = html.split('id="pager"')[1]
    link = re.search(r'href="([^"]+)">下一页', pager).group(1).replace("&amp;", "&")
    for part in ["region=", "category=", "sort=sales", "dir=asc", "page=3"]:
        assert part in link
    export = re.search(r'id="exportBtn" href="([^"]+)"', html).group(1)
    assert "region=" in export and "sort=sales" in export and "page=" not in export


# ---------------- 排序 ----------------
@pytest.mark.parametrize("col, index", [("sales", 9), ("profit", 12)])
def test_sorting_numbers(client, col, index):
    num = lambda values: [float(v) for v in values]
    desc = num(first_column(client, f"/data?sort={col}&dir=desc", index))
    asc = num(first_column(client, f"/data?sort={col}&dir=asc", index))
    assert desc == sorted(desc, reverse=True) and asc == sorted(asc)
    assert desc[0] > asc[0]


def test_sorting_date_default(client):
    days = first_column(client, "/data", 0)
    assert days == sorted(days, reverse=True) and days[0] == "2024-12-30"     # 默认按订单日期从新到旧
    days = first_column(client, "/data?sort=order_date&dir=asc", 0)
    assert days == sorted(days) and days[0] == "2021-01-01"


@pytest.mark.parametrize("bad", ["quantity", "row_id; DROP TABLE sales_order", "sales DESC, (SELECT 1)", "1"])
def test_sorting_whitelist(app, client, bad):
    """排序列只认白名单里的三个，其他一律按订单日期；方向只认 asc，其他都算 desc"""
    with app.test_request_context():
        f = order_service.clean_filters({"sort": bad, "dir": "asc; DELETE FROM sales_order"})
        assert (f["sort"], f["dir"]) == ("order_date", "desc")
        assert order_service.order_by(f) == " ORDER BY order_date DESC, row_id"
    assert client.get("/data", query_string={"sort": bad, "dir": bad}).status_code == 200
    assert total_of(client, "/data") == 9959


# ---------------- 导出 ----------------
def read_export(client, url):
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "attachment" in resp.headers["Content-Disposition"]
    return pd.read_excel(BytesIO(resp.data))


def test_export_columns_and_rows(client, clean):
    df = read_export(client, "/data/export?region=华东&category=家具")
    d = clean[(clean["地区"] == "华东") & (clean["类别"] == "家具")]
    assert list(df.columns) == constants.TEMPLATE_COLUMNS and len(df.columns) == 20
    assert len(df) == len(d)
    assert set(df["国家"]) == {"中国"} and set(df["地区"]) == {"华东"} and set(df["类别"]) == {"家具"}
    assert set(df["行 ID"]) == set(d["行 ID"])
    assert re.fullmatch(r"\d{4}/\d{2}/\d{2}", df["订单日期"].iloc[0])
    assert df["数量"].sum() == d["数量"].sum()
    assert abs(df["销售额"].sum() - d["销售额"].sum()) < 1


def test_export_follows_sort_and_ignores_page(client, clean):
    df = read_export(client, "/data/export?region=西北&sort=sales&dir=desc&page=3")
    assert len(df) == (clean["地区"] == "西北").sum() > 40         # 西北全部订单，不受页码影响
    assert df["销售额"].tolist() == sorted(df["销售额"], reverse=True)


def test_export_all(client):
    df = read_export(client, "/data/export")
    assert len(df) == 9959 and df["行 ID"].is_unique


def test_export_requires_login(anon_client):
    resp = anon_client.get("/data/export")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
