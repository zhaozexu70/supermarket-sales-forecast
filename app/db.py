# -*- coding: utf-8 -*-
"""数据库连接和常用查询函数。只用 SQLAlchemy 建连接、执行 SQL，不用 ORM"""
import threading
from contextlib import contextmanager

from flask import current_app
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


def make_engine(db_config, db_name):
    """按 config.py 里的账号和指定的库名建立连接池"""
    url = URL.create("mysql+pymysql", username=db_config["user"], password=db_config["password"],
                     host=db_config["host"], port=db_config["port"], database=db_name,
                     query={"charset": db_config["charset"]})
    return create_engine(url, pool_pre_ping=True, pool_recycle=3600)


def get_engine():
    return current_app.extensions["db_engine"]


def fetch_all(sql, params=None):
    """查询多行，每行是一个字典。SQL 里的参数写成 :name，值放在 params 里（参数绑定，不拼接字符串）"""
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params or {}).mappings()]


def fetch_one(sql, params=None):
    """查询一行，没有结果时返回 None"""
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    """执行一条增、删、改语句并提交，返回影响的行数"""
    with get_engine().begin() as conn:
        return conn.execute(text(sql), params or {}).rowcount


WRITE_LOCK = threading.Lock()


@contextmanager
def write_transaction():
    """
    改订单数据、重新生成月度表时用的事务：中途出错就全部不写。
    外面再加一把锁，同一时间只让一个导入、录入、撤销或数据处理在进行。不加的话，两个请求同时到，
    会查到同一个“最大行 ID”，编出同一个新行 ID；或者一边在处理，另一边又改了订单
    """
    with WRITE_LOCK, get_engine().begin() as conn:
        yield conn
