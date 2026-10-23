# -*- coding: utf-8 -*-
"""导出 Excel"""
from io import BytesIO

import pandas as pd

from app.constants import COUNTRY, ORDER_COLUMNS, TEMPLATE_COLUMNS
from app.db import fetch_all
from app.services.order_service import build_where, order_by


def orders_to_xlsx(f):
    """
    按当前筛选条件导出订单。列和导入模板完全相同（20 列，顺序一样），
    数据库里没存“国家”，导出时补上，这样导出的文件可以直接再导入
    """
    where, params = build_where(f)
    fields = [en for en, cn in ORDER_COLUMNS.items() if cn in TEMPLATE_COLUMNS]
    rows = fetch_all(f"SELECT {', '.join(fields)} FROM sales_order" + where + order_by(f), params)
    df = pd.DataFrame(rows, columns=fields).rename(columns=ORDER_COLUMNS)
    df["国家"] = COUNTRY
    for c in ["订单日期", "发货日期"]:                      # 日期写成和原始数据一样的 2024/04/27
        df[c] = pd.to_datetime(df[c]).dt.strftime("%Y/%m/%d")
    for c in ["销售额", "折扣", "利润"]:
        df[c] = df[c].astype(float)
    df = df[TEMPLATE_COLUMNS]

    buf = BytesIO()
    df.to_excel(buf, index=False, sheet_name="订单")
    buf.seek(0)
    return buf, len(df)
