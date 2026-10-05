#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金持仓与行业暴露领域契约 (stocklab.domain.fund_holding)
==============================================================================

【模块职责】
   定义 Phase 1 基金持仓与行业暴露域的列契约（列名 + 列序，与 009_fund_holding.sql 严格同序）
"""


__all__ = [
    "STOCK_INDUSTRY_MAPPING_COLUMNS",
    "FUND_HOLDING_COLUMNS",
    "FUND_INDUSTRY_EXPOSURE_COLUMNS",
    "FUND_INDUSTRY_EXPOSURE_DAILY_COLUMNS",
    "FUND_ATTRIBUTION_COLUMNS",
    "FUND_MANAGER_TENURE_COLUMNS",
]

# fund.stock_industry_mapping 全部列（与建表 DDL 同序）
STOCK_INDUSTRY_MAPPING_COLUMNS = (
    "ts_code",
    "sw_l1_code",
    "sw_l1_name",
    "sw_l2_code",
    "sw_l2_name",
    "sw_l3_code",
    "sw_l3_name",
    "citics_l1_code",
    "citics_l1_name",
    "citics_l2_code",
    "citics_l2_name",
    "wind_l1_code",
    "wind_l1_name",
    "updated_at",
)

# fund.fund_holding 全部列
FUND_HOLDING_COLUMNS = (
    "fund_code",
    "report_date",
    "report_type",
    "stock_code",
    "stock_name",
    "weight",
    "market_value",
    "rank",
    "source",
    "fetched_at",
)

# fund.fund_industry_exposure 全部列（按报告期）
FUND_INDUSTRY_EXPOSURE_COLUMNS = (
    "fund_code",
    "report_date",
    "level",
    "sector_code",
    "sector_name",
    "weight",
    "source",
)

# fund.fund_industry_exposure_daily 全部列（线性插值到每日）
FUND_INDUSTRY_EXPOSURE_DAILY_COLUMNS = (
    "fund_code",
    "trade_date",
    "level",
    "sector_code",
    "sector_name",
    "weight",
    "source",
)

# fund.fund_attribution 全部列（Phase 2 预留）
FUND_ATTRIBUTION_COLUMNS = (
    "fund_code",
    "trade_date",
    "benchmark_code",
    "total_return",
    "benchmark_return",
    "excess_return",
    "allocation_effect",
    "selection_effect",
    "interaction_effect",
)

# fund.fund_manager_tenure 全部列（Phase 3 预留）
FUND_MANAGER_TENURE_COLUMNS = (
    "fund_code",
    "manager_name",
    "start_date",
    "end_date",
    "is_current",
    "aum",
)