#!/usr/bin/env python3
"""
==============================================================================
StockLab - 成交记录 (stocklab.backtest.trade)
==============================================================================

【模块职责】
   V2 需求 §9.1 的 Trade：一笔**已发生**的成交，含成本拆分与已实现盈亏。

【设计原则】
   - 成交金额 amount = qty × price（股数 × 成交价，不含费用）；
   - 费用三段分列：佣金 commission、印花税 stamp_duty（仅卖出）、滑点 slippage，
     total_cost 为三者之和，买入实付 = amount + total_cost，卖出实收 = amount - total_cost；
   - realized_pnl 只对卖出有意义（按平均成本法、且已扣掉买入与卖出费用），
     买入恒为 None；胜率与盈亏比都建立在它之上，绝不另起一套口径。
"""

__all__ = ["Trade"]

from stocklab.backtest.order import BUY, SELL, BacktestError


class Trade:
    """
    一笔成交

    【字段】
       ts_code      标的代码
       trade_date   成交日
       side         BUY / SELL
       qty          成交股数
       price        成交价（回测中即当日收盘价）
       commission   佣金
       stamp_duty   印花税（卖出才可能非 0）
       slippage     滑点成本
       realized_pnl 卖出的已实现盈亏（含全部费用），买入为 None
    """

    def __init__(
        self,
        ts_code,
        trade_date,
        side,
        qty,
        price,
        commission=0.0,
        stamp_duty=0.0,
        slippage=0.0,
        realized_pnl=None,
    ):
        if side not in (BUY, SELL):
            raise BacktestError(
                "未知买卖方向 %r，可选 %s" % (side, [BUY, SELL])
            )
        if int(qty) <= 0:
            raise BacktestError("成交股数必须为正整数，收到 %r" % (qty,))
        if price is None or price <= 0:
            raise BacktestError("成交价必须为正数，收到 %r" % (price,))
        if side == BUY and realized_pnl is not None:
            raise BacktestError("买入成交不应有已实现盈亏")
        self.ts_code = ts_code
        self.trade_date = trade_date
        self.side = side
        self.qty = int(qty)
        self.price = float(price)
        self.commission = float(commission)
        self.stamp_duty = float(stamp_duty)
        self.slippage = float(slippage)
        self.realized_pnl = (
            None if realized_pnl is None else float(realized_pnl)
        )

    @property
    def amount(self):
        """成交金额（不含费用）"""
        return self.qty * self.price

    @property
    def total_cost(self):
        """全部交易费用"""
        return self.commission + self.stamp_duty + self.slippage

    @property
    def cash_flow(self):
        """现金方向：买入为负（流出），卖出为正（流入）"""
        if self.side == BUY:
            return -(self.amount + self.total_cost)
        return self.amount - self.total_cost

    def to_dict(self):
        """转成字典（供结果帧构建）"""
        return {
            "trade_date": self.trade_date,
            "ts_code": self.ts_code,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "amount": self.amount,
            "commission": self.commission,
            "stamp_duty": self.stamp_duty,
            "slippage": self.slippage,
            "total_cost": self.total_cost,
            "cash_flow": self.cash_flow,
            "realized_pnl": self.realized_pnl,
        }
