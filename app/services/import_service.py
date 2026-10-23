# -*- coding: utf-8 -*-
"""
表格导入、对话框录入、导入记录（设计说明 8.2 的 (3)(4)(5)）
流程：上传 → 文件先放到临时文件夹 → 校验并预览 → 用户点“确认导入”→ 重新校验 → 一次事务写入
"""
import json
import re
import secrets
import time
from datetime import date
from io import BytesIO

import pandas as pd
from flask import current_app
from sqlalchemy import text

from app.constants import (COUNTRY, ORDER_COLUMNS, ORDER_COLUMNS_CN, SOURCE_FILE, SOURCE_MANUAL,
                           SOURCE_RAW, TEMPLATE_COLUMNS)
from app.db import fetch_all, fetch_one, get_engine, write_transaction
from app.services import state_service
from app.services.validation_service import check_table, to_text

ALLOWED_TYPES = (".xlsx", ".csv")
KEEP_SECONDS = 24 * 3600                  # 临时文件最多留 1 天
INSERT_FIELDS = [en for en in ORDER_COLUMNS]          # sales_order 的 25 个字段
INSERT_SQL = text(f"INSERT INTO sales_order ({', '.join(INSERT_FIELDS)}) "
                  f"VALUES ({', '.join(':' + f for f in INSERT_FIELDS)})")


class FileRejected(Exception):
    """文件本身有问题（类型不对、读不了、表头不一致），整份文件都不能导入"""


# ---------------- 临时文件 ----------------
def temp_dir():
    folder = current_app.config["UPLOAD_TMP"]
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def clean_temp_files(folder, now=None):
    """删掉临时文件夹里超过 1 天的文件，网站启动时调用一次；返回删了几个"""
    now, removed = now or time.time(), 0
    for p in folder.glob("*"):
        if p.is_file() and now - p.stat().st_mtime > KEEP_SECONDS:
            p.unlink()
            removed += 1
    return removed


def save_upload(file):
    """把上传的文件用随机名字存到临时文件夹，返回 (随机名字, 原文件名)。原文件名只用来显示，不用来存"""
    name = (file.filename or "").strip()
    ext = name[name.rfind("."):].lower() if "." in name else ""
    if ext not in ALLOWED_TYPES:
        raise FileRejected("只能上传 .xlsx 或 .csv 文件")
    token = secrets.token_hex(16) + ext
    file.save(temp_dir() / token)
    return token, name[-200:]


def temp_path(token):
    """随机名字 -> 文件路径。名字不符合格式或文件不在了，返回 None"""
    if not re.fullmatch(r"[0-9a-f]{32}\.(xlsx|csv)", token or ""):
        return None
    path = temp_dir() / token
    return path if path.exists() else None


def remove_temp(token):
    path = temp_path(token)
    if path:
        path.unlink()


# ---------------- 导入模板 ----------------
EXAMPLE_ROW = ["", "示例-0001", "2025/01/05", "2025/01/07", "标准级", "示例客户-001", "示例客户", "公司", "上海",
               "上海", COUNTRY, "华东", "示例产品-001", "办公用品", "纸张", "示例商品（请改成实际商品名）",
               120.5, 3, 0.1, 25.3]
NOTES = [
    ("怎么用", "在“订单”工作表里从第 2 行开始填，一行是一笔订单里的一种商品。第 1 行的列名不要改。第 2 行是示例，请删掉或改成自己的数据"),
    ("行 ID", "建议留空，由系统自动生成。填了的话必须是正整数；系统里已有的行 ID 会被跳过"),
    ("订单 ID、客户 ID、客户名称、城市", "必填，最多 30 个字"),
    ("产品 ID / 产品名称", "必填。产品 ID 最多 40 个字，产品名称最多 200 个字"),
    ("订单日期、发货日期", "必填，写成 2025/01/05 或 2025-01-05。发货日期不能早于订单日期"),
    ("邮寄方式", "必填，只能是：当日、一级、二级、标准级"),
    ("细分", "必填，只能是：消费者、公司、小型企业"),
    ("省/自治区", "必填，写简称，比如 上海、广东、内蒙古（共 31 个）"),
    ("国家", "必填，只能是：中国"),
    ("地区", "必填，只能是：华东、中南、东北、华北、西南、西北"),
    ("类别、子类别", "必填。办公用品：信封、器具、收纳具、标签、用品、系固件、纸张、美术、装订机；"
                "技术：复印机、电话、设备、配件；家具：书架、桌子、椅子、用具"),
    ("销售额", "必填，大于 0"),
    ("数量", "必填，正整数"),
    ("折扣", "必填，大于等于 0、小于 1。不打折填 0，八折填 0.2"),
    ("利润", "必填，可以是负数"),
]


def template_xlsx():
    """导入模板：第一个工作表是 20 个列名加一行示例（行 ID 留空），第二个工作表是填写说明"""
    buf = BytesIO()
    with pd.ExcelWriter(buf) as xw:
        pd.DataFrame([EXAMPLE_ROW], columns=TEMPLATE_COLUMNS).to_excel(xw, sheet_name="订单", index=False)
        pd.DataFrame(NOTES, columns=["字段", "怎么填"]).to_excel(xw, sheet_name="填写说明", index=False)
        xw.sheets["填写说明"].column_dimensions["A"].width = 34
        xw.sheets["填写说明"].column_dimensions["B"].width = 110
    buf.seek(0)
    return buf


# ---------------- 读文件、校验 ----------------
def read_table(path):
    """读 xlsx 或 csv，检查表头，返回 20 列、值都是文字的表"""
    try:
        if path.suffix == ".csv":
            try:                                          # 先按 UTF-8 读，不行再按 GBK 读
                df = pd.read_csv(path, dtype=str, keep_default_na=False, skip_blank_lines=False,
                                 encoding="utf-8-sig")
            except UnicodeDecodeError:
                df = pd.read_csv(path, dtype=str, keep_default_na=False, skip_blank_lines=False,
                                 encoding="gbk")
        else:
            df = pd.read_excel(path, dtype=object)
    except Exception:
        raise FileRejected("文件读不出来，请确认它是用模板填写的 .xlsx 或 .csv 文件")

    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in TEMPLATE_COLUMNS if c not in df.columns]
    extra = [c for c in df.columns if c not in TEMPLATE_COLUMNS]
    if missing or extra:
        parts = (["缺少列：" + "、".join(missing)] if missing else []) + \
                (["多出列：" + "、".join(extra)] if extra else [])
        raise FileRejected("表头与导入模板不一致。" + "；".join(parts) + "。请下载模板，按模板的列填写")
    return df[TEMPLATE_COLUMNS].apply(lambda col: col.map(to_text))


def split_duplicates(ok, existing_ids, db_max_id, file_max_id):
    """
    处理行 ID：填了且已存在（或在本文件里重复）的行跳过；留空的按文件里的顺序自动编号。
    自动编号从“数据库里最大的行 ID”和“本文件里填写的最大行 ID”中较大的一个加 1 开始
    返回 (要导入的行, 跳过的行)
    """
    keep, skipped, seen = [], [], set()
    next_id = max(db_max_id, file_max_id) + 1
    for rec in ok.to_dict("records"):
        rid = None if pd.isna(rec["行 ID"]) else int(rec["行 ID"])
        if rid is None:
            rid, next_id = next_id, next_id + 1
        elif rid in existing_ids or rid in seen:
            where = "系统里已存在" if rid in existing_ids else "在本文件里重复出现"
            skipped.append({"row": rec["行号"], "reason": f"行 ID {rid} {where}，已跳过", "values": rec["原文"]})
            continue
        seen.add(rid)
        keep.append({**rec, "行 ID": rid})
    return pd.DataFrame(keep), skipped


def check_file(path, conn):
    """校验一份文件，只读不写。返回 total、rows（要导入的行）、failed、skipped"""
    result = check_table(read_table(path))
    existing = set(conn.execute(text("SELECT row_id FROM sales_order")).scalars())
    rows, skipped = split_duplicates(result["ok"], existing, max(existing, default=0), result["max_id"])
    return {"total": result["total"], "rows": rows, "failed": result["failed"], "skipped": skipped}


def preview_file(path):
    """上传后的预览：只校验，什么都不写"""
    with get_engine().connect() as conn:
        return check_file(path, conn)


def preview_rows(rows, n=20):
    """预览用：前 n 条要导入的行，只取模板的 20 列"""
    if not len(rows):
        return []
    show = rows.head(n).copy()
    for c in ("订单日期", "发货日期"):
        show[c] = show[c].dt.strftime("%Y-%m-%d")
    return show[TEMPLATE_COLUMNS].to_dict("records")


# ---------------- 写入数据库 ----------------
def to_db_rows(rows, source, batch_id):
    """把中文列名的表改成数据库字段名，补上来源和批次号"""
    out = rows.copy()
    for c in ("订单日期", "发货日期"):
        out[c] = out[c].dt.date
    out["数据来源"], out["导入批次"] = source, batch_id
    out = out.rename(columns=ORDER_COLUMNS_CN)
    return out[INSERT_FIELDS].to_dict("records")


def write_batch(conn, rows, method, file_name, counts, user_id):
    """在一个事务里：记一条导入批次 → 批量写入订单 → 订单涉及的年份版本号加 1。返回批次号"""
    batch_id = conn.execute(text(
        "INSERT INTO import_batch (method, file_name, total_rows, success_rows, failed_rows, "
        "skipped_rows, user_id) VALUES (:method, :file_name, :total, :success, :failed, :skipped, :user_id)"),
        {"method": method, "file_name": file_name, "user_id": user_id, **counts}).lastrowid
    records = to_db_rows(rows, method, batch_id)
    for i in range(0, len(records), 1000):                # 每 1000 行合成一条 INSERT，比一行一行写快
        conn.execute(INSERT_SQL, records[i:i + 1000])
    state_service.bump_years(conn, sorted({r["order_date"].year for r in records}))
    return batch_id


def import_file(path, file_name, user_id):
    """确认导入：重新校验一遍，再写入。返回 (批次号, 明细)；没有可导入的行时批次号为 None"""
    started = time.perf_counter()
    with write_transaction() as conn:
        result = check_file(path, conn)
        counts = {"total": result["total"], "success": len(result["rows"]),
                  "failed": len(result["failed"]), "skipped": len(result["skipped"])}
        if not counts["success"]:
            return None, result
        batch_id = write_batch(conn, result["rows"], SOURCE_FILE, file_name, counts, user_id)
    detail = {"failed": [{k: f[k] for k in ("row", "reason", "values")} for f in result["failed"]],
              "skipped": result["skipped"], "seconds": round(time.perf_counter() - started, 2)}
    (temp_dir() / f"batch_{batch_id}.json").write_text(json.dumps(detail, ensure_ascii=False),
                                                       encoding="utf-8")
    return batch_id, result


def batch_detail(batch_id):
    """某一批的失败、跳过明细和用时。明细是临时保存的，被清理后返回 None"""
    path = temp_dir() / f"batch_{int(batch_id)}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def problems_xlsx(failed, skipped):
    """失败明细文件：行号、原因，后面跟着这一行原来填的 20 个字段，方便改好后再导入"""
    def sheet(items, label):
        return pd.DataFrame([{"Excel 行号": f["row"], label: f["reason"], **f["values"]} for f in items],
                            columns=["Excel 行号", label] + TEMPLATE_COLUMNS)
    buf = BytesIO()
    with pd.ExcelWriter(buf) as xw:
        sheet(failed, "失败原因").to_excel(xw, sheet_name="失败的行", index=False)
        sheet(skipped, "跳过原因").to_excel(xw, sheet_name="跳过的行", index=False)
    buf.seek(0)
    return buf


# ---------------- 对话框录入 ----------------
def next_manual_id(conn, field, today=None):
    """自动生成订单 ID 或产品 ID：MAN-年月日-序号，序号接着当天已有的最大序号往下编"""
    prefix = f"MAN-{today or date.today():%Y%m%d}-"
    used = conn.execute(text(f"SELECT {field} FROM sales_order WHERE {field} LIKE :p"),
                        {"p": prefix + "%"}).scalars()
    numbers = [int(s[len(prefix):]) for s in used if s[len(prefix):].isdigit()]
    return prefix + str(max(numbers, default=0) + 1)


def add_order(form, user_id, today=None):
    """
    对话框录入一条订单。form：{数据库字段名: 填的内容}。
    返回 (行 ID, 批次号, 错误列表)；错误列表里每项是 {"field": 字段名, "msg": 原因}
    """
    r = {cn: to_text(form.get(en)) for en, cn in ORDER_COLUMNS.items() if cn in TEMPLATE_COLUMNS}
    r["国家"], r["行 ID"] = COUNTRY, ""
    with write_transaction() as conn:
        for field, cn in (("order_id", "订单 ID"), ("product_id", "产品 ID")):      # 留空就自动生成
            r[cn] = r[cn] or next_manual_id(conn, field, today)
        result = check_table(pd.DataFrame([r], columns=TEMPLATE_COLUMNS))
        if result["failed"]:
            return None, None, [{"field": ORDER_COLUMNS_CN.get(c, ""), "msg": m}
                                for c, m in result["failed"][0]["errors"]]
        db_max = conn.execute(text("SELECT COALESCE(MAX(row_id), 0) FROM sales_order")).scalar()
        rows, _ = split_duplicates(result["ok"], set(), db_max, 0)
        counts = {"total": 1, "success": 1, "failed": 0, "skipped": 0}
        batch_id = write_batch(conn, rows, SOURCE_MANUAL, None, counts, user_id)   # 每条单独一个批次
    return int(rows["行 ID"].iloc[0]), batch_id, []


# ---------------- 导入记录、撤销、恢复 ----------------
def list_batches(limit=200):
    """导入批次，新的在前"""
    return fetch_all(
        "SELECT b.id, b.method, b.file_name, b.total_rows, b.success_rows, b.failed_rows, b.skipped_rows, "
        "b.created_at, b.revoked, u.username, "
        "(SELECT COUNT(*) FROM sales_order o WHERE o.batch_id = b.id) AS current_rows "
        "FROM import_batch b LEFT JOIN sys_user u ON u.id = b.user_id ORDER BY b.id DESC LIMIT :n",
        {"n": limit})


def get_batch(batch_id):
    return fetch_one("SELECT b.*, u.username FROM import_batch b LEFT JOIN sys_user u ON u.id = b.user_id "
                     "WHERE b.id = :id", {"id": batch_id})


def imported_count():
    """现在库里有多少条不是原始数据的订单"""
    return fetch_one("SELECT COUNT(*) AS n FROM sales_order WHERE source <> :raw", {"raw": SOURCE_RAW})["n"]


def delete_orders(conn, where, params):
    """
    删除符合条件的导入订单（原始数据永远不删），并做好善后：
    被删订单涉及的年份版本号加 1；月度表里已经没有任何订单的月份，汇总记录一并删掉
    返回 (删了几条, 涉及的年份)
    """
    where = f"source <> :raw AND ({where})"
    params = {**params, "raw": SOURCE_RAW}
    years = sorted(conn.execute(text(f"SELECT DISTINCT YEAR(order_date) FROM sales_order WHERE {where}"),
                                params).scalars())
    deleted = conn.execute(text(f"DELETE FROM sales_order WHERE {where}"), params).rowcount
    state_service.bump_years(conn, years)
    for y in years:
        for table in ("monthly_sales", "monthly_sales_category"):
            conn.execute(text(f"DELETE FROM {table} WHERE ym LIKE :y "
                              "AND ym NOT IN (SELECT DISTINCT ym FROM sales_order)"), {"y": f"{y}-%"})
    return deleted, years


def revoke_batch(batch_id):
    """撤销一批：删掉这一批导入的订单，批次标为已撤销。返回 (删了几条, 涉及的年份)；批次不存在或已撤销返回 None"""
    with write_transaction() as conn:
        row = conn.execute(text("SELECT revoked FROM import_batch WHERE id = :id FOR UPDATE"),
                           {"id": batch_id}).first()
        if row is None or row[0]:
            return None
        result = delete_orders(conn, "batch_id = :id", {"id": batch_id})
        conn.execute(text("UPDATE import_batch SET revoked = 1 WHERE id = :id"), {"id": batch_id})
    return result


def restore_original():
    """恢复原始数据：删掉所有导入和录入的订单，全部批次标为已撤销。返回 (删了几条, 涉及的年份)"""
    with write_transaction() as conn:
        result = delete_orders(conn, "1 = 1", {})
        conn.execute(text("UPDATE import_batch SET revoked = 1"))
    return result
