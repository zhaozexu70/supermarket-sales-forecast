# -*- coding: utf-8 -*-
"""项目配置：路径、时间划分、模型参数、数据库连接"""
from pathlib import Path
import os
import secrets


def _load_env_file(path):
    """读项目根目录下的 .env（KEY=VALUE 一行一个），已经设置的环境变量优先"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


BASE_DIR = Path(__file__).resolve().parent.parent
_load_env_file(BASE_DIR / ".env")

# ---------- 路径 ----------
RAW_FILE = BASE_DIR / "data" / "raw" / "超市销售数据.xlsx"   # 天池下载的原始文件
PROCESSED_DIR = BASE_DIR / "data" / "processed"
FIG_DIR = BASE_DIR / "outputs" / "figures"
TABLE_DIR = BASE_DIR / "outputs" / "tables"
MODEL_DIR = BASE_DIR / "outputs" / "models"
SQL_DIR = BASE_DIR / "sql"

for _d in (PROCESSED_DIR, FIG_DIR, TABLE_DIR, MODEL_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------- 时间范围与数据集划分 ----------
DATA_START = "2021-01"
DATA_END = "2024-12"
VALID_TRAIN_END = "2022-12"  # 验证阶段：2021-01~2022-12 训练，2023 全年验证（用于确定建模方案）
TRAIN_END = "2023-12"        # 测试阶段：2021-01~2023-12 训练（36个月）
TEST_START = "2024-01"       # 测试集：2024-01~2024-12（12个月），只用于最终评估
SEASON = 12                # 季节周期（月）

# ---------- 预测与进货参数 ----------
FORECAST_STEPS = 6         # 向后预测月数（2025-01 ~ 2025-06）
SERVICE_LEVEL = 0.95       # 服务水平
Z_TABLE = {0.90: 1.28, 0.95: 1.65, 0.99: 2.33}
LEAD_TIME = 1              # 补货周期（月）
DENSE_ZERO_RATIO = 0.2     # 训练期零销量月份占比不超过该值的子类别序列，才单独建模

# ---------- MySQL 连接（账号密码从环境变量或 .env 读取，见 .env.example） ----------
DB_NAME = "supermarket_db"            # 正式库
TEST_DB_NAME = "supermarket_db_test"  # 测试库：自动测试只用它，名字必须以 _test 结尾
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)  # Flask 给登录状态签名的密钥
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": DB_NAME,
    "charset": "utf8mb4",
}
