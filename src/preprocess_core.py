# -*- coding: utf-8 -*-
"""
预处理核心函数：清洗、重复识别、特征构造、IQR 异常标记、按月汇总与补零
step1_preprocess.py 用它处理原始数据，后面的 Flask 系统处理导入的数据时也调用这里的函数，
两边用的是同一套规则。所有函数处理的都是列名为中文的表（和原始数据的列名一致）
"""
import pandas as pd

NUM_COLS = ["销售额", "数量", "折扣", "利润"]
DATE_COLS = ["订单日期", "发货日期"]


def strip_text(df):
    """文本字段去首尾空格"""
    df = df.copy()
    for c in df.columns:
        if df[c].dtype == object or pd.api.types.is_string_dtype(df[c]):
            df[c] = df[c].astype(str).str.strip()
    return df


def drop_duplicate_rows(df, ignore=()):
    """
    去掉重复的行，只保留第一条；返回 (去重后的表, 去掉的行数)
    ignore：比较时不看哪些列。step1 不忽略任何列；系统忽略“行 ID”，找内容相同、行 ID 不同的重复录入
    """
    cols = [c for c in df.columns if c not in ignore]
    out = df.drop_duplicates(subset=cols)
    return out, len(df) - len(out)


def parse_types(df, date_format="%Y/%m/%d"):
    """日期列转成日期类型，数值列转成数值"""
    df = df.copy()
    for c in DATE_COLS:
        df[c] = pd.to_datetime(df[c], format=date_format)
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c])
    return df


def drop_invalid_rows(df):
    """
    去掉不合规的行：发货早于下单、数量 ≤ 0、销售额 ≤ 0、折扣不在 [0, 1) 内；返回 (留下的表, 去掉的行数)
    日期、数值须已转好类型。原始数据里这样的行是 0 行，step1 不调用它；系统处理导入的数据时用
    """
    bad = ((df["发货日期"] < df["订单日期"]) | (df["数量"] <= 0) | (df["销售额"] <= 0)
           | (df["折扣"] < 0) | (df["折扣"] >= 1))
    return df[~bad], int(bad.sum())


def add_features(df):
    """构造订单键、商品键、时间字段、价格与利润字段（订单日期、发货日期须已是日期类型）"""
    df = df.copy()
    # 订单键、商品键
    df["订单键"] = df["订单 ID"] + "|" + df["订单日期"].dt.strftime("%Y%m%d") + "|" + df["客户 ID"]
    df["商品键"] = df["产品 ID"] + "|" + df["产品名称"]

    # 时间特征
    df["年"] = df["订单日期"].dt.year
    df["季度"] = df["订单日期"].dt.quarter
    df["月"] = df["订单日期"].dt.month
    df["年月"] = df["订单日期"].dt.strftime("%Y-%m")
    iso = df["订单日期"].dt.isocalendar()
    df["ISO年"] = iso["year"].astype(int)
    df["ISO周"] = iso["week"].astype(int)
    df["年周"] = df["ISO年"].astype(str) + "-W" + df["ISO周"].astype(str).str.zfill(2)
    df["周起始日"] = (df["订单日期"] - pd.to_timedelta(df["订单日期"].dt.weekday, unit="D")).dt.normalize()
    df["发货天数"] = (df["发货日期"] - df["订单日期"]).dt.days

    # 价格与利润特征
    df["折后单价"] = (df["销售额"] / df["数量"]).round(2)
    df["原价单价"] = (df["销售额"] / (df["数量"] * (1 - df["折扣"]))).round(2)
    df["利润率"] = (df["利润"] / df["销售额"]).round(4)
    df["是否亏损"] = (df["利润"] < 0).astype(int)
    return df


def iqr_bounds(s, k=1.5):
    """IQR 法的上下界：Q1 - k×IQR、Q3 + k×IQR"""
    q1, q3 = s.quantile([0.25, 0.75])
    iqr = q3 - q1
    return q1 - k * iqr, q3 + k * iqr


def flag_outliers(df, cols=("销售额", "利润")):
    """异常值标记（IQR 法），只标记不删除；上下界用传进来的这批数据来算"""
    df = df.copy()
    for c in cols:
        lo, hi = iqr_bounds(df[c])
        df[f"{c}_异常标记"] = ((df[c] < lo) | (df[c] > hi)).astype(int)
    return df


def month_list(start_ym, end_ym):
    """从 start_ym 到 end_ym 的全部月份，如 ['2021-01', ..., '2024-12']"""
    return pd.date_range(start_ym + "-01", end_ym + "-01", freq="MS").strftime("%Y-%m")


def summarize(df, keys, time_col, time_values, combos=None):
    """
    按 keys × 时间 汇总销售额、数量、利润、订单数；
    每个 keys 组合在 time_values 的每个时间点都要有一行，没有销售的补 0
    combos：要列出哪些组合（一张含 keys 这几列的表）。不给就用 df 里出现过的组合，step1 是这样用的；
            系统给的是全部组合，这样数据很少时（比如只导入了 3 条）每个组合也都有一行
    """
    g = df.groupby(keys + [time_col]).agg(
        销售额=("销售额", "sum"),
        数量=("数量", "sum"),
        利润=("利润", "sum"),
        订单数=("订单键", "nunique"),
        订单行数=("行 ID", "count"),
    )
    if keys:
        combos = (df if combos is None else combos)[keys].drop_duplicates().sort_values(keys)
        full = pd.MultiIndex.from_tuples(
            [tuple(c) + (t,) for c in combos.itertuples(index=False) for t in time_values],
            names=keys + [time_col])
    else:
        full = pd.Index(time_values, name=time_col)
    g = g.reindex(full, fill_value=0).reset_index()
    g[["销售额", "利润"]] = g[["销售额", "利润"]].round(2)
    return g


def monthly_region_sub(df, months, combos=None):
    """月度汇总：地区 × 子类别（带上所属类别）。combos：含 地区、类别、子类别 三列的表，见 summarize"""
    sub2cat = (df if combos is None else combos).drop_duplicates("子类别").set_index("子类别")["类别"]
    out = summarize(df, ["地区", "子类别"], "年月", months, combos)
    out.insert(1, "类别", out["子类别"].map(sub2cat))
    return out


def monthly_region_cat(df, months, combos=None):
    """月度汇总：地区 × 类别（预测用的层级）"""
    return summarize(df, ["地区", "类别"], "年月", months, combos)
