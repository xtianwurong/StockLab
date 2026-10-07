"""
StockLab 统一数据取数门面层 (stocklab.facade)

【模块职责】
   位于 datasource（远端取数）与 persistence（本地落库）之上，
   为上层提供唯一的取数入口，按配置的优先级在本地库与远端接口之间自动选择与回退。

   多个门面向各自的上层负责：
     MarketDataFacade       行情与估值：本地/远端优先级路由与 Cache-Aside 回写
     InsightDataFacade      投资人观点：**纯本地读**（语料由同步 CLI 落库）
     CommodityDataFacade    大宗商品：**纯本地读**，算价格历史分位并附数据新鲜度
     FundamentalDataFacade  基本面：**纯本地读**，Point-in-Time 安全查询与全市场横截面
     FundAnalysisFacade     基金调仓：**纯本地读**，风格分解+滚动暴露度+资金流佐证
     FundHoldingFacade      基金持仓：**纯本地读**，前十大重仓股→真实行业暴露+RBSA双轨对比
     BenchmarkDataFacade    基准指数：**缓存优先**，成分权重→行业权重快照（Brinson 归因基准）

   外加 1 个取数编排引擎：
     FundAttributionEngine  Brinson 归因：持仓 + 基准权重 + 区间收益三路取数，
                            再交给 analytics.BrinsonAttribution 做纯数学分解。
                            归因数学留在 analytics —— 那一层的契约是零层内依赖、只吃 DataFrame。

【分层约束】
   - 本层可依赖 datasource 与 persistence；
   - datasource 与 persistence 不得反向 import 本层（避免循环依赖）；
   - 上层（dashboard / 入口脚本）应经由本层取数，不直接拼接两层。
"""

from .benchmark_data import BenchmarkDataFacade
from .commodity_data import CommodityDataFacade
from .fund_analysis import FundAnalysisFacade
from .fund_attribution import FundAttributionEngine
from .fund_holding import FundHoldingFacade
from .fundamental_data import FundamentalDataFacade
from .insight_data import InsightDataFacade
from .market_data import MarketDataFacade

__all__ = [
    "MarketDataFacade",
    "InsightDataFacade",
    "CommodityDataFacade",
    "FundamentalDataFacade",
    "FundAnalysisFacade",
    "FundHoldingFacade",
    "BenchmarkDataFacade",
    "FundAttributionEngine",
]
