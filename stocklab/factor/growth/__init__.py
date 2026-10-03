#!/usr/bin/env python3
"""
==============================================================================
StockLab - 成长类因子 (stocklab.factor.growth)
==============================================================================

【模块职责】
   登记 V2 需求 §7.3 的成长因子：
   营收增长 / 利润增长 / EPS 增长 / 自由现金流增长 / ROE 趋势。

【输入帧约定】
   revenue_growth / profit_growth / eps_growth 直接用 fundamental.financial_indicators
   里**按报告期平移一年**算好的同比（缺去年同期为 NaN，不臆造）；
   fcf_growth 与 roe_trend 需要「as-of 减一年」时点的最新可见值
   （*_prior_year 列，由因子输入帧构造方按 Point-in-Time 口径拼入）。
"""

from stocklab.factor.base import Factor, divide
from stocklab.factor.registry import register

__all__ = []


def _pass_through(column):
    """取输入帧的一列作为因子值（原样透传，不改口径）"""

    def _compute(frame):
        return frame[column].astype("float64")

    return _compute


def _fcf_growth(frame):
    """自由现金流同比增长 = (本期 - 上年同期) / |上年同期|（上年同期为 0 -> NaN）"""
    current = frame["free_cashflow"].astype("float64")
    prior = frame["free_cashflow_prior_year"].astype("float64")
    return divide(current - prior, prior.abs())


def _roe_trend(frame):
    """ROE 趋势 = 最新可见 ROE - 上年同期（时点）可见 ROE（单位：小数）"""
    current = frame["roe"].astype("float64")
    prior = frame["roe_prior_year"].astype("float64")
    return current - prior


register(
    Factor(
        "revenue_growth",
        ("growth",),
        ("revenue_yoy",),
        _pass_through("revenue_yoy"),
        "营业收入同比 = (本期 - 去年同期) / |去年同期|（按报告期平移一年）",
    )
)
register(
    Factor(
        "profit_growth",
        ("growth",),
        ("profit_yoy",),
        _pass_through("profit_yoy"),
        "净利润同比，口径同上",
    )
)
register(
    Factor(
        "eps_growth",
        ("growth",),
        ("eps_yoy",),
        _pass_through("eps_yoy"),
        "每股收益同比，口径同上",
    )
)
register(
    Factor(
        "fcf_growth",
        ("growth",),
        ("free_cashflow", "free_cashflow_prior_year"),
        _fcf_growth,
        "自由现金流同比：本期减「as-of 减一年」最新可见值再除以 |后者|",
    )
)
register(
    Factor(
        "roe_trend",
        ("growth",),
        ("roe", "roe_prior_year"),
        _roe_trend,
        "ROE 趋势 = 最新可见 ROE - 上年同时点最新可见 ROE（正值为改善）",
    )
)
