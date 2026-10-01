"""
StockLab - 股票数据获取、时序对齐与多资产走势 Web 交互式可视化工具包
"""

__version__ = "1.2.0"

from stocklab.common import load_ini_config, safe_float, safe_int
from stocklab.datasource import (
    CLOSE_PRICE_COLUMN,
    PE_TTM_COLUMN,
    TRADE_DATE_COLUMN,
    StockDataFetchParams,
    StockDataFetcher,
    StockRealtimeQuote,
    TencentMarketClient,
)
from stocklab.visualizer import SectorTrendVisualizer, SectorWebPageGenerator

__all__ = [
    "safe_float",
    "safe_int",
    "load_ini_config",
    "CLOSE_PRICE_COLUMN",
    "PE_TTM_COLUMN",
    "TRADE_DATE_COLUMN",
    "StockDataFetchParams",
    "StockDataFetcher",
    "StockRealtimeQuote",
    "TencentMarketClient",
    "SectorTrendVisualizer",
    "SectorWebPageGenerator",
]
