#!/usr/bin/env python3
"""
==============================================================================
StockLab - 质量类因子 (stocklab.factor.quality)
==============================================================================

【模块职责】
   登记 V2 需求 §7.2 的质量因子：
   ROE / ROIC / 毛利率 / 营业利润率 / CFO/净利润 / 资产负债率意义上的 Debt/Equity。

【输入帧约定】
   roe / roic / gross_margin / operating_margin 直接来自 fundamental.financial_indicators
   （由 stocklab.fundamental 纯计算派生，Point-in-Time 公告日可见）；
   cfo_to_net_profit 与 debt_to_equity 需要现金流量表与资产负债表同口径拼接。
"""

from stocklab.factor.base import Factor, divide
from stocklab.factor.registry import register

__all__ = []


def _pass_through(column):
    """取输入帧的一列作为因子值（原样透传，不改口径）"""

    def _compute(frame):
        return frame[column].astype("float64")

    return _compute


register(
    Factor(
        "roe",
        ("quality",),
        ("roe",),
        _pass_through("roe"),
        "净资产收益率 = 归母净利润 / 净资产（同期累计口径）",
    )
)
register(
    Factor(
        "roic",
        ("quality",),
        ("roic",),
        _pass_through("roic"),
        "投入资本回报率 = NOPAT / (净资产 + 有息负债)",
    )
)
register(
    Factor(
        "gross_margin",
        ("quality",),
        ("gross_margin",),
        _pass_through("gross_margin"),
        "毛利率 = (营业收入 - 营业成本) / 营业收入",
    )
)
register(
    Factor(
        "operating_margin",
        ("quality",),
        ("operating_margin",),
        _pass_through("operating_margin"),
        "营业利润率 = 营业利润 / 营业收入",
    )
)
register(
    Factor(
        "cfo_to_net_profit",
        ("quality",),
        ("operating_cashflow", "net_profit"),
        lambda frame: divide(frame["operating_cashflow"], frame["net_profit"]),
        "经营现金流 / 净利润：大于 1 说明利润有现金支撑（累计口径，同期可比）",
    )
)
register(
    Factor(
        "debt_to_equity",
        ("quality",),
        ("total_liabilities", "equity"),
        lambda frame: divide(frame["total_liabilities"], frame["equity"]),
        "产权比率 = 总负债 / 所有者权益；净资产为 0 或缺失时为 NaN",
    )
)
