#!/usr/bin/env python3
"""
==============================================================================
StockLab - 行情领域契约 (stocklab.domain.market_data)
==============================================================================

【模块职责】
   定义行情域的列契约（列名 + 列序，与建表 DDL 严格同序）：
     - DAILY_PRICE_COLUMNS    market.daily_prices（日 K 行情，不复权）

【NULL 语义】
   pre_close / change / pct_chg / volume / amount 允许 NULL：
   数据源不提供的字段写 NULL，绝不省略列（省略列会导致整批写入失败）。
"""

__all__ = [
    "DAILY_PRICE_COLUMNS",
]

# 日 K 行情表全部列（与 001_initial.sql 同序）
DAILY_PRICE_COLUMNS = (
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "pct_chg",
    "volume",
    "amount",
)
