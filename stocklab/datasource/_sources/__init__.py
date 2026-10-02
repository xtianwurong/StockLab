"""
StockLab 数据源通道实现包 (stocklab.datasource._sources)

【包约定】
  下划线前缀表示内部实现，外部请勿直接依赖本包的任何符号。
  对外请通过 stocklab.datasource.quote_service 的 StockQuoteService 使用。
"""

from .akshare_source import AkShareDataSource
from .baostock_source import BaoStockDataSource
from .base import StockDataSource
from .tencent_source import TencentDataSource

__all__ = [
    "StockDataSource",
    "AkShareDataSource",
    "BaoStockDataSource",
    "TencentDataSource",
]
