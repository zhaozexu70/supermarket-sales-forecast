# -*- coding: utf-8 -*-
"""
数据库的备份，以及“备份能不能恢复”的安全验证。
  备份：      python src/db_backup.py backup --label 步骤11验收前
  验证恢复：  python src/db_backup.py restore-test "..\\backup\\某个备份.sql" --compare supermarket_db
  做演示库：  python src/db_backup.py restore-demo "..\\backup\\某个备份.sql" --db supermarket_db_demo

两条保险（2026-10-13 误把旧备份恢复进正式库之后定的）：
  1. 备份文件里不带库名（不用 --databases），所以恢复时必须明说恢复到哪个库
  2. 验证恢复只往名字以 _restore_test 结尾的临时库里恢复；执行前先查文件里有没有 USE、CREATE DATABASE，
     有就停下。这个脚本里没有“恢复到正式库”的功能，那件事只能由人照着《项目文件说明》手工做
"""
import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from config import BASE_DIR, DB_CONFIG, DB_NAME
from step3_load_mysql import connect

MYSQL_BIN = Path(r"C:\Program Files\MySQL\MySQL Server 8.0\bin")
BACKUP_DIR = BASE_DIR.parent / "backup"                 # 项目上一级的 backup 文件夹，在项目外面
SCRATCH_SUFFIX = "_restore_test"
DEMO_SUFFIX = "_demo"                                   # 演示库：正式库的一份拷贝，演示、截图用，随时可以重做
# 会让 .sql 文件自己切换到别的库的语句
SWITCH = re.compile(rb"^\s*(/\*!\d+\s*)?(USE\s+[`\w]|CREATE\s+DATABASE)", re.IGNORECASE)


class Refused(Exception):
    """不安全，拒绝执行"""


def client_args():
    """连接 MySQL 的公共参数。密码通过环境变量交给 mysql 程序，不出现在命令行和屏幕上"""
    args = ["-h", DB_CONFIG["host"], "-P", str(DB_CONFIG["port"]), "-u", DB_CONFIG["user"],
            "--default-character-set=utf8mb4"]
    return args, {**os.environ, "MYSQL_PWD": DB_CONFIG["password"]}


def find_database_switch(sql_path):
    """在 .sql 文件里找 USE、CREATE DATABASE。返回 [(行号, 这一行的开头)]，没有就是空列表。INSERT 的数据行不算"""
    found = []
    with open(sql_path, "rb") as f:
        for number, line in enumerate(f, start=1):
            if not line.startswith(b"INSERT INTO") and SWITCH.match(line):
                found.append((number, line[:80].decode("utf-8", "replace").strip()))
    return found


def table_summary(cur, db):
    """每张表的行数：{表名: 行数}"""
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY 1", (db,))
    summary = {}
    for (table,) in cur.fetchall():
        cur.execute(f"SELECT COUNT(*) FROM `{db}`.`{table}`")
        summary[table] = cur.fetchone()[0]
    return summary


def backup(db_name=DB_NAME, label="备份", out_dir=BACKUP_DIR):
    """把一个库完整导出成 .sql 文件（不带库名），返回文件路径。只读这个库，不改它"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{db_name}_{label}_{datetime.now():%Y%m%d_%H%M%S}.sql"
    args, env = client_args()
    with out.open("wb") as f:                           # 注意：没有 --databases，文件里就不会有 USE
        result = subprocess.run([str(MYSQL_BIN / "mysqldump.exe"), *args, "--single-transaction", db_name],
                                stdout=f, stderr=subprocess.PIPE, env=env)
    if result.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError("mysqldump 失败：" + result.stderr.decode("utf-8", "replace")[:300])
    switches = find_database_switch(out)
    if switches:                                        # 不应该发生；万一有，这份备份不能留着当“安全的”用
        out.rename(out.with_name("危险_含USE_" + out.name))
        raise Refused(f"刚做的备份里出现了切换库的语句 {switches}，已改名标记为危险")
    return out


def differences(cur, db_a, db_b):
    """比较两个库：表是否相同、每张表的字段是否相同、内容是否逐行相同。返回不一致的说明，空列表表示完全一致"""
    problems = []
    a, b = table_summary(cur, db_a), table_summary(cur, db_b)
    if set(a) != set(b):
        problems.append(f"表不一样：只在 {db_a} 的 {sorted(set(a) - set(b))}，只在 {db_b} 的 {sorted(set(b) - set(a))}")
    for table in sorted(set(a) & set(b)):
        columns = {}
        for db in (db_a, db_b):
            cur.execute("SELECT column_name, column_type FROM information_schema.columns "
                        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position", (db, table))
            columns[db] = cur.fetchall()
        if columns[db_a] != columns[db_b]:
            problems.append(f"{table}：字段不一样")
            continue
        cols = ", ".join(f"`{c[0]}`" for c in columns[db_a])
        for x, y in ((db_a, db_b), (db_b, db_a)):
            cur.execute(f"SELECT COUNT(*) FROM (SELECT {cols} FROM `{x}`.`{table}` "
                        f"EXCEPT SELECT {cols} FROM `{y}`.`{table}`) t")
            n = cur.fetchone()[0]
            if n:
                problems.append(f"{table}：有 {n} 行只在 {x} 里")
        if a[table] != b[table]:
            problems.append(f"{table}：行数不同（{db_a} {a[table]}，{db_b} {b[table]}）")
    return problems


def restore_test(sql_path, compare_with=None, scratch=None):
    """
    验证一份备份能不能恢复：恢复到临时库 → 数一数每张表 →（可选）和另一个库逐行比较 → 删掉临时库。
    返回 {"tables": {表: 行数}, "problems": [和 compare_with 不一致的地方]}。任何一条保险不满足就抛出 Refused
    """
    sql_path = Path(sql_path)
    scratch = scratch or f"check{SCRATCH_SUFFIX}"
    if not scratch.endswith(SCRATCH_SUFFIX) or not re.fullmatch(r"\w+", scratch):
        raise Refused(f"临时库的名字必须以 {SCRATCH_SUFFIX} 结尾：{scratch}")
    if scratch in (DB_NAME, compare_with):
        raise Refused("临时库不能是正式库，也不能是拿来比较的那个库")
    switches = find_database_switch(sql_path)
    if switches:
        raise Refused(f"{sql_path.name} 里有切换库的语句，执行它会改到别的库，不能用来测试：{switches[:3]}")

    args, env = client_args()
    admin = connect("information_schema")              # MySQL 自带的目录库。这条连接只用来建、查、删临时库
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = %s", (scratch,))
            if cur.fetchone()[0]:
                raise Refused(f"库 {scratch} 已经存在，不知道里面是什么，不动它。请先确认后手工处理")
            cur.execute(f"CREATE DATABASE `{scratch}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")
        try:
            with sql_path.open("rb") as f:              # --one-database：就算文件里有 USE，别的库的语句也会被跳过
                result = subprocess.run([str(MYSQL_BIN / "mysql.exe"), *args, "--one-database", scratch],
                                        stdin=f, stderr=subprocess.PIPE, env=env)
            if result.returncode != 0:
                raise RuntimeError("恢复到临时库失败：" + result.stderr.decode("utf-8", "replace")[:300])
            with admin.cursor() as cur:
                report = {"tables": table_summary(cur, scratch),
                          "problems": differences(cur, scratch, compare_with) if compare_with else []}
        finally:
            with admin.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{scratch}`")
        return report
    finally:
        admin.close()


def restore_demo(sql_path, target, replace=False):
    """
    把一份备份恢复成演示库（名字必须以 _demo 结尾），返回 {表: 行数}。和验证恢复同样的保险：
    先查文件里有没有 USE、CREATE DATABASE；恢复时带 --one-database；绝不会是正式库。
    演示库已经存在时默认不动它；replace=True 才删掉重做（只可能删名字以 _demo 结尾的库）
    """
    sql_path = Path(sql_path)
    if not target.endswith(DEMO_SUFFIX) or not re.fullmatch(r"\w+", target) or target == DB_NAME:
        raise Refused(f"演示库的名字必须以 {DEMO_SUFFIX} 结尾，而且不能是正式库：{target}")
    switches = find_database_switch(sql_path)
    if switches:
        raise Refused(f"{sql_path.name} 里有切换库的语句，执行它会改到别的库，不能用：{switches[:3]}")
    args, env = client_args()
    admin = connect("information_schema")
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = %s", (target,))
            if cur.fetchone()[0]:
                if not replace:
                    raise Refused(f"库 {target} 已经存在。确实要删掉重做的话，加上 --replace")
                cur.execute(f"DROP DATABASE `{target}`")
            cur.execute(f"CREATE DATABASE `{target}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")
        with sql_path.open("rb") as f:
            result = subprocess.run([str(MYSQL_BIN / "mysql.exe"), *args, "--one-database", target],
                                    stdin=f, stderr=subprocess.PIPE, env=env)
        if result.returncode != 0:
            raise RuntimeError("恢复到演示库失败：" + result.stderr.decode("utf-8", "replace")[:300])
        with admin.cursor() as cur:
            return table_summary(cur, target)
    finally:
        admin.close()


def main():
    parser = argparse.ArgumentParser(description="数据库备份，以及备份能否恢复的安全验证")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup", help="备份一个库（不带库名）")
    b.add_argument("--db", default=DB_NAME)
    b.add_argument("--label", default="备份", help="写进文件名里，说明为什么备份，比如 步骤11验收前")
    r = sub.add_parser("restore-test", help="把备份恢复到临时库验证一下，验证完删掉临时库")
    r.add_argument("sql_file")
    r.add_argument("--compare", help="恢复后和哪个库逐行比较（只读那个库）")
    d = sub.add_parser("restore-demo", help="把备份恢复成演示库（名字以 _demo 结尾），演示、截图用")
    d.add_argument("sql_file")
    d.add_argument("--db", default=DB_NAME + DEMO_SUFFIX)
    d.add_argument("--replace", action="store_true", help="演示库已经存在时删掉重做")
    args = parser.parse_args()
    try:
        if args.command == "restore-demo":
            print(f"演示库 {args.db} 已建好，各表行数：", restore_demo(args.sql_file, args.db, args.replace))
        elif args.command == "backup":
            out = backup(args.db, args.label)
            print(f"备份完成：{out}（{out.stat().st_size} 字节）；文件里没有 USE、CREATE DATABASE")
        else:
            report = restore_test(args.sql_file, args.compare)
            print("恢复到临时库成功，各表行数：", report["tables"])
            if args.compare:
                print(f"和 {args.compare} 比较：", "完全一致" if not report["problems"] else report["problems"])
            print("临时库已删除")
    except Refused as e:
        print(f"没有执行：{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
