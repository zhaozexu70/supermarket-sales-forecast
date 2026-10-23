# -*- coding: utf-8 -*-
"""Flask 应用入口：create_app() 读取配置、开启 CSRF 保护、接入登录检查、注册各模块的蓝图、设置错误页和日志"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, render_template
from flask_wtf.csrf import CSRFProtect, CSRFError

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR / "src") not in sys.path:      # 让 app 能直接导入 src 下的 config、forecast_core 等
    sys.path.insert(0, str(BASE_DIR / "src"))

import config                                  # noqa: E402
from app import auth, db                       # noqa: E402

csrf = CSRFProtect()

# 导航栏：按老师确认的模块顺序。(显示的名字, 对应的视图函数)
NAV_ITEMS = [
    ("首页", "data.index"),
    ("数据导入和展示", "data.orders"),
    ("数据处理和展示", "process.index"),
    ("销售分析", "analysis.index"),
    ("数据预测", "forecast.index"),
    ("预测结果", "forecast.history"),
]


def create_app(db_name=None):
    """db_name：连接哪个库，不写就是正式库；测试时传测试库的名字"""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["DB_NAME"] = db_name or config.DB_NAME
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024      # 上传文件不超过 10 MB
    app.config["UPLOAD_TMP"] = BASE_DIR / "tmp_uploads"       # 上传的文件临时放这里，不在 static 下，浏览器访问不到
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True              # 网页里的脚本读不到登录用的 Cookie
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"             # 从别的网站发起的提交不带上这个 Cookie
    app.json.ensure_ascii = False                             # JSON 里的中文不转成 \uXXXX

    csrf.init_app(app)
    app.extensions["db_engine"] = db.make_engine(config.DB_CONFIG, app.config["DB_NAME"])

    auth.init_app(app)            # 注册、登录、退出，以及“没登录就跳到登录页”的检查
    from app.views import data_views, process_views, analysis_views, forecast_views
    for module in (data_views, process_views, analysis_views, forecast_views):
        app.register_blueprint(module.bp)

    @app.context_processor
    def inject_nav():
        return {"nav_items": NAV_ITEMS}

    register_error_pages(app)
    setup_logging(app)
    from app.services.import_service import clean_temp_files
    app.config["UPLOAD_TMP"].mkdir(exist_ok=True)
    clean_temp_files(app.config["UPLOAD_TMP"])               # 启动时删掉超过 1 天的临时文件
    from app.services import forecast_service
    with app.app_context():
        try:                                                 # 上次没算完就被关掉的预测任务，标为失败
            forecast_service.mark_interrupted()
        except Exception as e:                               # 库还没升级到新结构时没有这张表，不影响启动
            app.logger.warning("启动时检查预测任务失败：%s", e)
    return app


def register_error_pages(app):
    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404, title="页面不存在",
                               message="没有找到这个页面，请检查网址，或从导航栏重新进入。"), 404

    @app.errorhandler(413)
    def too_large(e):
        return render_template("error.html", code=413, title="文件太大",
                               message="上传的文件不能超过 10 MB，请拆成几个文件分别导入。"), 413

    @app.errorhandler(CSRFError)
    def csrf_error(e):
        return render_template("error.html", code=400, title="页面已过期",
                               message="页面已过期，请刷新后重试。"), 400

    @app.errorhandler(500)
    def server_error(e):
        app.logger.exception("服务器内部错误")
        return render_template("error.html", code=500, title="系统出错了",
                               message="系统处理时出了问题，错误已记录到 logs/app.log。"), 500


def setup_logging(app):
    """运行日志写到 logs/app.log，单个文件最大 1 MB，保留 3 个旧文件"""
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    if any(isinstance(h, RotatingFileHandler) for h in app.logger.handlers):
        return
    handler = RotatingFileHandler(log_dir / "app.log", maxBytes=1024 * 1024, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)
