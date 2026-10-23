# -*- coding: utf-8 -*-
"""
启动入口：在项目根目录运行  python run.py
然后用浏览器打开 http://127.0.0.1:5000 ；在这个窗口按 Ctrl + C 停止
创建演示账号：python run.py --create-admin （在终端里输入用户名和密码，不启动网站）
换一个库运行：python run.py --db supermarket_db_demo --port 5001 （演示、截图时用演示库，不碰正式库）
"""
import argparse
import re
import sys

from app import create_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="启动连锁超市销售数据分析与进货预测系统")
    parser.add_argument("--db", help="连接哪个库，不写就是正式库（config.py 里的 DB_NAME）")
    parser.add_argument("--port", type=int, default=5000, help="网站用哪个端口，默认 5000")
    parser.add_argument("--create-admin", action="store_true", help="创建一个账号，不启动网站")
    args = parser.parse_args()
    if args.db and not re.fullmatch(r"\w+", args.db):
        sys.exit("库名只能用字母、数字、下划线")

    app = create_app(db_name=args.db)
    print(f"连接的数据库：{app.config['DB_NAME']}")
    if args.create_admin:
        from app.auth import create_admin_cli
        with app.app_context():
            sys.exit(0 if create_admin_cli() else 1)
    # use_reloader=False：不开自动重载，保证启动时要执行的代码只跑一次
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
