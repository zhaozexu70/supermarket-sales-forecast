# -*- coding: utf-8 -*-
"""
第三步：把清洗后的数据和汇总结果存入 MySQL
  先建库建表（sql/schema.sql），再清空旧数据、批量插入
  需要：pip install pymysql，并在 config.py 里改好数据库账号密码
运行：python src/step3_load_mysql.py            # 导入明细和月度汇总
      python src/step3_load_mysql.py --results  # 运行 step4 之后，再导入模型评估和预测结果
      python src/step3_load_mysql.py --db supermarket_db_test   # 导入到指定的库（测试用）
      python src/step3_load_mysql.py --force    # 库里已有系统数据时，确认要清空重导
注意：导入明细会先清空订单表。库已经升级（有 year_state 表）时默认拒绝运行，
      加 --force 才继续，效果等于系统里的“恢复原始数据”
"""
import argparse
import re
import sys

import pandas as pd

from config import DB_CONFIG, DB_NAME, PROCESSED_DIR, TABLE_DIR, SQL_DIR

ORDER_COLS = {  # 数据库字段 : 清洗后 CSV 字段
    "row_id": "行 ID", "order_id": "订单 ID", "order_key": "订单键", "order_date": "订单日期",
    "ship_date": "发货日期", "ship_mode": "邮寄方式", "customer_id": "客户 ID", "customer_name": "客户名称",
    "segment": "细分", "city": "城市", "province": "省/自治区", "region": "地区", "product_id": "产品 ID",
    "category": "类别", "sub_category": "子类别", "product_name": "产品名称", "sales": "销售额",
    "quantity": "数量", "discount": "折扣", "profit": "利润", "unit_price": "折后单价",
    "profit_rate": "利润率", "ym": "年月",
}
MONTHLY_COLS = {"region": "地区", "category": "类别", "sub_category": "子类别", "ym": "年月",
                "sales": "销售额", "quantity": "数量", "profit": "利润", "order_count": "订单数"}
MONTHLY_CAT_COLS = {"region": "地区", "category": "类别", "ym": "年月", "sales": "销售额",
                    "quantity": "数量", "profit": "利润", "order_count": "订单数"}
EVAL_COLS = {"region": "地区", "category": "类别", "model_name": "模型", "model_order": "阶数",
             "mae": "MAE", "rmse": "RMSE", "wape": "WAPE", "r2": "R2"}
FORECAST_COLS = {"level_name": "层级", "region": "地区", "category": "类别", "sub_category": "子类别",
                 "ym": "预测月份", "forecast_qty": "预测销量", "lower_qty": "预测下限", "upper_qty": "预测上限",
                 "safety_stock": "安全库存", "suggested_purchase": "建议进货量"}


class GuardError(Exception):
    """库里已有系统数据、又没加 --force 时抛出"""


def connect(db_name, create=False):
    """连接 MySQL 并进入指定的库；create=True 时库不存在就先建"""
    import pymysql
    if not re.fullmatch(r"\w+", db_name):
        raise ValueError(f"库名只能用字母、数字、下划线：{db_name}")
    cfg = dict(DB_CONFIG)
    cfg.pop("database")
    conn = pymysql.connect(**cfg, autocommit=False)
    if create:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                        "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")
    conn.select_db(db_name)
    return conn


def table_exists(cur, table):
    cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = %s", (table,))
    return cur.fetchone()[0] > 0


def to_rows(df, mapping):
    """按映射取列，NaN 转为 None，返回 (字段列表, 行元组列表)"""
    sub = df[list(mapping.values())].copy()
    sub = sub.astype(object).where(pd.notna(sub), None)
    return list(mapping.keys()), [tuple(r) for r in sub.itertuples(index=False)]


def run_schema(cur, path=SQL_DIR / "schema.sql"):
    """在当前库里执行建表文件（文件里不写库名）"""
    sql = path.read_text(encoding="utf-8")
    for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
        lines = [ln for ln in stmt.splitlines() if not ln.strip().startswith("--")]
        if "".join(lines).strip():
            cur.execute("\n".join(lines))


def insert(cur, table, cols, rows, batch=1000):
    cur.execute(f"DELETE FROM {table}")
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
    for i in range(0, len(rows), batch):
        cur.executemany(sql, rows[i:i + batch])
    print(f"  {table}: 写入 {len(rows)} 行")


def build_base_rows():
    orders = pd.read_csv(PROCESSED_DIR / "orders_clean.csv", encoding="utf-8-sig")
    m_sub = pd.read_csv(PROCESSED_DIR / "月度_地区_子类别.csv", encoding="utf-8-sig")
    m_cat = pd.read_csv(PROCESSED_DIR / "月度_地区_类别.csv", encoding="utf-8-sig")
    return {"sales_order": to_rows(orders, ORDER_COLS),
            "monthly_sales": to_rows(m_sub, MONTHLY_COLS),
            "monthly_sales_category": to_rows(m_cat, MONTHLY_CAT_COLS)}


def build_result_rows():
    ev = pd.read_excel(TABLE_DIR / "模型评估结果.xlsx", sheet_name="各序列评估")
    fc = pd.read_excel(TABLE_DIR / "进货预测结果.xlsx", sheet_name="全部预测结果")
    return {"model_evaluation": to_rows(ev, EVAL_COLS),
            "forecast_result": to_rows(fc, FORECAST_COLS)}


def reset_state(cur):
    """库里只剩原始数据后：导入批次全部标为已撤销，各年份记为已处理（月度表就是 step1 的结果）"""
    cur.execute("UPDATE import_batch SET revoked = 1")
    cur.execute("DELETE FROM year_state")
    cur.execute("INSERT INTO year_state (`year`, data_version, processed_version) "
                "SELECT DISTINCT YEAR(order_date), 1, 1 FROM sales_order")


def load(db_name=DB_NAME, results=False, force=False):
    conn = connect(db_name, create=True)
    try:
        with conn.cursor() as cur:
            # 先检查再建表：新库没有 year_state 表，可以直接导入
            if not results and not force and table_exists(cur, "year_state"):
                raise GuardError(
                    f"库 {db_name} 已经升级过（有 year_state 表），里面可能有系统导入的订单。\n"
                    "继续运行会清空订单表，只留原始数据。确认要这样做，请加 --force 再运行。")
            run_schema(cur)
            tables = build_result_rows() if results else build_base_rows()
            for table, (cols, rows) in tables.items():
                insert(cur, table, cols, rows)
            if not results:
                reset_state(cur)
        conn.commit()
        print(f"导入完成（库：{db_name}）")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="把清洗后的数据和汇总结果存入 MySQL")
    ap.add_argument("--db", default=DB_NAME, help="导入到哪个库，不写就是正式库")
    ap.add_argument("--results", action="store_true", help="只导入模型评估和预测结果")
    ap.add_argument("--force", action="store_true", help="库已升级时仍然清空订单表重新导入")
    args = ap.parse_args()
    try:
        load(args.db, results=args.results, force=args.force)
    except GuardError as e:
        print(f"没有运行：{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
