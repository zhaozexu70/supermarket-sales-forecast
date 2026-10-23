# -*- coding: utf-8 -*-
"""
给 step1 的输出留底、做比对的工具
  CSV：算 SHA-256（文件指纹，内容差一个字节指纹就不同）
  xlsx：每次生成字节都不同，所以用 pandas 把每个工作表读出来，转成文本再比
运行：python tests/snapshot_tools.py   # 留底，只在改代码之前运行一次；底稿已存在时拒绝覆盖
"""
import hashlib
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
SHA_FILE = FIXTURES / "processed_sha256_before.txt"
XLSX_DIR = FIXTURES / "xlsx_before"
XLSX_NAMES = ["数据质量检查.xlsx"]          # step1 写出的 xlsx


def csv_hashes(folder):
    """文件夹里每个 CSV 的 SHA-256，返回 {文件名: 指纹}"""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(folder).glob("*.csv"))}


def xlsx_sheets(path):
    """把一个 xlsx 的每个工作表读出来转成文本，返回 {工作表名: 文本}"""
    sheets = pd.read_excel(path, sheet_name=None)
    return {name: df.to_csv(index=False, lineterminator="\n") for name, df in sheets.items()}


def read_saved_hashes():
    lines = SHA_FILE.read_text(encoding="utf-8").splitlines()
    return {name: sha for sha, name in (ln.split("  ", 1) for ln in lines if ln.strip())}


def read_saved_sheets(xlsx_name):
    folder = XLSX_DIR / Path(xlsx_name).stem
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.csv"))}


def save_baseline():
    if SHA_FILE.exists():
        sys.exit(f"底稿已存在，不覆盖：{SHA_FILE}")
    FIXTURES.mkdir(exist_ok=True)
    hashes = csv_hashes(ROOT / "data" / "processed")
    SHA_FILE.write_text("".join(f"{sha}  {name}\n" for name, sha in hashes.items()),
                        encoding="utf-8", newline="\n")
    print(f"CSV 指纹：{len(hashes)} 个文件 -> {SHA_FILE}")
    for xlsx_name in XLSX_NAMES:
        folder = XLSX_DIR / Path(xlsx_name).stem
        folder.mkdir(parents=True, exist_ok=True)
        sheets = xlsx_sheets(ROOT / "outputs" / "tables" / xlsx_name)
        for sheet, text in sheets.items():
            (folder / f"{sheet}.csv").write_text(text, encoding="utf-8", newline="\n")
        print(f"{xlsx_name}：{len(sheets)} 个工作表 -> {folder}")


if __name__ == "__main__":
    save_baseline()
