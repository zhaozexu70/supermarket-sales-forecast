# -*- coding: utf-8 -*-
"""Flask 骨架：能启动、各页面能打开、本地的 Bootstrap 和 ECharts 能访问、错误页是中文、CSRF 保护生效"""
import re
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

from app import NAV_ITEMS, constants
from conftest import log_in
from config import RAW_FILE
from step3_load_mysql import ORDER_COLS, MONTHLY_COLS

APP_DIR = Path(__file__).resolve().parent.parent / "app"
PAGES = ["/", "/data", "/process", "/analysis", "/forecast", "/forecast/history"]
VENDOR = {"/static/vendor/bootstrap/css/bootstrap.min.css": "Bootstrap  v5.3.3",
          "/static/vendor/bootstrap/js/bootstrap.bundle.min.js": "Bootstrap v5.3.3",
          "/static/vendor/echarts/echarts.min.js": 'version="5.5.1"'}


def test_app_starts(app, db_name):
    assert app.config["DB_NAME"] == db_name
    with app.app_context():
        from app.db import fetch_one, get_engine
        assert fetch_one("SELECT DATABASE() AS name")["name"] == db_name     # 连的是测试库
        with get_engine().connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1


@pytest.mark.parametrize("url", PAGES)
def test_pages_open(client, url):
    resp = client.get(url)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'name="csrf-token"' in html
    for name, _ in NAV_ITEMS:                    # 每个页面都有完整的导航栏
        assert name in html


def test_nav_order(client):
    """导航栏按老师确认的模块顺序排列"""
    html = client.get("/").get_data(as_text=True)
    names = ["首页", "数据导入和展示", "数据处理和展示", "销售分析", "数据预测", "预测结果"]
    assert [n for n, _ in NAV_ITEMS] == names
    positions = [html.index(f">{n}</a>") for n in names]
    assert positions == sorted(positions)


@pytest.mark.parametrize("url", VENDOR)
def test_vendor_files(client, url):
    resp = client.get(url)
    assert resp.status_code == 200
    assert VENDOR[url] in resp.get_data(as_text=True)


def test_no_external_urls(client):
    """模板、自己写的 css/js、以及实际输出的页面里都不能引用外网地址"""
    own_files = [*APP_DIR.glob("templates/**/*.html"), *APP_DIR.glob("static/css/*"),
                 *APP_DIR.glob("static/js/*")]
    texts = [p.read_text(encoding="utf-8") for p in own_files]
    texts += [client.get(u).get_data(as_text=True) for u in PAGES]
    assert len(own_files) >= 5
    for t in texts:
        assert not re.search(r"https?://|src=\"//|href=\"//", t)


def test_404_page(client):
    resp = client.get("/没有这个页面")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 404
    assert "页面不存在" in html and "回到首页" in html


def test_500_page(app):
    app.config["PROPAGATE_EXCEPTIONS"] = False       # 测试模式默认直接抛出异常，这里改成走错误页
    app.add_url_rule("/_boom", "boom", lambda: 1 / 0)
    resp = log_in(app.test_client()).get("/_boom")
    assert resp.status_code == 500
    assert "系统出错了" in resp.get_data(as_text=True)


def test_csrf_protection(app):
    """POST 不带令牌被拒绝（400），带上页面里的令牌就能通过"""
    app.add_url_rule("/_post", "post_demo", lambda: "ok", methods=["POST"])
    client = log_in(app.test_client())
    resp = client.post("/_post")
    assert resp.status_code == 400
    assert "页面已过期" in resp.get_data(as_text=True)

    html = client.get("/").get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    assert client.post("/_post", headers={"X-CSRFToken": token}).status_code == 200


def test_log_file(client):
    client.get("/")
    assert (APP_DIR.parent / "logs" / "app.log").exists()


def test_constants_match_raw_data():
    """constants.py 里写的取值必须和原始数据里实际出现的一致"""
    df = pd.read_excel(RAW_FILE)
    assert list(df.columns) == constants.TEMPLATE_COLUMNS
    assert set(df["地区"]) == set(constants.REGIONS) and len(constants.REGIONS) == 6
    subs = {c: set(g["子类别"]) for c, g in df.groupby("类别")}
    assert subs == {c: set(v) for c, v in constants.CATEGORY_SUBS.items()}
    assert sum(len(v) for v in constants.CATEGORY_SUBS.values()) == 17
    assert set(df["邮寄方式"]) == set(constants.SHIP_MODES)
    assert set(df["细分"]) == set(constants.SEGMENTS)
    assert set(df["省/自治区"]) == set(constants.PROVINCES) and len(constants.PROVINCES) == 31
    assert set(df["国家"]) == {constants.COUNTRY}


def test_column_mapping():
    """字段对照与 step3 导入时用的对照一致，多出来的只有两个新字段"""
    extra = {"source": "数据来源", "batch_id": "导入批次"}
    assert constants.ORDER_COLUMNS == {**ORDER_COLS, **extra}
    assert constants.MONTHLY_COLUMNS == MONTHLY_COLS
    assert constants.ORDER_COLUMNS_CN["销售额"] == "sales"
    assert len(constants.ORDER_COLUMNS_CN) == len(constants.ORDER_COLUMNS) == 25
