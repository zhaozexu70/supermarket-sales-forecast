# -*- coding: utf-8 -*-
"""升级脚本 migrate_v2.py：在旧结构的库上运行两次，订单数据不变，新结构齐全"""
from pathlib import Path

from migrate_v2 import migrate
from step3_load_mysql import build_base_rows, connect, insert, run_schema

SCHEMA_V1 = Path(__file__).parent / "schema_v1.sql"
NEW_TABLES = {"import_batch", "year_state", "process_log", "forecast_task", "forecast_detail"}


def build_v1_db(db_name):
    """按阶段一的旧结构建表，装入原始数据"""
    conn = connect(db_name)
    with conn.cursor() as cur:
        run_schema(cur, SCHEMA_V1)
        for table, (cols, rows) in build_base_rows().items():
            insert(cur, table, cols, rows)
    conn.commit()
    conn.close()


def test_migration(empty_db, query):
    build_v1_db(empty_db)
    assert len(query("SHOW TABLES")) == 6
    assert query("SELECT COUNT(*), SUM(quantity) FROM sales_order")[0] == (9959, 37534)

    first = migrate(empty_db)
    second = migrate(empty_db)          # 第二次运行不能报错，也不应该再改任何东西
    # 5 张表 + sales_order 2 个字段 + sys_user 2 个字段 + 1 个索引 + year_state + process_log
    assert len(first["done"]) == 12
    assert second["done"] == []
    assert first["before"] == first["after"] == second["after"]

    # 订单一条没少，总销量不变，来源都是原始数据
    assert query("SELECT COUNT(*), SUM(quantity) FROM sales_order")[0] == (9959, 37534)
    assert query("SELECT source, batch_id, COUNT(*) FROM sales_order GROUP BY source, batch_id") == \
        (("原始数据", None, 9959),)

    # 新表都在；四个年份都是第 1 版、已处理
    tables = {r[0] for r in query("SHOW TABLES")}
    assert NEW_TABLES <= tables and len(tables) == 11
    assert query("SELECT `year`, data_version, processed_version FROM year_state ORDER BY `year`") == \
        ((2021, 1, 1), (2022, 1, 1), (2023, 1, 1), (2024, 1, 1))

    # 只有一条阶段一的处理记录
    log = query("SELECT start_year, end_year, order_rows, duplicate_rows, invalid_rows, "
                "outlier_sales, outlier_profit, month_count FROM process_log")
    assert log == ((2021, 2024, 9959, 0, 0, 1037, 1839, 48),)


def test_migration_adds_later_columns(empty_db, query):
    """步骤 1 之后才加的字段：库里缺这些字段时，再跑升级脚本会补上，补完再跑就没有改动"""
    build_v1_db(empty_db)
    migrate(empty_db)
    query("ALTER TABLE sys_user DROP COLUMN failed_count, DROP COLUMN locked_until")
    query("ALTER TABLE forecast_task DROP COLUMN year_versions")
    query("ALTER TABLE process_log DROP COLUMN seconds")

    again = migrate(empty_db)
    assert again["done"] == ["sys_user 加字段 failed_count", "sys_user 加字段 locked_until",
                             "forecast_task 加字段 year_versions", "process_log 加字段 seconds"]
    assert migrate(empty_db)["done"] == []
    assert query("SELECT seconds FROM process_log") == ((None,),)        # 已有的处理记录还在，用时为空
    assert query("SELECT COUNT(*), SUM(quantity) FROM sales_order")[0] == (9959, 37534)
