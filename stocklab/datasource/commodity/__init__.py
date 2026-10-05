#!/usr/bin/env python3
"""
==============================================================================
StockLab - 大宗商品数据源包 (stocklab.datasource.commodity)
==============================================================================

【模块职责】
   只出不进：把大宗商品日线价格从外部源站取回来并归一成契约列序。
   落库由 persistence 层完成，两层之间不互相 import（见 stocklab.domain 包说明）。

【包内布局】
   - registry   品种登记：跟踪哪些品种、叫什么、什么单位、抓哪个合约
   - source     akshare 主力连续采集 + 归一化

【使用方式】
   从 stocklab.datasource.commodity import commodities, fetch_main_history
"""

from stocklab.datasource.commodity.registry import (
    CATEGORIES,
    CommoditySpec,
    SOURCE_AKSHARE_SINA_MAIN,
    by_symbol,
    commodities,
)
from stocklab.datasource.commodity.source import (
    CommoditySourceError,
    fetch_main_history,
)

__all__ = [
    "CATEGORIES",
    "CommoditySpec",
    "CommoditySourceError",
    "SOURCE_AKSHARE_SINA_MAIN",
    "by_symbol",
    "commodities",
    "fetch_main_history",
]
