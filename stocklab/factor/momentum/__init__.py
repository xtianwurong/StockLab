#!/usr/bin/env python3
"""
==============================================================================
StockLab - 动量类因子 (stocklab.factor.momentum)
==============================================================================

【模块职责】
   登记 V2 需求 §7.4 的动量因子：1M / 3M / 6M / 12M 区间收益率。

【输入帧约定】
   动量本身是「价格序列」的函数，但因子层保持纯计算：由因子输入帧构造方
   （stocklab.research.frame）从 market.daily_prices 取 as-of 及回看锚点收盘价，
   预先算成 ret_1m / ret_3m / ret_6m / ret_12m 列（不复权或前复权由取数方决定）。
   本地无日线数据时这些列为 NaN，筛选器会逐只报「无数据」，不会静默通过。
"""

from stocklab.factor.base import Factor
from stocklab.factor.registry import register

__all__ = []


from stocklab.factor.base import pass_through as _pass_through


register(
    Factor(
        "momentum_1m",
        ("momentum",),
        ("ret_1m",),
        _pass_through("ret_1m"),
        "近 1 个月收益率 = as-of 收盘 / 约 1 个月前收盘 - 1",
    )
)
register(
    Factor(
        "momentum_3m",
        ("momentum",),
        ("ret_3m",),
        _pass_through("ret_3m"),
        "近 3 个月收益率，锚点取 as-of 往前 90 个自然日的最近交易日",
    )
)
register(
    Factor(
        "momentum_6m",
        ("momentum",),
        ("ret_6m",),
        _pass_through("ret_6m"),
        "近 6 个月收益率，锚点取 as-of 往前 180 个自然日的最近交易日",
    )
)
register(
    Factor(
        "momentum_12m",
        ("momentum",),
        ("ret_12m",),
        _pass_through("ret_12m"),
        "近 12 个月收益率，锚点取 as-of 往前 365 个自然日的最近交易日",
    )
)
