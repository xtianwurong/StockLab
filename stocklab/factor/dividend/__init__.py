#!/usr/bin/env python3
"""
==============================================================================
StockLab - 分红类因子 (stocklab.factor.dividend)
==============================================================================

【模块职责】
   登记 V2 需求 §7.5 的分红因子：
   股息率 / 分红增长 / 分红率（派息比例）/ 分红稳定性。

【数据现状（务必先读）】
   本项目当前**没有分红数据源**：
     - dividend_yield 的输入 dv_ttm 来自全市场估值快照（该阶段依赖东方财富，
       本机代理不可用时为空）；
     - dps / dps_prior_year / dividend_years_* 需要一张尚未接入的分红送转表。
   因此这四个因子先按契约落地，输入帧构造方在取不到数据时给出**全 NaN 列**
   并记 WARNING；筛选条件命中它们时，逐只原因显示「无数据」，
   不会出现「假装通过」的结果。接入分红数据源后无需改因子层。
"""

from stocklab.factor.base import Factor, divide
from stocklab.factor.registry import register

__all__ = []


def _pass_through(column):
    """取输入帧的一列作为因子值（原样透传，不改口径）"""

    def _compute(frame):
        return frame[column].astype("float64")

    return _compute


def _dividend_growth(frame):
    """分红同比 = (本期每股分红 - 上年同期每股分红) / |上年同期|"""
    current = frame["dps"].astype("float64")
    prior = frame["dps_prior_year"].astype("float64")
    return divide(current - prior, prior.abs())


def _payout_ratio(frame):
    """分红率 = 每股现金分红 / 每股收益（EPS 为 0 或缺失 -> NaN）"""
    return divide(frame["dps"], frame["eps"])


def _dividend_stability(frame):
    """
    分红稳定性 = 近 N 年中实际分红的年数占比（0~1）

    【口径】N 由输入帧的 dividend_years_total 决定（构造方固定为 5 年），
    中途上市导致的不足 5 年按实际可观察年数计算。
    """
    return divide(frame["dividend_years_paid"], frame["dividend_years_total"])


register(
    Factor(
        "dividend_yield",
        ("value", "dividend"),
        ("dv_ttm",),
        _pass_through("dv_ttm"),
        "股息率 = 近 12 个月每股分红 / 最新价（%），对应 §7.1 与 §7.5",
    )
)
register(
    Factor(
        "dividend_growth",
        ("dividend",),
        ("dps", "dps_prior_year"),
        _dividend_growth,
        "每股分红同比 = (本期 - 上年同期) / |上年同期|",
    )
)
register(
    Factor(
        "payout_ratio",
        ("dividend",),
        ("dps", "eps"),
        _payout_ratio,
        "分红率 = 每股现金分红 / 每股收益",
    )
)
register(
    Factor(
        "dividend_stability",
        ("dividend",),
        ("dividend_years_paid", "dividend_years_total"),
        _dividend_stability,
        "分红稳定性 = 近 5 年实际分红年数 / 可观察年数（0~1）",
    )
)
