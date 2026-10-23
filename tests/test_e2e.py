# -*- coding: utf-8 -*-
"""
端到端测试：用程序操作一个真的浏览器（本机的 Edge），像人一样点按钮、填表单，检查页面和数据库的结果。
页面里的 JavaScript（联动下拉框、拖放上传、对话框、确认框、排序、图表）只有这样才能测到。
网站在测试进程里临时启动，连的是测试库，端口由系统随机给一个空闲的，不碰正式库，也不占 5000 端口
"""
import base64
import re
import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from config import TEST_DB_NAME
from conftest import require_test_db, reset_imported_data, run_sql
from step3_load_mysql import connect, load

ROOT = Path(__file__).resolve().parent.parent
DEMO_FILE = ROOT / "docs" / "演示数据" / "导入演示_2对1错.xlsx"
USER, PASSWORD = "e2e_tester", "e2e-only-password"          # 只存在于测试库里的账号

ORDER = {"order_date": "2025-02-10", "ship_date": "2025-02-12", "ship_mode": "标准级",
         "customer_id": "C-009", "customer_name": "浏览器测试客户", "segment": "消费者", "region": "华东",
         "province": "浙江", "city": "杭州", "category": "家具", "sub_category": "椅子",
         "product_name": "浏览器里录入的椅子", "sales": "800", "quantity": "2", "discount": "0.1",
         "profit": "120"}
SELECTS = {"ship_mode", "segment", "region", "province", "category", "sub_category"}


SITE = {}


# ---------------- 启动网站和浏览器 ----------------
def open_with_retry(page):
    """
    测试跑得又快又多时，Windows 偶尔会报“没有可用的缓冲区空间”（ERR_NO_BUFFER_SPACE），页面打不开。
    这是系统的网络连接一时周转不过来，不是网站的错：碰上了就等 2 秒再开一次，最多试 3 次。别的错误照常报出来
    """
    real_goto = page.goto

    def goto(url, **kwargs):
        for attempt in range(3):
            try:
                return real_goto(url, **kwargs)
            except BrowserError as e:
                if "ERR_NO_BUFFER_SPACE" not in str(e) or attempt == 2:
                    raise
                page.wait_for_timeout(2000)
    page.goto = goto


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """在后台线程里启动网站（测试库、空闲端口），返回网址，比如 http://127.0.0.1:53124"""
    db_name = require_test_db(TEST_DB_NAME)
    load(db_name, force=True)                                  # 测试库装成 9959 条原始订单
    from app import auth, create_app, db
    from migrate_v2 import add_missing_columns
    conn = connect(db_name)
    with conn.cursor() as cur:
        add_missing_columns(cur)
    conn.commit()
    conn.close()
    app = create_app(db_name=db_name)
    assert app.config["DB_NAME"].endswith("_test")
    app.config["UPLOAD_TMP"] = tmp_path_factory.mktemp("uploads")
    # 样式表、脚本这些不变的文件让浏览器记住 1 小时，不用每开一个页面都重新取一遍。
    # 测试用的开发服务器每取一个文件就要新开一条网络连接，少取一些，系统的压力小很多
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600
    with app.app_context():
        db.execute("DELETE FROM sys_user")
        auth.create_user(USER, PASSWORD)
    SITE["app"] = app                                          # 个别测试要直接调用服务来准备数据
    server = make_server("127.0.0.1", 0, app, threaded=True)   # 端口写 0：让系统挑一个空闲的
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    reset_imported_data()


@pytest.fixture(scope="module")
def browser():
    """本机已经装好的 Edge，不显示窗口"""
    with sync_playwright() as p:
        edge = p.chromium.launch(channel="msedge", headless=True)
        yield edge
        edge.close()


@pytest.fixture(scope="module")
def context(site, browser):
    """一个浏览器窗口环境，这个文件里的测试共用（这样浏览器记住的文件能接着用）。每个测试开始时会清掉登录状态"""
    context = browser.new_context(base_url=site, locale="zh-CN", viewport={"width": 1440, "height": 900})
    yield context
    context.close()


@pytest.fixture
def page(context):
    """一个已经登录的浏览器页面。测试结束时，页面上只要出现过 JavaScript 报错，这个测试就算失败"""
    reset_imported_data()
    context.clear_cookies()                    # 上一个测试的登录状态、提示消息都清掉，从头登录
    page = context.new_page()
    page.set_default_timeout(10000)
    problems = []
    page.on("console", lambda m: problems.append(f"控制台报错：{m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(f"脚本出错：{e}"))
    open_with_retry(page)
    page.goto("/login")
    page.fill("input[name=username]", USER)
    page.fill("input[name=password]", PASSWORD)
    page.click("button[type=submit]")
    expect(page.locator("#statRows")).to_have_text("9959")
    yield page
    page.close()
    assert problems == []


_db = {}


def db_value(sql):
    """到测试库里查一个数。这个文件里的测试共用一条数据库连接，不用每查一次就新开一条"""
    if "conn" not in _db:
        _db["conn"] = connect(require_test_db(TEST_DB_NAME))
        _db["conn"].autocommit(True)           # 每次查询都能看到别的连接刚提交的数据
    with _db["conn"].cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def manual_orders():
    return db_value("SELECT COUNT(*) FROM sales_order WHERE source = '手工录入'")


# ---------------- 新增订单对话框 ----------------
def open_dialog(page):
    page.goto("/data")
    page.click("#newOrderBtn")
    expect(page.locator("#orderModal")).to_be_visible()
    expect(page.locator("#orderModal")).to_have_class(re.compile(r"\bshow\b"))


def fill_order(page, **change):
    for name, value in {**ORDER, **change}.items():
        field = page.locator(f"#orderForm [name={name}]")
        if name in SELECTS:
            field.select_option(value)
        else:
            field.fill(value)


def expect_saved(page, total=1):
    """保存成功的样子：页面刷新，出现绿色提示，对话框不在了，数据库里多了订单"""
    expect(page.locator(".alert-success")).to_contain_text("订单已保存：行 ID")
    expect(page.locator("#orderModal")).to_be_hidden()
    assert manual_orders() == total


@pytest.mark.parametrize("round_no", range(1, 11))
def test_dialog_fix_discount_then_save(page, round_no):
    """学生手工验收时走的那条路：折扣先填 1.5 → 提示出错 → 改成 0.1 → 保存成功。连做 10 遍"""
    open_dialog(page)
    fill_order(page, discount="1.5")
    page.click("#orderSave")
    expect(page.locator("#orderErrors")).to_be_visible()
    expect(page.locator("#orderErrors")).to_contain_text("折扣应大于等于 0、小于 1")
    expect(page.locator("#new_discount")).to_have_class(re.compile("is-invalid"))
    expect(page.locator("#orderModal")).to_be_visible()                     # 对话框没有关
    expect(page.locator("#new_product_name")).to_have_value(ORDER["product_name"])   # 填过的内容还在
    assert manual_orders() == 0

    page.fill("#new_discount", "0.1")
    page.click("#orderSave")
    expect_saved(page)
    assert float(db_value("SELECT discount FROM sales_order WHERE source = '手工录入'")) == 0.1
    assert db_value("SELECT COUNT(*) FROM import_batch WHERE method = '手工录入'") == 1


def test_dialog_close_and_reopen_before_saving(page):
    """出错后先关掉对话框再打开：旧的错误提示清掉了，改好以后照样能保存"""
    open_dialog(page)
    fill_order(page, discount="1.5")
    page.click("#orderSave")
    expect(page.locator("#orderErrors")).to_be_visible()
    page.click("#orderModal .modal-footer .btn-secondary")                  # 取消
    expect(page.locator("#orderModal")).to_be_hidden()
    page.click("#newOrderBtn")
    expect(page.locator("#orderModal")).to_have_class(re.compile(r"\bshow\b"))
    expect(page.locator("#orderErrors")).to_be_hidden()
    expect(page.locator("#orderForm .is-invalid")).to_have_count(0)
    page.fill("#new_discount", "0.1")
    page.click("#orderSave")
    expect_saved(page)


def test_dialog_save_with_enter_key(page):
    open_dialog(page)
    fill_order(page)
    page.press("#new_profit", "Enter")                                      # 在输入框里按回车，不点按钮
    expect_saved(page)


def test_dialog_double_click_saves_once(page):
    open_dialog(page)
    fill_order(page)
    page.dblclick("#orderSave")
    expect_saved(page, total=1)                                             # 只有 1 条，没有重复保存


def test_dialog_cannot_submit_twice_while_saving(page):
    """保存的请求发出去之后、新页面出来之前，再怎么点、怎么按回车，都不会多存一条"""
    open_dialog(page)
    fill_order(page)
    page.evaluate("""() => {
        const form = document.getElementById("orderForm");
        for (let i = 0; i < 5; i++) form.requestSubmit();                   // 一口气提交 5 次
    }""")
    expect_saved(page, total=1)


def test_five_requests_at_once_get_different_row_ids(page):
    """绕过按钮，5 个保存请求同时发到服务器：5 条都存上，行 ID 各不相同，服务器不报错"""
    open_dialog(page)
    fill_order(page)
    results = page.evaluate("""() => {
        const form = document.getElementById("orderForm");
        const data = Object.fromEntries(new FormData(form));
        return Promise.all([1, 2, 3, 4, 5].map(() => postJSON(form.dataset.url, data)));
    }""")
    assert all(r["ok"] for r in results)
    assert sorted(r["row_id"] for r in results) == [10001, 10002, 10003, 10004, 10005]
    assert manual_orders() == 5 and db_value("SELECT COUNT(DISTINCT order_id) FROM sales_order "
                                             "WHERE source = '手工录入'") == 5


def test_dialog_keeps_save_button_locked_after_success(page):
    """
    服务器说保存成功以后，按钮要一直不可点，直到页面刷新，这样同一条订单不会被存两次。
    做法：页面即将刷新的那一刻，把按钮的状态记下来，并再提交一次；刷新后检查
    """
    open_dialog(page)
    fill_order(page)
    page.evaluate("""() => window.addEventListener("beforeunload", () => {
        const button = document.getElementById("orderSave");
        sessionStorage.setItem("saveButton", JSON.stringify([button.disabled, button.textContent]));
        document.getElementById("orderForm").requestSubmit();
    })""")
    page.click("#orderSave")
    expect_saved(page, total=1)
    assert page.evaluate("JSON.parse(sessionStorage.getItem('saveButton'))") == [True, "正在保存…"]
    page.wait_for_timeout(300)
    assert manual_orders() == 1


def test_dialog_chinese_full_stop_in_discount(page):
    """
    中文输入法下敲句号，出来的是“。”。数字输入框不收这个字符，0。1 实际变成 01，也就是折扣 1。
    这时不能保存，提示里要能看出系统收到的是 01
    """
    open_dialog(page)
    fill_order(page, discount="1.5")
    page.click("#orderSave")
    expect(page.locator("#orderErrors")).to_contain_text("填的是 1.5")
    page.click("#new_discount")
    page.keyboard.press("Control+A")
    page.keyboard.type("0。1")
    expect(page.locator("#new_discount")).to_have_value("01")
    page.click("#orderSave")
    expect(page.locator("#orderErrors")).to_contain_text("没有保存")
    expect(page.locator("#orderErrors")).to_contain_text("填的是 01")
    expect(page.locator("#orderModal")).to_be_visible()
    assert manual_orders() == 0
    page.fill("#new_discount", "0.1")
    page.click("#orderSave")
    expect_saved(page)


def test_dialog_several_errors_and_linked_subcategory(page):
    open_dialog(page)
    sub = page.locator("#new_sub_category")
    expect(sub.locator("option")).to_have_text(["请先选类别"])
    page.select_option("#new_category", "技术")
    expect(sub.locator("option")).to_have_text(["请选择", "复印机", "电话", "设备", "配件"])
    page.select_option("#new_category", "家具")
    expect(sub.locator("option")).to_have_text(["请选择", "书架", "桌子", "椅子", "用具"])

    fill_order(page, quantity="0", city="", ship_date="2025-02-01")
    page.click("#orderSave")
    expect(page.locator("#orderErrors li")).to_have_count(3)
    for name in ("quantity", "city", "ship_date"):
        expect(page.locator(f"#orderForm [name={name}]")).to_have_class(re.compile("is-invalid"))
    expect(page.locator("#new_sales")).not_to_have_class(re.compile("is-invalid"))
    assert manual_orders() == 0


# ---------------- 订单查询：联动、排序 ----------------
def test_filter_subcategory_follows_category(page):
    page.goto("/data")
    options = page.locator("#sub_category option")
    expect(options).to_have_count(18)                                       # 全部 + 17 个子类别
    page.select_option("#category", "家具")
    expect(options).to_have_text(["全部", "书架", "桌子", "椅子", "用具"])
    page.select_option("#sub_category", "桌子")
    page.select_option("#region", "华东")
    page.click("#filterForm button[type=submit]")
    expect(page.locator("#totalCount")).not_to_have_text("9959")
    expect(page.locator("#category")).to_have_value("家具")                 # 查询后条件还在
    expect(page.locator("#sub_category")).to_have_value("桌子")
    expect(options).to_have_count(5)
    assert set(page.locator("#orderTable tbody tr td:nth-child(8)").all_inner_texts()) == {"桌子"}
    page.select_option("#category", "")
    expect(options).to_have_count(18)


def test_sort_by_clicking_header(page):
    page.goto("/data?region=西北")
    sales = lambda: [float(t) for t in page.locator("#orderTable tbody tr td:nth-child(10)").all_inner_texts()]
    page.click("#orderTable thead a:has-text('销售额')")
    expect(page.locator("#orderTable thead")).to_contain_text("销售额 ↓")
    assert "sort=sales" in page.url and "dir=desc" in page.url and sales() == sorted(sales(), reverse=True)
    page.click("#orderTable thead a:has-text('销售额')")
    expect(page.locator("#orderTable thead")).to_contain_text("销售额 ↑")
    assert "dir=asc" in page.url and sales() == sorted(sales()) and len(sales()) == 20
    expect(page.locator("#region")).to_have_value("西北")                   # 排序时筛选条件不丢


# ---------------- 首页图表 ----------------
def test_home_chart(page):
    page.goto("/")
    expect(page.locator("#salesChart canvas")).to_be_visible()
    info = page.evaluate("""() => {
        const chart = echarts.getInstanceByDom(document.getElementById("salesChart"));
        const option = chart.getOption();
        return { months: option.xAxis[0].data.length, first: option.xAxis[0].data[0],
                 points: option.series[0].data.length, positive: option.series[0].data.every(v => v > 0),
                 width: chart.getWidth() };
    }""")
    assert info["months"] == info["points"] == 48 and info["first"] == "2021-01"
    assert info["positive"] and info["width"] > 300
    page.set_viewport_size({"width": 1000, "height": 800})                  # 窗口变窄，图表跟着变
    page.wait_for_function(f"echarts.getInstanceByDom(document.getElementById('salesChart')).getWidth() < {info['width']}")


# ---------------- 表格导入：拖放、选文件、预览、确认 ----------------
def drop_file(page, path):
    """模拟把文件拖到虚线框里松手"""
    page.evaluate("""([name, b64]) => {
        const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
        const data = new DataTransfer();
        data.items.add(new File([bytes], name));
        const zone = document.getElementById("dropZone");
        zone.dispatchEvent(new DragEvent("dragover", { dataTransfer: data, bubbles: true, cancelable: true }));
        zone.dispatchEvent(new DragEvent("drop", { dataTransfer: data, bubbles: true, cancelable: true }));
    }""", [path.name, base64.b64encode(path.read_bytes()).decode()])


def expect_preview_of_demo_file(page):
    expect(page.locator("#previewNote")).to_contain_text("还没有写入数据库")
    for element_id, value in [("pvTotal", "3"), ("pvOk", "2"), ("pvFailed", "1"), ("pvSkipped", "0")]:
        expect(page.locator(f"#{element_id}")).to_have_text(value)
    expect(page.locator("#failedTable tbody")).to_contain_text("第 4 行")
    expect(page.locator("#previewTable tbody tr")).to_have_count(2)
    assert db_value("SELECT COUNT(*) FROM sales_order") == 9959            # 预览不写数据库


def test_drag_and_drop_upload_preview_confirm(page):
    page.goto("/data/import")
    expect(page.locator("#uploadBtn")).to_be_disabled()                     # 没选文件时不能上传
    page.locator("#dropZone").dispatch_event("dragover")
    expect(page.locator("#dropZone")).to_have_class(re.compile("drag-over"))
    drop_file(page, DEMO_FILE)
    expect(page.locator("#dropZone")).not_to_have_class(re.compile("drag-over"))
    expect(page.locator("#fileName")).to_have_text(f"已选择：{DEMO_FILE.name}")
    expect(page.locator("#uploadBtn")).to_be_enabled()
    page.click("#uploadBtn")
    expect_preview_of_demo_file(page)

    page.click("#confirmBtn")
    expect(page.locator("#resultNote")).to_contain_text("导入完成")
    for element_id, value in [("rsTotal", "3"), ("rsOk", "2"), ("rsFailed", "1"), ("rsSkipped", "0")]:
        expect(page.locator(f"#{element_id}")).to_have_text(value)
    expect(page.locator("#importTime")).to_have_text(re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d"))
    assert db_value("SELECT COUNT(*) FROM sales_order WHERE source = '表格导入'") == 2
    with page.expect_download() as download:                                # 失败明细能下载
        page.click("#problemsBtn")
    assert download.value.suggested_filename.endswith(".xlsx")


def test_choose_file_by_clicking_then_cancel(page):
    page.goto("/data/import")
    with page.expect_file_chooser() as chooser:                             # 点虚线框会打开选文件的窗口
        page.click("#dropZone")
    chooser.value.set_files(str(DEMO_FILE))
    expect(page.locator("#fileName")).to_have_text(f"已选择：{DEMO_FILE.name}")
    page.click("#uploadBtn")
    expect_preview_of_demo_file(page)
    page.click("text=取消")
    expect(page.locator(".alert")).to_contain_text("已取消，数据没有变化")
    assert db_value("SELECT COUNT(*) FROM sales_order") == 9959


def test_template_download(page):
    page.goto("/data/import")
    with page.expect_download() as download:
        page.click("#templateBtn")
    assert download.value.suggested_filename == "订单导入模板.xlsx"


# ---------------- 导入记录：撤销、恢复的确认框 ----------------
def import_demo_file(page):
    page.goto("/data/import")
    page.set_input_files("#fileInput", str(DEMO_FILE))
    page.click("#uploadBtn")
    page.click("#confirmBtn")
    expect(page.locator("#resultNote")).to_contain_text("导入完成")


def test_revoke_needs_confirmation(page):
    import_demo_file(page)
    page.goto("/data/batches")
    expect(page.locator("#importedCount")).to_have_text("2")
    page.click(".revoke-btn")
    modal = page.locator("#confirmModal")
    expect(modal).to_be_visible()
    expect(page.locator("#confirmTitle")).to_have_text(re.compile(r"撤销批次 \d+"))
    expect(page.locator("#confirmText")).to_contain_text("导入的 2 条订单")
    modal.locator(".btn-secondary").click()                                 # 点“取消”：什么都不变
    expect(modal).to_be_hidden()
    assert db_value("SELECT COUNT(*) FROM sales_order") == 9961

    page.click(".revoke-btn")
    expect(modal).to_have_class(re.compile(r"\bshow\b"))
    page.click("#confirmOk")                                                # 点“确定”：撤销
    expect(page.locator(".alert-success")).to_contain_text("已撤销，删除了 2 条订单")
    expect(page.locator("#batchTable")).to_contain_text("已撤销")
    expect(page.locator(".revoke-btn")).to_have_count(0)
    assert db_value("SELECT COUNT(*) FROM sales_order") == 9959


def test_restore_needs_confirmation(page):
    expect_disabled = page.locator("#restoreBtn")
    page.goto("/data/batches")
    expect(expect_disabled).to_be_disabled()                                # 没有导入数据时不能点
    import_demo_file(page)
    open_dialog(page)
    fill_order(page)
    page.click("#orderSave")
    expect_saved(page)
    page.goto("/data/batches")
    expect(page.locator("#batchTable tbody tr")).to_have_count(2)
    page.click("#restoreBtn")
    modal = page.locator("#confirmModal")
    expect(page.locator("#confirmTitle")).to_have_text("恢复原始数据")
    expect(page.locator("#confirmText")).to_contain_text("全部 3 条")
    modal.locator(".btn-close").click()                                     # 点右上角的叉：什么都不变
    expect(modal).to_be_hidden()
    assert db_value("SELECT COUNT(*) FROM sales_order") == 9962

    page.click("#restoreBtn")
    expect(modal).to_have_class(re.compile(r"\bshow\b"))
    page.click("#confirmOk")
    expect(page.locator(".alert-success")).to_contain_text("已恢复原始数据，删除了 3 条订单")
    assert db_value("SELECT COUNT(*) FROM sales_order") == 9959
    page.goto("/")
    expect(page.locator("#statRows")).to_have_text("9959")


# ---------------- 数据处理和展示 ----------------
def chart_info(page, element_id):
    return page.evaluate("""id => {
        const option = echarts.getInstanceByDom(document.getElementById(id)).getOption();
        return { months: option.xAxis[0].data, values: option.series[0].data,
                 zoom: (option.dataZoom || []).map(z => z.type) };
    }""", element_id)


def test_process_year_selects_follow_each_other(page):
    page.goto("/process")
    expect(page.locator("#start_year option")).to_have_text(["2021", "2022", "2023", "2024"])
    expect(page.locator("#start_year")).to_have_value("2021")
    expect(page.locator("#end_year")).to_have_value("2024")
    page.select_option("#end_year", "2022")
    page.select_option("#start_year", "2023")                               # 起始年选得比结束年晚：结束年跟着变
    expect(page.locator("#end_year")).to_have_value("2023")
    page.select_option("#end_year", "2021")                                 # 结束年选得比起始年早：起始年跟着变
    expect(page.locator("#start_year")).to_have_value("2021")


def test_process_run_shows_summary(page):
    """选 2021–2024 年点“开始处理”：按钮变成“正在处理…”，处理完显示摘要里的各个数字"""
    logs_before = db_value("SELECT COUNT(*) FROM process_log")
    page.goto("/process")
    page.evaluate("""() => window.addEventListener("beforeunload", () => {
        const button = document.getElementById("runBtn");
        sessionStorage.setItem("runButton", JSON.stringify([button.disabled, button.textContent]));
        document.getElementById("runForm").requestSubmit();                 // 处理中再提交一次：应该没有效果
    })""")
    page.click("#runBtn")
    expect(page.locator(".alert-success")).to_contain_text("处理完成：2021–2024 年")
    assert page.evaluate("JSON.parse(sessionStorage.getItem('runButton'))") == [True, "正在处理…"]
    for element_id, value in [("sumRows", "9959"), ("sumDuplicates", "0"), ("sumInvalid", "0"),
                              ("sumOutlierSales", "1037"), ("sumOutlierProfit", "1839"), ("sumMonths", "48"),
                              ("sumZeroSub", "1360"), ("sumZeroCat", "37")]:
        expect(page.locator(f"#{element_id}")).to_have_text(value)
    expect(page.locator("#sumSeconds")).to_have_text(re.compile(r"^\d+\.\d+$"))
    expect(page.locator("#summary")).to_contain_text("异常值按所选年份范围统计，只标记不删除")
    expect(page.locator("#yearTable .badge")).to_have_text(["已处理"] * 4)
    assert db_value("SELECT COUNT(*) FROM process_log") == logs_before + 1  # 只处理了一次
    assert db_value("SELECT SUM(quantity) FROM monthly_sales_category") == 37534


def test_process_state_changes_in_browser(page):
    """录入一条 2023 年的订单 → 2023 年变黄“需重新处理” → 只处理 2023 年 → 变回绿色“已处理”"""
    open_dialog(page)
    fill_order(page, order_date="2023-06-01", ship_date="2023-06-02")
    page.click("#orderSave")
    expect_saved(page)
    page.goto("/process")
    row = page.locator("#yearTable tr[data-year='2023']")
    expect(row.locator(".badge")).to_have_text("数据有更新，需重新处理")
    expect(row.locator(".badge")).to_have_class(re.compile("text-bg-warning"))
    expect(page.locator("#staleNote")).to_contain_text("2023 年")
    page.select_option("#start_year", "2023")
    page.select_option("#end_year", "2023")
    page.click("#runBtn")
    expect(page.locator(".alert-success")).to_contain_text("处理完成：2023–2023 年")
    expect(page.locator("#sumRows")).to_have_text("2655")
    expect(row.locator(".badge")).to_have_text("已处理")
    expect(page.locator("#staleNote")).to_have_count(0)
    # 收尾：删掉这条订单，再处理一次，月度表回到原样
    reset_imported_data()
    page.goto("/process")
    page.select_option("#start_year", "2023")
    page.select_option("#end_year", "2023")
    page.click("#runBtn")
    expect(page.locator("#sumRows")).to_have_text("2654")


def test_process_result_filters_and_chart(page):
    page.goto("/process")
    expect(page.locator("#quantityChart canvas")).to_be_visible()
    info = chart_info(page, "quantityChart")
    assert len(info["months"]) == 48 and sum(info["values"]) == 37534
    assert sorted(info["zoom"]) == ["inside", "slider"]                     # 有缩放条，也能用滚轮缩放
    expect(page.locator("#subBox")).to_be_hidden()                          # 类别层级下没有子类别下拉框
    expect(page.locator("#totalCount")).to_have_text("864")

    page.select_option("#level", "子类别")
    expect(page.locator("#subBox")).to_be_visible()
    expect(page.locator("#sub_category option")).to_have_count(18)
    page.select_option("#category", "技术")
    expect(page.locator("#sub_category option")).to_have_text(["全部", "复印机", "电话", "设备", "配件"])
    page.select_option("#region", "西北")
    page.select_option("#sub_category", "电话")
    page.click("#viewBtn")
    expect(page.locator("#totalCount")).to_have_text("48")
    expect(page.locator("#monthlyTable thead")).to_contain_text("子类别")
    expect(page.locator("#sub_category")).to_have_value("电话")              # 查看后条件还在
    expect(page.locator("#subBox")).to_be_visible()
    want = db_value("SELECT SUM(quantity) FROM monthly_sales WHERE region = '西北' AND sub_category = '电话'")
    zeros = db_value("SELECT COUNT(*) FROM monthly_sales WHERE region = '西北' AND sub_category = '电话' "
                     "AND order_count = 0")
    assert sum(chart_info(page, "quantityChart")["values"]) == want
    expect(page.locator("#zeroCount")).to_have_text(str(zeros))

    page.click("#pager a:has-text('下一页')")                               # 翻页后条件不丢
    expect(page.locator("#monthlyTable tbody tr")).to_have_count(20)
    expect(page.locator("#region")).to_have_value("西北")
    page.select_option("#level", "类别")
    expect(page.locator("#subBox")).to_be_hidden()
    with page.expect_download() as download:
        page.click("#exportBtn")
    assert download.value.suggested_filename.startswith("月度汇总_子类别_")


def test_process_chart_zoom(page):
    """拖动缩放条相当于只看一段时间：这里直接让图表缩到后一半，检查它确实缩了"""
    page.goto("/process?region=华东&category=办公用品")
    result = page.evaluate("""() => {
        const chart = echarts.getInstanceByDom(document.getElementById("quantityChart"));
        chart.dispatchAction({ type: "dataZoom", start: 50, end: 100 });
        const zoom = chart.getOption().dataZoom[0];
        return [zoom.start, zoom.end, zoom.startValue, zoom.endValue];
    }""")
    assert result[:2] == [50, 100] and result[3] == 47 and 20 <= result[2] <= 26


# ---------------- 销售分析 ----------------
ANALYSIS_CHARTS = ["trend", "season", "yoy", "annual", "region", "subcategory", "heatmap", "quadrant"]


def open_analysis(page, query=""):
    page.goto("/analysis" + query)
    wait_charts(page)


def wait_charts(page):
    """等 8 张图都画完（页面脚本画完后会在 body 上做个记号）"""
    page.wait_for_function("document.body.dataset.ready === '1'")


def analysis_option(page, name):
    """某张图当前画的内容：横轴（或纵轴）的类目、各系列的数值"""
    return page.evaluate("""name => {
        const option = window.analysisCharts[name].getOption();
        const value = v => (v !== null && typeof v === "object" && !Array.isArray(v)) ? v.value : v;
        return { x: (option.xAxis[0] || {}).data || [], y: (option.yAxis[0] || {}).data || [],
                 series: option.series.map(s => ({ name: s.name, type: s.type, data: (s.data || []).map(value) })),
                 save: !!(option.toolbox && option.toolbox[0].feature.saveAsImage),
                 zoom: (option.dataZoom || []).map(z => z.type) };
    }""", name)


def click_bar(page, name, category, value):
    """
    在图上找到某个类目的柱子，用鼠标真的点一下。value 是这根柱子的数值，点在柱子的中间。
    图刚画出来或刚更新时柱子有一段“长高”的动画，所以要反复找，直到那个位置上真的有柱子；
    找到以后再等动画完全停下来，重新确认一次位置才点，免得点的时候柱子还在动
    """
    find = """([name, category, value]) => {
        const chart = window.analysisCharts[name];
        const option = chart.getOption();
        const horizontal = option.yAxis[0].type === "category";
        const axis = horizontal ? option.yAxis[0].data : option.xAxis[0].data;
        const at = (i, v) => chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, horizontal ? [v, i] : [i, v]);
        const band = Math.abs(horizontal ? at(1, 0)[1] - at(0, 0)[1] : at(1, 0)[0] - at(0, 0)[0]);
        chart.getDom().scrollIntoView({ block: "center", behavior: "instant" });   // 不要滚动动画，位置马上算得准
        for (const shift of [-0.2, 0.2, -0.1, 0.1, 0]) {         // 一个类目里并排两根柱子，逐个位置找点得中的
            const [x, y] = at(axis.indexOf(category), value / 2);
            const px = horizontal ? x : x + shift * band, py = horizontal ? y + shift * band : y;
            const hover = chart.getZr().findHover(px, py);
            if (hover && hover.target) {
                const box = chart.getDom().getBoundingClientRect();
                return [box.left + px, box.top + py];
            }
        }
        return null;
    }"""
    page.wait_for_function(find, arg=[name, category, value])
    page.wait_for_timeout(500)
    spot = page.wait_for_function(find, arg=[name, category, value]).json_value()
    page.mouse.click(*spot)


def trend_total(page):
    return sum(analysis_option(page, "trend")["series"][1]["data"])


def monthly_quantity(where="1 = 1"):
    return int(db_value(f"SELECT SUM(quantity) FROM monthly_sales WHERE {where}"))


def test_analysis_all_charts_render(page):
    """8 张图都画出来了，每张都有“保存为图片”按钮，数字和数据库一致"""
    open_analysis(page)
    for name in ANALYSIS_CHARTS:
        expect(page.locator(f"#chart_{name} canvas")).to_be_visible()
        option = analysis_option(page, name)
        assert option["save"], name
        assert option["series"] and all(len(s["data"]) > 0 for s in option["series"]), name
    trend = analysis_option(page, "trend")
    assert len(trend["x"]) == 48 and sum(trend["series"][1]["data"]) == 37534
    assert sorted(trend["zoom"]) == ["inside", "slider"]
    assert [s["type"] for s in trend["series"]] == ["bar", "line"]           # 销售额是柱子，销量是折线，各用一边的刻度
    assert analysis_option(page, "season")["series"][0]["data"] == [5.2, 4.5, 5.8, 5.2, 10.2, 10.5, 5.2, 10.6,
                                                                      10.0, 10.9, 10.9, 11.0]
    assert [s["name"] for s in analysis_option(page, "yoy")["series"]] == ["2021 年", "2022 年", "2023 年", "2024 年"]
    assert analysis_option(page, "annual")["x"] == ["2021 年", "2022 年", "2023 年", "2024 年"]
    assert analysis_option(page, "region")["x"] == ["华东", "中南", "东北", "华北", "西南", "西北"]
    sub = analysis_option(page, "subcategory")
    assert len(sub["y"]) == 17 and min(sub["series"][1]["data"]) < 0         # 有亏损的子类别
    assert len(analysis_option(page, "heatmap")["series"][0]["data"]) == 102
    assert len(analysis_option(page, "quadrant")["series"][0]["data"]) == 17
    expect(page.locator("#quadrantTable tbody tr")).to_have_count(4)
    expect(page.locator("#quadrantTable tbody")).to_contain_text("重点保障")
    expect(page.locator("#quadrantTable tbody")).to_contain_text("减少进货")
    expect(page.locator("#clearLink")).to_be_disabled()
    expect(page.locator("#staleBanner")).to_have_count(0)


def test_analysis_loss_bars_are_red_and_labels_do_not_overlap(page):
    open_analysis(page)
    colors = page.evaluate("""() => {
        const option = window.analysisCharts.subcategory.getOption();
        const names = option.yAxis[0].data;
        return Object.fromEntries(option.series[1].data.map((d, i) => [names[i], d.itemStyle.color]));
    }""")
    assert colors["桌子"] == colors["美术"] == "#c0392b" and colors["书架"] == "#2e8b57"
    # 四象限上 17 个子类别的名字，两两不重叠
    boxes = page.evaluate("""() => {
        const chart = window.analysisCharts.quadrant, data = chart.getOption().series[0].data;
        return data.map(d => {
            const [x, y] = chart.convertToPixel({ seriesIndex: 0 }, [d.value[0], d.value[1]]);
            const left = x - 7 + d.label.position[0], top = y - 7 + d.label.position[1];
            return [left, top, left + d.value[2].length * 13, top + 14];
        });
    }""")
    assert len(boxes) == 17
    overlap = [(i, k) for i in range(17) for k in range(i + 1, 17)
               if boxes[i][0] < boxes[k][2] and boxes[k][0] < boxes[i][2]
               and boxes[i][1] < boxes[k][3] and boxes[k][1] < boxes[i][3]]
    assert overlap == []


def test_analysis_filters_change_charts(page):
    open_analysis(page)
    page.select_option("#end", "2022")
    page.select_option("#start", "2023")                                    # 起始年比结束年晚：结束年跟着变
    expect(page.locator("#end")).to_have_value("2023")
    page.select_option("#end", "2024")
    page.select_option("#region", "华东")
    page.select_option("#category", "家具")
    page.click("#applyBtn")
    wait_charts(page)
    assert "start=2023" in page.url and "end=2024" in page.url
    expect(page.locator("#region")).to_have_value("华东")
    trend = analysis_option(page, "trend")
    assert len(trend["x"]) == 24 and trend["x"][0] == "2023-01"
    assert sum(trend["series"][1]["data"]) == monthly_quantity(
        "region = '华东' AND category = '家具' AND ym >= '2023-01'")
    assert analysis_option(page, "region")["x"] == ["华东"]
    assert sorted(analysis_option(page, "subcategory")["y"]) == sorted(["书架", "桌子", "椅子", "用具"])
    assert [s["name"] for s in analysis_option(page, "yoy")["series"]] == ["2023 年", "2024 年"]
    assert len(analysis_option(page, "quadrant")["series"][0]["data"]) == 4


def test_analysis_click_region_links_other_charts(page):
    """点“地区对比”里华东的柱子：其他图只看华东；再点一次取消"""
    open_analysis(page)
    assert trend_total(page) == 37534
    east_sales = analysis_option(page, "region")["series"][0]["data"][0]
    click_bar(page, "region", "华东", east_sales)
    expect(page.locator("#linkStatus")).to_have_text("当前联动：地区 = 华东")
    expect(page.locator("#clearLink")).to_be_enabled()
    wait_charts(page)
    assert trend_total(page) == monthly_quantity("region = '华东'") < 37534
    assert analysis_option(page, "heatmap")["y"] == ["华东"]
    assert sum(analysis_option(page, "season")["series"][0]["data"]) == pytest.approx(100, abs=0.5)
    assert len(analysis_option(page, "region")["x"]) == 6                    # 地区对比自己仍然显示 6 个地区
    opacity = page.evaluate("window.analysisCharts.region.getOption().series[0].data.map(d => d.itemStyle.opacity)")
    assert opacity == [1, 0.3, 0.3, 0.3, 0.3, 0.3]                           # 华东正常，其他变淡
    expect(page.locator("#region")).to_have_value("")                       # 上面的筛选条件没有被改

    click_bar(page, "region", "华东", east_sales)                           # 再点一次：取消
    expect(page.locator("#linkStatus")).to_contain_text("点“地区对比”里的柱子")
    expect(page.locator("#clearLink")).to_be_disabled()
    wait_charts(page)
    assert trend_total(page) == 37534 and len(analysis_option(page, "heatmap")["y"]) == 6


def test_analysis_clear_link_button(page):
    open_analysis(page)
    region = analysis_option(page, "region")
    click_bar(page, "region", "中南", region["series"][0]["data"][1])
    expect(page.locator("#linkStatus")).to_have_text("当前联动：地区 = 中南")
    wait_charts(page)
    click_bar(page, "region", "西北", region["series"][0]["data"][5])        # 换一个地区：直接换过去
    expect(page.locator("#linkStatus")).to_have_text("当前联动：地区 = 西北")
    wait_charts(page)
    assert trend_total(page) == monthly_quantity("region = '西北'")
    page.click("#clearLink")
    expect(page.locator("#clearLink")).to_be_disabled()
    wait_charts(page)
    assert trend_total(page) == 37534
    opacity = page.evaluate("window.analysisCharts.region.getOption().series[0].data.map(d => d.itemStyle.opacity)")
    assert opacity == [1] * 6


def test_analysis_click_subcategory_links_category(page):
    """点“品类贡献”里桌子的柱子：其他图只看家具这个类别"""
    open_analysis(page)
    sub = analysis_option(page, "subcategory")
    click_bar(page, "subcategory", "桌子", sub["series"][0]["data"][sub["y"].index("桌子")])
    expect(page.locator("#linkStatus")).to_have_text("当前联动：类别 = 家具")
    wait_charts(page)
    assert trend_total(page) == monthly_quantity("category = '家具'")
    assert len(analysis_option(page, "quadrant")["series"][0]["data"]) == 4
    assert len(analysis_option(page, "heatmap")["x"]) == 4
    assert len(analysis_option(page, "subcategory")["y"]) == 17              # 品类贡献自己仍然显示 17 个
    east = analysis_option(page, "region")["series"][0]["data"][0]
    click_bar(page, "region", "华东", east)                                 # 地区和类别可以同时联动
    expect(page.locator("#linkStatus")).to_have_text("当前联动：地区 = 华东，类别 = 家具")
    wait_charts(page)
    assert trend_total(page) == monthly_quantity("region = '华东' AND category = '家具'")
    page.click("#clearLink")
    wait_charts(page)
    assert trend_total(page) == 37534


def test_analysis_save_chart_as_image(page):
    """每张图都能导出成图片；点右上角的按钮会下载一个 png 文件"""
    open_analysis(page)
    sizes = page.evaluate("""() => Object.fromEntries(Object.entries(window.analysisCharts).map(
        ([name, chart]) => [name, chart.getDataURL({ type: "png", pixelRatio: 2 }).length]))""")
    assert set(sizes) == set(ANALYSIS_CHARTS) and all(n > 5000 for n in sizes.values())
    page.locator("#chart_annual").scroll_into_view_if_needed()
    box = page.locator("#chart_annual").bounding_box()
    with page.expect_download() as download:
        page.mouse.click(box["x"] + box["width"] - 16, box["y"] + 13)       # 右上角的保存按钮
    assert download.value.suggested_filename == "年度增长.png"


def test_analysis_banner_when_year_needs_processing(page):
    """录入一条 2023 年的订单后，分析页顶部出现提示；点按钮去数据处理页"""
    open_dialog(page)
    fill_order(page, order_date="2023-06-01", ship_date="2023-06-02")
    page.click("#orderSave")
    expect_saved(page)
    open_analysis(page)
    expect(page.locator("#staleBanner")).to_contain_text("部分年份数据已更新但尚未处理（2023 年）")
    open_analysis(page, "?start=2021&end=2022")                             # 所选年份里没有 2023：不提示
    expect(page.locator("#staleBanner")).to_have_count(0)
    open_analysis(page)
    page.click("#staleBanner a")
    expect(page).to_have_url(re.compile(r"/process$"))
    expect(page.locator("#yearTable tr[data-year='2023'] .badge")).to_have_text("数据有更新，需重新处理")


# ---------------- 数据预测、预测结果 ----------------
def open_forecast_dialog(page):
    page.goto("/forecast")
    page.click("#newForecastBtn")
    expect(page.locator("#forecastModal")).to_have_class(re.compile(r"\bshow\b"))


def start_forecast(page, target="2025", start=None, region="华东", category="办公用品"):
    """在对话框里选好设置，点“开始预测”"""
    open_forecast_dialog(page)
    page.select_option("#target_year", target)
    if start:
        page.select_option("#history_start", start)
    page.select_option("#f_region", region)
    page.select_option("#f_category", category)
    page.click("#startBtn")


def wait_result(page):
    """等后台算完、结果页把图和表画出来"""
    expect(page.locator("#forecastChart canvas")).to_be_visible(timeout=60000)
    page.wait_for_function("document.body.dataset.ready === '1'")


def forecast_series_names(page):
    return page.evaluate("window.forecastChart.getOption().series.map(s => s.name)")


def table_rows(page):
    return [row.locator("td").all_inner_texts() for row in page.locator("#forecastTable tbody tr").all()]


def wait_no_running_task():
    """上一个测试的后台计算可能还没结束，等它结束再开始下一个"""
    for _ in range(300):
        if db_value("SELECT COUNT(*) FROM forecast_task WHERE status = '运行中'") == 0:
            return
        time.sleep(0.2)


@pytest.fixture
def forecast_page(page):
    """预测相关的测试用：开始前清空预测记录，结束后也清空"""
    wait_no_running_task()
    run_sql(["DELETE FROM forecast_detail", "DELETE FROM forecast_task"])
    yield page
    wait_no_running_task()
    run_sql(["DELETE FROM forecast_detail", "DELETE FROM forecast_task"])


def test_forecast_dialog_options(forecast_page):
    """对话框：先选预测年份，历史起始年跟着变；子类别、库存按规则出现和置灰"""
    page = forecast_page
    open_forecast_dialog(page)
    expect(page.locator("#target_year option")).to_have_text(["2024", "2025"])
    expect(page.locator("#target_year")).to_have_value("2025")                       # 默认最晚的一年
    expect(page.locator("#history_start option")).to_have_text(["2021", "2022"])
    expect(page.locator("#historyText")).to_have_value("2021–2024 年（4 年）")
    page.select_option("#history_start", "2022")
    expect(page.locator("#historyText")).to_have_value("2022–2024 年（3 年）")
    page.select_option("#target_year", "2024")                                       # 预测 2024：历史起始年只有 2021
    expect(page.locator("#history_start option")).to_have_text(["2021"])
    expect(page.locator("#historyText")).to_have_value("2021–2023 年（3 年）")

    # 库存：地区、类别都选了具体的才可以填
    inventory = page.locator("#f_inventory")
    expect(inventory).to_be_disabled()
    expect(page.locator("#seriesHint")).to_contain_text("18 条")
    page.select_option("#f_region", "华东")
    expect(inventory).to_be_disabled()
    page.select_option("#f_category", "办公用品")
    expect(inventory).to_be_enabled()
    expect(page.locator("#seriesHint")).to_contain_text("1 条")
    inventory.fill("25")
    # 子类别：层级选“子类别”而且选了具体类别才出现；选“全部”子类别时库存又不能填了
    expect(page.locator("#subBox")).to_be_hidden()
    page.select_option("#f_level", "子类别")
    expect(page.locator("#subBox")).to_be_visible()
    expect(page.locator("#f_sub option")).to_have_count(10)                          # 全部 + 9 个子类别
    expect(inventory).to_be_disabled()
    expect(inventory).to_have_value("0")
    page.select_option("#f_sub", "纸张")
    expect(inventory).to_be_enabled()
    page.select_option("#f_category", "全部")                                        # 类别改回全部：子类别下拉框消失
    expect(page.locator("#subBox")).to_be_hidden()
    expect(inventory).to_be_disabled()
    expect(page.locator("#f_lead option")).to_have_count(3)
    expect(page.locator("#f_service")).to_have_value("0.95")


def test_forecast_flow_2025(forecast_page):
    """完整流程：2022–2024 预测 2025，华东、办公用品。能看到进度，算完显示图和表，能导出"""
    page = forecast_page
    start_forecast(page, "2025", "2022")
    expect(page).to_have_url(re.compile(r"/forecast/result/\d+$"))
    expect(page.locator("#progressText")).to_have_text(re.compile(r"已完成 \d+ / 1"))   # 进度
    expect(page.locator("#progressBar")).to_be_attached()
    wait_result(page)
    expect(page.locator("#settings")).to_contain_text("2022–2024 年")
    expect(page.locator("#settings")).to_contain_text("华东")
    names = forecast_series_names(page)
    assert names == ["历史销量", "区间下限", "95% 预测区间", "预测销量", "预测销量 + 安全库存"]   # 没有实际销量这条线
    info = page.evaluate("""() => {
        const o = window.forecastChart.getOption();
        return { months: o.xAxis[0].data, zoom: o.dataZoom.map(z => z.type), save: !!o.toolbox[0].feature.saveAsImage,
                 dashed: o.series[3].lineStyle.type, dotted: o.series[4].lineStyle.type, solid: o.series[0].lineStyle.type,
                 band: !!o.series[2].areaStyle, forecast: o.series[3].data, history: o.series[0].data };
    }""")
    assert len(info["months"]) == 48 and info["months"][0] == "2022-01" and info["months"][-1] == "2025-12"
    assert sorted(info["zoom"]) == ["inside", "slider"] and info["save"] and info["band"]
    assert (info["solid"], info["dashed"], info["dotted"]) == ("solid", "dashed", "dotted")
    assert info["history"][36:] == [None] * 12 and all(v is not None for v in info["forecast"][36:])

    rows = table_rows(page)
    assert len(rows) == 12 and rows[0][0] == "2025-01" and rows[11][0] == "2025-12"
    assert all(r[4] == "—" for r in rows)                                            # 2025 年没有实际销量
    task_id = int(page.url.rsplit("/", 1)[1])
    assert db_value(f"SELECT status FROM forecast_task WHERE id = {task_id}") == "已完成"
    assert db_value(f"SELECT COUNT(*) FROM forecast_detail WHERE task_id = {task_id}") == 12
    first = db_value(f"SELECT CONCAT(forecast_qty, '|', suggested_purchase) FROM forecast_detail "
                     f"WHERE task_id = {task_id} AND ym = '2025-01'")
    assert f"{rows[0][1]}|{rows[0][6]}" == first                                     # 表里的数和数据库一致
    expect(page.locator("#sumPurchase")).to_have_text(str(sum(int(r[6]) for r in rows)))
    expect(page.locator("#purchaseText")).to_contain_text("补货周期 1 个月")
    with page.expect_download() as download:
        page.click("#exportBtn")
    assert download.value.suggested_filename == f"预测结果_2025年_任务{task_id}.xlsx"


def test_forecast_2024_shows_actual_line(forecast_page):
    """2021–2023 预测 2024：图上多一条实际销量的线，表里有实际销量"""
    page = forecast_page
    start_forecast(page, "2024")
    wait_result(page)
    assert forecast_series_names(page)[-1] == "实际销量"
    actual = page.evaluate("window.forecastChart.getOption().series[5].data.slice(36)")
    want = [db_value(f"SELECT quantity FROM monthly_sales_category WHERE region = '华东' AND category = '办公用品' "
                     f"AND ym = '2024-{m:02d}'") for m in range(1, 13)]
    assert actual == want
    rows = table_rows(page)
    assert [int(r[4]) for r in rows] == want
    expect(page.locator("#sumActual")).to_have_text(str(sum(want)))
    expect(page.locator("#actualNote")).to_have_count(0)


def test_forecast_refused_when_year_not_processed(forecast_page):
    """录入一条 2023 年的订单后去预测：对话框里提示先处理，并给出“去数据处理”的按钮"""
    page = forecast_page
    open_dialog(page)
    fill_order(page, order_date="2023-06-01", ship_date="2023-06-02")
    page.click("#orderSave")
    expect_saved(page)
    start_forecast(page, "2025", "2022")
    expect(page.locator("#forecastError")).to_be_visible()
    expect(page.locator("#forecastErrorText")).to_have_text("所选年份的数据有更新或尚未处理，请先到【数据处理】处理 2023–2023 年")
    expect(page.locator("#forecastModal")).to_be_visible()
    expect(page.locator("#startBtn")).to_be_enabled()
    expect(page.locator("#goRunning")).to_be_hidden()
    assert db_value("SELECT COUNT(*) FROM forecast_task") == 0
    page.click("#goProcess")
    expect(page).to_have_url(re.compile(r"/process$"))
    expect(page.locator("#yearTable tr[data-year='2023'] .badge")).to_have_text("数据有更新，需重新处理")


def test_forecast_reuse_and_recalculate(forecast_page):
    """同样的设置再提交一次：直接显示上次的结果并说明；点“重新计算”才真的再算一遍"""
    page = forecast_page
    start_forecast(page, "2025", "2022")
    wait_result(page)
    first = int(page.url.rsplit("/", 1)[1])
    start_forecast(page, "2025", "2022")
    expect(page).to_have_url(re.compile(rf"/forecast/result/{first}\?reused=1$"))
    wait_result(page)
    expect(page.locator("#reusedNote")).to_contain_text("数据未变化，显示")
    expect(page.locator("#reusedNote")).to_contain_text("日的结果")
    assert db_value("SELECT COUNT(*) FROM forecast_task") == 1
    page.click("#rerunBtn")
    expect(page).not_to_have_url(re.compile(rf"/forecast/result/{first}"))
    wait_result(page)
    expect(page.locator("#reusedNote")).to_have_count(0)
    assert db_value("SELECT COUNT(*) FROM forecast_task WHERE status = '已完成'") == 2


def test_forecast_one_task_at_a_time(forecast_page):
    """已经有任务在运行：再提交会在对话框里提示，并能点过去看那个任务的进度"""
    page = forecast_page
    run_sql(["INSERT INTO forecast_task (history_start, history_end, target_year, region, category, level_name, "
             "status, progress_done, progress_total, user_id) VALUES (2021, 2024, 2025, '全部', '全部', '类别', "
             "'运行中', 5, 18, 1)"])
    running = db_value("SELECT id FROM forecast_task")
    try:
        page.goto("/forecast")
        expect(page.locator("#runningNote")).to_contain_text(f"任务 {running}")
        start_forecast(page, "2025", "2022")
        expect(page.locator("#forecastErrorText")).to_contain_text(f"已有一个预测任务正在运行（任务 {running}）")
        expect(page.locator("#goProcess")).to_be_hidden()
        assert db_value("SELECT COUNT(*) FROM forecast_task") == 1
        page.click("#goRunning")
        expect(page).to_have_url(re.compile(rf"/forecast/result/{running}$"))
        expect(page.locator("#progressText")).to_have_text("已完成 5 / 18")
        expect(page.locator("#progressBar")).to_have_attribute("style", re.compile(r"width: 28%"))
        # 任务结束后（这里直接改成失败来模拟），进度页会自己发现并换成结果
        run_sql([f"UPDATE forecast_task SET status = '失败', error_msg = '系统重启中断' WHERE id = {running}"])
        expect(page.locator("#failedReason")).to_have_text("系统重启中断", timeout=10000)
    finally:
        run_sql([f"UPDATE forecast_task SET status = '失败' WHERE id = {running}"])


def test_forecast_failure_shows_reason(forecast_page, monkeypatch):
    """计算失败：结果页写明原因，可以用同样的设置再试"""
    from app.services import forecast_service
    page = forecast_page
    monkeypatch.setattr(forecast_service, "forecast_series",
                        lambda job: {"region": job["region"], "category": job["category"],
                                     "error": "RuntimeError: 所有候选阶数都拟合失败"})
    start_forecast(page, "2025", "2022")
    expect(page.locator("#failedReason")).to_contain_text("华东-办公用品 预测失败：RuntimeError: 所有候选阶数都拟合失败",
                                                           timeout=30000)
    expect(page.locator("#forecastChart")).to_have_count(0)
    monkeypatch.undo()                                                               # 恢复正常以后再试一次：成功
    page.click("#rerunBtn")
    wait_result(page)
    assert db_value("SELECT COUNT(*) FROM forecast_task WHERE status = '已完成'") == 1


def test_forecast_switch_series(forecast_page):
    """子类别层级的结果：切换类别时子类别下拉框跟着变，图和表换成另一条序列"""
    from app.services import forecast_service
    page = forecast_page
    with SITE["app"].app_context():                                                  # 直接让服务算好一个任务，再去看结果页
        task_id = forecast_service.create_task({"target_year": 2025, "history_start": 2021, "region": "华东",
                                                "category": "全部", "level": "子类别", "lead_time": 3}, 1)
        forecast_service.run_task(task_id)
    page.goto(f"/forecast/result/{task_id}")
    wait_result(page)
    expect(page.locator("#s_region option")).to_have_text(["华东"])
    expect(page.locator("#s_sub option")).to_have_count(9)
    expect(page.locator("#tableTitle")).to_have_text("华东 · 办公用品 · 信封：2025 年每月预测")
    page.select_option("#s_category", "家具")
    expect(page.locator("#s_sub option")).to_have_text(["书架", "桌子", "椅子", "用具"])
    page.select_option("#s_sub", "椅子")
    expect(page.locator("#tableTitle")).to_have_text("华东 · 家具 · 椅子：2025 年每月预测")
    page.wait_for_function("document.body.dataset.ready === '1'")
    title = page.evaluate("window.forecastChart.getOption().title[0].text")
    assert title.startswith("华东 · 家具 · 椅子")
    rows = table_rows(page)
    purchase = [int(r[6]) for r in rows]
    assert [i for i, q in enumerate(purchase) if q > 0] == [0, 3, 6, 9]              # 补货周期 3 个月：只有 1、4、7、10 月进货
    want = db_value(f"SELECT SUM(suggested_purchase) FROM forecast_detail WHERE task_id = {task_id} "
                    "AND sub_category = '椅子'")
    assert sum(purchase) == int(want)
    expect(page.locator("#purchaseText")).to_contain_text("每 3 个月进一次货")


def test_forecast_history_and_delete(forecast_page):
    """历史记录：能看到做过的预测，能点进去看；删除要先确认"""
    page = forecast_page
    start_forecast(page, "2025", "2022")
    wait_result(page)
    task_id = int(page.url.rsplit("/", 1)[1])
    page.goto("/forecast/history")
    row = page.locator(f"#taskTable tr[data-task='{task_id}']")
    expect(row).to_contain_text("2022–2024")
    expect(row.locator(".badge")).to_have_text("已完成")
    row.locator(".view-link").click()
    expect(page).to_have_url(re.compile(rf"/forecast/result/{task_id}$"))
    page.goto("/forecast/history")
    page.click(".delete-btn")
    modal = page.locator("#deleteModal")
    expect(modal).to_be_visible()
    expect(page.locator("#deleteText")).to_contain_text(f"将删除任务 {task_id}")
    modal.locator(".btn-secondary").click()                                          # 取消：记录还在
    expect(modal).to_be_hidden()
    assert db_value("SELECT COUNT(*) FROM forecast_task") == 1
    page.click(".delete-btn")
    expect(modal).to_have_class(re.compile(r"\bshow\b"))
    page.click("#deleteOk")
    expect(page.locator(".alert-success")).to_contain_text(f"预测记录 {task_id} 已删除")
    expect(page.locator("#taskTable")).to_contain_text("还没有预测记录")
    assert db_value("SELECT COUNT(*) FROM forecast_task") == 0
    assert db_value("SELECT COUNT(*) FROM forecast_detail") == 0


def test_forecast_lower_bound_note(forecast_page):
    """区间下限有 0 的序列，表格下面出现“下限按 0 显示（销量不能为负）”；换到没有 0 的序列，这句话消失"""
    from app.services import forecast_service
    page = forecast_page
    with SITE["app"].app_context():
        task_id = forecast_service.create_task({"target_year": 2025, "history_start": 2021, "region": "全部",
                                                "category": "全部", "level": "类别"}, 1)
        forecast_service.run_task(task_id)
    lowest = {}
    for region in ("华东", "中南", "东北", "华北", "西南", "西北"):
        for category in ("办公用品", "技术", "家具"):
            lowest[(region, category)] = float(db_value(
                f"SELECT MIN(lower_qty) FROM forecast_detail WHERE task_id = {task_id} "
                f"AND region = '{region}' AND category = '{category}'"))
    zero = next(k for k, v in lowest.items() if v == 0)
    positive = next(k for k, v in lowest.items() if v > 0)

    page.goto(f"/forecast/result/{task_id}?region={zero[0]}&category={zero[1]}")
    wait_result(page)
    expect(page.locator("#lowerNote")).to_be_visible()
    expect(page.locator("#lowerNote")).to_have_text("区间下限按 0 显示（销量不能为负）。")
    rows = table_rows(page)
    assert "0.00" in [r[2] for r in rows] and all(float(r[2]) >= 0 and float(r[1]) >= 0 and int(r[6]) >= 0 for r in rows)
    lows = page.evaluate("window.forecastChart.getOption().series[1].data.filter(v => v !== null)")
    assert min(lows) == 0 and len(lows) == 12                                        # 图上的下限也是从 0 起

    page.select_option("#s_region", positive[0])
    page.select_option("#s_category", positive[1])
    expect(page.locator("#tableTitle")).to_contain_text(f"{positive[0]} · {positive[1]}")
    page.wait_for_function("document.body.dataset.ready === '1'")
    expect(page.locator("#lowerNote")).to_be_hidden()
    assert all(float(r[2]) > 0 for r in table_rows(page))
