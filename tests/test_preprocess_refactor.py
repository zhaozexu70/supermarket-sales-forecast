# -*- coding: utf-8 -*-
"""
把 step1 的函数抽到 preprocess_core.py 之后，step1 的结果必须和改之前一模一样
底稿（改代码之前留的）：tests/fixtures/processed_sha256_before.txt、tests/fixtures/xlsx_before/
这个测试不连数据库
"""
import pandas as pd

import step1_preprocess
from config import PROCESSED_DIR, RAW_FILE, TABLE_DIR
from preprocess_core import add_features, drop_duplicate_rows, flag_outliers, parse_types, strip_text
from snapshot_tools import XLSX_NAMES, csv_hashes, read_saved_hashes, read_saved_sheets, xlsx_sheets


def test_step1_output_unchanged(tmp_path):
    """用现在的代码重跑 step1（结果写到临时文件夹），和底稿逐个比"""
    step1_preprocess.main(processed_dir=tmp_path, table_dir=tmp_path)
    saved = read_saved_hashes()
    assert len(saved) == 12
    assert csv_hashes(tmp_path) == saved                      # 12 个 CSV 的 SHA-256 全部相同
    for name in XLSX_NAMES:
        assert xlsx_sheets(tmp_path / name) == read_saved_sheets(name)   # xlsx 每个工作表内容相同


def test_files_on_disk_unchanged():
    """项目里现在放着的结果文件，也和底稿相同"""
    assert csv_hashes(PROCESSED_DIR) == read_saved_hashes()
    for name in XLSX_NAMES:
        assert xlsx_sheets(TABLE_DIR / name) == read_saved_sheets(name)


def test_duplicates_and_outliers():
    """原始数据上：两种去重方式都找到 0 行；异常行数与阶段一相同"""
    raw = pd.read_excel(RAW_FILE, dtype={"订单日期": str, "发货日期": str})
    df = strip_text(raw)
    _, n_all = drop_duplicate_rows(df)                        # step1 的做法：整行相同才算重复
    _, n_ignore_id = drop_duplicate_rows(df, ignore=["行 ID"])  # 系统的做法：不看行 ID
    assert (n_all, n_ignore_id) == (0, 0)

    df = flag_outliers(add_features(parse_types(df)))
    assert len(df) == 9959
    assert df["订单键"].nunique() == 4963
    assert (df["销售额_异常标记"].sum(), df["利润_异常标记"].sum()) == (1037, 1839)


def test_duplicate_ignore_columns():
    """造两行只有行 ID 不同的数据：不忽略时不算重复，忽略行 ID 时去掉 1 行"""
    df = pd.DataFrame({"行 ID": [1, 2], "订单 ID": ["A", "A"], "数量": [3, 3]})
    assert drop_duplicate_rows(df)[1] == 0
    out, n = drop_duplicate_rows(df, ignore=["行 ID"])
    assert n == 1 and out["行 ID"].tolist() == [1]
