#!/usr/bin/env python3
"""
==============================================================================
StockLab - 交易成本 (stocklab.backtest.cost)
==============================================================================

【模块职责】
   V2 需求 §9.1 / Phase 3 第 12 项 Transaction Cost：
     - CostModel  A 股交易成本模型：佣金 + 印花税（仅卖出）+ 滑点

【设计原则】
   - 三种费用**分列**而不是揉成一个「费率」：佣金有最低值、印花税只对卖出征收、
     滑点按成交金额计，三者口径不同，合并后无法核对；
   - 费率与最低佣金一律是入参，默认值按 A 股常见水平写死在签名里，
     调用方随时可以覆盖，禁止在撮合逻辑里散落魔法数字；
   - 入参非负校验在构造期完成（负费率 = 数据错误，等到成交时才发现就晚了）；
   - 滑点按「成本」入账而不是偷偷改成交价：成交价仍是当日收盘价，
     这样成交流水与行情能一一对上。
"""

__all__ = ["CostModel"]

from stocklab.backtest.order import SELL, BacktestError


class CostModel:
    """
    A 股交易成本模型

    【口径】
       佣金   = max(成交金额 × commission_rate, min_commission)，买卖双边
       印花税 = 成交金额 × stamp_duty_rate，**仅卖出**
       滑点   = 成交金额 × slippage_bps / 10000，买卖双边

    【默认值】
       佣金 0.025%（最低 5 元）、印花税 0.05%（2023-08-28 起的卖出税率）、
       滑点 0（默认不假设冲击成本，需要时显式传入）。
       过户费（上交所 0.001%）金额量级远低于佣金，暂不建模，见 AGENT.md 注意事项。
    """

    def __init__(
        self,
        commission_rate=0.00025,
        min_commission=5.0,
        stamp_duty_rate=0.0005,
        slippage_bps=0.0,
    ):
        for name, value in (
            ("commission_rate", commission_rate),
            ("min_commission", min_commission),
            ("stamp_duty_rate", stamp_duty_rate),
            ("slippage_bps", slippage_bps),
        ):
            if value < 0:
                raise BacktestError("%s 不得为负数，收到 %r" % (name, value))
        self.commission_rate = float(commission_rate)
        self.min_commission = float(min_commission)
        self.stamp_duty_rate = float(stamp_duty_rate)
        self.slippage_bps = float(slippage_bps)

    @property
    def slippage_rate(self):
        """滑点费率（小数），便于撮合时估算可买数量"""
        return self.slippage_bps / 10000.0

    def apply(self, side, price, qty):
        """
        计算一笔成交的全部费用

        Args:
            side (str): BUY 或 SELL
            price (float): 成交价
            qty (int): 成交股数

        Returns:
            dict: {"commission": 佣金, "stamp_duty": 印花税,
                   "slippage": 滑点, "total": 合计}
        """
        if side not in ("BUY", "SELL"):
            raise BacktestError(
                "未知买卖方向 %r，可选 %s" % (side, ["BUY", "SELL"])
            )
        if price is None or price <= 0:
            raise BacktestError("成交价必须为正数，收到 %r" % (price,))
        if int(qty) <= 0:
            raise BacktestError("成交股数必须为正整数，收到 %r" % (qty,))

        amount = float(price) * int(qty)
        commission = amount * self.commission_rate
        if commission > 0 or self.min_commission > 0:
            # 有成交就有佣金，向下取到最低值；费率为 0 且最低佣金为 0 时才是 0
            commission = max(commission, self.min_commission)
        stamp_duty = (
            amount * self.stamp_duty_rate if side == SELL else 0.0
        )
        slippage = amount * self.slippage_rate
        total = commission + stamp_duty + slippage
        return {
            "commission": commission,
            "stamp_duty": stamp_duty,
            "slippage": slippage,
            "total": total,
        }
