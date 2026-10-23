# -*- coding: utf-8 -*-
"""
预测核心函数：评价指标、三种预测模型、安全库存与进货量计算
step4_model.py 用它做实验，后面的 Flask 系统也直接调用这里的函数
"""
import itertools
import warnings

import numpy as np
import pandas as pd
from statsmodels.tsa.statespace.sarimax import SARIMAX
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from statsmodels.tsa.stattools import adfuller
from statsmodels.stats.diagnostic import acorr_ljungbox

warnings.filterwarnings("ignore")


def to_series(values, start_ym):
    """把一列数转成按月索引的时间序列（月初为索引）"""
    idx = pd.date_range(start=f"{start_ym}-01", periods=len(values), freq="MS")
    return pd.Series(np.asarray(values, dtype=float), index=idx)


# ---------------- 评价指标 ----------------
def metrics(y_true, y_pred):
    y = np.asarray(y_true, float)
    p = np.asarray(y_pred, float)
    e = y - p
    mse = float(np.mean(e ** 2))
    sst = float(np.sum((y - y.mean()) ** 2))
    return {
        "MAE": float(np.mean(np.abs(e))),
        "MSE": mse,
        "RMSE": float(np.sqrt(mse)),
        # 加权绝对百分比误差：序列中有 0 值时 MAPE 无法计算，改用 WAPE
        "WAPE": float(np.sum(np.abs(e)) / np.sum(np.abs(y))) if np.sum(np.abs(y)) > 0 else np.nan,
        "R2": 1 - float(np.sum(e ** 2)) / sst if sst > 0 else np.nan,
    }


# ---------------- 模型一：季节朴素法 ----------------
def seasonal_naive(train, steps, s=12):
    """用去年同月的销量作为预测值"""
    last = np.asarray(train, float)[-s:]
    return np.array([last[i % s] for i in range(steps)])


# ---------------- 模型二：Holt-Winters 指数平滑 ----------------
def holt_winters(train, steps, s=12):
    model = ExponentialSmoothing(train, trend="add", damped_trend=True,
                                 seasonal="add", seasonal_periods=s,
                                 initialization_method="estimated")
    fit = model.fit()
    return np.clip(np.asarray(fit.forecast(steps), float), 0, None), fit


# ---------------- 模型三：SARIMA ----------------
def adf_pvalue(x):
    x = np.asarray(x, float)
    if len(x) < 8 or np.allclose(x, x[0]):
        return np.nan
    try:
        return float(adfuller(x, autolag="AIC")[1])
    except Exception:
        return np.nan


def choose_d(train, s=12, alpha=0.05):
    """先做一次季节差分，再用 ADF 检验判断是否还需要一阶差分"""
    seasonal_diff = pd.Series(np.asarray(train, float)).diff(s).dropna()
    p = adf_pvalue(seasonal_diff)
    d = 0 if (not np.isnan(p) and p < alpha) else 1
    return d, p


def fit_sarima(train, s=12, p_range=(0, 1), q_range=(0, 1), P_range=(0, 1), Q_range=(0, 1), D=1):
    """
    在候选阶数中按 AIC 最小选择 SARIMA(p,d,q)(P,D,Q)s
    d 由 ADF 检验确定；d=0 时加常数项，用来刻画逐年增长的趋势
    """
    d, adf_p = choose_d(train, s)
    trend = "c" if d == 0 else None
    best = None
    for p, q, P, Q in itertools.product(p_range, q_range, P_range, Q_range):
        try:
            res = SARIMAX(train, order=(p, d, q), seasonal_order=(P, D, Q, s),
                          trend=trend).fit(disp=False)
        except Exception:
            continue
        if not np.isfinite(res.aic):
            continue
        if best is None or res.aic < best["aic"]:
            best = {"res": res, "order": (p, d, q), "seasonal_order": (P, D, Q, s),
                    "trend": trend, "aic": float(res.aic)}
    if best is None:
        raise RuntimeError("所有候选阶数都拟合失败")
    best["adf_p"] = adf_p
    return best


def sarima_forecast(res, steps, alpha=0.05):
    """返回 预测值、区间下限、区间上限（销量不能为负，截断到 0）"""
    fc = res.get_forecast(steps=steps)
    mean = np.asarray(fc.predicted_mean, float)
    ci = np.asarray(fc.conf_int(alpha=alpha), float)
    return np.clip(mean, 0, None), np.clip(ci[:, 0], 0, None), np.clip(ci[:, 1], 0, None)


def refit_sarima(y, order, seasonal_order, trend):
    """用确定好的阶数在全部数据上重新估计参数"""
    return SARIMAX(y, order=tuple(order), seasonal_order=tuple(seasonal_order),
                   trend=trend).fit(disp=False)


def ljungbox_pvalue(res, order, seasonal_order):
    """残差白噪声检验；去掉差分造成的前 d + D*s 个残差"""
    burn = order[1] + seasonal_order[1] * seasonal_order[3]
    r = np.asarray(res.resid, float)[burn:]
    lag = int(min(12, max(1, len(r) // 2 - 1)))
    out = acorr_ljungbox(r, lags=[lag], return_df=True)
    if isinstance(out, pd.DataFrame):
        return float(out["lb_pvalue"].iloc[0])
    return float(np.asarray(out[1])[0])


def order_text(order, seasonal_order):
    p, d, q = order
    P, D, Q, s = seasonal_order
    return f"({p},{d},{q})({P},{D},{Q}){s}"


# ---------------- 最终模型：SARIMA 与 Holt-Winters 等权组合 ----------------
def combine(sarima_mean, sarima_lo, sarima_hi, hw_mean):
    """
    组合预测 = (SARIMA + Holt-Winters) / 2
    区间：沿用 SARIMA 95% 预测区间的上下半宽，以组合预测值为中心（近似处理）
    """
    comb = (np.asarray(sarima_mean) + np.asarray(hw_mean)) / 2
    lo = comb - (np.asarray(sarima_mean) - np.asarray(sarima_lo))
    hi = comb + (np.asarray(sarima_hi) - np.asarray(sarima_mean))
    return np.clip(comb, 0, None), np.clip(lo, 0, None), np.clip(hi, 0, None)


def forecast_combined(y, steps, order=None, seasonal_order=None, trend=None, s=12):
    """
    系统调用的预测入口：给一条月销量序列，返回组合预测及两个单模型结果
    order 为空时先按 AIC 选阶
    """
    if order is None:
        best = fit_sarima(y, s)
        order, seasonal_order, trend = best["order"], best["seasonal_order"], best["trend"]
    res = refit_sarima(y, order, seasonal_order, trend)
    s_mean, s_lo, s_hi = sarima_forecast(res, steps)
    hw_mean, _ = holt_winters(y, steps, s)
    comb, lo, hi = combine(s_mean, s_lo, s_hi, hw_mean)
    return {"组合": comb, "下限": lo, "上限": hi, "SARIMA": s_mean, "Holt-Winters": hw_mean,
            "order": order, "seasonal_order": seasonal_order, "trend": trend}


# ---------------- 安全库存与建议进货量 ----------------
def safety_stock(sigma, z=1.65, lead_time=1):
    """安全库存 = z × σ × √L"""
    return float(z * sigma * np.sqrt(lead_time))


def purchase_plan(forecast, ss, inventory=0, lead_time=1):
    """
    lead_time：补货周期（月），每隔这么多个月进一次货，一次进够接下来这几个月的量
    第 1 次进货（第 1 个月）：这几个月的预测销量之和 + 安全库存 - 当前库存（补到目标库存）
    之后每次进货：接下来这几个月的预测销量之和，安全库存保持不动
    不进货的月份是 0。lead_time = 1 时就是每月进一次货：第 1 个月 = 预测 + 安全库存 - 库存，之后每月 = 当月预测
    """
    plan = []
    for i, f in enumerate(forecast):
        if i % lead_time:                       # 不是进货的月份
            plan.append(0)
            continue
        q = f if lead_time == 1 else float(np.sum(forecast[i:i + lead_time]))
        if i == 0:
            q = q + ss - inventory
        plan.append(int(np.ceil(max(q, 0))))
    return plan
