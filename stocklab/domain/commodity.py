#!/usr/bin/env python3
"""
==============================================================================
StockLab - 大宗商品领域契约 (stocklab.domain.commodity)
==============================================================================

【模块职责】
   定义「大宗商品价格」域的列契约（列名 + 列序，与 007_commodities.sql 严格同序）：
     - COMMODITY_PRICE_COLUMNS  commodity.price_history  收盘价序列

【为什么不落品种登记表】
   一个品种的完整描述里，只有「最新价、样本数」是数据，其余（名称、分类、
   计价单位、抓哪个合约）是**抓取策略**，写在
   stocklab/datasource/commodity/registry.py。拆进表里会多出一步 load-sources，
   并制造「代码改了、表没改」的静默分叉；而品种元信息没有被并发修改的需求。

【为什么是 UPSERT 而不是只 INSERT】
   域内其它不可变数据（言论、公告）走只插入，因为「改了就没法审计」；
   价格不同：它是可修正的**观测值**，源站会补数据、合约换月会回填，
   下一轮同步必须覆盖旧行。fetched_at 同步刷新，于是表里能回答
   「这条记录是哪一轮写进来的」。

【为什么价格没有单位列】
   每个品种的计价单位不同（元/吨、元/克、元/桶），但同一个品种的单位是
   合约规定的、不随时间变，属于品种元信息而不是观测值属性。
   放进表里等于每行重复一份不变量；放 registry 里则一处定义、处处一致。
"""

__all__ = [
    "COMMODITY_PRICE_COLUMNS",
]

# commodity.price_history 全部列（与建表 DDL 同序）
COMMODITY_PRICE_COLUMNS = (
    "symbol",
    "trade_date",
    "close",
    "source",
    "fetched_at",
)
