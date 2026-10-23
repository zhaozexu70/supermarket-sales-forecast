# -*- coding: utf-8 -*-
"""
第五步（阶段一的补充实验）：加一个机器学习模型 LightGBM，和已有的四个方案做对比
  计划和判定规则事先写在 docs/开发日志/04_机器学习对比实验.md 里，这里照着做，不临时改
  1. 一个全局模型：18 条“地区 × 类别”序列合在一起训练
  2. 直接预测 12 个月，不递归：预测起点是目标年份前一年的年底，特征只用起点之前的真实销量
  3. 划分和阶段一相同：验证 = 2021–2022 训练、预测 2023；测试 = 2021–2023 训练、预测 2024
  4. 只在验证集上调参（36 组），选定后在测试集上只跑一次
  5. 另外四个方案的数字直接读 outputs/tables/模型评估结果.xlsx，不重新运行 step4
只新增三个文件：机器学习对比.xlsx、m08_五模型对比.png、m09_LightGBM特征重要性.png，不改任何已有的结果
运行：python src/step5_ml_compare.py
"""
import itertools

import lightgbm as lgb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from config import FIG_DIR, PROCESSED_DIR, TABLE_DIR
from forecast_core import metrics
from plot_style import C_AQUA, C_GRAY, C_MAGENTA, C_ORANGE, TEXT2, save, setup

SEED = 42
LGB = "LightGBM"
FINAL = "组合模型"
OLD_MODELS = ["季节朴素法", "Holt-Winters", "SARIMA", FINAL]
MODELS = OLD_MODELS + [LGB]
COLORS = {"季节朴素法": C_GRAY, "Holt-Winters": C_AQUA, "SARIMA": C_ORANGE, FINAL: "#6250d6", LGB: C_MAGENTA}
THRESHOLD = 0.03            # 判定规则：两个阶段的 WAPE 都比组合模型低 3 个百分点以上才特别标出
FIRST_YEAR = 2021

# 8 个特征：程序里用的名字 -> 图表上显示的名字
FEATURES = {"month": "月份", "region": "地区", "category": "类别", "lag12": "去年同月销量（lag 12）",
            "lag24": "前年同月销量（lag 24）", "mean12": "近 12 个月均值", "mean12_prev": "再往前 12 个月均值",
            "t": "时间序号"}
# 调参的候选值（事先定好的小网格，3 × 2 × 3 × 2 = 36 组）
GRID = {"num_leaves": [4, 8, 16], "n_estimators": [100, 300], "min_child_samples": [5, 10, 20],
        "learning_rate": [0.03, 0.1]}
NEW_FILES = [TABLE_DIR / "机器学习对比.xlsx", FIG_DIR / "m08_五模型对比.png", FIG_DIR / "m09_LightGBM特征重要性.png"]


# =============== 1. 数据和特征 ===============
def load_series():
    """读月度汇总表，返回 {(地区, 类别): 48 个月的销量}"""
    cat = pd.read_csv(PROCESSED_DIR / "月度_地区_类别.csv", encoding="utf-8-sig").sort_values("年月")
    return {key: g["数量"].to_numpy(float) for key, g in cat.groupby(["地区", "类别"])}


def make_rows(series, years):
    """
    每条序列、years 里每一年的每个月各一行。要预测的是这个月的销量（y），
    特征按“这一年的前一年年底”为预测起点来算：只用起点之前的真实销量，往年数据不够的留空（NaN）
    """
    rows = []
    for (region, category), y in series.items():
        for year in years:
            origin = (year - FIRST_YEAR) * 12             # 这一年 1 月在序列里的位置，也就是预测起点
            for month in range(1, 13):
                i = origin + month - 1                    # 要预测的这个月的位置
                rows.append({
                    "地区": region, "类别": category, "年月": f"{year}-{month:02d}", "y": y[i],
                    "month": month, "region": region, "category": category, "t": i + 1,
                    "lag12": y[i - 12] if i >= 12 else np.nan,
                    "lag24": y[i - 24] if i >= 24 else np.nan,
                    "mean12": y[origin - 12:origin].mean() if origin >= 12 else np.nan,
                    "mean12_prev": y[origin - 24:origin - 12].mean() if origin >= 24 else np.nan})
    df = pd.DataFrame(rows)
    for c in ("region", "category"):                      # 地区、类别是类别型特征，类别的顺序固定下来
        df[c] = pd.Categorical(df[c], categories=sorted({k[0 if c == "region" else 1] for k in series}))
    return df


def fit_predict(train, test, params):
    """用训练样本训练一个全局模型，预测 test 里的每一行；预测值小于 0 的截成 0"""
    settings = {"objective": "regression", "seed": SEED, "num_threads": 1, "deterministic": True,
                "force_row_wise": True, "verbose": -1, "num_leaves": params["num_leaves"],
                "min_data_in_leaf": params["min_child_samples"], "learning_rate": params["learning_rate"]}
    data = lgb.Dataset(train[list(FEATURES)], label=train["y"])       # 地区、类别两列是类别型，LightGBM 会自己识别
    model = lgb.train(settings, data, num_boost_round=params["n_estimators"])
    return np.clip(model.predict(test[list(FEATURES)]), 0, None), model


# =============== 2. 指标：和阶段一 step4 的算法相同 ===============
def per_series(pred):
    """每条序列的指标。pred 里有 地区、类别、实际销量、预测 四列"""
    rows = []
    for (region, category), g in pred.groupby(["地区", "类别"]):
        rows.append({"地区": region, "类别": category, "模型": LGB, **metrics(g["实际销量"], g["预测"])})
    return pd.DataFrame(rows).drop(columns=["MSE"])


def overall(pred, by_series):
    """整体指标：平均 MAE、平均 RMSE 是 18 条序列的平均；整体 WAPE 和合并 R² 是把 216 个值放在一起算"""
    err = pred["实际销量"] - pred["预测"]
    sst = ((pred["实际销量"] - pred["实际销量"].mean()) ** 2).sum()
    return {"平均MAE": by_series["MAE"].mean(), "平均RMSE": by_series["RMSE"].mean(),
            "整体WAPE": err.abs().sum() / pred["实际销量"].sum(), "合并R2": 1 - (err ** 2).sum() / sst}


def evaluate(train, test, params):
    pred, model = fit_predict(train, test, params)
    detail = test[["地区", "类别", "年月"]].assign(实际销量=test["y"].to_numpy(), 预测=pred)
    by_series = per_series(detail)
    return detail, by_series, overall(detail, by_series), model


# =============== 3. 调参：只看验证集 ===============
def search(train, valid):
    rows = []
    for values in itertools.product(*GRID.values()):
        params = dict(zip(GRID, values))
        _, _, summary, _ = evaluate(train, valid, params)
        rows.append({**params, "验证集整体WAPE": summary["整体WAPE"], "验证集平均RMSE": summary["平均RMSE"],
                     "验证集合并R2": summary["合并R2"]})
    table = pd.DataFrame(rows)
    # WAPE 最低的排第一；并列时选更简单的：叶子少、树少、每片叶子样本多、步子小
    table = table.sort_values(["验证集整体WAPE", "num_leaves", "n_estimators", "min_child_samples", "learning_rate"],
                              ascending=[True, True, True, False, True]).reset_index(drop=True)
    table.insert(0, "名次", table.index + 1)
    best = {k: table.loc[0, k].item() for k in GRID}
    return table, best


# =============== 4. 和已有的四个方案放在一起 ===============
def five_models(old_summary, old_series, lgb_summary, lgb_series):
    """五个方案的整体对比表和每条序列的对比表。“RMSE 最低的序列数”在五个方案之间重新数"""
    series = pd.concat([old_series[["地区", "类别", "模型", "MAE", "RMSE", "WAPE", "R2"]], lgb_series],
                       ignore_index=True)
    winners = series.loc[series.groupby(["地区", "类别"])["RMSE"].idxmin(), "模型"].value_counts()
    summary = pd.concat([old_summary[["模型", "平均MAE", "平均RMSE", "整体WAPE", "合并R2"]],
                         pd.DataFrame([{"模型": LGB, **lgb_summary}])], ignore_index=True)
    summary["RMSE最低的序列数（五个方案里）"] = summary["模型"].map(winners).fillna(0).astype(int)
    series["模型"] = pd.Categorical(series["模型"], categories=MODELS)
    return summary, series.sort_values(["地区", "类别", "模型"]).reset_index(drop=True)


def decision(val, test):
    """按事先写好的规则判定：LightGBM 是否在两个阶段都比组合模型低 3 个百分点以上"""
    wape = lambda table, model: float(table.loc[table["模型"] == model, "整体WAPE"].iloc[0])
    rows, hits = [], []
    for stage, table in (("验证集（2023年）", val), ("测试集（2024年）", test)):
        gap = wape(table, FINAL) - wape(table, LGB)       # 正数表示 LightGBM 更低（更好）
        hits.append(gap > THRESHOLD)
        rows.append({"阶段": stage, "组合模型WAPE": wape(table, FINAL), "LightGBM的WAPE": wape(table, LGB),
                     "LightGBM低多少（百分点）": gap * 100, "是否低 3 个百分点以上": "是" if gap > THRESHOLD else "否"})
    triggered = all(hits)
    rows.append({"阶段": "结论", "是否低 3 个百分点以上":
                 "两个阶段都满足：触发，需要和老师讨论" if triggered else "没有同时满足：不触发。最终模型不变"})
    return pd.DataFrame(rows), triggered


# =============== 5. 作图 ===============
def fig_five_models(val, test):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    for ax, s, title in zip(axes, [val, test], ["验证集（2023年）", "测试集（2024年）"]):
        vals = s["整体WAPE"] * 100
        ax.bar(s["模型"], vals, color=[COLORS[m] for m in s["模型"]], width=0.55)
        for i, v in enumerate(vals):
            ax.text(i, v, f"{v:.1f}%", ha="center", va="bottom", fontsize=9, color=TEXT2)
        ax.set_title(f"{title} 整体WAPE")
    axes[0].set_ylabel("整体WAPE（%）")
    fig.suptitle("五种方案在验证集与测试集上的误差对比（地区×类别，18条序列）", y=1.02)
    fig.tight_layout()
    save(fig, FIG_DIR / "m08_五模型对比.png")


def fig_importance(importance):
    d = importance.sort_values("增益占比")
    fig, ax = plt.subplots(figsize=(7.5, 4))
    ax.barh(d["特征"], d["增益占比"] * 100, color=C_MAGENTA, height=0.55)
    for i, v in enumerate(d["增益占比"] * 100):
        ax.text(v, i, f" {v:.1f}%", va="center", fontsize=9, color=TEXT2)
    ax.set_xlabel("增益占比（%）")
    ax.set_title("LightGBM 各特征的重要性（测试阶段的模型，2021–2023 年训练）")
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    save(fig, FIG_DIR / "m09_LightGBM特征重要性.png")


def main():
    setup()
    series = load_series()
    assert len(series) == 18 and all(len(y) == 48 for y in series.values())
    old = pd.read_excel(TABLE_DIR / "模型评估结果.xlsx", sheet_name=None)       # 阶段一的结果：只读，不改

    # ---- 验证阶段：2021–2022 训练，预测 2023；36 组参数都试一遍 ----
    train_val, valid = make_rows(series, [2021, 2022]), make_rows(series, [2023])
    grid, best = search(train_val, valid)
    val_detail, val_series, val_summary, _ = evaluate(train_val, valid, best)
    print(f"验证阶段：训练样本 {len(train_val)} 条，预测 {len(valid)} 个值；36 组参数里选出 {best}")

    # ---- 测试阶段：用选好的参数在 2021–2023 上重新训练，预测 2024，只做这一次 ----
    train_test, test = make_rows(series, [2021, 2022, 2023]), make_rows(series, [2024])
    test_detail, test_series, test_summary, model = evaluate(train_test, test, best)
    print(f"测试阶段：训练样本 {len(train_test)} 条，预测 {len(test)} 个值")

    # ---- 核对：实际销量、指标的算法都和阶段一对得上 ----
    saved = old["测试期预测明细"].merge(test_detail, on=["地区", "类别", "年月"], suffixes=("_阶段一", ""))
    assert len(saved) == 216 and (saved["实际销量_阶段一"] == saved["实际销量"]).all()
    check = saved.rename(columns={"预测": "_", FINAL: "预测"})[["地区", "类别", "实际销量", "预测"]]
    recomputed = overall(check, per_series(check))["整体WAPE"]
    stored = float(old["测试集模型对比"].set_index("模型").loc[FINAL, "整体WAPE"])
    assert abs(recomputed - stored) < 1e-4, (recomputed, stored)

    # ---- 五个方案放在一起 ----
    val5, val5_series = five_models(old["验证集模型对比"], old["验证集各序列评估"], val_summary, val_series)
    test5, test5_series = five_models(old["测试集模型对比"], old["各序列评估"], test_summary, test_series)
    verdict, triggered = decision(val5, test5)
    gain = model.feature_importance(importance_type="gain")
    importance = pd.DataFrame({"特征": list(FEATURES.values()), "增益占比": gain / gain.sum(),
                               "被用来分裂的次数": model.feature_importance(importance_type="split")}
                              ).sort_values("增益占比", ascending=False)
    detail = pd.concat([val_detail.assign(阶段="验证集"), test_detail.assign(阶段="测试集")], ignore_index=True)
    detail = detail.rename(columns={"预测": "LightGBM预测"})[["阶段", "地区", "类别", "年月", "实际销量", "LightGBM预测"]]
    notes = pd.DataFrame([
        ("最终模型", "不变，仍是 SARIMA 与 Holt-Winters 的等权组合"),
        ("LightGBM 的做法", "一个全局模型；直接预测 12 个月、不递归；8 个特征都只用预测起点之前的信息"),
        ("选出的参数", "，".join(f"{k}={v}" for k, v in best.items())),
        ("训练样本", f"验证阶段 {len(train_val)} 条，测试阶段 {len(train_test)} 条"),
        ("注意", "LightGBM 在验证集上的数字是 36 组参数里最好的一组，另外四个方案没有这样挑过，验证集的比较对它偏有利；测试集没有参与任何选择"),
        ("另外四个方案的数字", "直接读自阶段一的 模型评估结果.xlsx，没有重新运行"),
        ("随机种子", str(SEED)), ("lightgbm 版本", lgb.__version__),
    ], columns=["项目", "内容"])

    with pd.ExcelWriter(NEW_FILES[0]) as xw:
        val5.round(4).to_excel(xw, sheet_name="验证集五模型对比", index=False)
        test5.round(4).to_excel(xw, sheet_name="测试集五模型对比", index=False)
        verdict.round(4).to_excel(xw, sheet_name="判定规则的结论", index=False)
        val5_series.round(4).to_excel(xw, sheet_name="验证集各序列", index=False)
        test5_series.round(4).to_excel(xw, sheet_name="测试集各序列", index=False)
        detail.round(2).to_excel(xw, sheet_name="LightGBM预测明细", index=False)
        grid.round(4).to_excel(xw, sheet_name="参数搜索（验证集）", index=False)
        importance.round(4).to_excel(xw, sheet_name="特征重要性", index=False)
        notes.to_excel(xw, sheet_name="说明", index=False)
    fig_five_models(val5, test5)
    fig_importance(importance)

    pd.set_option("display.unicode.east_asian_width", True)
    print("\n验证集（2023 年）五个方案：\n", val5.round(4).to_string(index=False))
    print("\n测试集（2024 年）五个方案：\n", test5.round(4).to_string(index=False))
    print("\n判定：\n", verdict.round(4).to_string(index=False))
    print("\n特征重要性：\n", importance.round(4).to_string(index=False))
    print("\n参数搜索前 5 名和最后 1 名：\n", pd.concat([grid.head(5), grid.tail(1)]).round(4).to_string(index=False))
    print("\n是否触发判定规则：", "是" if triggered else "否")
    print("结果在", " / ".join(p.name for p in NEW_FILES))


if __name__ == "__main__":
    main()
