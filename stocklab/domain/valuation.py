#!/usr/bin/env python3
"""
==============================================================================
StockLab - 估值领域契约 (stocklab.domain.valuation)
==============================================================================

【模块职责】
   定义估值域的列契约（列名 + 列序，与建表 DDL 严格同序）：
     - DAILY_VALUATION_COLUMNS    market.daily_valuations（全市场单日估值快照）
     - VALUATION_HISTORY_COLUMNS  market.valuation_history（单股逐日历史估值序列）
     - INDUSTRY_VALUATION_COLUMNS market.industry_valuations（行业估值横截面）

【NULL 语义】
   估值字段（PE / PB / PS / 股息率等）允许 NULL，表示亏损或数据源不提供，
   不得强制填充 0；快照中数据源不提供的指标整列写 NULL 但不省略列。
"""

__all__ = [
    "DAILY_VALUATION_COLUMNS",
    "VALUATION_HISTORY_COLUMNS",
    "INDUSTRY_VALUATION_COLUMNS",
]

# 全市场估值快照表全部列（与 001_initial.sql 同序）
DAILY_VALUATION_COLUMNS = (
    "ts_code",
    "trade_date",
    "turnover_rate",
    "turnover_rate_f",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dv_ratio",
    "dv_ttm",
    "total_share",
    "float_share",
    "free_share",
    "total_mv",
    "circ_mv",
)

# 单股历史估值序列表全部列（与 001_initial.sql 同序）
VALUATION_HISTORY_COLUMNS = (
    "ts_code",
    "trade_date",
    "pe_ttm",
    "pe_static",
    "pb",
    "ps",
    "pcf",
)

# 行业估值横截面表全部列（与 001_initial.sql 同序）
INDUSTRY_VALUATION_COLUMNS = (
    "industry_code",
    "stat_date",
    "classification",
    "industry_level",
    "industry_name",
    "company_count",
    "priced_company_count",
    "total_market_value",
    "net_profit",
    "pe_weighted",
    "pe_median",
    "pe_arithmetic",
)
