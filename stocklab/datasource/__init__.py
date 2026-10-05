"""
==============================================================================
StockLab - 数据源接入层 (stocklab.datasource)
==============================================================================

【模块职责】
   统一导出所有数据源模块
"""

from .quote_service import (
    CLOSE_PRICE_COLUMN,
    PE_TTM_COLUMN,
    TRADE_DATE_COLUMN,
    StockQuoteService,
    StockDataFetchParams,
    StockRealtimeQuote,
)
from .tencent_client import (
    TencentMarketClient,
    normalize_symbol,
)
from .sw_indices import (
    SW_INDUSTRY_LEVELS,
    SW_INDEX_SOURCE_AKSHARE,
    SWIndexSpec,
    fetch_sw_index_daily,
    fetch_sw_industry_mapping,
)
from .fund import (
    FUND_SOURCE_AKSHARE,
    fetch_all_fund_codes,
    fetch_fund_info,
    fetch_fund_nav_history,
)
from .capital_flow import (
    CAPITAL_FLOW_SOURCE_AKSHARE,
    fetch_sector_capital_flow,
    fetch_sw_level1_capital_flow,
    fetch_sw_level2_capital_flow,
    fetch_concept_capital_flow,
)
from .fund_holding import (
    FUND_HOLDING_SOURCE_EASTMONEY,
    fetch_fund_holding_history,
    fetch_fund_holding_by_report_date,
    fetch_latest_fund_holding,
)
from .benchmark_index import (
    BENCHMARK_INDEX_CODES,
    CSI_BENCHMARK_CODES,
    aggregate_industry_weights,
    fetch_index_constituent_weights,
)

__all__ = [
    "TencentMarketClient",
    "StockQuoteService",
    "StockRealtimeQuote",
    "StockDataFetchParams",
    "TRADE_DATE_COLUMN",
    "CLOSE_PRICE_COLUMN",
    "PE_TTM_COLUMN",
    "normalize_symbol",
    "SW_INDUSTRY_LEVELS",
    "SW_INDEX_SOURCE_AKSHARE",
    "SWIndexSpec",
    "fetch_sw_index_daily",
    "fetch_sw_industry_mapping",
    "FUND_SOURCE_AKSHARE",
    "fetch_all_fund_codes",
    "fetch_fund_info",
    "fetch_fund_nav_history",
    "CAPITAL_FLOW_SOURCE_AKSHARE",
    "fetch_sector_capital_flow",
    "fetch_sw_level1_capital_flow",
    "fetch_sw_level2_capital_flow",
    "fetch_concept_capital_flow",
    "BENCHMARK_INDEX_CODES",
    "CSI_BENCHMARK_CODES",
    "fetch_index_constituent_weights",
    "aggregate_industry_weights",
    "FUND_HOLDING_SOURCE_EASTMONEY",
    "fetch_fund_holding_history",
    "fetch_latest_fund_holding",
    "fetch_fund_holding_by_report_date",
]