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

【使用约定】
   - Repository 写入前一律 align_columns(frame, 契约, 表名)，INSERT 显式列出列名；
   - 数据源 Normalizer 先 require_columns 校验源列，再输出契约列序的 DataFrame；
   - 修改任何契约必须同步修改对应 migration 的 DDL，否则契约测试会失败。
"""

from .contract import (
    DataContractError,
    align_columns,
    check_columns,
    require_columns,
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
]
