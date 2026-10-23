# -*- coding: utf-8 -*-
"""
测试的公共部分。所有测试只用测试库（config.py 里的 TEST_DB_NAME），不碰正式库。
运行：在项目根目录下执行  python -m pytest tests -v
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))                 # 让测试能导入 app 包

import time                                # noqa: E402

import pymysql                             # noqa: E402

from config import TEST_DB_NAME            # noqa: E402
from step3_load_mysql import connect       # noqa: E402

_real_connect = pymysql.connect


def _connect_with_retry(*args, **kwargs):
    """
    测试会在很短的时间里开关上千次数据库连接。Windows 关掉的连接要过一会儿才真正释放，
    偶尔会一时周转不过来，报“连不上数据库”（错误号 2003）。碰上了就等 1 秒再连，最多试 3 次。
    只在测试里这样做；别的错误（比如密码不对、库不存在）照常马上报出来
    """
    for attempt in range(3):
        try:
            return _real_connect(*args, **kwargs)
        except pymysql.err.OperationalError as e:
            if e.args[0] != 2003 or attempt == 2:
                raise
            time.sleep(1)


pymysql.connect = _connect_with_retry


def require_test_db(name):
    """保险：库名不是以 _test 结尾就拒绝，防止配置写错时改到正式库"""
    if not name.endswith("_test"):
        raise RuntimeError(f"拒绝运行：{name} 不是测试库（库名必须以 _test 结尾）")
    return name


def pytest_sessionstart(session):
    require_test_db(TEST_DB_NAME)


@pytest.fixture
def db_name():
    return require_test_db(TEST_DB_NAME)


@pytest.fixture
def empty_db(db_name):
    """把测试库删掉重建，得到一个空库"""
    conn = connect(require_test_db(db_name), create=True)
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE `{db_name}`")
    conn.close()
    connect(db_name, create=True).close()
    return db_name


@pytest.fixture
def query(db_name):
    """query(sql, 参数) -> 查询结果的所有行"""
    def run(sql, args=None):
        conn = connect(require_test_db(db_name))
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                rows = cur.fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()
    return run


@pytest.fixture
def app(db_name):
    """连接测试库的 Flask 应用"""
    from app import create_app
    from migrate_v2 import add_missing_columns
    from step3_load_mysql import run_schema
    conn = connect(db_name, create=True)           # 测试库不存在时先建；保证表和字段齐全
    with conn.cursor() as cur:
        run_schema(cur)
        add_missing_columns(cur)
        cur.execute("DELETE FROM sys_user")        # 每个测试从没有用户开始
    conn.commit()
    conn.close()
    app = create_app(db_name=db_name)
    app.config["TESTING"] = True
    return app


def run_sql(statements):
    """在测试库里依次执行几条 SQL"""
    conn = connect(require_test_db(TEST_DB_NAME))
    with conn.cursor() as cur:
        for sql in statements:
            cur.execute(sql)
    conn.commit()
    conn.close()


def reset_imported_data():
    """删掉导入的订单和批次，年份状态、月度表回到只有原始数据时的样子"""
    run_sql(["DELETE FROM sales_order WHERE source <> '原始数据'",
             "DELETE FROM import_batch",
             "DELETE FROM year_state",
             "INSERT INTO year_state (`year`, data_version, processed_version) VALUES "
             "(2021, 1, 1), (2022, 1, 1), (2023, 1, 1), (2024, 1, 1)",
             "DELETE FROM monthly_sales WHERE ym > '2024-12'",
             "DELETE FROM monthly_sales_category WHERE ym > '2024-12'"])


def log_in(client, user_id=1, username="tester"):
    """让测试用的浏览器直接处于已登录状态（不经过登录页）"""
    with client.session_transaction() as sess:
        sess["user_id"], sess["username"] = user_id, username
    return client


@pytest.fixture
def anon_client(app):
    """没登录的浏览器"""
    return app.test_client()


@pytest.fixture
def client(app):
    """已登录的浏览器：client.get("/") 相当于登录后在浏览器里打开首页"""
    return log_in(app.test_client())
