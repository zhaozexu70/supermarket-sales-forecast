-- 阶段一的表结构（v1，6 张表），只给 test_migration 用：先按旧结构建库，再测试升级脚本
-- 内容与升级前的 sql/schema.sql 相同，只去掉了 CREATE DATABASE 和 USE 两句

-- 1. 系统用户表（登录注册）
CREATE TABLE IF NOT EXISTS sys_user (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(50)  NOT NULL UNIQUE COMMENT '用户名',
    password_hash VARCHAR(255) NOT NULL COMMENT '加密后的密码',
    phone         VARCHAR(20)  NULL COMMENT '联系方式',
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统用户';

-- 2. 清洗后的订单明细
CREATE TABLE IF NOT EXISTS sales_order (
    row_id        INT          PRIMARY KEY COMMENT '行ID',
    order_id      VARCHAR(30)  NOT NULL COMMENT '订单ID（原始，存在重复使用）',
    order_key     VARCHAR(60)  NOT NULL COMMENT '订单键=订单ID|订单日期|客户ID',
    order_date    DATE         NOT NULL COMMENT '订单日期',
    ship_date     DATE         NOT NULL COMMENT '发货日期',
    ship_mode     VARCHAR(20)  NULL COMMENT '邮寄方式',
    customer_id   VARCHAR(30)  NOT NULL COMMENT '客户ID',
    customer_name VARCHAR(30)  NULL COMMENT '客户名称',
    segment       VARCHAR(20)  NULL COMMENT '细分',
    city          VARCHAR(30)  NULL COMMENT '城市',
    province      VARCHAR(30)  NULL COMMENT '省/自治区',
    region        VARCHAR(10)  NOT NULL COMMENT '地区',
    product_id    VARCHAR(40)  NOT NULL COMMENT '产品ID',
    category      VARCHAR(20)  NOT NULL COMMENT '类别',
    sub_category  VARCHAR(20)  NOT NULL COMMENT '子类别',
    product_name  VARCHAR(200) NULL COMMENT '产品名称',
    sales         DECIMAL(12,2) NOT NULL COMMENT '销售额',
    quantity      INT          NOT NULL COMMENT '数量',
    discount      DECIMAL(4,2) NOT NULL COMMENT '折扣',
    profit        DECIMAL(12,2) NOT NULL COMMENT '利润',
    unit_price    DECIMAL(12,2) NULL COMMENT '折后单价',
    profit_rate   DECIMAL(8,4) NULL COMMENT '利润率',
    ym            CHAR(7)      NOT NULL COMMENT '年月',
    INDEX idx_date (order_date),
    INDEX idx_region_sub (region, sub_category),
    INDEX idx_ym (ym)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单明细（清洗后）';

-- 3. 月度汇总：地区×子类别（已补零）
CREATE TABLE IF NOT EXISTS monthly_sales (
    region        VARCHAR(10)  NOT NULL,
    category      VARCHAR(20)  NOT NULL,
    sub_category  VARCHAR(20)  NOT NULL,
    ym            CHAR(7)      NOT NULL,
    sales         DECIMAL(14,2) NOT NULL DEFAULT 0,
    quantity      INT          NOT NULL DEFAULT 0,
    profit        DECIMAL(14,2) NOT NULL DEFAULT 0,
    order_count   INT          NOT NULL DEFAULT 0,
    PRIMARY KEY (region, sub_category, ym)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='月度汇总-地区子类别';

-- 4. 月度汇总：地区×类别（建模主层级）
CREATE TABLE IF NOT EXISTS monthly_sales_category (
    region        VARCHAR(10)  NOT NULL,
    category      VARCHAR(20)  NOT NULL,
    ym            CHAR(7)      NOT NULL,
    sales         DECIMAL(14,2) NOT NULL DEFAULT 0,
    quantity      INT          NOT NULL DEFAULT 0,
    profit        DECIMAL(14,2) NOT NULL DEFAULT 0,
    order_count   INT          NOT NULL DEFAULT 0,
    PRIMARY KEY (region, category, ym)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='月度汇总-地区类别';

-- 5. 模型评估结果
CREATE TABLE IF NOT EXISTS model_evaluation (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    region        VARCHAR(10)  NOT NULL,
    category      VARCHAR(20)  NOT NULL,
    model_name    VARCHAR(30)  NOT NULL COMMENT '季节朴素法/Holt-Winters/SARIMA',
    model_order   VARCHAR(40)  NULL COMMENT 'SARIMA阶数',
    mae           DECIMAL(10,3) NULL,
    rmse          DECIMAL(10,3) NULL,
    wape          DECIMAL(8,4) NULL,
    r2            DECIMAL(8,4) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='测试期模型评估';

-- 6. 预测与进货建议结果
CREATE TABLE IF NOT EXISTS forecast_result (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    level_name         VARCHAR(20)  NOT NULL COMMENT '地区类别 / 地区子类别',
    region             VARCHAR(10)  NOT NULL,
    category           VARCHAR(20)  NOT NULL,
    sub_category       VARCHAR(20)  NULL,
    ym                 CHAR(7)      NOT NULL COMMENT '预测月份',
    forecast_qty       DECIMAL(10,2) NOT NULL COMMENT '预测销量',
    lower_qty          DECIMAL(10,2) NULL COMMENT '95%预测区间下限',
    upper_qty          DECIMAL(10,2) NULL COMMENT '95%预测区间上限',
    safety_stock       DECIMAL(10,2) NULL COMMENT '安全库存',
    suggested_purchase INT          NULL COMMENT '建议进货量（期初库存为0时）',
    created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_query (region, category, sub_category, ym)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='预测结果';
