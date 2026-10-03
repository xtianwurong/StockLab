#!/usr/bin/env python3
"""
==============================================================================
StockLab - 风险类因子 (stocklab.factor.risk)
==============================================================================

【模块职责】
   登记 V2 需求 §7 目录里 risk 域的因子：区间波动率与最大回撤。
   （§7 正文未列出具体风险因子，此处按「与动量同源、可由日线直接算」的原则取最小集。）

【输入帧约定】
   与动量一致：由因子输入帧构造方从 market.daily_prices 算出
   vol_6m / vol_12m（日收益率年化前的样本标准差，未年化）与
   max_dd_12m（近 12 个月最大回撤，负数，-0.25 表示 -25%）。
   本地无日线数据时为 NaN，筛选器逐只报「无数据」。
"""

from stocklab.factor.base import Factor
from stocklab.factor.registry import register

__all__ = []


def _pass_through(column):
    """取输入帧的一列作为因子值（原样透传，不改口径）"""

    def _compute(frame):
        return frame[column].astype("float64")

    return _compute


register(
    Factor(
        "volatility_6m",
        ("risk",),
        ("vol_6m",),
        _pass_through("vol_6m"),
        "近 6 个月日收益率的样本标准差（未年化；样本不足时为 NaN）",
    )
)
register(
    Factor(
        "volatility_12m",
        ("risk",),
        ("vol_12m",),
        _pass_through("vol_12m"),
        "近 12 个月日收益率的样本标准差（未年化）",
    )
)
register(
    Factor(
        "max_drawdown_12m",
        ("risk",),
        ("max_dd_12m",),
        _pass_through("max_dd_12m"),
        "近 12 个月最大回撤（负数，-0.25 = -25%）",
    )
)
