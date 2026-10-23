# 数据说明

本仓库不包含原始数据。数据来自阿里云天池公开数据集“超市销售数据”（公开的模拟零售订单数据，9959 行 × 20 列，2021-01 至 2024-12）：

https://tianchi.aliyun.com/dataset/207075

下载后把 Excel 文件放到：

```
data/raw/超市销售数据.xlsx
```

然后在项目根目录运行 `python src/step1_preprocess.py`，会在 `data/processed/` 生成清洗后的明细和各粒度汇总表。
