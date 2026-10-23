# -*- coding: utf-8 -*-
"""
数据库升级脚本：把阶段一的库（6 张表）升级到系统要用的 v2 结构
  1. 新增 5 张表：import_batch、year_state、process_log、forecast_task、forecast_detail
  2. 给已有的表加字段：
       sales_order   source（数据来源）、batch_id（导入批次），加索引 idx_batch
       sys_user      failed_count（连续登录失败次数）、locked_until（锁定到什么时候）
       forecast_task year_versions（预测时各年份的数据版本，用来判断结果能否复用）
       process_log   seconds（这次处理用了多少秒）
  3. year_state 写入已有年份（都记为已处理），process_log 写入一条阶段一的处理记录
只加字段、加表，不删不改已有数据；可以重复运行，已经有的就跳过
运行：python src/migrate_v2.py                          # 升级正式库
      python src/migrate_v2.py --db supermarket_db_test # 升级指定的库
"""
import argparse

import pandas as pd

from config import DB_NAME
from preprocess_core import iqr_bounds
from step3_load_mysql import connect, run_schema, table_exists

NEW_COLUMNS = {  # 表 : {要加的字段 : 字段定义}，与 sql/schema.sql 保持一致
    "sales_order": {
        "source": "VARCHAR(10) NOT NULL DEFAULT '原始数据' COMMENT '数据来源：原始数据/表格导入/手工录入'",
        "batch_id": "INT NULL COMMENT '导入批次号，原始数据为空'",
    },
    "sys_user": {
        "failed_count": "INT NOT NULL DEFAULT 0 COMMENT '连续登录失败次数'",
        "locked_until": "DATETIME NULL COMMENT '锁定到这个时间为止，没锁定为空'",
    },
    "forecast_task": {
        "year_versions": "VARCHAR(200) NULL COMMENT '预测时各年份的数据版本，如 2021:1,2022:1'",
    },
    "process_log": {
        "seconds": "DECIMAL(8,2) NULL COMMENT '这次处理用了多少秒，早先的记录为空'",
    },
}


def column_exists(cur, table, column):
    cur.execute("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() "
                "AND table_name = %s AND column_name = %s", (table, column))
    return cur.fetchone()[0] > 0


def index_exists(cur, table, index):
    cur.execute("SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() "
                "AND table_name = %s AND index_name = %s", (table, index))
    return cur.fetchone()[0] > 0


def list_tables(cur):
    cur.execute("SHOW TABLES")
    return {r[0] for r in cur.fetchall()}


def order_totals(cur):
    """订单表的行数、总销量、总销售额，升级前后各查一次，用来确认数据没被动过"""
    cur.execute("SELECT COUNT(*), SUM(quantity), SUM(sales) FROM sales_order")
    rows, qty, sales = cur.fetchone()
    return {"行数": rows, "总销量": int(qty or 0), "总销售额": str(sales or 0)}


def outlier_count(values):
    s = pd.Series(values, dtype=float)
    lo, hi = iqr_bounds(s)
    return int(((s < lo) | (s > hi)).sum())


def stage1_process_row(cur):
    """阶段一已经用 step1 处理过全部原始数据，这里按库里的数据补记一条处理记录"""
    cur.execute("SELECT MIN(YEAR(order_date)), MAX(YEAR(order_date)), COUNT(*) FROM sales_order")
    start_year, end_year, order_rows = cur.fetchone()
    cur.execute("SELECT sales, profit FROM sales_order")
    data = cur.fetchall()
    cur.execute("SELECT COUNT(DISTINCT ym) FROM monthly_sales_category")
    month_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM monthly_sales WHERE order_count = 0")
    zero_sub = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM monthly_sales_category WHERE order_count = 0")
    zero_cat = cur.fetchone()[0]
    return {"start_year": start_year, "end_year": end_year, "order_rows": order_rows,
            "duplicate_rows": 0, "invalid_rows": 0,
            "outlier_sales": outlier_count([r[0] for r in data]),
            "outlier_profit": outlier_count([r[1] for r in data]),
            "month_count": month_count, "zero_filled_sub": zero_sub, "zero_filled_cat": zero_cat}


def add_missing_columns(cur):
    """缺哪个字段就加哪个（MySQL 8.0 不支持 ADD COLUMN IF NOT EXISTS，所以先查再加），返回做了哪些改动"""
    done = []
    for table, columns in NEW_COLUMNS.items():
        for col, ddl in columns.items():
            if not column_exists(cur, table, col):
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                done.append(f"{table} 加字段 {col}")
    if not index_exists(cur, "sales_order", "idx_batch"):
        cur.execute("ALTER TABLE sales_order ADD INDEX idx_batch (batch_id)")
        done.append("sales_order 加索引 idx_batch")
    return done


def migrate(db_name=DB_NAME):
    conn = connect(db_name)
    done = []   # 这次实际做了哪些改动，重复运行时应该是空的
    try:
        with conn.cursor() as cur:
            if not table_exists(cur, "sales_order"):
                raise RuntimeError(f"库 {db_name} 里没有 sales_order 表，不是阶段一的库")
            before = order_totals(cur)

            # 1. 新增的表：schema.sql 里都是 CREATE TABLE IF NOT EXISTS，已有的表不会被动
            old_tables = list_tables(cur)
            run_schema(cur)
            done += [f"新建表 {t}" for t in sorted(list_tables(cur) - old_tables)]

            # 2. 给已有的表加字段、加索引
            done += add_missing_columns(cur)

            # 3. 年份处理状态：已有年份都记为第 1 版、已处理
            cur.execute("SELECT COUNT(*) FROM year_state")
            if cur.fetchone()[0] == 0:
                cur.execute("INSERT INTO year_state (`year`, data_version, processed_version) "
                            "SELECT DISTINCT YEAR(order_date), 1, 1 FROM sales_order")
                done.append("year_state 写入已有年份")

            # 4. 补一条阶段一的处理记录
            cur.execute("SELECT COUNT(*) FROM process_log")
            if cur.fetchone()[0] == 0:
                row = stage1_process_row(cur)
                cur.execute(f"INSERT INTO process_log ({', '.join(row)}) "
                            f"VALUES ({', '.join(['%s'] * len(row))})", list(row.values()))
                done.append("process_log 写入阶段一的处理记录")
            conn.commit()

            after = order_totals(cur)
            if after != before:
                raise RuntimeError(f"订单数据发生了变化！升级前 {before}，升级后 {after}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"库：{db_name}")
    print(f"升级前：{before}")
    print(f"升级后：{after}（与升级前相同）")
    print("本次改动：" + ("；".join(done) if done else "无（已经是新结构）"))
    return {"before": before, "after": after, "done": done}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="把数据库升级到 v2 结构")
    ap.add_argument("--db", default=DB_NAME, help="升级哪个库，不写就是正式库")
    migrate(ap.parse_args().db)
