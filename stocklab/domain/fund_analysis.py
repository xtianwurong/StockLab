#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金调仓分析领域契约 (stocklab.domain.fund_analysis)
==============================================================================

【模块职责】
   定义「基金调仓分析」域的列契约（列名 + 列序，与 008_sw_indices.sql 严格同序）
"""


__all__ = [
    "SW_INDEX_DAILY_COLUMNS",
    "SW_INDUSTRY_MAPPING_COLUMNS",
    "FUND_INFO_COLUMNS",
    "FUND_NAV_HISTORY_COLUMNS",
    "CAPITAL_FLOW_DAILY_COLUMNS",
    "FUND_ALLOCATION_ANALYSIS_COLUMNS",
]

# sw.index_daily 全部列（与建表 DDL 同序）
SW_INDEX_DAILY_COLUMNS = (
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
    "fetched_at",
)

# sw.industry_mapping 全部列
SW_INDUSTRY_MAPPING_COLUMNS = (
    "index_code",
    "index_name",
    "level",
    "parent_code",
    "sw_first_code",
    "description",
)

# fund.fund_info 全部列
FUND_INFO_COLUMNS = (
    "fund_code",
    "fund_name",
    "fund_short_name",
    "fund_type",
    "manager_name",
    "company_name",
    "establish_date",
    "benchmark",
    "status",
    "source",
    "fetched_at",
)

# fund.nav_history 全部列
FUND_NAV_HISTORY_COLUMNS = (
    "fund_code",
    "nav_date",
    "nav",
    "acc_nav",
    "change_pct",
    "source",
    "fetched_at",
)

# capital.flow_daily 全部列
CAPITAL_FLOW_DAILY_COLUMNS = (
    "sector_code",
    "sector_name",
    "sector_type",
    "trade_date",
    "net_inflow",
    "inflow",
    "outflow",
    "net_inflow_rate",
    "main_net_inflow",
    "retail_net_inflow",
    "source",
    "fetched_at",
)

# fund.allocation_analysis 全部列
FUND_ALLOCATION_ANALYSIS_COLUMNS = (
    "fund_code",
    "analysis_date",
    "window_days",
    "level",
    "sector_code",
    "sector_name",
    "exposure",
    "exposure_change",
    "exposure_change_5d",
    "exposure_change_20d",
    "r_squared",
    "capital_flow_corr",
    "confidence",
    "source",
    "computed_at",
)