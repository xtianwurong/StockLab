#!/usr/bin/env python3
"""
==============================================================================
StockLab - 委托与交易语义 (stocklab.backtest.order)
==============================================================================

【模块职责】
   V2 需求 §9.1 的 Order 与买卖方向语义：
     - BacktestError  回测入参或运行状态非法（本包统一异常）
     - SIDES          买卖方向表（BUY / SELL，含中文描述，供 explain 使用）
     - Order          一笔委托：由信号生成，下一个交易日按收盘价撮合

【设计原则】
   - 委托只有三种归宿：待撮合 / 已成交 / 已拒绝；**拒绝必须写明原因**
     （涨停不可买、跌停不可卖、停牌或退市无价格、现金不足一手、T+1 不可卖），
     绝不静默丢弃，否则「为什么没成交」无法复核；
   - 撮合日序在生成时就固定（created_date 信号日 → 下一交易日成交），
     所以「用当天收盘价算出信号、又按当天收盘价成交」这种前视偏差在结构上不可能发生；
   - 委托只是意图，成交以 Trade 为准，两者分开建模。
"""

__all__ = ["BacktestError", "Order", "SIDES"]

BUY = "BUY"
SELL = "SELL"

# 方向表（中文描述用于日志与测试输出）
SIDES = {"BUY": "买入", "SELL": "卖出"}

# 委托状态表
ORDER_STATUS = {"pending": "待撮合", "filled": "已成交", "rejected": "已拒绝"}


class BacktestError(Exception):
    """回测入参或运行状态非法（价格缺失/权重越界/可用持仓不足等）"""


class Order:
    """
    一笔委托

    【字段】
       ts_code      标的代码
       side         BUY / SELL（见 SIDES）
       qty          计划数量（股，正整数）
       created_date 信号日（委托生成日）
       status       pending / filled / rejected
       reason       拒绝或部分成交的说明（成交时为空或写明「部分成交」）
       fill_date    实际成交日（= 下一交易日）
       fill_price   实际成交价（收盘价）
       filled_qty   实际成交数量（部分成交时小于 qty）
    """

    def __init__(self, ts_code, side, qty, created_date):
        if side not in SIDES:
            raise BacktestError(
                "未知买卖方向 %r，可选 %s" % (side, sorted(SIDES))
            )
        quantity = int(qty)
        if quantity <= 0:
            raise BacktestError("委托数量必须为正整数，收到 %r" % (qty,))
        self.ts_code = ts_code
        self.side = side
        self.qty = quantity
        self.created_date = created_date
        self.status = "pending"
        self.reason = ""
        self.fill_date = None
        self.fill_price = None
        self.filled_qty = 0

    def fill(self, trade, note=""):
        """登记成交（note 用于标注部分成交等事实）"""
        self.status = "filled"
        self.fill_date = trade.trade_date
        self.fill_price = trade.price
        self.filled_qty = trade.qty
        self.reason = note

    def reject(self, reason):
        """登记拒绝，reason 必须写清楚为什么没成交"""
        if not reason:
            raise BacktestError("拒绝委托必须写明原因")
        self.status = "rejected"
        self.reason = reason

    def describe(self):
        """人类可读的委托描述（含状态与原因），用于日志与测试断言"""
        head = "%s %s %s %d 股" % (
            self.created_date,
            SIDES[self.side],
            self.ts_code,
            self.qty,
        )
        if self.status == "filled":
            tail = "已成交（%s 收盘价 %s" % (self.fill_date, self.fill_price)
            if self.filled_qty != self.qty:
                tail += "，部分成交 %d 股" % self.filled_qty
            return tail + "）"
        if self.status == "rejected":
            return "%s 已拒绝：%s" % (head, self.reason)
        return "%s 待撮合" % head

    def to_dict(self):
        """转成字典（供结果帧构建）"""
        return {
            "created_date": self.created_date,
            "fill_date": self.fill_date,
            "ts_code": self.ts_code,
            "side": self.side,
            "qty": self.qty,
            "filled_qty": self.filled_qty,
            "price": self.fill_price,
            "status": self.status,
            "reason": self.reason,
        }
