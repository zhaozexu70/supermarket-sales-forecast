# -*- coding: utf-8 -*-
"""系统里固定的取值和字段对照。取值都来自原始数据，tests/test_app.py 会核对是否一致"""

# 地区（按销售额从高到低）
REGIONS = ["华东", "中南", "东北", "华北", "西南", "西北"]

# 类别 -> 子类别
CATEGORY_SUBS = {
    "办公用品": ["信封", "器具", "收纳具", "标签", "用品", "系固件", "纸张", "美术", "装订机"],
    "技术": ["复印机", "电话", "设备", "配件"],
    "家具": ["书架", "桌子", "椅子", "用具"],
}
CATEGORIES = list(CATEGORY_SUBS)

SHIP_MODES = ["当日", "一级", "二级", "标准级"]       # 邮寄方式
SEGMENTS = ["消费者", "公司", "小型企业"]             # 细分
COUNTRY = "中国"

PROVINCES = [
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏", "浙江",
    "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西", "海南", "重庆",
    "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
]

# 数据来源
SOURCE_RAW, SOURCE_FILE, SOURCE_MANUAL = "原始数据", "表格导入", "手工录入"
SOURCES = [SOURCE_RAW, SOURCE_FILE, SOURCE_MANUAL]

# 导入模板、导出文件的 20 个字段，顺序与原始数据相同
TEMPLATE_COLUMNS = [
    "行 ID", "订单 ID", "订单日期", "发货日期", "邮寄方式", "客户 ID", "客户名称", "细分", "城市",
    "省/自治区", "国家", "地区", "产品 ID", "类别", "子类别", "产品名称", "销售额", "数量", "折扣", "利润",
]

# sales_order 表：数据库字段（英文） -> 中文列名。数据处理时把英文列名改回中文，再调用 preprocess_core
ORDER_COLUMNS = {
    "row_id": "行 ID", "order_id": "订单 ID", "order_key": "订单键", "order_date": "订单日期",
    "ship_date": "发货日期", "ship_mode": "邮寄方式", "customer_id": "客户 ID", "customer_name": "客户名称",
    "segment": "细分", "city": "城市", "province": "省/自治区", "region": "地区", "product_id": "产品 ID",
    "category": "类别", "sub_category": "子类别", "product_name": "产品名称", "sales": "销售额",
    "quantity": "数量", "discount": "折扣", "profit": "利润", "unit_price": "折后单价",
    "profit_rate": "利润率", "ym": "年月", "source": "数据来源", "batch_id": "导入批次",
}
ORDER_COLUMNS_CN = {cn: en for en, cn in ORDER_COLUMNS.items()}   # 中文列名 -> 数据库字段

# 月度汇总表：数据库字段 -> 中文列名
MONTHLY_COLUMNS = {"region": "地区", "category": "类别", "sub_category": "子类别", "ym": "年月",
                   "sales": "销售额", "quantity": "数量", "profit": "利润", "order_count": "订单数"}

# 文本字段的最大长度，与 sql/schema.sql 一致，导入校验用
TEXT_LIMITS = {"订单 ID": 30, "客户 ID": 30, "客户名称": 30, "城市": 30, "产品 ID": 40, "产品名称": 200}
ORDER_KEY_LIMIT = 60
