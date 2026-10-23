# -*- coding: utf-8 -*-
"""
表格导入（模板、上传、预览、确认、结果）、对话框录入、导入记录（撤销、恢复）。
只用测试库。每个测试结束后把测试库清回 9959 条原始订单
"""
import os
import re
import time
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from app.constants import ORDER_KEY_LIMIT, TEMPLATE_COLUMNS, TEXT_LIMITS
from app.services import import_service, state_service
from app.services.validation_service import check_table
from config import RAW_FILE, TEST_DB_NAME
from conftest import log_in, require_test_db, reset_imported_data, run_sql
from step3_load_mysql import load

ROOT = Path(__file__).resolve().parent.parent
DEMO_FILE = ROOT / "docs" / "演示数据" / "导入演示_2对1错.xlsx"


# ---------------- 准备和清理 ----------------
@pytest.fixture(scope="module", autouse=True)
def original_data():
    """这个文件里的测试开始前，把测试库重新装成原始数据"""
    load(require_test_db(TEST_DB_NAME), force=True)


@pytest.fixture(autouse=True)
def back_to_original():
    """每个测试结束后：删掉导入的订单和批次，年份状态、月度表回到原样"""
    yield
    reset_imported_data()


@pytest.fixture
def client(app, tmp_path):
    """已登录的浏览器；上传的临时文件放到这个测试专用的文件夹里"""
    app.config["UPLOAD_TMP"] = tmp_path
    return log_in(app.test_client())


def one(query, sql):
    return query(sql)[0][0]


def year_state(query):
    return {y: (dv, pv) for y, dv, pv in query("SELECT `year`, data_version, processed_version FROM year_state")}


UNCHANGED = {2021: (1, 1), 2022: (1, 1), 2023: (1, 1), 2024: (1, 1)}


# ---------------- 造文件、上传、确认 ----------------
def good_row(**change):
    """一行合格的订单（2025 年 1 月），change 里写要改掉的字段"""
    row = {"行 ID": "", "订单 ID": "T-0001", "订单日期": "2025/01/05", "发货日期": "2025/01/07",
           "邮寄方式": "标准级", "客户 ID": "C-001", "客户名称": "测试客户", "细分": "公司", "城市": "上海",
           "省/自治区": "上海", "国家": "中国", "地区": "华东", "产品 ID": "P-001", "类别": "办公用品",
           "子类别": "纸张", "产品名称": "测试用纸", "销售额": 120.5, "数量": 3, "折扣": 0.1, "利润": 25.3}
    return {**row, **change}


def xlsx(rows, columns=TEMPLATE_COLUMNS):
    buf = BytesIO()
    pd.DataFrame(rows, columns=columns).to_excel(buf, index=False)
    buf.seek(0)
    return buf


def csrf(client):
    html = client.get("/data/import").get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def upload(client, content, name="订单.xlsx"):
    """上传文件，跟着跳转走到预览页（或带错误提示的上传页），返回页面内容"""
    resp = client.post("/data/import", data={"csrf_token": csrf(client), "file": (content, name)},
                       content_type="multipart/form-data", follow_redirects=True)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def confirm(client, preview_html):
    """在预览页点“确认导入”，返回结果页内容"""
    token = re.search(r'name="token" value="([^"]+)"', preview_html).group(1)
    resp = client.post("/data/import/confirm", data={"csrf_token": csrf(client), "token": token},
                       follow_redirects=True)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def number(html, element_id):
    return int(re.search(rf'id="{element_id}">(\d+)<', html).group(1))


def counts(html, prefix):
    """预览页（pv）或结果页（rs）上的四个数：总行数、成功、失败、跳过"""
    return tuple(number(html, prefix + k) for k in ("Total", "Ok", "Failed", "Skipped"))


def check(rows):
    """直接调用校验函数（不经过网页、不连数据库）"""
    df = pd.DataFrame(rows, columns=TEMPLATE_COLUMNS).astype(str)
    return check_table(df)


def reason_of(**change):
    result = check([good_row(**change)])
    assert len(result["ok"]) == 0 and len(result["failed"]) == 1
    assert result["failed"][0]["row"] == 2
    return result["failed"][0]["reason"]


# ---------------- 导入模板 ----------------
def test_template(client):
    resp = client.get("/data/import/template")
    assert resp.status_code == 200 and "attachment" in resp.headers["Content-Disposition"]
    sheets = pd.read_excel(BytesIO(resp.data), sheet_name=None)
    assert list(sheets) == ["订单", "填写说明"]
    orders = sheets["订单"]
    assert list(orders.columns) == TEMPLATE_COLUMNS and len(orders) == 1
    assert pd.isna(orders["行 ID"].iloc[0])                       # 示例行的行 ID 留空
    notes = sheets["填写说明"].astype(str).agg("".join, axis=1).str.cat()
    assert "建议留空，由系统自动生成" in notes
    assert counts(upload(client, BytesIO(resp.data)), "pv") == (1, 1, 0, 0)     # 模板本身能通过校验


def test_import_page(client):
    html = client.get("/data/import").get_data(as_text=True)
    assert 'id="dropZone"' in html and 'accept=".xlsx,.csv"' in html and "下载导入模板" in html
    assert re.search(r'class="nav-link active"\s+href="/data">数据导入和展示', html)   # 导航栏仍然高亮本模块


# ---------------- 逐行校验：每条规则 ----------------
@pytest.mark.parametrize("column", [c for c in TEMPLATE_COLUMNS if c != "行 ID"])
def test_required_fields(column):
    assert f"{column}不能为空" in reason_of(**{column: ""})


@pytest.mark.parametrize("change, expected", [
    ({"国家": "美国"}, "国家只能填“中国”"),
    ({"行 ID": "abc"}, "行 ID 应为正整数或留空"),
    ({"行 ID": "0"}, "行 ID 应为正整数或留空"),
    ({"行 ID": "-5"}, "行 ID 应为正整数或留空"),
    ({"行 ID": "1.5"}, "行 ID 应为正整数或留空"),
    ({"订单日期": "2025年1月5日"}, "订单日期“2025年1月5日”认不出来"),
    ({"订单日期": "2025/13/40"}, "订单日期“2025/13/40”认不出来"),
    ({"发货日期": "明天"}, "发货日期“明天”认不出来"),
    ({"订单日期": "1999/01/05"}, "订单日期的年份应在 2000–2100 之间"),
    ({"订单日期": "2025/01/05", "发货日期": "2025/01/04"}, "不能早于订单日期"),
    ({"数量": "0"}, "数量应为正整数"),
    ({"数量": "-2"}, "数量应为正整数"),
    ({"数量": "2.5"}, "数量应为正整数"),
    ({"数量": "两个"}, "数量应为数字"),
    ({"销售额": "0"}, "销售额应大于 0"),
    ({"销售额": "-10"}, "销售额应大于 0"),
    ({"销售额": "很多"}, "销售额应为数字"),
    ({"折扣": "1.5"}, "折扣应大于等于 0、小于 1"),
    ({"折扣": "1"}, "折扣应大于等于 0、小于 1"),
    ({"折扣": "-0.1"}, "折扣应大于等于 0、小于 1"),
    ({"折扣": "八折"}, "折扣应为数字"),
    ({"利润": "亏了"}, "利润应为数字"),
    ({"利润": "nan"}, "利润应为数字"),
    ({"地区": "火星"}, "地区“火星”不在已有的取值里"),
    ({"类别": "零食"}, "类别“零食”不在已有的取值里"),
    ({"子类别": "薯片"}, "子类别“薯片”不在已有的取值里"),
    ({"类别": "家具", "子类别": "纸张"}, "子类别“纸张”不属于类别“家具”"),
    ({"邮寄方式": "空运"}, "邮寄方式“空运”不在已有的取值里"),
    ({"细分": "学校"}, "细分“学校”不在已有的取值里"),
    ({"省/自治区": "上海市"}, "省/自治区“上海市”不在已有的取值里"),
])
def test_invalid_values(change, expected):
    assert expected in reason_of(**change)


@pytest.mark.parametrize("column, limit", TEXT_LIMITS.items())
def test_text_too_long(column, limit):
    other = {"客户 ID": "C"} if column == "订单 ID" else {"订单 ID": "T"} if column == "客户 ID" else {}
    assert f"{column}超过 {limit} 个字符（现在 {limit + 1} 个）" in reason_of(**{column: "字" * (limit + 1)}, **other)
    assert len(check([good_row(**{column: "字" * limit}, **other)])["ok"]) == 1      # 刚好等于上限可以


def test_order_key_too_long():
    """订单 ID、客户 ID 各 30 个字符都没超长，但拼成的订单键有 70 个字符，超过 60"""
    result = check([good_row(**{"订单 ID": "A" * 30, "客户 ID": "B" * 30})])
    assert len(result["ok"]) == 0
    assert "订单键有 70 个字符，最多 60 个" in result["failed"][0]["reason"]
    ok = check([good_row(**{"订单 ID": "A" * 25, "客户 ID": "B" * 25})])["ok"]     # 25 + 1 + 8 + 1 + 25 = 60
    assert len(ok) == 1 and len(ok["订单键"].iloc[0]) == ORDER_KEY_LIMIT == 60


def test_several_problems_in_one_row():
    reason = reason_of(**{"折扣": "1.5", "数量": "0", "地区": ""})
    assert "折扣应大于等于 0、小于 1" in reason and "数量应为正整数" in reason and "地区不能为空" in reason


@pytest.mark.parametrize("change", [
    {"折扣": "0"}, {"折扣": "0.99"}, {"利润": "-60.704"}, {"数量": "3.0"}, {"行 ID": "20000"},
    {"订单日期": "2025-01-05", "发货日期": "2025-01-05"}, {"订单日期": "2025/1/5"},
    {"产品名称": "  前后有空格  "},
])
def test_valid_values(change):
    result = check([good_row(**change)])
    assert len(result["ok"]) == 1 and result["failed"] == []


def test_spaces_are_stripped():
    ok = check([good_row(**{"产品名称": "  测试用纸  ", "地区": " 华东 "})])["ok"]
    assert ok["产品名称"].iloc[0] == "测试用纸" and ok["地区"].iloc[0] == "华东"


def test_empty_rows_are_ignored():
    empty = {c: "" for c in TEMPLATE_COLUMNS}
    result = check([good_row(), empty, good_row(折扣="2"), empty])
    assert result["total"] == 2 and len(result["ok"]) == 1
    assert result["failed"][0]["row"] == 4                       # 行号仍按文件里的位置算


# ---------------- 文件本身的问题 ----------------
def test_header_mismatch(client, query):
    missing = [c for c in TEMPLATE_COLUMNS if c not in ("折扣", "利润")]
    html = upload(client, xlsx([good_row()], missing))
    assert "表头与导入模板不一致" in html and "缺少列：折扣、利润" in html
    html = upload(client, xlsx([{**good_row(), "备注": "x"}], TEMPLATE_COLUMNS + ["备注"]))
    assert "多出列：备注" in html
    assert one(query, "SELECT COUNT(*) FROM sales_order") == 9959
    assert list(client.application.config["UPLOAD_TMP"].iterdir()) == []      # 被拒绝的文件不留在临时文件夹


def test_bad_files(client):
    assert "只能上传 .xlsx 或 .csv 文件" in upload(client, BytesIO(b"abc"), "订单.txt")
    assert "只能上传 .xlsx 或 .csv 文件" in upload(client, BytesIO(b"abc"), "订单.xls")
    assert "文件读不出来" in upload(client, BytesIO(b"this is not excel"), "订单.xlsx")
    resp = client.post("/data/import", data={"csrf_token": csrf(client)}, follow_redirects=True)
    assert "请先选择要上传的文件" in resp.get_data(as_text=True)


def test_file_too_large(client):
    big = BytesIO(b"0" * (10 * 1024 * 1024 + 1))
    resp = client.post("/data/import", data={"csrf_token": csrf(client), "file": (big, "大文件.csv")},
                       content_type="multipart/form-data")
    assert resp.status_code == 413 and "不能超过 10 MB" in resp.get_data(as_text=True)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gbk"])
def test_csv_encodings(client, query, encoding):
    text = pd.DataFrame([good_row(), good_row(折扣=1.5)], columns=TEMPLATE_COLUMNS).to_csv(index=False)
    html = upload(client, BytesIO(text.encode(encoding)), "订单.csv")
    assert counts(html, "pv") == (2, 1, 1, 0) and "测试客户" in html
    assert counts(confirm(client, html), "rs") == (2, 1, 1, 0)
    assert query("SELECT customer_name, city FROM sales_order WHERE row_id = 10001") == (("测试客户", "上海"),)


def test_excel_date_cells(client, query):
    """Excel 里设成日期格式的单元格也能读"""
    row = good_row(**{"订单日期": datetime(2025, 3, 8), "发货日期": datetime(2025, 3, 9)})
    assert counts(confirm(client, upload(client, xlsx([row]))), "rs") == (1, 1, 0, 0)
    assert query("SELECT order_date, ship_date, ym FROM sales_order WHERE row_id = 10001") == \
        ((date(2025, 3, 8), date(2025, 3, 9), "2025-03"),)


# ---------------- 预览：不写数据库 ----------------
def test_preview_writes_nothing(client, query):
    rows = [good_row(**{"订单 ID": f"T-{i:04d}"}) for i in range(25)] + [good_row(折扣=1.5)]
    html = upload(client, xlsx(rows), "我的订单.xlsx")
    assert counts(html, "pv") == (26, 25, 1, 0)
    assert "还没有写入数据库" in html and "我的订单.xlsx" in html and "确认导入 25 行" in html
    assert "第 27 行" in html and "折扣应大于等于 0、小于 1（比如八折填 0.2），填的是 1.5" in html
    table = html.split('id="previewTable"')[1].split("</table>")[0]
    assert table.count("<tr>") == 21 and "T-0019" in table and "T-0020" not in table   # 表头 + 前 20 行
    assert ">10001<" in table and ">10020<" in table              # 预览里能看到准备使用的行 ID

    assert one(query, "SELECT COUNT(*) FROM sales_order") == 9959
    assert one(query, "SELECT COUNT(*) FROM import_batch") == 0
    assert year_state(query) == UNCHANGED
    files = list(client.application.config["UPLOAD_TMP"].iterdir())
    assert len(files) == 1 and re.fullmatch(r"[0-9a-f]{32}\.xlsx", files[0].name)   # 随机名字，不用原文件名


def test_cancel_and_new_upload_remove_old_file(client, query):
    folder = client.application.config["UPLOAD_TMP"]
    upload(client, xlsx([good_row()]))
    upload(client, xlsx([good_row(), good_row()]))                 # 再传一份，前一份作废
    assert len(list(folder.iterdir())) == 1
    resp = client.post("/data/import/cancel", data={"csrf_token": csrf(client)}, follow_redirects=True)
    assert "已取消，数据没有变化" in resp.get_data(as_text=True)
    assert list(folder.iterdir()) == []
    assert "没有待确认的文件" in client.get("/data/import/preview", follow_redirects=True).get_data(as_text=True)
    assert one(query, "SELECT COUNT(*) FROM sales_order") == 9959


def test_nothing_to_import(client, query):
    html = upload(client, xlsx([good_row(折扣=1.5)]))
    assert counts(html, "pv") == (1, 0, 1, 0) and "没有可以导入的行" in html
    assert re.search(r'id="confirmBtn" disabled', html)
    assert "没有可以导入的行，数据没有变化" in confirm(client, html)   # 绕过按钮直接提交也不会写入
    assert one(query, "SELECT COUNT(*) FROM import_batch") == 0


# ---------------- 确认导入 ----------------
def test_confirm_imports(client, query):
    rows = [good_row(), good_row(**{"订单日期": "2023/06/01", "发货日期": "2023/06/03"}), good_row(折扣=1.5)]
    preview = upload(client, xlsx(rows), "三行.xlsx")
    html = confirm(client, preview)
    assert counts(html, "rs") == (3, 2, 1, 0)
    assert "导入完成" in html and "三行.xlsx" in html and "第 4 行" in html and 'id="importSeconds"' in html
    assert re.search(r'id="importTime">\d{4}-\d\d-\d\d \d\d:\d\d:\d\d<', html)

    assert one(query, "SELECT COUNT(*) FROM sales_order") == 9961
    batch = query("SELECT id, method, file_name, total_rows, success_rows, failed_rows, skipped_rows, "
                  "user_id, revoked FROM import_batch")
    assert len(batch) == 1 and batch[0][1:] == ("表格导入", "三行.xlsx", 3, 2, 1, 0, 1, 0)
    assert query("SELECT row_id, source, batch_id, ym FROM sales_order WHERE source <> '原始数据' "
                 "ORDER BY row_id") == ((10001, "表格导入", batch[0][0], "2025-01"),
                                        (10002, "表格导入", batch[0][0], "2023-06"))
    # 只有订单涉及的年份版本号变了：2025 是新出现的年份，2023 加 1
    assert year_state(query) == {**UNCHANGED, 2023: (2, 1), 2025: (1, None)}
    assert [p.name for p in client.application.config["UPLOAD_TMP"].iterdir()] == [f"batch_{batch[0][0]}.json"]

    # 同一份文件不能确认两次
    assert "已经处理过或已失效" in confirm(client, preview)
    assert one(query, "SELECT COUNT(*) FROM sales_order") == 9961


def test_confirm_needs_matching_token(client, query):
    html = upload(client, xlsx([good_row()]))
    wrong = html.replace(re.search(r'name="token" value="([^"]+)"', html).group(1), "0" * 32 + ".xlsx")
    assert "已经处理过或已失效" in confirm(client, wrong)
    other = log_in(client.application.test_client(), user_id=2, username="other")   # 另一个人拿不到这份文件
    assert "已经处理过或已失效" in confirm(other, html)
    assert one(query, "SELECT COUNT(*) FROM sales_order") == 9959


def test_all_or_nothing(client, query, monkeypatch):
    """写到一半出错，整批都不写（事务回滚）"""
    def boom(conn, years):
        raise RuntimeError("模拟出错")
    monkeypatch.setattr(state_service, "bump_years", boom)
    html = upload(client, xlsx([good_row(), good_row()]))
    with pytest.raises(RuntimeError):
        confirm(client, html)
    assert one(query, "SELECT COUNT(*) FROM sales_order") == 9959
    assert one(query, "SELECT COUNT(*) FROM import_batch") == 0


def test_result_page_in_orders(client):
    confirm(client, upload(client, xlsx([good_row(产品名称="导入后能查到的商品")])))
    html = client.get("/data?source=表格导入").get_data(as_text=True)
    assert 'id="totalCount">1<' in html and "导入后能查到的商品" in html and ">10001</td>" in html
    assert 'id="statRows">9960<' in client.get("/").get_data(as_text=True)


# ---------------- 行 ID：重复的跳过，留空的自动编号 ----------------
def test_duplicate_row_ids_skipped(client, query):
    rows = [good_row(**{"行 ID": 1}),            # 系统里已有
            good_row(**{"行 ID": 30000}),
            good_row(**{"行 ID": 30000}),        # 和上一行重复
            good_row()]
    html = upload(client, xlsx(rows))
    assert counts(html, "pv") == (4, 2, 0, 2)
    assert "行 ID 1 系统里已存在，已跳过" in html and "行 ID 30000 在本文件里重复出现，已跳过" in html
    assert counts(confirm(client, html), "rs") == (4, 2, 0, 2)
    # 留空的那一行从“文件里填的最大行 ID”后面接着编，不会和 30000 撞号
    assert query("SELECT row_id FROM sales_order WHERE source = '表格导入' ORDER BY row_id") == ((30000,), (30001,))
    assert query("SELECT order_id FROM sales_order WHERE row_id = 1")[0][0] != "T-0001"   # 原来的第 1 行没被覆盖


def test_auto_row_ids_in_file_order(client, query):
    rows = [good_row(**{"订单 ID": f"T-{i}"}) for i in range(5)]
    confirm(client, upload(client, xlsx(rows)))
    assert query("SELECT row_id, order_id FROM sales_order WHERE source = '表格导入' ORDER BY row_id") == \
        tuple((10001 + i, f"T-{i}") for i in range(5))            # 数据库里最大的行 ID 是 10000
    confirm(client, upload(client, xlsx([good_row()])))
    assert one(query, "SELECT MAX(row_id) FROM sales_order") == 10006


def test_export_then_reimport(client, query):
    """订单查询导出的文件原样再上传：表头通过，全部因行 ID 已存在被跳过"""
    exported = client.get("/data/export?region=西北").data
    html = upload(client, BytesIO(exported), "订单导出.xlsx")
    assert counts(html, "pv") == (478, 0, 0, 478)
    assert "这里只列出前 50 行，全部 478 行" in html
    assert one(query, "SELECT COUNT(*) FROM sales_order") == 9959


# ---------------- 派生字段和 step1 的规则一致 ----------------
def test_derived_fields_equal_step1(client, query):
    """把原始数据的前 300 行去掉行 ID 再导入，派生字段应该和 step1 算出来存进库里的完全一样"""
    raw = pd.read_excel(RAW_FILE, dtype={"订单日期": str, "发货日期": str}).head(300)
    ids = raw["行 ID"].tolist()
    html = confirm(client, upload(client, xlsx(raw.assign(**{"行 ID": ""}).to_dict("records"))))
    assert counts(html, "rs") == (300, 300, 0, 0)

    fields = ("order_key, ym, unit_price, profit_rate, order_date, ship_date, sales, quantity, discount, "
              "profit, order_id, customer_id, product_id, product_name, region, category, sub_category")
    imported = query(f"SELECT {fields} FROM sales_order WHERE source = '表格导入' ORDER BY row_id")
    original = {r[0]: r[1:] for r in query(
        f"SELECT row_id, {fields} FROM sales_order WHERE row_id IN ({', '.join(map(str, ids))})")}
    assert len(imported) == 300
    assert [original[i] for i in ids] == list(imported)


# ---------------- 失败明细文件 ----------------
def test_problem_file(client):
    rows = [good_row(), good_row(折扣=1.5, 产品名称="折扣填错的商品"), good_row(**{"行 ID": 1})]
    preview = upload(client, xlsx(rows))
    for url in ["/data/import/preview/problems", None]:
        if url is None:                                           # 第二轮：确认导入后从结果页下载
            result = confirm(client, preview)
            url = re.search(r'id="problemsBtn" href="([^"]+)"', result).group(1)
        resp = client.get(url)
        assert resp.status_code == 200 and "attachment" in resp.headers["Content-Disposition"]
        sheets = pd.read_excel(BytesIO(resp.data), sheet_name=None)
        failed, skipped = sheets["失败的行"], sheets["跳过的行"]
        assert list(failed.columns) == ["Excel 行号", "失败原因"] + TEMPLATE_COLUMNS
        assert failed["Excel 行号"].tolist() == [3] and "折扣" in failed["失败原因"].iloc[0]
        assert failed["产品名称"].iloc[0] == "折扣填错的商品" and failed["折扣"].iloc[0] == 1.5
        assert skipped["Excel 行号"].tolist() == [4] and "已存在" in skipped["跳过原因"].iloc[0]


def test_result_page_after_detail_cleaned(client):
    html = confirm(client, upload(client, xlsx([good_row(), good_row(折扣=1.5)])))
    url = re.search(r'id="problemsBtn" href="([^"]+)"', html).group(1)
    for p in client.application.config["UPLOAD_TMP"].iterdir():
        p.unlink()
    page = client.get(url.rsplit("/", 1)[0]).get_data(as_text=True)
    assert counts(page, "rs") == (2, 1, 1, 0) and "现在已经清理掉了" in page
    assert client.get(url).status_code == 404
    assert client.get("/data/import/result/999999").status_code == 404


def test_old_temp_files_removed(tmp_path):
    old, new = tmp_path / "old.xlsx", tmp_path / "new.xlsx"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    two_days_ago = time.time() - 2 * 24 * 3600
    os.utime(old, (two_days_ago, two_days_ago))
    assert import_service.clean_temp_files(tmp_path) == 1
    assert not old.exists() and new.exists()


def test_cleanup_runs_on_startup(db_name, tmp_path, monkeypatch):
    import app as app_package
    old = tmp_path / "tmp_uploads" / "old.csv"
    old.parent.mkdir()
    old.write_bytes(b"x")
    os.utime(old, (0, 0))
    monkeypatch.setattr(app_package, "BASE_DIR", tmp_path)
    app_package.create_app(db_name=db_name)
    assert not old.exists()


# ---------------- 演示文件 ----------------
def test_demo_file(client, query):
    html = upload(client, DEMO_FILE.open("rb"), DEMO_FILE.name)
    assert counts(html, "pv") == (3, 2, 1, 0)
    assert "第 4 行" in html and "折扣应大于等于 0、小于 1（比如八折填 0.2），填的是 1.5" in html
    assert counts(confirm(client, html), "rs") == (3, 2, 1, 0)
    assert query("SELECT row_id FROM sales_order WHERE source = '表格导入' ORDER BY row_id") == ((10001,), (10002,))


# ---------------- 对话框录入 ----------------
FORM = {"order_date": "2025-02-10", "ship_date": "2025-02-12", "ship_mode": "标准级", "order_id": "",
        "customer_id": "C-009", "customer_name": "手工客户", "segment": "消费者", "region": "华东",
        "province": "浙江", "city": "杭州", "category": "家具", "sub_category": "椅子", "product_id": "",
        "product_name": "手工录入的椅子", "sales": "800", "quantity": "2", "discount": "0", "profit": "120"}


def post_order(client, **change):
    return client.post("/data/orders/new", json={**FORM, **change}, headers={"X-CSRFToken": csrf(client)})


def test_dialog_in_page(client):
    html = client.get("/data").get_data(as_text=True)
    assert 'id="newOrderBtn"' in html and 'id="orderModal"' in html and 'id="new_sub_category"' in html
    assert 'type="date"' in html.split('id="orderModal"')[1] and "留空自动生成" in html


def test_dialog_entry(client, query):
    today = f"{date.today():%Y%m%d}"
    data = post_order(client).get_json()
    assert data == {"ok": True, "row_id": 10001, "batch_id": data["batch_id"]}
    row = query("SELECT source, batch_id, order_id, product_id, order_key, ym, unit_price, profit_rate, "
                "province FROM sales_order WHERE row_id = 10001")[0]
    assert row[:4] == ("手工录入", data["batch_id"], f"MAN-{today}-1", f"MAN-{today}-1")
    assert row[4] == f"MAN-{today}-1|20250210|C-009" and row[5] == "2025-02"
    assert float(row[6]) == 400 and float(row[7]) == 0.15 and row[8] == "浙江"
    assert query("SELECT method, file_name, total_rows, success_rows FROM import_batch") == \
        (("手工录入", None, 1, 1),)
    assert year_state(query) == {**UNCHANGED, 2025: (1, None)}

    second = post_order(client, order_id="自己填的订单号").get_json()         # 第二条：另一个批次，序号接着编
    assert second["row_id"] == 10002 and second["batch_id"] != data["batch_id"]
    assert query("SELECT order_id, product_id FROM sales_order WHERE row_id = 10002") == \
        (("自己填的订单号", f"MAN-{today}-2"),)
    assert year_state(query)[2025] == (2, None)

    html = client.get("/data?source=手工录入").get_data(as_text=True)
    assert 'id="totalCount">2<' in html and "订单已保存：行 ID 10002" in html and "手工录入的椅子" in html


def test_dialog_errors(client, query):
    data = post_order(client, discount="1.5", quantity="0", city="", sub_category="纸张").get_json()
    assert data["ok"] is False
    errors = {e["field"]: e["msg"] for e in data["errors"]}
    assert set(errors) == {"discount", "quantity", "city", "sub_category"}
    assert "折扣应大于等于 0、小于 1" in errors["discount"] and "不属于类别“家具”" in errors["sub_category"]
    assert post_order(client, product_name="字" * 201).get_json()["errors"][0]["field"] == "product_name"
    assert post_order(client, ship_date="2025-02-01").get_json()["errors"][0]["field"] == "ship_date"
    assert one(query, "SELECT COUNT(*) FROM sales_order") == 9959
    assert one(query, "SELECT COUNT(*) FROM import_batch") == 0


def test_dialog_needs_csrf_token(client, query):
    resp = client.post("/data/orders/new", json=FORM)
    assert resp.status_code == 400 and one(query, "SELECT COUNT(*) FROM sales_order") == 9959


# ---------------- 导入记录：撤销、恢复 ----------------
def revoke(client, batch_id):
    return client.post(f"/data/batches/{batch_id}/revoke", data={"csrf_token": csrf(client)},
                       follow_redirects=True).get_data(as_text=True)


def restore(client):
    return client.post("/data/batches/restore", data={"csrf_token": csrf(client)},
                       follow_redirects=True).get_data(as_text=True)


def test_batch_list(client):
    assert "还没有导入记录" in client.get("/data/batches").get_data(as_text=True)
    confirm(client, upload(client, xlsx([good_row(), good_row(折扣=1.5)]), "两行.xlsx"))
    post_order(client)
    html = client.get("/data/batches").get_data(as_text=True)
    table = html.split('id="batchTable"')[1].split("</table>")[0]
    assert "两行.xlsx" in table and "表格导入" in table and "手工录入" in table
    assert table.count("revoke-btn") == 2 and table.count(">有效<") == 2
    assert 'id="importedCount">2<' in html and 'id="confirmModal"' in html


def test_revoke(client, query):
    rows = [good_row(), good_row(), good_row(**{"订单日期": "2023/06/01", "发货日期": "2023/06/03"})]
    confirm(client, upload(client, xlsx(rows)))
    post_order(client)                                            # 另一个批次，不应该受影响
    first, second = [r[0] for r in query("SELECT id FROM import_batch ORDER BY id")]
    assert one(query, "SELECT COUNT(*) FROM sales_order") == 9963
    assert year_state(query) == {**UNCHANGED, 2023: (2, 1), 2025: (2, None)}

    html = revoke(client, first)
    assert f"批次 {first} 已撤销，删除了 3 条订单" in html and "2023、2025" in html
    assert one(query, "SELECT COUNT(*) FROM sales_order") == 9960
    assert query("SELECT batch_id FROM sales_order WHERE source <> '原始数据'") == ((second,),)
    assert query("SELECT id, revoked FROM import_batch ORDER BY id") == ((first, 1), (second, 0))
    # 只有被删订单所在的年份加 1
    assert year_state(query) == {**UNCHANGED, 2023: (3, 1), 2025: (3, None)}
    assert "已经撤销过了" in revoke(client, first)                 # 不能撤销两次
    assert "不存在" in revoke(client, 999999)
    assert year_state(query)[2023] == (3, 1)
    assert 'id="totalCount">0<' in client.get("/data?source=表格导入").get_data(as_text=True)


def test_year_row_stays_after_all_orders_removed(client, query):
    """撤销了唯一一批 2025 年的订单：year_state 里 2025 这一行还在，首页的年份表里不再列 2025"""
    confirm(client, upload(client, xlsx([good_row()])))
    assert "<td>2025</td><td>1</td>" in client.get("/").get_data(as_text=True)
    revoke(client, one(query, "SELECT id FROM import_batch"))
    assert year_state(query) == {**UNCHANGED, 2025: (2, None)}
    assert "<td>2025</td>" not in client.get("/").get_data(as_text=True)


def test_restore_original(client, query):
    before = query("SELECT COUNT(*), SUM(quantity), SUM(sales) FROM sales_order")
    assert before[0][:2] == (9959, 37534)
    rows = [good_row(), good_row(**{"订单日期": "2022/03/01", "发货日期": "2022/03/02"})]
    confirm(client, upload(client, xlsx(rows)))
    post_order(client)
    post_order(client, order_date="2024-05-01", ship_date="2024-05-02")
    assert one(query, "SELECT COUNT(*) FROM sales_order") == 9963

    html = restore(client)
    assert "已恢复原始数据，删除了 4 条订单" in html and "2022、2024、2025" in html
    assert query("SELECT COUNT(*), SUM(quantity), SUM(sales) FROM sales_order") == before
    assert one(query, "SELECT COUNT(*) FROM sales_order WHERE source <> '原始数据'") == 0
    assert query("SELECT COUNT(*), SUM(revoked) FROM import_batch") == ((3, 3),)
    # 2022：导入 +1，恢复 +1；2024：录入 +1，恢复 +1；2025：两次写入、一次恢复；2021、2023 没动过
    assert year_state(query) == {2021: (1, 1), 2022: (3, 1), 2023: (1, 1), 2024: (3, 1), 2025: (3, None)}
    assert 'id="statRows">9959<' in client.get("/").get_data(as_text=True)
    assert "删除了 0 条订单" in restore(client)                    # 没有导入数据时再点一次，什么都不变
    assert year_state(query)[2022] == (3, 1)


def test_year_status_on_home_page(client):
    """导入 1 条 2023 年的订单：只有 2023 年变成需重新处理"""
    confirm(client, upload(client, xlsx([good_row(**{"订单日期": "2023/06/01", "发货日期": "2023/06/03"})])))
    html = client.get("/").get_data(as_text=True)
    assert html.count(">已处理<") == 3 and html.count(">数据有更新，需重新处理<") == 1
    row = re.search(r"<td>2023</td>.*?</tr>", html, re.S).group(0)
    assert "需重新处理" in row and "<td>2655</td>" in row          # 2023 年原有 2654 行


def test_monthly_rows_cleaned(client, query):
    """撤销后，月度表里已经没有任何订单的月份，汇总记录要删掉；还有订单的月份不动"""
    run_sql(["INSERT INTO monthly_sales (region, category, sub_category, ym, sales, quantity, profit, order_count) "
             "VALUES ('华东', '办公用品', '纸张', '2025-01', 241, 6, 50.6, 2), "
             "('华东', '办公用品', '纸张', '2025-02', 120.5, 3, 25.3, 1)",
             "INSERT INTO monthly_sales_category (region, category, ym, sales, quantity, profit, order_count) "
             "VALUES ('华东', '办公用品', '2025-01', 241, 6, 50.6, 2), ('华东', '办公用品', '2025-02', 120.5, 3, 25.3, 1)"])
    confirm(client, upload(client, xlsx([good_row(), good_row()])))                       # 2025 年 1 月
    confirm(client, upload(client, xlsx([good_row(**{"订单日期": "2025/02/03", "发货日期": "2025/02/04"})])))
    january, february = [r[0] for r in query("SELECT id FROM import_batch ORDER BY id")]

    months = lambda table: [r[0] for r in query(f"SELECT ym FROM {table} WHERE ym > '2024-12' ORDER BY ym")]
    revoke(client, january)
    assert months("monthly_sales") == ["2025-02"] and months("monthly_sales_category") == ["2025-02"]
    restore(client)
    assert months("monthly_sales") == [] and months("monthly_sales_category") == []
    assert one(query, "SELECT COUNT(*) FROM monthly_sales") == 4896            # 2021–2024 的汇总没有被动
    assert one(query, "SELECT COUNT(*) FROM monthly_sales_category") == 864


# ---------------- 登录和 CSRF ----------------
@pytest.mark.parametrize("url", ["/data/import", "/data/import/template", "/data/import/preview",
                                 "/data/batches", "/data/import/result/1"])
def test_pages_need_login(anon_client, url):
    resp = anon_client.get(url)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


@pytest.mark.parametrize("url", ["/data/import", "/data/import/confirm", "/data/import/cancel",
                                 "/data/batches/1/revoke", "/data/batches/restore"])
def test_posts_need_csrf_token(client, query, url):
    assert client.post(url).status_code == 400
    assert one(query, "SELECT COUNT(*) FROM sales_order") == 9959
