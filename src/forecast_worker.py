# -*- coding: utf-8 -*-
"""
一条“地区 × 类别”序列的完整预测流程（设计说明第 9 节），模型都调用 forecast_core.py。
这个文件只做计算，不连数据库：输入是一个普通的字典，输出也是一个普通的字典，
所以既可以在网站的进程里直接调用，也可以交给另外开的进程去算（并行预测）
"""
import numpy as np
from threadpoolctl import threadpool_limits

from config import SEASON
from forecast_core import (combine, fit_sarima, forecast_combined, holt_winters, metrics, purchase_plan,
                           safety_stock, sarima_forecast, to_series)

STEPS = 12          # 回测最后 12 个月；向后预测 12 个月（预测年份的 1–12 月）


def forecast_series(job):
    """
    job：{"region", "category", "start_ym": 历史数据的第一个月, "y": 这条序列每个月的销量（至少 36 个月）,
          "subs": {子类别: 每个月的销量}（按子类别预测时才有）,
          "z": 服务水平对应的系数, "lead_time": 补货周期, "inventory": 当前库存, "sub_inventory": {子类别: 库存}}
    返回这条序列的预测结果；出错时返回 {"error": 原因}，由调用的一方决定怎么处理
    """
    try:
        # 计算时底层的数值库只用 1 个线程。线程数不同，算出来的数在小数点后第 7 位以后会有差别；
        # 固定成 1 个，不管是在网站的进程里算还是在另开的进程里算，结果都一模一样
        with threadpool_limits(limits=1):
            return _forecast(job)
    except Exception as e:                      # 任何一步失败都不让进程崩掉，把原因带回去
        return {"region": job["region"], "category": job["category"], "error": f"{type(e).__name__}: {e}"}


def _forecast(job):
    y = np.asarray(job["y"], float)
    z, lead = job["z"], job["lead_time"]

    # 1. 回测拆分：最后 12 个月留出来，前面的做训练
    train, test = to_series(y[:-STEPS], job["start_ym"]), y[-STEPS:]
    # 2. 在训练段上选阶（AIC 最小）
    best = fit_sarima(train, SEASON)
    # 3. 训练段上两个模型各预测 12 个月，组合后和留出来的 12 个月对比，RMSE 作为 σ
    s_mean, s_lo, s_hi = sarima_forecast(best["res"], STEPS)
    hw_mean, _ = holt_winters(train, STEPS, SEASON)
    back, _, _ = combine(s_mean, s_lo, s_hi, hw_mean)
    sigma = metrics(test, back)["RMSE"]
    # 4、5. 用选好的阶数在全部历史数据上重新估计，预测 12 个月，两个模型组合
    full = forecast_combined(to_series(y, job["start_ym"]), STEPS, best["order"], best["seasonal_order"],
                             best["trend"], SEASON)
    fc, lo, hi = full["组合"], full["下限"], full["上限"]
    # 6、7. 安全库存 = z × σ × √L；建议进货量
    ss = safety_stock(sigma, z, lead)
    result = {"region": job["region"], "category": job["category"],
              "order": list(best["order"]), "seasonal_order": list(best["seasonal_order"]), "trend": best["trend"],
              "sigma": sigma, "forecast": fc.tolist(), "lower": lo.tolist(), "upper": hi.tolist(),
              "safety_stock": ss, "plan": purchase_plan(fc, ss, job.get("inventory", 0), lead), "subs": {}}

    # 子类别：自上而下分配。占比 = 这个子类别在某 12 个月里占本地区本类别销量的比例
    subs = {name: np.asarray(values, float) for name, values in job.get("subs", {}).items()}
    before = sum(s[-2 * STEPS:-STEPS].sum() for s in subs.values())      # 回测段之前 12 个月的类别销量
    last = sum(s[-STEPS:].sum() for s in subs.values())                 # 历史最后 12 个月的类别销量
    for name, s in subs.items():
        # 子类别的 σ：用回测段之前 12 个月的占比去分回测时的类别预测，和子类别自己的实际值对比
        back_share = s[-2 * STEPS:-STEPS].sum() / before if before > 0 else 0
        sub_sigma = metrics(s[-STEPS:], back_share * back)["RMSE"]
        # 预测：用历史最后 12 个月的占比去分类别的组合预测，区间按同一个占比缩放
        share = s[-STEPS:].sum() / max(last, 1)
        sub_fc, sub_ss = share * fc, safety_stock(sub_sigma, z, lead)
        result["subs"][name] = {
            "share": float(share), "sigma": sub_sigma, "forecast": sub_fc.tolist(),
            "lower": (share * lo).tolist(), "upper": (share * hi).tolist(), "safety_stock": sub_ss,
            "plan": purchase_plan(sub_fc, sub_ss, job.get("sub_inventory", {}).get(name, 0), lead)}
    return result
