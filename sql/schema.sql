-- 连锁超市销售数据分析与进货预测系统 数据库结构（v2，MySQL 8.0）
-- 本文件只建表，不写库名。库由代码按 config.py 里的 DB_NAME / TEST_DB_NAME 创建后，在库里执行本文件
-- 已有的库用 src/migrate_v2.py 升级到这个结构

-- 1. 系统用户表（登录注册）
CREATE TABLE IF NOT EXISTS sys_user (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(50)  NOT NULL UNIQUE COMMENT '用户名',
    password_hash VARCHAR(255) NOT NULL COMMENT '加密后的密码',
    phone         VARCHAR(20)  NULL COMMENT '联系方式',
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
    failed_count  INT          NOT NULL DEFAULT 0 COMMENT '连续登录失败次数',
    locked_until  DATETIME     NULL COMMENT '锁定到这个时间为止，没锁定为空'
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
    source        VARCHAR(10)  NOT NULL DEFAULT '原始数据' COMMENT '数据来源：原始数据/表格导入/手工录入',
    batch_id      INT          NULL COMMENT '导入批次号，原始数据为空',
    INDEX idx_date (order_date),
    INDEX idx_region_sub (region, sub_category),
    INDEX idx_ym (ym),
    INDEX idx_batch (batch_id)
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

-- 5. 模型评估结果（阶段一实验存档，系统页面不使用）
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

-- 6. 预测与进货建议结果（阶段一实验存档，系统页面不使用）
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

-- 7. 导入批次：每次表格导入、每条手工录入各一行
CREATE TABLE IF NOT EXISTS import_batch (
    id            INT AUTO_INCREMENT PRIMARY KEY COMMENT '批次号',
    method        VARCHAR(10)  NOT NULL COMMENT '表格导入/手工录入',
    file_name     VARCHAR(200) NULL COMMENT '上传的文件名，手工录入为空',
    total_rows    INT          NOT NULL DEFAULT 0 COMMENT '文件总行数',
    success_rows  INT          NOT NULL DEFAULT 0 COMMENT '成功导入行数',
    failed_rows   INT          NOT NULL DEFAULT 0 COMMENT '失败行数',
    skipped_rows  INT          NOT NULL DEFAULT 0 COMMENT '因行ID重复跳过的行数',
    user_id       INT          NOT NULL COMMENT '操作人，对应 sys_user.id',
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked       TINYINT      NOT NULL DEFAULT 0 COMMENT '是否已撤销'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='导入批次';

-- 8. 年份处理状态：每个年份一行，两个版本号相等表示这一年已处理
CREATE TABLE IF NOT EXISTS year_state (
    `year`            INT      PRIMARY KEY COMMENT '年份',
    data_version      INT      NOT NULL COMMENT '这一年的数据版本号，订单有增删时加1',
    processed_version INT      NULL COMMENT '最近一次处理时的数据版本号，没处理过为空',
    updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='年份处理状态';

-- 9. 处理记录：每点一次开始处理记一行
CREATE TABLE IF NOT EXISTS process_log (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    start_year      INT NOT NULL COMMENT '处理的起始年',
    end_year        INT NOT NULL COMMENT '处理的结束年',
    order_rows      INT NOT NULL DEFAULT 0 COMMENT '参与处理的订单行数',
    duplicate_rows  INT NOT NULL DEFAULT 0 COMMENT '重复录入行数',
    invalid_rows    INT NOT NULL DEFAULT 0 COMMENT '不合规行数',
    outlier_sales   INT NOT NULL DEFAULT 0 COMMENT '销售额IQR异常行数（只标记不删除）',
    outlier_profit  INT NOT NULL DEFAULT 0 COMMENT '利润IQR异常行数（只标记不删除）',
    month_count     INT NOT NULL DEFAULT 0 COMMENT '覆盖月份数',
    zero_filled_sub INT NOT NULL DEFAULT 0 COMMENT '地区×子类别补零格子数',
    zero_filled_cat INT NOT NULL DEFAULT 0 COMMENT '地区×类别补零格子数',
    user_id         INT NULL COMMENT '操作人，升级脚本写入的记录为空',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    seconds         DECIMAL(8,2) NULL COMMENT '这次处理用了多少秒，早先的记录为空'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='处理记录';

-- 10. 预测任务
CREATE TABLE IF NOT EXISTS forecast_task (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    history_start  INT          NOT NULL COMMENT '历史起始年',
    history_end    INT          NOT NULL COMMENT '历史结束年=预测年份-1',
    target_year    INT          NOT NULL COMMENT '预测年份',
    region         VARCHAR(10)  NOT NULL COMMENT '地区，全部表示所有地区',
    category       VARCHAR(20)  NOT NULL COMMENT '类别，全部表示所有类别',
    level_name     VARCHAR(10)  NOT NULL COMMENT '类别/子类别',
    sub_category   VARCHAR(20)  NULL COMMENT '子类别层级时选的子类别，类别层级为空',
    inventory      INT          NOT NULL DEFAULT 0 COMMENT '当前库存',
    lead_time      INT          NOT NULL DEFAULT 1 COMMENT '补货周期（月）',
    service_level  DECIMAL(4,2) NOT NULL DEFAULT 0.95 COMMENT '服务水平',
    status         VARCHAR(10)  NOT NULL COMMENT '运行中/已完成/失败',
    progress_done  INT          NOT NULL DEFAULT 0 COMMENT '已完成序列数',
    progress_total INT          NOT NULL DEFAULT 0 COMMENT '总序列数',
    error_msg      VARCHAR(500) NULL COMMENT '失败原因',
    user_id        INT          NOT NULL COMMENT '操作人',
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at    DATETIME     NULL,
    year_versions  VARCHAR(200) NULL COMMENT '预测时各年份的数据版本，如 2021:1,2022:1'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='预测任务';

-- 11. 预测明细
CREATE TABLE IF NOT EXISTS forecast_detail (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    task_id            INT           NOT NULL COMMENT '对应 forecast_task.id',
    region             VARCHAR(10)   NOT NULL,
    category           VARCHAR(20)   NOT NULL,
    sub_category       VARCHAR(20)   NULL COMMENT '类别层级为空',
    ym                 CHAR(7)       NOT NULL COMMENT '预测月份',
    forecast_qty       DECIMAL(10,2) NOT NULL COMMENT '预测销量',
    lower_qty          DECIMAL(10,2) NULL COMMENT '95%预测区间下限',
    upper_qty          DECIMAL(10,2) NULL COMMENT '95%预测区间上限',
    actual_qty         INT           NULL COMMENT '实际销量，没有或未处理时为空',
    safety_stock       DECIMAL(10,2) NULL COMMENT '安全库存',
    suggested_purchase INT           NULL COMMENT '建议进货量，不进货的月份为0',
    INDEX idx_task (task_id, region, category, sub_category, ym)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='预测明细';
