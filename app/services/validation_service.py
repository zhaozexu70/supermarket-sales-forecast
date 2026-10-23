# -*- coding: utf-8 -*-
"""
订单的校验规则（设计说明 8.2）。表格导入和对话框录入用的是同一套：
check_row 检查一行，check_table 检查整张表并算出派生字段
"""
import math
from datetime import date, datetime

import pandas as pd

from app.constants import (CATEGORIES, CATEGORY_SUBS, COUNTRY, ORDER_KEY_LIMIT, PROVINCES, REGIONS,
                           SEGMENTS, SHIP_MODES, TEMPLATE_COLUMNS, TEXT_LIMITS)
from preprocess_core import add_features, strip_text

INT_MAX = 2147483647            # 数据库 INT 字段能存的最大值
MONEY_MAX = 9999999999.99       # 数据库 DECIMAL(12,2) 字段能存的最大值
YEAR_MIN, YEAR_MAX = 2000, 2100
ALL_SUBS = [s for subs in CATEGORY_SUBS.values() for s in subs]
CHOICES = {"邮寄方式": SHIP_MODES, "细分": SEGMENTS, "省/自治区": PROVINCES, "地区": REGIONS,
           "类别": CATEGORIES, "子类别": ALL_SUBS}


def to_text(v):
    """单元格的值统一成文字：空单元格是空字符串，Excel 日期格式的单元格写成 2024-01-05"""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    return str(v).strip()


def parse_day(text):
    """认 2024/01/05 和 2024-01-05 两种写法，认不出来返回 None"""
    text = text.removesuffix(" 00:00:00")
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def parse_number(text):
    """文字转成数字，不是数字返回 None"""
    try:
        x = float(text)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def check_row(r):
    """
    检查一行。r：{列名: 文字}，20 个字段。
    返回 (转好类型的值, 错误列表)，错误列表里每项是 (字段, 原因)，没有问题时为空
    """
    v, errors = dict(r), []

    def bad(field, reason):
        errors.append((field, reason))

    # 1. 必填：除行 ID 外都不能空；国家只能是中国
    for c in TEMPLATE_COLUMNS:
        if c != "行 ID" and r[c] == "":
            bad(c, f"{c}不能为空")
    if r["国家"] not in ("", COUNTRY):
        bad("国家", f"国家只能填“{COUNTRY}”，填的是“{r['国家']}”")

    # 2. 行 ID：可以留空，填了就必须是正整数
    v["行 ID"] = None
    if r["行 ID"]:
        n = parse_number(r["行 ID"])
        if n is None or n != int(n) or not 1 <= n <= INT_MAX:
            bad("行 ID", f"行 ID 应为正整数或留空，填的是“{r['行 ID']}”")
        else:
            v["行 ID"] = int(n)

    # 3. 日期：能认出来，年份合理，发货不早于下单
    days = {}
    for c in ("订单日期", "发货日期"):
        if r[c]:
            d = parse_day(r[c])
            if d is None:
                bad(c, f"{c}“{r[c]}”认不出来，请写成 2024/01/05 或 2024-01-05")
            elif not YEAR_MIN <= d.year <= YEAR_MAX:
                bad(c, f"{c}的年份应在 {YEAR_MIN}–{YEAR_MAX} 之间，填的是“{r[c]}”")
            else:
                days[c] = v[c] = d
    if len(days) == 2 and days["发货日期"] < days["订单日期"]:
        bad("发货日期", f"发货日期（{r['发货日期']}）不能早于订单日期（{r['订单日期']}）")

    # 4. 数值：数量是正整数，销售额大于 0，折扣在 [0, 1) 内，利润是数字（可以为负）
    rules = {"数量": (lambda x: x == int(x) and 1 <= x <= INT_MAX, "数量应为正整数"),
             "销售额": (lambda x: 0 < x <= MONEY_MAX, "销售额应大于 0"),
             "折扣": (lambda x: 0 <= x < 1, "折扣应大于等于 0、小于 1（比如八折填 0.2）"),
             "利润": (lambda x: abs(x) <= MONEY_MAX, "利润的数值太大")}
    for c, (ok, hint) in rules.items():
        if r[c]:
            x = parse_number(r[c])
            if x is None:
                bad(c, f"{c}应为数字，填的是“{r[c]}”")
            elif not ok(x):
                bad(c, f"{hint}，填的是 {r[c]}")
            else:
                v[c] = int(x) if c == "数量" else x

    # 5. 只能从已有取值里选的字段；子类别要属于所填的类别
    for c, options in CHOICES.items():
        if r[c] and r[c] not in options:
            hint = f"，可以填：{'、'.join(options)}" if len(options) <= 6 else ""
            bad(c, f"{c}“{r[c]}”不在已有的取值里{hint}")
    if r["类别"] in CATEGORIES and r["子类别"] in ALL_SUBS and r["子类别"] not in CATEGORY_SUBS[r["类别"]]:
        bad("子类别", f"子类别“{r['子类别']}”不属于类别“{r['类别']}”")

    # 6. 文本长度不能超过数据库字段的长度
    for c, limit in TEXT_LIMITS.items():
        if len(r[c]) > limit:
            bad(c, f"{c}超过 {limit} 个字符（现在 {len(r[c])} 个）")
    return v, errors


def check_table(df):
    """
    检查整张表。df：20 列，值都是文字。
    返回 {"total": 有内容的行数, "ok": 合格的行（已算好派生字段）, "failed": 失败的行, "max_id": 填写的最大行 ID}
    失败的行每项有：row 行号、reason 原因（几条原因用分号连起来）、errors [(字段, 原因)]、values 原来填的内容
    行号是 Excel 里看到的行号：第 1 行是表头，所以第 1 条数据是第 2 行
    """
    df = strip_text(df)                                   # 去首尾空格，和 step1 用同一个函数
    good, failed, max_id, total = [], [], 0, 0
    for i, r in enumerate(df.to_dict("records")):
        if not any(r.values()):                           # 整行都是空的，不算
            continue
        total += 1
        v, errors = check_row(r)
        max_id = max(max_id, v["行 ID"] or 0)
        if errors:
            failed.append({"row": i + 2, "reason": "；".join(msg for _, msg in errors),
                           "errors": errors, "values": r})
        else:
            good.append({**v, "行号": i + 2, "原文": r})

    ok = pd.DataFrame(good)
    if len(ok):
        ok = add_features(ok)                             # 订单键、年月、折后单价、利润率：step1 的规则
        too_long = ok["订单键"].str.len() > ORDER_KEY_LIMIT
        for rec in ok[too_long].to_dict("records"):
            reason = (f"订单 ID 和客户 ID 合起来太长：订单键有 {len(rec['订单键'])} 个字符，"
                      f"最多 {ORDER_KEY_LIMIT} 个")
            failed.append({"row": rec["行号"], "reason": reason, "errors": [("订单 ID", reason)],
                           "values": rec["原文"]})
        ok = ok[~too_long]
    failed.sort(key=lambda f: f["row"])
    return {"total": total, "ok": ok, "failed": failed, "max_id": max_id}
