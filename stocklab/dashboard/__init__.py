"""
StockLab 仪表板与 Web 呈现层 (stocklab.dashboard)
"""

from .page_generator import SectorWebPageGenerator
from .sector_trend import SectorTrendVisualizer

__all__ = [
    "SectorWebPageGenerator",
    "SectorTrendVisualizer",
]
