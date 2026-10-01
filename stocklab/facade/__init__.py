"""
StockLab 统一数据取数门面层 (stocklab.facade)

【模块职责】
   位于 datasource（远端取数）与 persistence（本地落库）之上，
   为上层提供唯一的取数入口，按配置的优先级在本地库与远端接口之间自动选择与回退。

【分层约束】
   - 本层可依赖 datasource 与 persistence；
   - datasource 与 persistence 不得反向 import 本层（避免循环依赖）；
   - 上层（visualizer / 入口脚本）应经由本层取数，不直接拼接两层。
"""

from .market_data import MarketDataFacade

__all__ = [
    "MarketDataFacade",
]
