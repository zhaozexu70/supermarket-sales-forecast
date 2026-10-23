# -*- coding: utf-8 -*-
"""注册、登录、退出、登录检查、连续输错后的锁定。只用测试库"""
import re
from datetime import datetime, timedelta

import pytest

from app import auth

GOOD = {"username": "zhang_san", "password": "abc12345", "confirm": "abc12345", "phone": ""}


def token(client, url="/login"):
    """从页面里取出 CSRF 令牌，提交表单时要带上"""
    html = client.get(url).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def post(client, url, data, follow=False):
    return client.post(url, data={**data, "csrf_token": token(client, url)}, follow_redirects=follow)


def register(client, **change):
    return post(client, "/register", {**GOOD, **change})


def login(client, username="zhang_san", password="abc12345", url="/login"):
    return post(client, url, {"username": username, "password": password})


# ---------------- 注册 ----------------
@pytest.mark.parametrize("change, message", [
    ({"username": "ab"}, "用户名为 3–20 位"),
    ({"username": "a" * 21}, "用户名为 3–20 位"),
    ({"username": "张三abc"}, "用户名为 3–20 位"),
    ({"username": "zhang san"}, "用户名为 3–20 位"),
    ({"password": "12345", "confirm": "12345"}, "密码至少 6 位"),
    ({"confirm": "abc123456"}, "两次输入的密码不一致"),
    ({"phone": "1380000"}, "手机号应为 11 位数字"),
    ({"phone": "1380000abcd"}, "手机号应为 11 位数字"),
])
def test_register_validation(anon_client, query, change, message):
    resp = register(anon_client, **change)
    assert resp.status_code == 200                      # 留在注册页
    assert message in resp.get_data(as_text=True)
    assert query("SELECT COUNT(*) FROM sys_user")[0][0] == 0   # 没有写进数据库


def test_register_ok_and_duplicate(anon_client, query):
    resp = register(anon_client, phone="13800001111")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")
    assert query("SELECT username, phone, failed_count, locked_until FROM sys_user") == \
        (("zhang_san", "13800001111", 0, None),)

    resp = register(anon_client)                        # 同一个用户名再注册一次
    assert "这个用户名已被注册" in resp.get_data(as_text=True)
    assert query("SELECT COUNT(*) FROM sys_user")[0][0] == 1


def test_only_hash_is_stored(anon_client, query):
    register(anon_client)
    stored = query("SELECT password_hash FROM sys_user")[0][0]
    assert "abc12345" not in stored and len(stored) > 50
    assert stored.startswith(("scrypt:", "pbkdf2:"))    # werkzeug 生成的哈希
    assert auth.check_password_hash(stored, "abc12345")
    assert not auth.check_password_hash(stored, "abc12346")


# ---------------- 登录、退出 ----------------
def test_login_logout(anon_client):
    register(anon_client)
    resp = login(anon_client)
    assert resp.status_code == 302 and resp.headers["Location"] == "/"

    home = anon_client.get("/").get_data(as_text=True)
    assert "zhang_san" in home and "退出" in home        # 导航栏显示用户名和退出

    csrf = re.search(r'name="csrf_token" value="([^"]+)"', home).group(1)
    resp = anon_client.post("/logout", data={"csrf_token": csrf})
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert anon_client.get("/").status_code == 302       # 退出后首页又进不去了


def test_same_message_for_wrong_user_or_password(anon_client):
    register(anon_client)
    wrong_password = login(anon_client, password="wrong-pass").get_data(as_text=True)
    wrong_user = login(anon_client, username="nobody").get_data(as_text=True)
    assert auth.LOGIN_ERROR in wrong_password and auth.LOGIN_ERROR in wrong_user
    error = lambda html: re.search(r'id="loginError">([^<]+)<', html).group(1)
    assert error(wrong_password) == error(wrong_user) == "用户名或密码错误"


# ---------------- 登录检查 ----------------
@pytest.mark.parametrize("url", ["/", "/data", "/process", "/analysis", "/forecast",
                                 "/forecast/history", "/没有这个页面"])
def test_protected_pages_redirect(anon_client, url):
    resp = anon_client.get(url)
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")


def test_public_pages(anon_client):
    assert anon_client.get("/login").status_code == 200
    assert anon_client.get("/register").status_code == 200
    assert anon_client.get("/static/css/app.css").status_code == 200


def test_back_to_original_page_after_login(anon_client):
    register(anon_client)
    resp = anon_client.get("/analysis")
    assert resp.headers["Location"] == "/login?next=/analysis"
    resp = login(anon_client, url="/login?next=/analysis")
    assert resp.headers["Location"] == "/analysis"


@pytest.mark.parametrize("bad", ["http://evil.example/", "//evil.example/", "evil.example",
                                 "/\\evil.example", "javascript:alert(1)"])
def test_next_only_accepts_relative_urls(app, bad):
    with app.test_request_context():
        assert auth.safe_next(bad) == "/"
        assert auth.safe_next("/process?year=2024") == "/process?year=2024"


def test_session_cookie_flags(anon_client):
    register(anon_client)
    cookie = login(anon_client).headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


# ---------------- 锁定 ----------------
def test_lockout_after_5_failures(anon_client, query):
    register(anon_client)
    for n in range(1, 5):                                # 前 4 次：普通的错误提示，记下次数
        html = login(anon_client, password="wrong").get_data(as_text=True)
        assert "用户名或密码错误" in html and "锁定" not in html
        assert query("SELECT failed_count, locked_until FROM sys_user")[0] == (n, None)

    html = login(anon_client, password="wrong").get_data(as_text=True)     # 第 5 次：锁定
    assert "账号已锁定，请 5 分钟后再试" in html
    locked_until = query("SELECT locked_until FROM sys_user")[0][0]
    assert timedelta(minutes=4) < locked_until - datetime.now() <= timedelta(minutes=5)

    resp = login(anon_client)                            # 锁定期间密码对了也进不去
    assert resp.status_code == 200 and "账号已锁定" in resp.get_data(as_text=True)


def test_remaining_minutes(app, anon_client, query):
    register(anon_client)
    query("UPDATE sys_user SET locked_until = %s", (datetime.now() + timedelta(seconds=150),))
    with app.app_context():
        user, message = auth.check_login("zhang_san", "abc12345")
    assert user is None and "请 3 分钟后再试" in message  # 还剩 2 分半，向上取整显示 3 分钟


def test_unlock_after_time_and_reset_on_success(anon_client, query):
    register(anon_client)
    for _ in range(5):
        login(anon_client, password="wrong")
    query("UPDATE sys_user SET locked_until = %s", (datetime.now() - timedelta(seconds=1),))  # 假装 5 分钟过去了

    login(anon_client, password="wrong")                 # 解锁后重新计数
    assert query("SELECT failed_count FROM sys_user")[0][0] == 1
    assert login(anon_client).status_code == 302         # 密码对了，登录成功
    assert query("SELECT failed_count, locked_until FROM sys_user")[0] == (0, None)


def test_unknown_user_is_not_recorded(anon_client, query):
    for _ in range(6):
        html = login(anon_client, username="nobody", password="x").get_data(as_text=True)
    assert "用户名或密码错误" in html and "锁定" not in html
    assert query("SELECT COUNT(*) FROM sys_user")[0][0] == 0


# ---------------- 创建演示账号的命令 ----------------
def test_create_admin_cli(app, query, monkeypatch, capsys):
    answers = iter(["demo_admin", "secret123", "secret123"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    with app.app_context():
        assert auth.create_admin_cli() is True
    assert query("SELECT username FROM sys_user") == (("demo_admin",),)
    assert "secret123" not in capsys.readouterr().out    # 密码不显示在屏幕上

    answers = iter(["demo_admin", "secret123", "secret123"])     # 再建一次同名的：不创建
    with app.app_context():
        assert auth.create_admin_cli() is False
    assert query("SELECT COUNT(*) FROM sys_user")[0][0] == 1
