# -*- coding: utf-8 -*-
"""用户注册、登录、退出；登录检查；连续输错密码后的锁定"""
import math
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import execute, fetch_one

bp = Blueprint("auth", __name__)

MAX_FAILS = 5        # 连续输错几次后锁定
LOCK_MINUTES = 5     # 锁定多少分钟
LOGIN_ERROR = "用户名或密码错误"      # 用户名不存在和密码不对用同一句话，不让人试出哪些用户名存在
PUBLIC_ENDPOINTS = {"auth.login", "auth.register", "static"}   # 不登录也能访问的页面


# ---------------- 注册 ----------------
def validate_register(username, password, confirm, phone=""):
    """检查注册信息，返回 {字段: 错误提示}，没有问题时是空字典"""
    errors = {}
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
        errors["username"] = "用户名为 3–20 位，只能用字母、数字、下划线"
    elif fetch_one("SELECT id FROM sys_user WHERE username = :u", {"u": username}):
        errors["username"] = "这个用户名已被注册"
    if len(password) < 6:
        errors["password"] = "密码至少 6 位"
    elif password != confirm:
        errors["confirm"] = "两次输入的密码不一致"
    if phone and not re.fullmatch(r"\d{11}", phone):
        errors["phone"] = "手机号应为 11 位数字（可以不填）"
    return errors


def create_user(username, password, phone=""):
    """新建用户。数据库里只存密码的哈希值，不存密码本身"""
    execute("INSERT INTO sys_user (username, password_hash, phone) VALUES (:u, :p, :ph)",
            {"u": username, "p": generate_password_hash(password), "ph": phone or None})


@bp.route("/register", methods=["GET", "POST"])
def register():
    errors, form = {}, {"username": "", "phone": ""}
    if request.method == "POST":
        form = {"username": request.form.get("username", "").strip(),
                "phone": request.form.get("phone", "").strip()}
        password = request.form.get("password", "")
        errors = validate_register(form["username"], password, request.form.get("confirm", ""),
                                   form["phone"])
        if not errors:
            create_user(form["username"], password, form["phone"])
            flash("注册成功，请登录", "success")
            return redirect(url_for("auth.login"))
    return render_template("register.html", errors=errors, form=form)


# ---------------- 登录 ----------------
def check_login(username, password, now=None):
    """
    核对用户名和密码，返回 (用户, 错误提示)，成功时错误提示为 None
    同一个用户名连续输错 MAX_FAILS 次，锁定 LOCK_MINUTES 分钟；登录成功后失败次数清零
    """
    now = now or datetime.now()
    user = fetch_one("SELECT id, username, password_hash, failed_count, locked_until "
                     "FROM sys_user WHERE username = :u", {"u": username})
    if user is None:
        return None, LOGIN_ERROR

    if user["locked_until"] and user["locked_until"] > now:
        minutes = math.ceil((user["locked_until"] - now).total_seconds() / 60)
        return None, f"密码连续输错 {MAX_FAILS} 次，账号已锁定，请 {minutes} 分钟后再试"

    if not check_password_hash(user["password_hash"], password):
        fails = user["failed_count"] + 1
        # 达到次数：锁定，并把次数清零，解锁后重新计数。时间去掉秒以下的部分再存（数据库只存到秒）
        if fails >= MAX_FAILS:
            execute("UPDATE sys_user SET failed_count = 0, locked_until = :t WHERE id = :id",
                    {"t": now.replace(microsecond=0) + timedelta(minutes=LOCK_MINUTES), "id": user["id"]})
            return None, f"密码连续输错 {MAX_FAILS} 次，账号已锁定，请 {LOCK_MINUTES} 分钟后再试"
        execute("UPDATE sys_user SET failed_count = :n WHERE id = :id", {"n": fails, "id": user["id"]})
        return None, LOGIN_ERROR

    execute("UPDATE sys_user SET failed_count = 0, locked_until = NULL WHERE id = :id", {"id": user["id"]})
    return user, None


def safe_next(target):
    """登录后要跳回的地址：只接受本站的相对地址（以 / 开头），其他一律回首页"""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return url_for("data.index")
    parts = urlparse(target)
    if parts.scheme or parts.netloc:
        return url_for("data.index")
    return target


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("data.index"))
    error, username = None, ""
    next_url = request.values.get("next", "")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        user, error = check_login(username, request.form.get("password", ""))
        if user:
            session.clear()                      # 登录时换一个新的会话
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(safe_next(next_url))
    return render_template("login.html", error=error, username=username, next_url=next_url)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("已退出登录", "success")
    return redirect(url_for("auth.login"))


# ---------------- 登录检查：每个请求进来先过这里 ----------------
def load_user():
    """从会话里取出当前用户，放到 g.user，模板和后面的代码都能用"""
    g.user = None
    if session.get("user_id"):
        g.user = {"id": session["user_id"], "username": session.get("username", "")}


def require_login():
    """除登录、注册、静态文件外，没登录一律跳到登录页，并记下原来要去的地址"""
    if g.user or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    target = request.full_path.rstrip("?") if request.method == "GET" else ""
    return redirect(url_for("auth.login", next=target or None))


def init_app(app):
    app.register_blueprint(bp)
    app.before_request(load_user)
    app.before_request(require_login)


# ---------------- 命令行：创建演示账号 ----------------
def create_admin_cli():
    """python run.py --create-admin：在终端里输入用户名和密码（密码不显示），账号密码不写进代码"""
    from getpass import getpass
    username = input("用户名（3–20 位，字母、数字、下划线）：").strip()
    password = getpass("密码（至少 6 位，输入时不显示）：")
    confirm = getpass("再输入一次密码：")
    errors = validate_register(username, password, confirm)
    if errors:
        print("没有创建：" + "；".join(errors.values()))
        return False
    create_user(username, password)
    print(f"账号 {username} 已创建，可以用它登录了")
    return True
