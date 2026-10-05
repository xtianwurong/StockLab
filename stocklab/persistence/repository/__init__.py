"""
StockLab 数据访问层 (stocklab.persistence.repository)

【模块职责】
   表级 SQL 读写封装（Repository 模式），只依赖 stocklab.domain（列契约）
   与 stocklab.persistence.storage，不依赖任何外部数据源。

【写入约定】
   一律经 BaseRepository：先按领域契约对齐列序，再用显式列名 INSERT，
   不依赖 DataFrame 列序（见 V2 需求 §4.3）。
"""

from .announcement import AnnouncementRepository
from .base import BaseRepository
from .benchmark import BenchmarkIndustryWeightRepository
from .commodity import CommodityPriceRepository
from .daily_price import DailyPriceRepository
from .fund_analysis import (
    SWIndexDailyRepository,
    SWIndustryMappingRepository,
    FundInfoRepository,
    FundNavHistoryRepository,
    CapitalFlowDailyRepository,
    FundAllocationAnalysisRepository,
)
from .fund_holding import (
    StockIndustryMappingRepository,
    FundHoldingRepository,
    FundIndustryExposureRepository,
    FundIndustryExposureDailyRepository,
    FundAttributionRepository,
    FundManagerTenureRepository,
)
from .daily_valuation import DailyValuationRepository
from .fundamental import (
    BalanceSheetRepository,
    CashflowStatementRepository,
    FinancialIndicatorRepository,
    IncomeStatementRepository,
)
from .index_membership import IndexMembershipRepository
from .industry_valuation import IndustryValuationRepository
from .insight import (
    InvestorAccountRepository,
    InvestorQuoteRepository,
    InvestorRepository,
)
from .research_snapshot import ResearchSnapshotRepository, SnapshotResultRepository
from .security import SecurityRepository
from .security_event import SecurityEventRepository
from .valuation_history import ValuationHistoryRepository

__all__ = [
    "BaseRepository",
    "AnnouncementRepository",
    "BenchmarkIndustryWeightRepository",
    "CommodityPriceRepository",
    "SWIndexDailyRepository",
    "SWIndustryMappingRepository",
    "FundInfoRepository",
    "FundNavHistoryRepository",
    "CapitalFlowDailyRepository",
    "FundAllocationAnalysisRepository",
    "StockIndustryMappingRepository",
    "FundHoldingRepository",
    "FundIndustryExposureRepository",
    "FundIndustryExposureDailyRepository",
    "FundAttributionRepository",
    "FundManagerTenureRepository",
    "SecurityRepository",
    "SecurityEventRepository",
    "DailyPriceRepository",
    "DailyValuationRepository",
    "ValuationHistoryRepository",
    "IndexMembershipRepository",
    "IndustryValuationRepository",
    "ResearchSnapshotRepository",
    "SnapshotResultRepository",
    "IncomeStatementRepository",
    "BalanceSheetRepository",
    "CashflowStatementRepository",
    "FinancialIndicatorRepository",
    "InvestorRepository",
    "InvestorAccountRepository",
    "InvestorQuoteRepository",
]
