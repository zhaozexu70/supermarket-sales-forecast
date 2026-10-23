# -*- coding: utf-8 -*-
"""
第四步：进货预测模型的构建、训练与评估
  1. 建模层级：地区×类别 的月销量（18 条序列，零值少，适合直接建模）
  2. 数据集划分（按时间顺序，不打乱）：
       验证阶段 2021-01~2022-12 训练，2023 全年验证 —— 用来比较方案、确定最终模型
       测试阶段 2021-01~2023-12 训练，2024 全年测试 —— 只用来做最终评估
  3. 四种方案对比：季节朴素法、Holt-Winters、SARIMA（ADF 定差分阶数、AIC 网格选阶）、
     SARIMA 与 Holt-Winters 等权组合（最终模型）
  4. 子类别层级：组合预测按销量占比自上而下分配，与子类别直接建模、季节朴素法对比
  5. 用全部 48 个月重新拟合，预测 2025 年 1~6 月销量，计算安全库存和建议进货量
运行：python src/step4_model.py   （需先运行 step1）
"""
import json
import time

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from statsmodels.graphics.tsaplots import plot_acf, plot_pacf

from config import (PROCESSED_DIR, FIG_DIR, TABLE_DIR, MODEL_DIR, DATA_START, VALID_TRAIN_END,
                    TRAIN_END, SEASON, FORECAST_STEPS, SERVICE_LEVEL, Z_TABLE, LEAD_TIME,
                    DENSE_ZERO_RATIO)
from forecast_core import (to_series, metrics, seasonal_naive, holt_winters, fit_sarima,
                           sarima_forecast, refit_sarima, ljungbox_pvalue, order_text, combine,
                           safety_stock, purchase_plan)
from plot_style import setup, save, C_BLUE, C_ORANGE, C_AQUA, C_GRAY, TEXT2

FINAL = "组合模型"
MODELS = ["季节朴素法", "Holt-Winters", "SARIMA", FINAL]
MODEL_COLORS = {"季节朴素法": C_GRAY, "Holt-Winters": C_AQUA, "SARIMA": C_ORANGE, FINAL: "#6250d6"}
Z = Z_TABLE[SERVICE_LEVEL]


def load_series():
    cat = pd.read_csv(PROCESSED_DIR / "月度_地区_类别.csv", encoding="utf-8-sig")
    sub = pd.read_csv(PROCESSED_DIR / "月度_地区_子类别.csv", encoding="utf-8-sig")
    months = sorted(cat["年月"].unique())
    return cat, sub, months


def get_y(df, **cond):
    m = np.ones(len(df), dtype=bool)
    for k, v in cond.items():
        m &= (df[k] == v).values
    return df[m].sort_values("年月")["数量"].values.astype(float)


def four_models(train, h):
    """在一段训练数据上拟合四种方案，返回预测值和 SARIMA 拟合信息"""
    preds = {"季节朴素法": seasonal_naive(train, h, SEASON)}
    preds["Holt-Winters"], _ = holt_winters(train, h, SEASON)
    best = fit_sarima(train, SEASON)
    preds["SARIMA"], s_lo, s_hi = sarima_forecast(best["res"], h)
    preds[FINAL], lo, hi = combine(preds["SARIMA"], s_lo, s_hi, preds["Holt-Winters"])
    return preds, best, lo, hi


def summarize(ev, pred):
    rows = []
    for m in MODELS:
        e = ev[ev["模型"] == m]
        err = pred["实际销量"] - pred[m]
        sst = ((pred["实际销量"] - pred["实际销量"].mean()) ** 2).sum()
        best_count = (ev.loc[ev.groupby(["地区", "类别"])["RMSE"].idxmin(), "模型"] == m).sum()
        rows.append({"模型": m, "平均MAE": e["MAE"].mean(), "平均RMSE": e["RMSE"].mean(),
                     "整体WAPE": err.abs().sum() / pred["实际销量"].sum(),
                     "合并R2": 1 - (err ** 2).sum() / sst,
                     "RMSE最低的序列数": int(best_count)})
    return pd.DataFrame(rows)


# =============== 1. 地区×类别：验证阶段 / 测试阶段 ===============
def run_stage(cat, months, train_end, stage):
    n_train = months.index(train_end) + 1
    eval_rows, pred_rows, param_rows, fitted = [], [], [], {}
    keys = cat[["地区", "类别"]].drop_duplicates().sort_values(["地区", "类别"]).values.tolist()
    t0 = time.time()
    for region, category in keys:
        y = get_y(cat, 地区=region, 类别=category)
        train = to_series(y[:n_train], DATA_START)
        test = y[n_train:n_train + 12]
        preds, best, lo, hi = four_models(train, len(test))
        fitted[(region, category)] = best
        otext = order_text(best["order"], best["seasonal_order"])
        for m in MODELS:
            eval_rows.append({"地区": region, "类别": category, "模型": m,
                              "阶数": otext if m in ("SARIMA", FINAL) else "", **metrics(test, preds[m])})
        lb_p = ljungbox_pvalue(best["res"], best["order"], best["seasonal_order"])
        param_rows.append({"地区": region, "类别": category, "阶数": otext,
                           "常数项": "有" if best["trend"] == "c" else "无", "AIC": best["aic"],
                           "季节差分后ADF检验p值": best["adf_p"], "残差Ljung-Box检验p值": lb_p,
                           "残差是否为白噪声(p>0.05)": "是" if lb_p > 0.05 else "否"})
        for i in range(len(test)):
            pred_rows.append({"地区": region, "类别": category, "年月": months[n_train + i],
                              "实际销量": test[i], **{m: preds[m][i] for m in MODELS},
                              "组合下限": lo[i], "组合上限": hi[i]})
        if stage == "测试":
            print(f"  {region}-{category}: SARIMA{otext}  RMSE 朴素/HW/SARIMA/组合 = " +
                  "/".join(f"{metrics(test, preds[m])['RMSE']:.1f}" for m in MODELS))
    print(f"  {stage}阶段用时 {time.time() - t0:.1f} 秒")
    ev, pred = pd.DataFrame(eval_rows), pd.DataFrame(pred_rows)
    return ev, pred, pd.DataFrame(param_rows), fitted, n_train


# =============== 2. 子类别层级 ===============
def evaluate_subcategory_level(sub, pred_cat, months, n_train):
    last12 = months[n_train - 12:n_train]
    rows, detail = [], []
    keys = sub[["地区", "类别", "子类别"]].drop_duplicates().sort_values(["地区", "类别", "子类别"]).values.tolist()
    n_dense, t0 = 0, time.time()
    for region, category, subcat in keys:
        y = get_y(sub, 地区=region, 子类别=subcat)
        train, test = y[:n_train], y[n_train:]
        same = sub[(sub["地区"] == region) & (sub["类别"] == category) & (sub["年月"].isin(last12))]
        mine = same[same["子类别"] == subcat]["数量"].sum()
        share = mine / same["数量"].sum() if same["数量"].sum() > 0 else 0
        cat_pred = pred_cat[(pred_cat["地区"] == region) & (pred_cat["类别"] == category)][FINAL].values
        preds = {"自上而下分配": share * cat_pred, "季节朴素法": seasonal_naive(train, len(test), SEASON)}
        zero_ratio = float((train == 0).mean())
        dense = zero_ratio <= DENSE_ZERO_RATIO
        if dense:
            n_dense += 1
            best = fit_sarima(to_series(train, DATA_START), SEASON, P_range=(0,))
            preds["子类别直接SARIMA"], _, _ = sarima_forecast(best["res"], len(test))
        for m, p in preds.items():
            rows.append({"地区": region, "类别": category, "子类别": subcat, "训练期零销量占比": zero_ratio,
                         "是否稠密序列": "是" if dense else "否", "方法": m, "分配占比": share,
                         **metrics(test, p)})
        for i in range(len(test)):
            detail.append({"地区": region, "类别": category, "子类别": subcat, "年月": months[n_train + i],
                           "实际销量": test[i], **{m: preds[m][i] for m in preds}})
    print(f"  子类别层级：{len(keys)} 条序列，其中稠密序列 {n_dense} 条，用时 {time.time() - t0:.1f} 秒")
    return pd.DataFrame(rows), pd.DataFrame(detail)


def summarize_sub(detail):
    out = []
    scopes = [("全部102条序列", detail.index == detail.index, ["自上而下分配", "季节朴素法"])]
    if "子类别直接SARIMA" in detail.columns:
        scopes.append(("稠密序列", detail["子类别直接SARIMA"].notna(),
                       ["自上而下分配", "季节朴素法", "子类别直接SARIMA"]))
    for scope, mask, methods in scopes:
        d = detail[mask]
        grp = [d["地区"], d["子类别"]]
        for m in methods:
            err = d["实际销量"] - d[m]
            out.append({"范围": scope, "方法": m, "序列数": d.groupby(["地区", "子类别"]).ngroups,
                        "平均MAE": err.abs().groupby(grp).mean().mean(),
                        "平均RMSE": np.sqrt((err ** 2).groupby(grp).mean()).mean(),
                        "整体WAPE": err.abs().sum() / d["实际销量"].sum()})
    return pd.DataFrame(out)


# =============== 3. 全量重拟合，预测未来并计算进货量 ===============
def forecast_future(cat, sub, months, fitted, ev_test, ev_sub):
    future = pd.date_range(f"{months[-1]}-01", periods=FORECAST_STEPS + 1, freq="MS")[1:].strftime("%Y-%m")
    last12 = months[-12:]
    final_rmse = ev_test[ev_test["模型"] == FINAL].set_index(["地区", "类别"])["RMSE"]
    sub_rmse = ev_sub[ev_sub["方法"] == "自上而下分配"].set_index(["地区", "子类别"])["RMSE"]
    cat_rows, sub_rows = [], []
    config = {"final_model": "SARIMA 与 Holt-Winters 等权组合", "service_level": SERVICE_LEVEL, "z": Z,
              "lead_time": LEAD_TIME, "last_month": months[-1], "series": {}}

    for (region, category), best in fitted.items():
        y = to_series(get_y(cat, 地区=region, 类别=category), DATA_START)
        res = refit_sarima(y, best["order"], best["seasonal_order"], best["trend"])
        s_mean, s_lo, s_hi = sarima_forecast(res, FORECAST_STEPS)
        hw, _ = holt_winters(y, FORECAST_STEPS, SEASON)
        fc, lo, hi = combine(s_mean, s_lo, s_hi, hw)
        sigma = float(final_rmse.loc[(region, category)])
        ss = safety_stock(sigma, Z, LEAD_TIME)
        plan = purchase_plan(fc, ss, inventory=0)
        for i, ym in enumerate(future):
            cat_rows.append({"层级": "地区类别", "地区": region, "类别": category, "子类别": None,
                             "预测月份": ym, "SARIMA预测": round(s_mean[i], 2), "Holt-Winters预测": round(hw[i], 2),
                             "预测销量": round(fc[i], 2), "预测下限": round(lo[i], 2), "预测上限": round(hi[i], 2),
                             "安全库存": round(ss, 2), "建议进货量": plan[i]})
        same = sub[(sub["地区"] == region) & (sub["类别"] == category) & (sub["年月"].isin(last12))]
        shares = (same.groupby("子类别")["数量"].sum() / max(same["数量"].sum(), 1)).to_dict()
        config["series"][f"{region}|{category}"] = {
            "sarima_order": list(best["order"]), "seasonal_order": list(best["seasonal_order"]),
            "trend": best["trend"], "test_rmse": sigma, "sub_shares": shares}
        for subcat, share in shares.items():
            sss = safety_stock(float(sub_rmse.loc[(region, subcat)]), Z, LEAD_TIME)
            sfc = share * fc
            splan = purchase_plan(sfc, sss, inventory=0)
            for i, ym in enumerate(future):
                sub_rows.append({"层级": "地区子类别", "地区": region, "类别": category, "子类别": subcat,
                                 "预测月份": ym, "分配占比": round(share, 4), "预测销量": round(sfc[i], 2),
                                 "预测下限": round(share * lo[i], 2), "预测上限": round(share * hi[i], 2),
                                 "安全库存": round(sss, 2), "建议进货量": splan[i]})
    return pd.DataFrame(cat_rows), pd.DataFrame(sub_rows), config


# =============== 4. 作图 ===============
def fig_acf_pacf(cat, region, category):
    s = to_series(get_y(cat, 地区=region, 类别=category), DATA_START)
    diff = s.diff(SEASON).dropna()
    fig, axes = plt.subplots(2, 2, figsize=(11, 6.5))
    axes[0, 0].plot(s.index, s.values, color=C_BLUE)
    axes[0, 0].set_title(f"{region}-{category} 月销量原序列")
    axes[0, 0].set_ylabel("月销量（件）")
    axes[0, 1].plot(diff.index, diff.values, color=C_BLUE)
    axes[0, 1].axhline(0, color=TEXT2, linewidth=0.8)
    axes[0, 1].set_title("季节差分（滞后12）后的序列")
    plot_acf(diff, ax=axes[1, 0], lags=16, color=C_BLUE, vlines_kwargs={"colors": C_BLUE})
    axes[1, 0].set_title("季节差分后序列的自相关图（ACF）")
    plot_pacf(diff, ax=axes[1, 1], lags=16, method="ywm", color=C_BLUE, vlines_kwargs={"colors": C_BLUE})
    axes[1, 1].set_title("季节差分后序列的偏自相关图（PACF）")
    fig.tight_layout()
    save(fig, FIG_DIR / "m01_ACF与PACF图.png")


def fig_test_compare(pred, cat, months, n_train, keys):
    fig, axes = plt.subplots(3, 2, figsize=(11, 10))
    x_all = pd.to_datetime(pd.Series(months))
    for ax, (region, category) in zip(axes.ravel(), keys):
        y = get_y(cat, 地区=region, 类别=category)
        p = pred[(pred["地区"] == region) & (pred["类别"] == category)]
        xt = pd.to_datetime(p["年月"])
        ax.plot(x_all, y, color=C_BLUE, label="实际销量")
        ax.fill_between(xt, p["组合下限"], p["组合上限"], color=MODEL_COLORS[FINAL], alpha=0.10, linewidth=0)
        for m in MODELS:
            ax.plot(xt, p[m], color=MODEL_COLORS[m], linestyle="--" if m == "季节朴素法" else "-",
                    linewidth=2.2 if m == FINAL else 1.4, label=m)
        ax.axvline(x_all[n_train], color=TEXT2, linewidth=0.8, linestyle=":")
        ax.set_title(f"{region}-{category}")
        ax.set_ylabel("月销量（件）")
    axes[0, 0].legend(ncol=2, fontsize=8)
    fig.suptitle("测试期（2024年）各模型预测值与实际销量对比（点线左侧为训练期）", y=1.0)
    fig.tight_layout()
    save(fig, FIG_DIR / "m02_测试期预测对比.png")


def fig_model_bar(sum_val, sum_test):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    for ax, s, title in zip(axes, [sum_val, sum_test], ["验证集（2023年）", "测试集（2024年）"]):
        vals = s["整体WAPE"] * 100
        ax.bar(s["模型"], vals, color=[MODEL_COLORS[m] for m in s["模型"]], width=0.55)
        for i, v in enumerate(vals):
            ax.text(i, v, f"{v:.1f}%", ha="center", va="bottom", fontsize=9, color=TEXT2)
        ax.set_title(f"{title} 整体WAPE")
    axes[0].set_ylabel("整体WAPE（%）")
    fig.suptitle("四种方案在验证集与测试集上的误差对比（地区×类别，18条序列）", y=1.02)
    fig.tight_layout()
    save(fig, FIG_DIR / "m03_模型误差对比.png")


def fig_residual(fitted, region, category):
    best = fitted[(region, category)]
    burn = best["order"][1] + best["seasonal_order"][1] * SEASON
    r = pd.Series(np.asarray(best["res"].resid, float)[burn:])
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.8))
    axes[0].plot(range(1, len(r) + 1), r.values, color=C_BLUE, marker="o", markersize=3)
    axes[0].axhline(0, color=TEXT2, linewidth=0.8)
    axes[0].set_title("残差序列")
    axes[1].hist(r, bins=10, color=C_BLUE, edgecolor="white")
    axes[1].set_title("残差分布")
    plot_acf(r, ax=axes[2], lags=min(10, len(r) // 2 - 1), color=C_BLUE, vlines_kwargs={"colors": C_BLUE})
    axes[2].set_title("残差自相关图")
    fig.suptitle(f"{region}-{category} SARIMA{order_text(best['order'], best['seasonal_order'])} 残差诊断", y=1.03)
    fig.tight_layout()
    save(fig, FIG_DIR / "m04_SARIMA残差诊断.png")


def fig_future(cat, cat_fc, region, category):
    y = cat[(cat["地区"] == region) & (cat["类别"] == category)].sort_values("年月")
    f = cat_fc[(cat_fc["地区"] == region) & (cat_fc["类别"] == category)]
    fig, ax = plt.subplots(figsize=(10, 4.2))
    ax.plot(pd.to_datetime(y["年月"]), y["数量"], color=C_BLUE, label="历史销量")
    xf = pd.to_datetime(f["预测月份"])
    ax.fill_between(xf, f["预测下限"], f["预测上限"], color=MODEL_COLORS[FINAL], alpha=0.12, linewidth=0,
                    label="95%预测区间")
    ax.plot(xf, f["预测销量"], color=MODEL_COLORS[FINAL], marker="o", markersize=4, label="组合模型预测")
    ax.plot(xf, f["预测销量"] + f["安全库存"], color=C_ORANGE, linestyle=":", linewidth=1.6,
            label="预测销量＋安全库存")
    ax.set_title(f"{region}-{category} 未来6个月销量预测")
    ax.set_ylabel("月销量（件）")
    ax.legend(ncol=4, fontsize=8, loc="upper left")
    save(fig, FIG_DIR / "m05_未来6个月预测.png")


def fig_total(pred, cat, months, n_train):
    total = cat.groupby("年月")["数量"].sum().reindex(months)
    p = pred.groupby("年月")[MODELS].sum()
    fig, ax = plt.subplots(figsize=(10, 4.2))
    ax.plot(pd.to_datetime(total.index), total.values, color=C_BLUE, label="实际销量")
    for m in MODELS:
        ax.plot(pd.to_datetime(p.index), p[m], color=MODEL_COLORS[m],
                linestyle="--" if m == "季节朴素法" else "-", linewidth=2.2 if m == FINAL else 1.4, label=m)
    ax.axvline(pd.to_datetime(months[n_train]), color=TEXT2, linewidth=0.8, linestyle=":")
    ax.set_title("全部地区类别合计月销量：测试期预测与实际对比")
    ax.set_ylabel("月销量（件）")
    ax.legend(ncol=5, fontsize=9, loc="upper left")
    save(fig, FIG_DIR / "m06_合计销量预测对比.png")


def fig_sub_compare(sub_sum):
    d = sub_sum[sub_sum["范围"] == "稠密序列"]
    if d.empty:
        return
    colors = {"自上而下分配": MODEL_COLORS[FINAL], "季节朴素法": C_GRAY, "子类别直接SARIMA": C_ORANGE}
    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    for ax, col, title in zip(axes, ["平均RMSE", "整体WAPE"], ["平均RMSE（件）", "整体WAPE（%）"]):
        vals = d[col] * (100 if col == "整体WAPE" else 1)
        ax.bar(d["方法"], vals, color=[colors[m] for m in d["方法"]], width=0.55)
        for i, v in enumerate(vals):
            ax.text(i, v, f"{v:.1f}", ha="center", va="bottom", fontsize=9, color=TEXT2)
        ax.set_title(title)
    fig.suptitle(f"子类别层级三种做法在测试期的对比（稠密序列 {int(d['序列数'].iloc[0])} 条）", y=1.02)
    fig.tight_layout()
    save(fig, FIG_DIR / "m07_子类别预测方法对比.png")


def main():
    setup()
    cat, sub, months = load_series()

    print("[1/5] 验证阶段：2021-2022 训练，2023 验证")
    ev_val, pred_val, _, _, _ = run_stage(cat, months, VALID_TRAIN_END, "验证")
    sum_val = summarize(ev_val, pred_val)
    print(sum_val.round(3).to_string(index=False))

    print("[2/5] 测试阶段：2021-2023 训练，2024 测试")
    ev, pred, params, fitted, n_train = run_stage(cat, months, TRAIN_END, "测试")
    sum_test = summarize(ev, pred)
    print(sum_test.round(3).to_string(index=False))

    print("[3/5] 子类别层级对比")
    ev_sub, detail = evaluate_subcategory_level(sub, pred, months, n_train)
    sub_sum = summarize_sub(detail)
    print(sub_sum.round(3).to_string(index=False))

    print("[4/5] 全量重拟合，预测未来 6 个月并计算进货量")
    cat_fc, sub_fc, config = forecast_future(cat, sub, months, fitted, ev, ev_sub)
    all_fc = pd.concat([cat_fc.drop(columns=["SARIMA预测", "Holt-Winters预测"]),
                        sub_fc.drop(columns=["分配占比"])], ignore_index=True)

    print("[5/5] 保存结果与作图")
    with pd.ExcelWriter(TABLE_DIR / "模型评估结果.xlsx") as xw:
        sum_val.round(4).to_excel(xw, sheet_name="验证集模型对比", index=False)
        sum_test.round(4).to_excel(xw, sheet_name="测试集模型对比", index=False)
        ev.round(4).to_excel(xw, sheet_name="各序列评估", index=False)
        params.round(4).to_excel(xw, sheet_name="SARIMA参数与检验", index=False)
        pred.round(2).to_excel(xw, sheet_name="测试期预测明细", index=False)
        ev_val.round(4).to_excel(xw, sheet_name="验证集各序列评估", index=False)
        sub_sum.round(4).to_excel(xw, sheet_name="子类别方法对比", index=False)
        ev_sub.round(4).to_excel(xw, sheet_name="子类别评估明细", index=False)
    with pd.ExcelWriter(TABLE_DIR / "进货预测结果.xlsx") as xw:
        cat_fc.to_excel(xw, sheet_name="地区类别预测", index=False)
        sub_fc.to_excel(xw, sheet_name="地区子类别预测", index=False)
        all_fc.to_excel(xw, sheet_name="全部预测结果", index=False)
    with open(MODEL_DIR / "model_config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    vol = cat.groupby(["地区", "类别"])["数量"].sum().sort_values(ascending=False)
    rep, top6 = vol.index[0], list(vol.index[:6])
    fig_acf_pacf(cat, *rep)
    fig_test_compare(pred, cat, months, n_train, top6)
    fig_model_bar(sum_val, sum_test)
    fig_residual(fitted, *rep)
    fig_future(cat, cat_fc, *rep)
    fig_total(pred, cat, months, n_train)
    fig_sub_compare(sub_sum)
    print("全部完成：结果见 outputs/tables、outputs/figures、outputs/models")


if __name__ == "__main__":
    main()
