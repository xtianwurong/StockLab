"""
StockLab 本地数据持久化层 (stocklab.persistence)

【模块职责】
   封装本地 DuckDB 数据仓库的存储基础设施（连接管理、Schema 定义）
   与数据访问映射（Repository），与外部数据源接入完全隔离。

【分层约束】
   - 本层只做 DataFrame / SQL 与 DuckDB 之间的映射，不依赖任何外部数据源。
   - 外部数据源接入位于 stocklab.datasource，由调用方负责把数据交给本层。
"""

from .repository import (
    AnnouncementRepository,
    BalanceSheetRepository,
    CashflowStatementRepository,
    DailyPriceRepository,
    DailyValuationRepository,
    FinancialIndicatorRepository,
    IndexMembershipRepository,
    IncomeStatementRepository,
    IndustryValuationRepository,
    ResearchSnapshotRepository,
    SecurityEventRepository,
    SecurityRepository,
    SnapshotResultRepository,
    ValuationHistoryRepository,
)
from .storage import DEFAULT_DB_PATH, Database, initialize_database

__all__ = [
    "Database",
    "DEFAULT_DB_PATH",
    "initialize_database",
    "SecurityRepository",
    "SecurityEventRepository",
    "DailyPriceRepository",
    "DailyValuationRepository",
    "ValuationHistoryRepository",
    "IndexMembershipRepository",
    "IndustryValuationRepository",
    "IncomeStatementRepository",
    "BalanceSheetRepository",
    "CashflowStatementRepository",
    "FinancialIndicatorRepository",
    "ResearchSnapshotRepository",
    "SnapshotResultRepository",
    "AnnouncementRepository",
]
