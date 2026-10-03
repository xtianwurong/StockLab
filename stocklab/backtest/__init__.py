#!/usr/bin/env python3
"""
==============================================================================
StockLab - 回测层 (stocklab.backtest)
==============================================================================

【模块职责】
   V2 需求 §9 与 Phase 3（回测 / 组合 / 交易成本 / 幸存者安全股票池）：
     - engine          BacktestEngine：信号 → 委托 → 下一交易日收盘成交 → 计值
     - portfolio       Portfolio / Position：现金、持仓、T+1 可卖数量
     - order           Order / BacktestError：委托与拒绝理由
     - trade           Trade：成交、费用三段、已实现盈亏
     - cost            CostModel：佣金 + 印花税（卖出）+ 滑点
     - universe        universe_as_of：历史时点真实存在的股票池（防幸存者偏差）
     - metrics         §9.3 的 11 个指标（一年按 252 个交易日年化）

【分层约束】
   与 factor / screener 同级的**纯计算包**：只吃 DataFrame，不联网、不查库；
   行情、信号、分红、基准由调用方（research 层）准备，回测结果是否落库也由
   调用方决定。异常统一为 BacktestError。
"""

from stocklab.backtest.cost import CostModel
from stocklab.backtest.engine import BacktestEngine, BacktestResult
from stocklab.backtest.metrics import compute_metrics
from stocklab.backtest.order import BacktestError, Order
from stocklab.backtest.portfolio import Portfolio, Position
from stocklab.backtest.trade import Trade
from stocklab.backtest.universe import universe_as_of

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "BacktestError",
    "Portfolio",
    "Position",
    "Order",
    "Trade",
    "CostModel",
    "universe_as_of",
    "compute_metrics",
]
