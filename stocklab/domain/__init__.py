"""
StockLab 领域契约包 (stocklab.domain)

【模块职责】
   定义「列名 + 列序」的唯一权威来源，被数据源层（datasource）与持久化层
   （persistence）共同依赖，两层之间不互相 import。

【包内布局】
   - contract        契约校验：DataContractError / require_columns / align_columns
   - security        证券身份与生命周期：securities / security_events / index_memberships
   - market_data     行情：daily_prices
   - valuation       估值：daily_valuations / valuation_history / industry_valuations
   - fundamental     基本面（Point-in-Time）：三表 + 财务指标
   - research        研究快照：snapshots / snapshot_results
   - insight         投资人观点：investors / investor_accounts / investor_quotes
   - commodity       大宗商品价格：price_history
    - fund_analysis   基金调仓分析：行业指数/基金净值/资金流/调仓分析结果
   - benchmark        基准指数：行业权重快照（Brinson 归因基准）

【使用约定】
   - Repository 写入前一律 align_columns(frame, 契约, 表名)，INSERT 显式列出列名；
   - 数据源 Normalizer 先 require_columns 校验源列，再输出契约列序的 DataFrame；
   - 修改任何契约必须同步修改对应 migration 的 DDL，否则契约测试会失败。
"""

from .commodity import COMMODITY_PRICE_COLUMNS
from .benchmark import (
    BENCHMARK_INDUSTRY_WEIGHT_COLUMNS,
    BENCHMARK_INDEX_LEVEL1,
)
from .fund_analysis import (
    SW_INDEX_DAILY_COLUMNS,
    SW_INDUSTRY_MAPPING_COLUMNS,
    FUND_INFO_COLUMNS,
    FUND_NAV_HISTORY_COLUMNS,
    CAPITAL_FLOW_DAILY_COLUMNS,
    FUND_ALLOCATION_ANALYSIS_COLUMNS,
)
from .fund_holding import (
    STOCK_INDUSTRY_MAPPING_COLUMNS,
    FUND_HOLDING_COLUMNS,
    FUND_INDUSTRY_EXPOSURE_COLUMNS,
    FUND_INDUSTRY_EXPOSURE_DAILY_COLUMNS,
    FUND_ATTRIBUTION_COLUMNS,
    FUND_MANAGER_TENURE_COLUMNS,
)
from .contract import (
    DataContractError,
    align_columns,
    check_columns,
    require_columns,
)
from .disclosure import ANNOUNCEMENT_COLUMNS
from .insight import (
    INVESTOR_ACCOUNT_COLUMNS,
    INVESTOR_COLUMNS,
    INVESTOR_QUOTE_COLUMNS,
    INVESTOR_STYLES,
    PLATFORMS,
    QUOTE_TYPES,
    VERIFICATION_STATUSES,
    decode_raw_meta,
    dumps_raw_meta,
)
from .fundamental import (
    BALANCE_SHEET_COLUMNS,
    CASHFLOW_STATEMENT_COLUMNS,
    FINANCIAL_INDICATOR_COLUMNS,
    FUNDAMENTAL_PIT_COLUMNS,
    INCOME_STATEMENT_COLUMNS,
)
from .market_data import DAILY_PRICE_COLUMNS
from .research import SNAPSHOT_COLUMNS, SNAPSHOT_RESULT_COLUMNS
from .security import (
    INDEX_MEMBERSHIP_COLUMNS,
    SECURITY_COLUMNS,
    SECURITY_EVENT_COLUMNS,
    SECURITY_EVENT_TYPES,
    SECURITY_STATUSES,
)
from .valuation import (
    DAILY_VALUATION_COLUMNS,
    INDUSTRY_VALUATION_COLUMNS,
    VALUATION_HISTORY_COLUMNS,
)

__all__ = [
    "DataContractError",
    "check_columns",
    "require_columns",
    "align_columns",
    "SECURITY_COLUMNS",
    "SECURITY_EVENT_COLUMNS",
    "INDEX_MEMBERSHIP_COLUMNS",
    "SECURITY_EVENT_TYPES",
    "SECURITY_STATUSES",
    "DAILY_PRICE_COLUMNS",
    "DAILY_VALUATION_COLUMNS",
    "VALUATION_HISTORY_COLUMNS",
    "INDUSTRY_VALUATION_COLUMNS",
    "FUNDAMENTAL_PIT_COLUMNS",
    "INCOME_STATEMENT_COLUMNS",
    "BALANCE_SHEET_COLUMNS",
    "CASHFLOW_STATEMENT_COLUMNS",
    "FINANCIAL_INDICATOR_COLUMNS",
    "SNAPSHOT_COLUMNS",
    "SNAPSHOT_RESULT_COLUMNS",
    "ANNOUNCEMENT_COLUMNS",
    "INVESTOR_COLUMNS",
    "INVESTOR_ACCOUNT_COLUMNS",
    "INVESTOR_QUOTE_COLUMNS",
    "COMMODITY_PRICE_COLUMNS",
    "BENCHMARK_INDUSTRY_WEIGHT_COLUMNS",
    "BENCHMARK_INDEX_LEVEL1",
    "SW_INDEX_DAILY_COLUMNS",
    "SW_INDUSTRY_MAPPING_COLUMNS",
    "FUND_INFO_COLUMNS",
    "FUND_NAV_HISTORY_COLUMNS",
    "CAPITAL_FLOW_DAILY_COLUMNS",
    "FUND_ALLOCATION_ANALYSIS_COLUMNS",
    "STOCK_INDUSTRY_MAPPING_COLUMNS",
    "FUND_HOLDING_COLUMNS",
    "FUND_INDUSTRY_EXPOSURE_COLUMNS",
    "FUND_INDUSTRY_EXPOSURE_DAILY_COLUMNS",
    "FUND_ATTRIBUTION_COLUMNS",
    "FUND_MANAGER_TENURE_COLUMNS",
    "PLATFORMS",
    "QUOTE_TYPES",
    "VERIFICATION_STATUSES",
    "dumps_raw_meta",
    "decode_raw_meta",
    "INVESTOR_STYLES",
]
