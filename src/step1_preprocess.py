# -*- coding: utf-8 -*-
"""
第一步：数据预处理
  1. 读取原始订单数据，做数据质量检查，输出《数据质量检查.xlsx》
  2. 数据清洗：日期转换、去空格、去重、构造订单键和商品键、异常值标记
  3. 特征构造：时间字段、折后单价、原价单价、利润率、发货天数等
  4. 按 月/周 × 地区 × 类别/子类别 汇总，并对无销售的时段补 0
  5. 比较不同汇总粒度下的零销量占比，作为选择月度汇总的依据
运行：python src/step1_preprocess.py
"""
import pandas as pd
import numpy as np

from config import RAW_FILE, PROCESSED_DIR, TABLE_DIR, DATA_START, DATA_END
from preprocess_core import (strip_text, drop_duplicate_rows, parse_types, add_features, iqr_bounds,
                             flag_outliers, month_list, summarize, monthly_region_sub,
                             monthly_region_cat)


# =============== 1. 读取 ===============
def load_raw(path=RAW_FILE):
    df = pd.read_excel(path, dtype={"订单日期": str, "发货日期": str})
    print(f"原始数据：{df.shape[0]} 行 × {df.shape[1]} 列")
    return df


# =============== 2. 数据质量检查 ===============
def field_profile(df):
    """字段概况：类型、缺失、唯一值个数、示例"""
    rows = []
    for c in df.columns:
        s = df[c]
        rows.append({
            "字段": c,
            "数据类型": "数值" if pd.api.types.is_numeric_dtype(s) else "文本",
            "非空数": int(s.notna().sum()),
            "缺失数": int(s.isna().sum()),
            "唯一值个数": int(s.nunique()),
            "示例值": str(s.iloc[0]),
        })
    return pd.DataFrame(rows)


def quality_checks(df):
    """逐项检查，返回检查结果表"""
    od = pd.to_datetime(df["订单日期"], format="%Y/%m/%d", errors="coerce")
    sd = pd.to_datetime(df["发货日期"], format="%Y/%m/%d", errors="coerce")
    unit_list = df["销售额"] / (df["数量"] * (1 - df["折扣"]))
    price_cv = (df.assign(_p=unit_list).groupby("产品 ID")["_p"]
                .agg(lambda x: (x.max() - x.min()) / x.min()))
    id_year = df["订单 ID"].str.split("-").str[1]
    checks = [
        ("缺失值", "所有字段缺失值总数", int(df.isna().sum().sum()), "无缺失，无需填充"),
        ("重复记录", "完全相同的行", int(df.duplicated().sum()), "无重复行"),
        ("重复记录", "重复的行 ID", int(df["行 ID"].duplicated().sum()), "行 ID 唯一"),
        ("日期格式", "订单日期无法解析", int(od.isna().sum()), "统一转换为日期类型"),
        ("日期格式", "发货日期无法解析", int(sd.isna().sum()), "统一转换为日期类型"),
        ("逻辑一致性", "发货日期早于订单日期", int((sd < od).sum()), "无此类错误"),
        ("取值范围", "数量 ≤ 0", int((df["数量"] <= 0).sum()), "无"),
        ("取值范围", "销售额 ≤ 0", int((df["销售额"] <= 0).sum()), "无"),
        ("取值范围", "折扣不在 [0, 1) 内", int(((df["折扣"] < 0) | (df["折扣"] >= 1)).sum()), "无"),
        ("编码一致性", "订单ID中的年份编码与订单日期年份不一致",
         int((id_year.str[-4:].astype(int) != od.dt.year).sum()),
         "订单ID中的年份（如 US-202023-… 里的 2023）都比订单日期早一年，分析时以订单日期为准"),
        ("编码一致性", "被多个订单重复使用的订单ID个数",
         int((df.groupby("订单 ID")[["订单日期", "客户 ID"]].nunique().max(axis=1) > 1).sum()),
         "同一订单ID对应不同日期或客户，改用“订单ID+订单日期+客户ID”作为订单键"),
        ("编码一致性", "对应多个商品名称的产品ID个数",
         int((df.groupby("产品 ID")["产品名称"].nunique() > 1).sum()),
         "改用“产品ID+产品名称”作为商品键；本研究按子类别汇总，不影响结果"),
        ("编码一致性", "原价单价不一致的产品ID个数（相差超过1%）",
         int((price_cv > 0.01).sum()),
         "与上一项为同一批产品ID，属同一编号对应不同商品"),
        ("编码一致性", "同时归属多个地区的省份个数",
         int((df.groupby("省/自治区")["地区"].nunique() > 1).sum()),
         "海南的记录分别归入中南和西南；地区字段按原数据保留"),
        ("编码一致性", "同名城市对应多个省份的个数",
         int((df.groupby("城市")["省/自治区"].nunique() > 1).sum()),
         "不按城市建模，保留原值"),
    ]
    return pd.DataFrame(checks, columns=["检查类别", "检查项", "问题记录数", "处理方式"])


def outlier_table(df):
    rows = []
    for c in ["销售额", "利润", "数量"]:
        lo, hi = iqr_bounds(df[c])
        n = int(((df[c] < lo) | (df[c] > hi)).sum())
        rows.append({"字段": c, "Q1": df[c].quantile(.25), "Q3": df[c].quantile(.75),
                     "下界": lo, "上界": hi, "超出范围记录数": n,
                     "占比": n / len(df),
                     "处理方式": "仅标记不删除：大额订单和亏损订单是真实业务情况，删除会低估销量"})
    return pd.DataFrame(rows).round(3)


# =============== 3. 清洗与特征构造 ===============
def clean(df):
    """清洗和特征构造，每一步的规则都在 preprocess_core.py 里"""
    df = strip_text(df)                 # 文本字段去首尾空格
    df, _ = drop_duplicate_rows(df)     # 去掉完全相同的行
    df = parse_types(df)                # 日期、数值转换类型
    df = add_features(df)               # 订单键、商品键、时间字段、单价、利润率
    df = flag_outliers(df)              # IQR 异常值标记，只标记不删除
    return df


# =============== 4. 汇总与补零 ===============
def build_aggregates(df):
    months = month_list(DATA_START, DATA_END)
    weeks = pd.date_range(df["周起始日"].min(), df["周起始日"].max(), freq="7D")
    sub2cat = df.drop_duplicates("子类别").set_index("子类别")["类别"]

    out = {}
    out["月度_地区_子类别"] = monthly_region_sub(df, months)
    out["月度_地区_类别"] = monthly_region_cat(df, months)
    out["月度_子类别"] = summarize(df, ["子类别"], "年月", months)
    out["月度_子类别"].insert(0, "类别", out["月度_子类别"]["子类别"].map(sub2cat))
    out["月度_地区"] = summarize(df, ["地区"], "年月", months)
    out["月度_整体"] = summarize(df, [], "年月", months)

    wk = {}
    wk["周度_地区_子类别"] = summarize(df, ["地区", "子类别"], "周起始日", weeks)
    wk["周度_地区_类别"] = summarize(df, ["地区", "类别"], "周起始日", weeks)
    wk["周度_子类别"] = summarize(df, ["子类别"], "周起始日", weeks)
    wk["周度_地区"] = summarize(df, ["地区"], "周起始日", weeks)
    wk["周度_整体"] = summarize(df, [], "周起始日", weeks)
    for k, w in wk.items():
        iso = w["周起始日"].dt.isocalendar()
        w.insert(w.columns.get_loc("周起始日") + 1, "年周",
                 iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2))
        w["是否完整周"] = ((w["周起始日"] >= df["订单日期"].min()) &
                      (w["周起始日"] + pd.Timedelta(days=6) <= df["订单日期"].max())).map({True: "是", False: "否"})
        w["周起始日"] = w["周起始日"].dt.strftime("%Y-%m-%d")
    return out, wk, len(months), len(weeks)


def granularity_profile(monthly, weekly, n_month, n_week):
    spec = [("月度", "地区×子类别", monthly["月度_地区_子类别"], ["地区", "子类别"], n_month),
            ("月度", "地区×类别", monthly["月度_地区_类别"], ["地区", "类别"], n_month),
            ("月度", "子类别（全国）", monthly["月度_子类别"], ["子类别"], n_month),
            ("月度", "地区", monthly["月度_地区"], ["地区"], n_month),
            ("周度", "地区×子类别", weekly["周度_地区_子类别"], ["地区", "子类别"], n_week),
            ("周度", "地区×类别", weekly["周度_地区_类别"], ["地区", "类别"], n_week),
            ("周度", "子类别（全国）", weekly["周度_子类别"], ["子类别"], n_week),
            ("周度", "地区", weekly["周度_地区"], ["地区"], n_week)]
    rows = []
    for gran, level, data, keys, n in spec:
        z = data["数量"] == 0
        per = z.groupby([data[k] for k in keys]).mean()
        rows.append({"时间粒度": gran, "汇总层级": level, "序列条数": len(per), "每条序列长度": n,
                     "零销量占比": round(z.mean(), 3),
                     "零销量超过一半的序列数": int((per > 0.5).sum()),
                     "单条序列最高零值占比": round(per.max(), 3),
                     "每期平均销量(件)": round(data["数量"].mean(), 1),
                     "每期销量中位数(件)": data["数量"].median()})
    return pd.DataFrame(rows)


def main(processed_dir=PROCESSED_DIR, table_dir=TABLE_DIR):
    """processed_dir、table_dir：结果存到哪里。测试时换成临时文件夹，不覆盖正式结果"""
    raw = load_raw()
    profile = field_profile(raw)
    checks = quality_checks(raw)
    outliers = outlier_table(raw)
    print(checks[["检查项", "问题记录数"]].to_string(index=False))

    df = clean(raw)
    print(f"清洗后：{len(df)} 行，订单键 {df['订单键'].nunique()} 个，订单ID {df['订单 ID'].nunique()} 个")

    monthly, weekly, n_m, n_w = build_aggregates(df)
    gran = granularity_profile(monthly, weekly, n_m, n_w)
    print(gran[["时间粒度", "汇总层级", "零销量占比"]].to_string(index=False))

    # ---- 保存 ----
    out_df = df.copy()
    for c in ["订单日期", "发货日期", "周起始日"]:
        out_df[c] = out_df[c].dt.strftime("%Y-%m-%d")
    out_df.to_csv(processed_dir / "orders_clean.csv", index=False, encoding="utf-8-sig")
    for name, data in {**monthly, **weekly}.items():
        data.to_csv(processed_dir / f"{name}.csv", index=False, encoding="utf-8-sig")
    gran.to_csv(processed_dir / "粒度评估.csv", index=False, encoding="utf-8-sig")

    with pd.ExcelWriter(table_dir / "数据质量检查.xlsx") as xw:
        profile.to_excel(xw, sheet_name="字段概况", index=False)
        checks.to_excel(xw, sheet_name="质量检查", index=False)
        outliers.to_excel(xw, sheet_name="异常值统计", index=False)
        gran.to_excel(xw, sheet_name="粒度评估", index=False)
    print("预处理完成，结果在 data/processed 和 outputs/tables")


if __name__ == "__main__":
    main()
