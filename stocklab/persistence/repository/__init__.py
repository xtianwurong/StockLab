"""
StockLab 数据访问层 (stocklab.persistence.repository)

【模块职责】
   表级 SQL 读写封装（Repository 模式），只依赖 stocklab.persistence.storage 与 pandas，
   不依赖任何外部数据源。
"""

from .daily_price import DailyPriceRepository
from .daily_valuation import DailyValuationRepository
from .security import SecurityRepository

__all__ = [
    "SecurityRepository",
    "DailyPriceRepository",
    "DailyValuationRepository",
]
