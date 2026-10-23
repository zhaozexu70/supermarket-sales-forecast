# -*- coding: utf-8 -*-
"""
备份和“验证恢复”这两个工具的保险措施（src/db_backup.py）。全部用测试库来试，不碰正式库：
备份文件里不带库名；带 USE / CREATE DATABASE 的文件拒绝执行；只往 _restore_test 结尾的临时库里恢复；用完删掉临时库
"""
import pytest

import db_backup
from config import DB_NAME, TEST_DB_NAME
from conftest import require_test_db
from step3_load_mysql import load

SCRATCH = "unit_restore_test"


@pytest.fixture(scope="module")
def backup_file(tmp_path_factory):
    """把测试库装成原始数据，再用工具备份它"""
    load(require_test_db(TEST_DB_NAME), force=True)
    return db_backup.backup(TEST_DB_NAME, label="测试", out_dir=tmp_path_factory.mktemp("backup"))


def databases(query):
    return {r[0] for r in query("SHOW DATABASES")}


def test_backup_has_no_database_name(backup_file):
    assert backup_file.name.startswith(f"{TEST_DB_NAME}_测试_") and backup_file.stat().st_size > 1_000_000
    assert db_backup.find_database_switch(backup_file) == []
    text = backup_file.read_bytes()
    assert b"CREATE TABLE `sales_order`" in text and b"INSERT INTO `sales_order`" in text
    assert b"\nUSE " not in text and b"CREATE DATABASE" not in text


def test_find_database_switch(tmp_path):
    """各种写法的 USE、CREATE DATABASE 都能查出来；数据行和注释里的字样不算"""
    bad = tmp_path / "bad.sql"
    bad.write_bytes(b"-- Host: 127.0.0.1    Database: supermarket_db\n"
                    b"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `supermarket_db`;\n\nUSE `supermarket_db`;\n"
                    b"  use other_db;\n/*!40000 CREATE DATABASE x */;\n"
                    b"INSERT INTO `t` VALUES ('USE this product','CREATE DATABASE');\n")
    assert [n for n, _ in db_backup.find_database_switch(bad)] == [2, 4, 5, 6]
    good = tmp_path / "good.sql"
    good.write_bytes(b"-- USE is only mentioned in a comment\nCREATE TABLE `used_items` (id INT);\n"
                     b"INSERT INTO `used_items` VALUES (1);\n")
    assert db_backup.find_database_switch(good) == []


def test_restore_test_roundtrip(backup_file, query):
    """正常情况：恢复到临时库，和测试库逐行比较完全一致，结束后临时库不在了"""
    report = db_backup.restore_test(backup_file, compare_with=TEST_DB_NAME, scratch=SCRATCH)
    assert report["problems"] == []
    assert report["tables"]["sales_order"] == 9959 and report["tables"]["monthly_sales"] == 4896
    assert len(report["tables"]) == 11
    assert SCRATCH not in databases(query)


def test_restore_test_reports_differences(backup_file, query):
    """备份之后库里的数据变了：比较时能看出来"""
    query("UPDATE sales_order SET quantity = quantity + 1 WHERE row_id = 1")
    try:
        report = db_backup.restore_test(backup_file, compare_with=TEST_DB_NAME, scratch=SCRATCH)
        assert any("sales_order" in p for p in report["problems"]) and SCRATCH not in databases(query)
    finally:
        query("UPDATE sales_order SET quantity = quantity - 1 WHERE row_id = 1")


def test_refuses_file_with_use(backup_file, tmp_path, query):
    """文件里有 USE：直接拒绝，连临时库都不建。库里的数据不变"""
    dangerous = tmp_path / "dangerous.sql"
    dangerous.write_bytes(f"USE `{TEST_DB_NAME}`;\nDROP TABLE IF EXISTS `sales_order`;\n".encode())
    before = set(databases(query))
    with pytest.raises(db_backup.Refused, match="有切换库的语句"):
        db_backup.restore_test(dangerous, scratch=SCRATCH)
    assert databases(query) == before
    assert query("SELECT COUNT(*) FROM sales_order")[0][0] == 9959


@pytest.mark.parametrize("name", [DB_NAME, TEST_DB_NAME, "restore_test_copy", "scratch", "bad name_restore_test"])
def test_refuses_wrong_scratch_name(backup_file, query, name):
    """临时库的名字不是以 _restore_test 结尾：拒绝。正式库、测试库的名字都过不了这一关"""
    before = set(databases(query))
    with pytest.raises(db_backup.Refused):
        db_backup.restore_test(backup_file, scratch=name)
    assert databases(query) == before


def test_refuses_existing_scratch_database(backup_file, query):
    """同名的临时库已经存在：不知道里面是什么，不动它"""
    query(f"CREATE DATABASE `{SCRATCH}`")
    try:
        with pytest.raises(db_backup.Refused, match="已经存在"):
            db_backup.restore_test(backup_file, scratch=SCRATCH)
        assert SCRATCH in databases(query)                      # 没有被删掉
    finally:
        query(f"DROP DATABASE `{SCRATCH}`")


def test_one_database_option_is_a_second_guard(backup_file, tmp_path, query, monkeypatch):
    """第二道保险：就算检查被绕过去了，mysql 的 --one-database 也会跳过切到别的库之后的语句"""
    sneaky = tmp_path / "sneaky.sql"
    sneaky.write_bytes(f"CREATE TABLE `a` (id INT);\nUSE `{TEST_DB_NAME}`;\n"
                       "CREATE TABLE `should_not_exist` (id INT);\n".encode())
    monkeypatch.setattr(db_backup, "find_database_switch", lambda path: [])       # 假装第一道检查失效了
    report = db_backup.restore_test(sneaky, scratch=SCRATCH)
    assert list(report["tables"]) == ["a"]
    assert query("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'should_not_exist'")[0][0] == 0


# ---------------- 演示库 ----------------
DEMO = "unit_copy_demo"


def test_restore_demo(backup_file, query):
    """把备份恢复成演示库：内容和原库一样；已经存在时默认不动，加 replace 才重做"""
    try:
        tables = db_backup.restore_demo(backup_file, DEMO)
        assert tables["sales_order"] == 9959 and len(tables) == 11
        query(f"DELETE FROM `{DEMO}`.sales_order WHERE row_id <= 10")           # 在演示库里随便改一点
        with pytest.raises(db_backup.Refused, match="已经存在"):
            db_backup.restore_demo(backup_file, DEMO)
        assert query(f"SELECT COUNT(*) FROM `{DEMO}`.sales_order")[0][0] == 9949  # 没有被动
        assert db_backup.restore_demo(backup_file, DEMO, replace=True)["sales_order"] == 9959
        assert query("SELECT COUNT(*) FROM sales_order")[0][0] == 9959            # 测试库本身不受影响
    finally:
        query(f"DROP DATABASE IF EXISTS `{DEMO}`")


@pytest.mark.parametrize("name", [DB_NAME, TEST_DB_NAME, "demo", "demo_copy", "x y_demo"])
def test_restore_demo_refuses_wrong_names(backup_file, query, name):
    before = set(databases(query))
    with pytest.raises(db_backup.Refused):
        db_backup.restore_demo(backup_file, name, replace=True)
    assert databases(query) == before


def test_restore_demo_refuses_file_with_use(tmp_path, query):
    dangerous = tmp_path / "dangerous.sql"
    dangerous.write_bytes(f"USE `{TEST_DB_NAME}`;\nDROP TABLE IF EXISTS `sales_order`;\n".encode())
    before = set(databases(query))
    with pytest.raises(db_backup.Refused, match="有切换库的语句"):
        db_backup.restore_demo(dangerous, DEMO, replace=True)
    assert databases(query) == before and query("SELECT COUNT(*) FROM sales_order")[0][0] == 9959
