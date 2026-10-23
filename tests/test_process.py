# -*- coding: utf-8 -*-
"""
数据处理和展示：处理结果和阶段一一致、不完整年份只补零到最后一个有数据的月份、各年份的处理状态。
只用测试库。每个测试结束后把测试库装回原始数据（月度表也回到 step1 的结果）
"""
import re
from io import BytesIO

import pandas as pd
import pytest

from app.services import process_service
from config import PROCESSED_DIR, TEST_DB_NAME
from conftest import log_in, require_test_db, run_sql
from step3_load_mysql import load

UNCHANGED = {2021: (1, 1), 2022: (1, 1), 2023: (1, 1), 2024: (1, 1)}


def load_original():
    load(require_test_db(TEST_DB_NAME), force=True)
    run_sql(["DELETE FROM process_log", "DELETE FROM import_batch"])


@pytest.fixture(scope="module", autouse=True)
def leave_clean():
    """这个文件的测试全部结束后，把测试库装回原始数据，不影响别的测试文件"""
    yield
    load_original()


@pytest.fixture(autouse=True)
def original_data():
    """每个测试都从“只有 9959 条原始订单、月度表是 step1 的结果”开始"""
    load_original()


@pytest.fixture
def client(app, tmp_path):
    app.config["UPLOAD_TMP"] = tmp_path
    return log_in(app.test_client())


def csrf(client):
    html = client.get("/process").get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def process(client, start, end):
    """在页面上选年份范围并点“开始处理”，返回处理后的页面"""
    resp = client.post("/process/run", data={"csrf_token": csrf(client), "start_year": start, "end_year": end},
                       follow_redirects=True)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def number(html, element_id):
    return int(re.search(rf'id="{element_id}">(\d+)<', html).group(1))


def summary(html):
    keys = ["sumRows", "sumDuplicates", "sumInvalid", "sumOutlierSales", "sumOutlierProfit", "sumMonths",
            "sumZeroSub", "sumZeroCat"]
    return tuple(number(html, k) for k in keys)


def year_state(query):
    return {y: (dv, pv) for y, dv, pv in query("SELECT `year`, data_version, processed_version FROM year_state")}


def status_of(html, year):
    row = re.search(rf'<tr data-year="{year}">.*?</tr>', html, re.S).group(0)
    return re.sub(r"<[^>]+>", " ", row).split()


def add_order(client, order_date, ship_date=None, **change):
    """用对话框录入的接口加一条订单"""
    form = {"order_date": order_date, "ship_date": ship_date or order_date, "ship_mode": "标准级",
            "order_id": "", "customer_id": "C-009", "customer_name": "测试客户", "segment": "消费者",
            "region": "华东", "province": "浙江", "city": "杭州", "category": "家具", "sub_category": "椅子",
            "product_id": "", "product_name": "测试用的椅子", "sales": "800", "quantity": "2", "discount": "0",
            "profit": "120", **change}
    data = client.post("/data/orders/new", json=form, headers={"X-CSRFToken": csrf(client)}).get_json()
    assert data["ok"], data
    return data


def monthly(query, table, keys):
    fields = ", ".join(keys + ["ym", "sales", "quantity", "profit", "order_count"])
    rows = query(f"SELECT {fields} FROM {table}")
    df = pd.DataFrame(rows, columns=keys + ["年月", "销售额", "数量", "利润", "订单数"])
    return df.astype({"销售额": float, "利润": float}).set_index(keys + ["年月"]).sort_index()


# ---------------- 和阶段一的结果一致 ----------------
def test_process_consistency(client, query):
    """处理 2021–2024 年：数量、订单数和 step1 的 CSV 完全相同；销售额、利润每格相差不超过 0.5"""
    run_sql(["DELETE FROM monthly_sales", "DELETE FROM monthly_sales_category"])   # 先清空，确认是系统自己算出来的
    html = process(client, 2021, 2024)
    assert "处理完成：2021–2024 年" in html
    # 订单 9959 行，重复 0，不合规 0，销售额异常 1037，利润异常 1839，48 个月，补零 1360 / 37
    assert summary(html) == (9959, 0, 0, 1037, 1839, 48, 1360, 37)
    assert "异常值按所选年份范围统计，只标记不删除" in html
    assert re.search(r'id="sumSeconds">\d+\.\d+<', html)

    for table, keys, csv, n_rows in [
            ("monthly_sales", ["region", "category", "sub_category"], "月度_地区_子类别.csv", 4896),
            ("monthly_sales_category", ["region", "category"], "月度_地区_类别.csv", 864)]:
        got = monthly(query, table, keys)
        want = pd.read_csv(PROCESSED_DIR / csv, encoding="utf-8-sig")
        want = want.rename(columns={"地区": "region", "类别": "category", "子类别": "sub_category"})
        want = want.set_index(keys + ["年月"]).sort_index()
        assert len(got) == len(want) == n_rows and got.index.equals(want.index)
        assert (got["数量"] == want["数量"]).all() and (got["订单数"] == want["订单数"]).all()
        assert (got["销售额"] - want["销售额"]).abs().max() <= 0.5
        assert (got["利润"] - want["利润"]).abs().max() <= 0.5
        assert got["数量"].sum() == 37534

    assert year_state(query) == UNCHANGED
    log = query("SELECT start_year, end_year, order_rows, duplicate_rows, invalid_rows, outlier_sales, "
                "outlier_profit, month_count, zero_filled_sub, zero_filled_cat, user_id FROM process_log")
    assert log == ((2021, 2024, 9959, 0, 0, 1037, 1839, 48, 1360, 37, 1),)


def test_process_twice_gives_same_result(client, query):
    process(client, 2021, 2024)
    first = monthly(query, "monthly_sales", ["region", "category", "sub_category"])
    process(client, 2021, 2024)
    assert monthly(query, "monthly_sales", ["region", "category", "sub_category"]).equals(first)
    assert query("SELECT COUNT(*) FROM process_log")[0][0] == 2


def test_outliers_depend_on_selected_years(client, query):
    """IQR 的上下界用所选年份的订单来算：只处理 2022–2024 年时，异常行数和处理四年时不同"""
    html = process(client, 2022, 2024)
    assert summary(html)[:3] == (8202, 0, 0) and summary(html)[5] == 36
    assert summary(html)[3:5] == (862, 1494)
    # 只重新生成了 2022–2024 的月份，2021 年的汇总还在，总行数不变
    assert query("SELECT COUNT(*), MIN(ym), MAX(ym) FROM monthly_sales_category") == ((864, "2021-01", "2024-12"),)


def test_process_one_year_keeps_other_years(client, query):
    before = monthly(query, "monthly_sales_category", ["region", "category"])
    process(client, 2023, 2023)
    after = monthly(query, "monthly_sales_category", ["region", "category"])
    other = [i for i in before.index if not i[2].startswith("2023")]
    assert len(after) == 864 and after.loc[other].equals(before.loc[other])      # 其他年份一个数都没变


# ---------------- 重复录入、不合规 ----------------
def test_duplicate_entry_counted_once(client, query):
    """把库里的第 1 行原样再录入一遍（只有行 ID 不同）：处理时算 1 行重复，汇总结果不变"""
    run_sql(["INSERT INTO sales_order (row_id, order_id, order_key, order_date, ship_date, ship_mode, customer_id, "
             "customer_name, segment, city, province, region, product_id, category, sub_category, product_name, "
             "sales, quantity, discount, profit, unit_price, profit_rate, ym, source, batch_id) "
             "SELECT 20001, order_id, order_key, order_date, ship_date, ship_mode, customer_id, customer_name, "
             "segment, city, province, region, product_id, category, sub_category, product_name, sales, quantity, "
             "discount, profit, unit_price, profit_rate, ym, '手工录入', 1 FROM sales_order WHERE row_id = 1"])
    html = process(client, 2021, 2024)
    assert summary(html)[:3] == (9960, 1, 0)
    assert query("SELECT SUM(quantity) FROM monthly_sales_category")[0][0] == 37534


def test_invalid_rows_excluded(client, query):
    """绕过导入校验，直接往库里放 2 条不合规的订单（数量 0、发货早于下单）：处理时排除，汇总结果不变"""
    template = ("INSERT INTO sales_order (row_id, order_id, order_key, order_date, ship_date, customer_id, region, "
                "product_id, category, sub_category, product_name, sales, quantity, discount, profit, ym, source) "
                "VALUES ({}, 'BAD', 'BAD|x|C', '2024-05-10', '{}', 'C', '华东', 'P', '家具', '椅子', '不合规的行{}', "
                "100, {}, 0, 10, '2024-05', '手工录入')")
    run_sql([template.format(20001, "2024-05-11", 1, 0), template.format(20002, "2024-05-01", 2, 5)])
    html = process(client, 2024, 2024)
    assert summary(html)[:3] == (3379, 0, 2)                     # 2024 年原有 3377 行
    assert query("SELECT SUM(quantity) FROM monthly_sales_category")[0][0] == 37534


# ---------------- 不完整年份 ----------------
def test_partial_year(client, query):
    """录入 2025 年 1–3 月各一条订单并处理 2025 年：补零只到 3 月，2025 不是完整年份"""
    for day in ("2025-01-15", "2025-02-15", "2025-03-15"):
        add_order(client, day)
    html = client.get("/process").get_data(as_text=True)
    assert status_of(html, 2025) == ["2025", "3", "3", "个月", "不完整", "尚未处理"]

    html = process(client, 2025, 2025)
    # 3 行订单，3 个月；102 个组合 × 3 个月 = 306 格，只有 3 格有销售；类别级 18 × 3 = 54 格
    assert summary(html) == (3, 0, 0, 0, 0, 3, 303, 51)
    assert status_of(html, 2025) == ["2025", "3", "3", "个月", "不完整", "已处理"]
    for table, cells in (("monthly_sales", 306), ("monthly_sales_category", 54)):
        assert query(f"SELECT COUNT(*), MIN(ym), MAX(ym), SUM(quantity) FROM {table} WHERE ym > '2024-12'") == \
            ((cells, "2025-01", "2025-03", 6),)                  # 4 月以后没有记录：还没发生的月份不补 0
    assert query("SELECT quantity, order_count FROM monthly_sales WHERE region = '华东' AND sub_category = '椅子' "
                 "AND ym = '2025-02'") == ((2, 1),)
    assert year_state(query) == {**UNCHANGED, 2025: (3, 3)}      # 2021–2024 仍然是已处理
    assert query("SELECT COUNT(*) FROM monthly_sales WHERE ym <= '2024-12'")[0][0] == 4896


def test_range_ending_in_partial_year(client, query):
    """一起处理 2024–2025 年：月份从 2024-01 到 2025-03，共 15 个月"""
    add_order(client, "2025-03-15")
    html = process(client, 2024, 2025)
    assert summary(html)[0] == 3378 and summary(html)[5] == 15
    assert query("SELECT MAX(ym), COUNT(DISTINCT ym) FROM monthly_sales_category WHERE ym >= '2024-01'") == \
        (("2025-03", 15),)


def test_gap_year_rejected(client, query):
    """范围中间有一整年没有订单：不处理，提示原因"""
    add_order(client, "2026-01-15")
    html = process(client, 2024, 2026)
    assert "2025 年没有任何订单" in html
    assert query("SELECT COUNT(*) FROM process_log")[0][0] == 0
    assert year_state(query)[2026] == (1, None)
    assert "处理完成：2026–2026 年" in process(client, 2026, 2026)   # 单独处理有数据的年份可以


@pytest.mark.parametrize("start, end, message", [
    (2024, 2021, "起始年不能晚于结束年"), (1999, 2024, "请从下拉框里选择"), ("abc", "", "请从下拉框里选择")])
def test_bad_range(client, query, start, end, message):
    assert message in process(client, start, end)
    assert query("SELECT COUNT(*) FROM process_log")[0][0] == 0


# ---------------- 处理状态 ----------------
def test_state(client, query):
    """录入 1 条 2023 年的订单：只有 2023 年变成需重新处理；处理 2023 年后又变回已处理"""
    html = client.get("/process").get_data(as_text=True)
    assert [status_of(html, y)[-1] for y in (2021, 2022, 2023, 2024)] == ["已处理"] * 4
    assert 'id="staleNote"' not in html
    before = query("SELECT quantity, order_count FROM monthly_sales_category WHERE region = '华东' "
                   "AND category = '家具' AND ym = '2023-06'")[0]

    add_order(client, "2023-06-01")
    html = client.get("/process").get_data(as_text=True)
    assert status_of(html, 2023) == ["2023", "2655", "12", "个月", "完整", "数据有更新，需重新处理"]
    assert [status_of(html, y)[-1] for y in (2021, 2022, 2024)] == ["已处理"] * 3
    assert 'id="staleNote"' in html and "2023 年的数据有更新" in html
    assert year_state(query) == {**UNCHANGED, 2023: (2, 1)}
    # 还没处理：月度表里还是旧的数
    assert query("SELECT quantity, order_count FROM monthly_sales_category WHERE region = '华东' "
                 "AND category = '家具' AND ym = '2023-06'")[0] == before

    html = process(client, 2023, 2023)
    assert status_of(html, 2023)[-1] == "已处理" and 'id="staleNote"' not in html
    assert year_state(query) == {**UNCHANGED, 2023: (2, 2)}
    assert query("SELECT quantity, order_count FROM monthly_sales_category WHERE region = '华东' "
                 "AND category = '家具' AND ym = '2023-06'")[0] == (before[0] + 2, before[1] + 1)
    assert 'text-bg-success">已处理<' in client.get("/").get_data(as_text=True)


def test_state_after_revoke(client, query):
    """撤销那条订单后 2023 年又要重新处理；再处理一次，月度表回到原来的数"""
    before = monthly(query, "monthly_sales_category", ["region", "category"])
    batch_id = add_order(client, "2023-06-01")["batch_id"]
    process(client, 2023, 2023)
    client.post(f"/data/batches/{batch_id}/revoke", data={"csrf_token": csrf(client)})
    assert year_state(query)[2023] == (3, 2)
    process(client, 2023, 2023)
    assert year_state(query)[2023] == (3, 3)
    after = monthly(query, "monthly_sales_category", ["region", "category"])
    assert (after["数量"] == before["数量"]).all() and (after["订单数"] == before["订单数"]).all()


def test_all_or_nothing(client, query, monkeypatch):
    """写到最后一步出错：月度表、处理记录、年份状态都不变"""
    from app.services import state_service
    add_order(client, "2023-06-01")
    before = monthly(query, "monthly_sales_category", ["region", "category"])

    def boom(conn, years):
        raise RuntimeError("模拟出错")
    monkeypatch.setattr(state_service, "mark_processed", boom)
    with pytest.raises(RuntimeError):
        process(client, 2021, 2024)
    assert monthly(query, "monthly_sales_category", ["region", "category"]).equals(before)
    assert query("SELECT COUNT(*) FROM process_log")[0][0] == 0
    assert year_state(query)[2023] == (2, 1)


# ---------------- 页面：摘要、结果表、图、导出 ----------------
def test_page_before_processing(client):
    html = client.get("/process").get_data(as_text=True)
    assert re.findall(r"<option[^>]*>(\d{4})</option>", html.split('id="start_year"')[1].split("</select>")[0]) \
        == ["2021", "2022", "2023", "2024"]
    assert status_of(html, 2021) == ["2021", "1757", "12", "个月", "完整", "已处理"]
    assert 'id="summary"' not in html                            # 这个测试里处理记录被清空了，没有摘要
    assert number(html, "totalCount") == 864 and "第 1 / 44 页" in html
    assert number(html, "cellCount") == 864 and number(html, "zeroCount") == 37
    assert '"2021-01"' in html and "dataZoom" in html


def test_result_filters(client, query):
    html = client.get("/process?region=华东&category=办公用品").get_data(as_text=True)
    assert number(html, "totalCount") == 48 and number(html, "cellCount") == 48
    want = [r[0] for r in query("SELECT quantity FROM monthly_sales_category WHERE region = '华东' "
                                "AND category = '办公用品' ORDER BY ym")]
    chart = re.search(r'"quantity": \[([^\]]+)\]', html).group(1)
    assert [int(x) for x in chart.split(",")] == want and len(want) == 48

    html = client.get("/process?level=子类别&region=西北").get_data(as_text=True)
    assert number(html, "totalCount") == 17 * 48 and "<th>子类别</th>" in html
    zeros = query("SELECT COUNT(*) FROM monthly_sales WHERE region = '西北' AND order_count = 0")[0][0]
    assert number(html, "zeroCount") == zeros > 0

    html = client.get("/process?level=子类别&category=家具&sub_category=桌子&page=2").get_data(as_text=True)
    assert number(html, "totalCount") == 6 * 48 and "第 2 / 15 页" in html and 'data-selected="桌子"' in html
    pager = html.split('id="pager"')[1]
    next_link = re.search(r'href="([^"]+)">下一页', pager).group(1)
    assert "page=3" in next_link and "sub_category=" in next_link and next_link.endswith("#result")

    # 取值不对的条件当作没选；类别层级下子类别不起作用
    assert number(client.get("/process?region=火星&category=零食").get_data(as_text=True), "totalCount") == 864
    assert number(client.get("/process?sub_category=桌子").get_data(as_text=True), "totalCount") == 864
    html = client.get("/process?level=子类别&category=家具&sub_category=纸张").get_data(as_text=True)
    assert number(html, "totalCount") == 4 * 6 * 48               # 纸张不属于家具，子类别条件被丢掉


def test_export(client, query):
    resp = client.get("/process/export?level=子类别&region=华东&category=技术")
    assert resp.status_code == 200 and "attachment" in resp.headers["Content-Disposition"]
    df = pd.read_excel(BytesIO(resp.data))
    assert list(df.columns) == ["地区", "类别", "子类别", "年月", "销售额", "数量", "利润", "订单数"]
    assert len(df) == 4 * 48 and set(df["地区"]) == {"华东"} and set(df["类别"]) == {"技术"}
    assert df["数量"].sum() == query("SELECT SUM(quantity) FROM monthly_sales WHERE region = '华东' "
                                    "AND category = '技术'")[0][0]
    df = pd.read_excel(BytesIO(client.get("/process/export").data))
    assert list(df.columns) == ["地区", "类别", "年月", "销售额", "数量", "利润", "订单数"] and len(df) == 864


def test_summary_of_earlier_run(client, query):
    """摘要默认显示最近一次处理，也可以指定看某一次；用时存在处理记录里，以后再看也有"""
    process(client, 2021, 2024)
    process(client, 2024, 2024)
    first, second = [r[0] for r in query("SELECT id FROM process_log ORDER BY id")]
    html = client.get("/process").get_data(as_text=True)
    assert "2024–2024 年" in html.split('id="summaryHead"')[1][:60] and summary(html)[0] == 3377
    assert re.search(r'id="sumSeconds">\d+\.\d+<', html)
    html = client.get(f"/process?log={first}").get_data(as_text=True)
    assert "2021–2024 年" in html.split('id="summaryHead"')[1][:60] and summary(html)[0] == 9959
    shown = float(re.search(r'id="sumSeconds">(\d+\.\d\d)<', html).group(1))
    saved = query(f"SELECT seconds FROM process_log WHERE id = {first}")[0][0]
    assert shown == float(saved) > 0
    # 升级数据库时补记的那种记录没有用时，显示“—”
    run_sql(["INSERT INTO process_log (start_year, end_year, order_rows) VALUES (2021, 2024, 9959)"])
    html = client.get("/process").get_data(as_text=True)
    assert 'id="sumSeconds">—<' in html and "升级数据库时补记" in html


def test_login_and_csrf(client, anon_client, query):
    for url in ("/process", "/process/export"):
        resp = anon_client.get(url)
        assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert client.post("/process/run", data={"start_year": 2021, "end_year": 2024}).status_code == 400
    assert query("SELECT COUNT(*) FROM process_log")[0][0] == 0


def test_core_functions_keep_step1_behaviour():
    """preprocess_core 里新加的参数不传时，结果和以前一样；传全部组合时，没出现过的组合也有一行"""
    from preprocess_core import monthly_region_cat, month_list
    clean = pd.read_csv(PROCESSED_DIR / "orders_clean.csv", encoding="utf-8-sig")
    months = month_list("2021-01", "2024-12")
    old = pd.read_csv(PROCESSED_DIR / "月度_地区_类别.csv", encoding="utf-8-sig")
    assert monthly_region_cat(clean, months)[["地区", "类别", "年月", "数量"]].equals(old[["地区", "类别", "年月", "数量"]])
    few = clean[clean["地区"] == "华东"]
    assert len(monthly_region_cat(few, months)) == 3 * 48
    assert len(monthly_region_cat(few, months, process_service.COMBOS)) == 18 * 48
