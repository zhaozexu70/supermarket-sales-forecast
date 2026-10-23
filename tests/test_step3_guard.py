# -*- coding: utf-8 -*-
"""step3 的保护：库里已有系统数据时，不加 --force 不能清空重导"""
import pytest

from step3_load_mysql import GuardError, connect, load


def add_imported_order(db_name):
    """模拟系统里导入了 1 条 2025 年的订单：多一个批次、多一行订单、多一个年份"""
    conn = connect(db_name)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO import_batch (method, file_name, total_rows, success_rows, user_id) "
                    "VALUES ('表格导入', '测试.xlsx', 1, 1, 1)")
        batch_id = cur.lastrowid
        cur.execute(
            "INSERT INTO sales_order (row_id, order_id, order_key, order_date, ship_date, customer_id, "
            "region, product_id, category, sub_category, sales, quantity, discount, profit, ym, "
            "source, batch_id) VALUES (10001, 'T-1', 'T-1|20250105|C-1', '2025-01-05', '2025-01-06', "
            "'C-1', '华东', 'P-1', '办公用品', '纸张', 100, 2, 0, 10, '2025-01', '表格导入', %s)",
            (batch_id,))
        cur.execute("INSERT INTO year_state (`year`, data_version) VALUES (2025, 1)")
    conn.commit()
    conn.close()


def test_step3_guard(empty_db, query):
    # 空库：没有 year_state 表，可以直接导入，导入后就是新结构
    load(empty_db)
    assert query("SELECT COUNT(*) FROM sales_order")[0][0] == 9959
    assert query("SELECT `year`, data_version, processed_version FROM year_state ORDER BY `year`") == \
        ((2021, 1, 1), (2022, 1, 1), (2023, 1, 1), (2024, 1, 1))

    add_imported_order(empty_db)
    assert query("SELECT COUNT(*) FROM sales_order")[0][0] == 9960

    # 不加 --force：拒绝运行，数据原样不动
    with pytest.raises(GuardError):
        load(empty_db)
    assert query("SELECT COUNT(*) FROM sales_order")[0][0] == 9960
    assert query("SELECT revoked FROM import_batch") == ((0,),)
    assert len(query("SELECT * FROM year_state")) == 5

    # 加 --force：只剩原始数据，批次标为已撤销，年份状态回到四年已处理
    load(empty_db, force=True)
    assert query("SELECT source, COUNT(*), SUM(quantity) FROM sales_order GROUP BY source") == \
        (("原始数据", 9959, 37534),)
    assert query("SELECT revoked FROM import_batch") == ((1,),)
    assert query("SELECT `year`, data_version, processed_version FROM year_state ORDER BY `year`") == \
        ((2021, 1, 1), (2022, 1, 1), (2023, 1, 1), (2024, 1, 1))

    # --results 只导入实验结果，不碰订单，不需要 --force
    load(empty_db, results=True)
    assert query("SELECT COUNT(*) FROM forecast_result")[0][0] == 720
    assert query("SELECT COUNT(*) FROM sales_order")[0][0] == 9959


def test_refuse_non_test_db():
    """保险本身也要测：库名不以 _test 结尾时必须拒绝"""
    from conftest import require_test_db
    with pytest.raises(RuntimeError):
        require_test_db("supermarket_db")
