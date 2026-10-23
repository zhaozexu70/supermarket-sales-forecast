# -*- coding: utf-8 -*-
"""
第二步：探索性数据分析（EDA）
  描述性统计表 + 论文用图（分布、箱线图、相关性、趋势、季节性、地区/品类结构、粒度对比）
运行：python src/step2_eda.py   （需先运行 step1）
"""
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from config import PROCESSED_DIR, FIG_DIR, TABLE_DIR
from plot_style import setup, save, COLORS, C_BLUE, C_ORANGE, C_AQUA, C_GRAY, TEXT2

REGION_ORDER = ["华东", "中南", "东北", "华北", "西南", "西北"]


def _boxplot(ax, data, labels, **kw):
    """兼容新旧版本 matplotlib（3.9 起参数名改为 tick_labels）"""
    try:
        return ax.boxplot(data, tick_labels=labels, **kw)
    except TypeError:
        return ax.boxplot(data, labels=labels, **kw)


def load():
    df = pd.read_csv(PROCESSED_DIR / "orders_clean.csv", parse_dates=["订单日期", "发货日期"], encoding="utf-8-sig")
    m_all = pd.read_csv(PROCESSED_DIR / "月度_整体.csv", encoding="utf-8-sig")
    m_rs = pd.read_csv(PROCESSED_DIR / "月度_地区_子类别.csv", encoding="utf-8-sig")
    gran = pd.read_csv(PROCESSED_DIR / "粒度评估.csv", encoding="utf-8-sig")
    return df, m_all, m_rs, gran


def describe_table(df):
    cols = ["销售额", "数量", "折扣", "利润", "折后单价", "利润率", "发货天数"]
    d = df[cols].describe().T
    d["偏度"] = df[cols].skew()
    d = d.rename(columns={"count": "样本数", "mean": "均值", "std": "标准差", "min": "最小值",
                          "25%": "下四分位数", "50%": "中位数", "75%": "上四分位数", "max": "最大值"})
    return d.round(3)


def fig_distribution(df):
    fig, axes = plt.subplots(2, 2, figsize=(10, 7))
    specs = [("销售额", 60), ("利润", 60), ("数量", np.arange(0.5, 15.5, 1))]
    for ax, (col, bins) in zip(axes.ravel()[:3], specs):
        ax.hist(df[col], bins=bins, color=C_BLUE, edgecolor="white", linewidth=0.8)
        ax.set_title(f"{col}分布")
        ax.set_xlabel(col)
        ax.set_ylabel("订单行数")
    # 折扣只有 7 个取值，用条形图
    vc = df["折扣"].value_counts().sort_index()
    ax = axes[1, 1]
    ax.bar([f"{v:.2f}" for v in vc.index], vc.values, color=C_BLUE, width=0.6)
    ax.set_title("折扣分布")
    ax.set_xlabel("折扣")
    ax.set_ylabel("订单行数")
    fig.tight_layout()
    save(fig, FIG_DIR / "eda01_数值字段分布直方图.png")


def fig_boxplots(df):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    cats = ["办公用品", "家具", "技术"]
    data = [df.loc[df["类别"] == c, "销售额"] for c in cats]
    bp = _boxplot(axes[0], data, cats, patch_artist=True, widths=0.5,
                         flierprops=dict(marker="o", markersize=2, markerfacecolor=C_GRAY,
                                         markeredgecolor=C_GRAY, alpha=0.5),
                         medianprops=dict(color="white", linewidth=2))
    for p in bp["boxes"]:
        p.set_facecolor(C_BLUE)
        p.set_edgecolor(C_BLUE)
    axes[0].set_title("各类别订单行销售额箱线图")
    axes[0].set_ylabel("销售额")

    levels = sorted(df["折扣"].unique())
    data = [df.loc[df["折扣"] == v, "利润"] for v in levels]
    bp = _boxplot(axes[1], data, [f"{v:.2f}" for v in levels], patch_artist=True, widths=0.5,
                         flierprops=dict(marker="o", markersize=2, markerfacecolor=C_GRAY,
                                         markeredgecolor=C_GRAY, alpha=0.5),
                         medianprops=dict(color="white", linewidth=2))
    for p in bp["boxes"]:
        p.set_facecolor(C_BLUE)
        p.set_edgecolor(C_BLUE)
    axes[1].axhline(0, color=TEXT2, linewidth=0.8)
    axes[1].set_title("不同折扣下订单行利润箱线图")
    axes[1].set_xlabel("折扣")
    axes[1].set_ylabel("利润")
    fig.tight_layout()
    save(fig, FIG_DIR / "eda02_销售额与利润箱线图.png")


def fig_corr(df):
    cols = ["销售额", "数量", "折扣", "利润", "折后单价", "利润率"]
    c = df[cols].corr()
    fig, ax = plt.subplots(figsize=(6.5, 5.5))
    im = ax.imshow(c.values, cmap="RdBu_r", vmin=-1, vmax=1)
    ax.set_xticks(range(len(cols)), cols, rotation=30)
    ax.set_yticks(range(len(cols)), cols)
    ax.grid(False)
    for i in range(len(cols)):
        for j in range(len(cols)):
            v = c.values[i, j]
            ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=9,
                    color="white" if abs(v) > 0.6 else "#0b0b0b")
    fig.colorbar(im, ax=ax, shrink=0.8, label="皮尔逊相关系数")
    ax.set_title("数值字段相关性热力图")
    save(fig, FIG_DIR / "eda03_相关性热力图.png")
    return c.round(3)


def fig_trend(m_all):
    x = pd.to_datetime(m_all["年月"])
    fig, axes = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True)
    axes[0].plot(x, m_all["销售额"] / 1e4, color=C_BLUE, marker="o", markersize=3)
    axes[0].set_title("月度销售额走势（2021年1月～2024年12月）")
    axes[0].set_ylabel("销售额（万）")
    axes[1].plot(x, m_all["数量"], color=C_BLUE, marker="o", markersize=3)
    axes[1].set_title("月度销售数量走势")
    axes[1].set_ylabel("销售数量（件）")
    fig.tight_layout()
    save(fig, FIG_DIR / "eda04_月度销售走势.png")


def fig_season(m_all):
    m = m_all.copy()
    m["年"] = m["年月"].str[:4]
    m["月"] = m["年月"].str[5:].astype(int)
    p = m.pivot(index="月", columns="年", values="数量")
    share = p / p.sum()
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.bar(share.index, share.mean(axis=1) * 100, color=C_BLUE, width=0.6, label="四年平均")
    for i, y in enumerate(share.columns):
        ax.plot(share.index, share[y] * 100, linestyle="none", marker="o", markersize=5,
                color=COLORS[i + 1] if i + 1 < len(COLORS) else C_GRAY, label=f"{y}年")
    ax.set_xticks(range(1, 13), [f"{i}月" for i in range(1, 13)])
    ax.set_ylabel("占全年销量比例（%）")
    ax.set_title("各月份销量占全年比例")
    ax.set_ylim(0, share.values.max() * 100 * 1.25)
    ax.legend(ncol=5, loc="upper left")
    save(fig, FIG_DIR / "eda05_月份季节性.png")
    out = share.copy()
    out["四年平均"] = share.mean(axis=1)
    return (out * 100).round(2)


def fig_region(df):
    g = df.groupby("地区").agg(销售额=("销售额", "sum"), 利润=("利润", "sum"), 数量=("数量", "sum"),
                             订单数=("订单键", "nunique")).reindex(REGION_ORDER)
    g["销售额占比"] = g["销售额"] / g["销售额"].sum()
    g["利润率"] = g["利润"] / g["销售额"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    axes[0].bar(g.index, g["销售额"] / 1e4, color=C_BLUE, width=0.6)
    for i, v in enumerate(g["销售额占比"]):
        axes[0].text(i, g["销售额"].iloc[i] / 1e4, f"{v:.1%}", ha="center", va="bottom", fontsize=9, color=TEXT2)
    axes[0].set_title("各地区销售额")
    axes[0].set_ylabel("销售额（万）")
    axes[1].bar(g.index, g["利润"] / 1e4, color=C_BLUE, width=0.6)
    axes[1].set_title("各地区利润")
    axes[1].set_ylabel("利润（万）")
    fig.tight_layout()
    save(fig, FIG_DIR / "eda06_地区销售与利润.png")
    return g.round(4)


def fig_subcat(df):
    g = df.groupby(["类别", "子类别"]).agg(销售额=("销售额", "sum"), 利润=("利润", "sum"),
                                       数量=("数量", "sum"), 平均折扣=("折扣", "mean")).reset_index()
    g = g.sort_values("销售额")
    fig, axes = plt.subplots(1, 2, figsize=(11, 6), sharey=True)
    axes[0].barh(g["子类别"], g["销售额"] / 1e4, color=C_BLUE, height=0.6)
    axes[0].set_title("各子类别销售额")
    axes[0].set_xlabel("销售额（万）")
    colors = [C_BLUE if v >= 0 else C_ORANGE for v in g["利润"]]
    axes[1].barh(g["子类别"], g["利润"] / 1e4, color=colors, height=0.6)
    axes[1].axvline(0, color=TEXT2, linewidth=0.8)
    axes[1].set_title("各子类别利润（橙色为亏损）")
    axes[1].set_xlabel("利润（万）")
    fig.tight_layout()
    save(fig, FIG_DIR / "eda07_子类别销售额与利润.png")
    g["利润率"] = g["利润"] / g["销售额"]
    return g.sort_values("销售额", ascending=False).round(4)


def fig_heatmap(m_rs):
    p = m_rs.groupby(["地区", "子类别"])["数量"].sum().unstack().reindex(REGION_ORDER)
    p = p[p.sum().sort_values(ascending=False).index]
    fig, ax = plt.subplots(figsize=(12, 4.2))
    im = ax.imshow(p.values, cmap="Blues", aspect="auto")
    ax.set_xticks(range(p.shape[1]), p.columns, rotation=0)
    ax.set_yticks(range(p.shape[0]), p.index)
    ax.grid(False)
    vmax = p.values.max()
    for i in range(p.shape[0]):
        for j in range(p.shape[1]):
            v = p.values[i, j]
            ax.text(j, i, f"{int(v)}", ha="center", va="center", fontsize=8,
                    color="white" if v > vmax * 0.55 else "#0b0b0b")
    fig.colorbar(im, ax=ax, shrink=0.9, label="销售数量（件）")
    ax.set_title("地区×子类别 四年累计销售数量")
    save(fig, FIG_DIR / "eda08_地区子类别销量热力图.png")
    return p


def fig_granularity(gran):
    levels = ["地区×子类别", "地区×类别", "子类别（全国）", "地区"]
    m = gran[gran["时间粒度"] == "月度"].set_index("汇总层级").reindex(levels)["零销量占比"] * 100
    w = gran[gran["时间粒度"] == "周度"].set_index("汇总层级").reindex(levels)["零销量占比"] * 100
    x = np.arange(len(levels))
    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    ax.bar(x - 0.18, w, width=0.34, color=C_ORANGE, label="按周汇总")
    ax.bar(x + 0.18, m, width=0.34, color=C_BLUE, label="按月汇总")
    for xi, v in zip(x - 0.18, w):
        ax.text(xi, v, f"{v:.1f}%", ha="center", va="bottom", fontsize=9, color=TEXT2)
    for xi, v in zip(x + 0.18, m):
        ax.text(xi, v, f"{v:.1f}%", ha="center", va="bottom", fontsize=9, color=TEXT2)
    ax.set_xticks(x, levels)
    ax.set_ylabel("零销量时段占比（%）")
    ax.set_title("不同汇总粒度下零销量时段占比")
    ax.legend()
    save(fig, FIG_DIR / "eda09_汇总粒度零值占比.png")


def fig_discount(df):
    g = df.groupby("折扣").agg(平均利润=("利润", "mean"), 亏损占比=("是否亏损", "mean"),
                             订单行数=("行 ID", "count")).reset_index()
    labels = [f"{v:.2f}" for v in g["折扣"]]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    colors = [C_BLUE if v >= 0 else C_ORANGE for v in g["平均利润"]]
    axes[0].bar(labels, g["平均利润"], color=colors, width=0.6)
    axes[0].axhline(0, color=TEXT2, linewidth=0.8)
    axes[0].set_title("不同折扣下的平均利润（橙色为亏损）")
    axes[0].set_xlabel("折扣")
    axes[0].set_ylabel("平均每行利润")
    axes[1].bar(labels, g["亏损占比"] * 100, color=C_BLUE, width=0.6)
    axes[1].set_title("不同折扣下亏损订单行占比")
    axes[1].set_xlabel("折扣")
    axes[1].set_ylabel("亏损占比（%）")
    fig.tight_layout()
    save(fig, FIG_DIR / "eda10_折扣与利润.png")
    return g.round(3)


def main():
    setup()
    df, m_all, m_rs, gran = load()
    desc = describe_table(df)
    fig_distribution(df)
    fig_boxplots(df)
    corr = fig_corr(df)
    fig_trend(m_all)
    season = fig_season(m_all)
    region = fig_region(df)
    subcat = fig_subcat(df)
    heat = fig_heatmap(m_rs)
    fig_granularity(gran)
    disc = fig_discount(df)

    year = df.groupby("年").agg(销售额=("销售额", "sum"), 数量=("数量", "sum"), 利润=("利润", "sum"),
                               订单数=("订单键", "nunique"), 订单行数=("行 ID", "count"))
    year["销售额同比增长率"] = year["销售额"].pct_change()
    year["数量同比增长率"] = year["数量"].pct_change()

    with pd.ExcelWriter(TABLE_DIR / "描述性统计与分析结果.xlsx") as xw:
        desc.to_excel(xw, sheet_name="描述性统计")
        corr.to_excel(xw, sheet_name="相关系数")
        year.round(4).to_excel(xw, sheet_name="年度汇总")
        season.to_excel(xw, sheet_name="月份销量占比(%)")
        region.to_excel(xw, sheet_name="地区汇总")
        subcat.to_excel(xw, sheet_name="子类别汇总", index=False)
        heat.to_excel(xw, sheet_name="地区子类别销量")
        disc.to_excel(xw, sheet_name="折扣与利润", index=False)
    print(desc.to_string())
    print(year.round(3).to_string())
    print("EDA 完成")


if __name__ == "__main__":
    main()
