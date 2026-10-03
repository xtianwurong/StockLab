#!/usr/bin/env python3
"""
==============================================================================
StockLab - 估值类因子 (stocklab.factor.value)
==============================================================================

【模块职责】
   登记 V2 需求 §7.1 的估值因子（PE / PB / PS / EV/EBITDA / FCF Yield）。
   Dividend Yield 同时属于 §7.1 与 §7.5，由 stocklab.factor.dividend 登记，
   分类声明为 ("value", "dividend")，因此 value 分类下同样能列举到它。

【输入帧约定】
   估值列来自全市场估值快照与历史估值序列（pe_ttm / pb / ps 单位为「倍」），
   total_mv 单位为「元」、free_cashflow 为同期**累计**口径（与报表一致），
   故 fcf_yield 的绝对水平随季度进度变化，只宜做同一时点的横截面比较。
"""

import pandas as pd

from stocklab.factor.base import Factor, divide
from stocklab.factor.registry import register

__all__ = []


def _pass_through(column):
    """取输入帧的一列作为因子值（原样透传，不改口径）"""

    def _compute(frame):
        return frame[column].astype("float64")

    return _compute


def _ev_ebitda(frame):
    """EV/EBITDA：企业价值 ÷ 息税折旧摊销前利润（EBITDA <= 0 时无意义 -> NaN）"""
    ev = frame["ev"].astype("float64")
    ebitda = frame["ebitda"].astype("float64")
    valid = (ebitda > 0) & ev.notna()
    result = pd.Series(float("nan"), index=ev.index, dtype="float64")
    result[valid] = ev[valid] / ebitda[valid]
    return result


def _fcf_yield(frame):
    """自由现金流收益率 = 自由现金流 / 总市值（同为元口径；分母为 0 -> NaN）"""
    return divide(frame["free_cashflow"], frame["total_mv"])


register(
    Factor(
        "pe_ttm",
        ("value",),
        ("pe_ttm",),
        _pass_through("pe_ttm"),
        "滚动市盈率 = 最新价 / 最近四个季度每股收益（倍，越低越便宜）",
    )
)
register(
    Factor(
        "pb",
        ("value",),
        ("pb",),
        _pass_through("pb"),
        "市净率 = 最新价 / 每股净资产（倍）",
    )
)
register(
    Factor(
        "ps",
        ("value",),
        ("ps",),
        _pass_through("ps"),
        "市销率 = 最新价 / 每股营业收入（倍）",
    )
)
register(
    Factor(
        "ev_ebitda",
        ("value",),
        ("ev", "ebitda"),
        _ev_ebitda,
        "企业价值倍数 = (总市值 + 有息负债 - 现金) / EBITDA；EBITDA 未加回折旧摊销则不可用",
    )
)
register(
    Factor(
        "fcf_yield",
        ("value",),
        ("free_cashflow", "total_mv"),
        _fcf_yield,
        "自由现金流收益率 = 自由现金流(累计) / 总市值(元)；负值表示现金流为负",
    )
)
