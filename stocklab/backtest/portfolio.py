#!/usr/bin/env python3
"""
==============================================================================
StockLab - 组合与持仓 (stocklab.backtest.portfolio)
==============================================================================

【模块职责】
   V2 需求 §9.1 的 Cash / Position / T+1：
     - Position   单只持仓：数量、可用数量（T+1）、平均成本、已实现盈亏
     - Portfolio  组合：现金 + 全部持仓，只接受**已发生成交**来改状态

【设计原则】
   - 组合不做任何行情判断（不知道涨跌停、不知道停牌），它只回答三件事：
     现在有多少现金、每只票有多少**可卖**数量、按给定价格值多少钱；
   - **T+1 在这里落地**：买入后 available 不动，直到下一个交易日 unlock()；
     卖出走 available 而不是 qty，用光即报错，绝不靠「反正撮合是按天的」蒙过去；
   - 平均成本**含买入费用**（费用是真实成本的一部分），卖出时费用从已实现盈亏里扣，
     这样「已实现盈亏 + 持仓市值」能和现金对得上账，没有第二套口径；
   - 缺价的持仓直接抛错：宁可让调用方知道价格没喂进来，也不静默按 0 计价。
"""

__all__ = ["Portfolio", "Position"]

from stocklab.backtest.order import BacktestError


class Position:
    """单只持仓（数量为 0 时由 Portfolio 移除）"""

    def __init__(self, ts_code, qty=0, avg_cost=0.0, available=0, realized_pnl=0.0):
        self.ts_code = ts_code
        self.qty = int(qty)
        self.avg_cost = float(avg_cost)
        self.available = int(available)
        self.realized_pnl = float(realized_pnl)

    def value(self, price):
        """按给定价格计值"""
        return self.qty * float(price)

    def to_dict(self):
        """转成字典（供结果帧构建）"""
        return {
            "ts_code": self.ts_code,
            "qty": self.qty,
            "available": self.available,
            "avg_cost": self.avg_cost,
            "realized_pnl": self.realized_pnl,
        }


class Portfolio:
    """组合：现金 + 持仓，只由成交驱动"""

    def __init__(self, cash):
        if cash < 0:
            raise BacktestError("初始现金不得为负数，收到 %r" % (cash,))
        self.cash = float(cash)
        self.positions = {}

    def position(self, ts_code):
        """取持仓对象，没有则返回 None"""
        return self.positions.get(ts_code)

    def held_codes(self):
        """当前有持仓的标的（数量 > 0）"""
        return [code for code, pos in self.positions.items() if pos.qty > 0]

    def unlock(self):
        """T+1 解锁：进入新的交易日，昨日买入的股份今日可卖"""
        for pos in self.positions.values():
            pos.available = pos.qty

    def apply_buy(self, trade):
        """
        登记一笔买入成交

        买入只增加 qty，**不增加 available**（T+1 当日不可卖）；
        买入费用计入持仓成本（含费口径）。
        """
        if trade.side != "BUY":
            raise BacktestError("apply_buy 只接受买入成交，收到 %s" % trade.side)
        cost_total = trade.total_cost
        if trade.cash_flow * -1 > self.cash + 1e-9:
            raise BacktestError(
                "现金不足：需要 %.2f，只有 %.2f" % (-trade.cash_flow, self.cash)
            )
        self.cash += trade.cash_flow
        pos = self.positions.get(trade.ts_code)
        if pos is None:
            pos = Position(trade.ts_code)
            self.positions[trade.ts_code] = pos
        new_qty = pos.qty + trade.qty
        # 平均成本含买入费用：把这次的实付金额摊进成本
        pos.avg_cost = (pos.avg_cost * pos.qty + -trade.cash_flow) / new_qty
        pos.qty = new_qty
        return pos

    def apply_sell(self, trade):
        """
        登记一笔卖出成交，返回已实现盈亏（已扣买卖全部费用）

        只允许卖 available（T+1 口径），不足直接抛错。
        """
        if trade.side != "SELL":
            raise BacktestError("apply_sell 只接受卖出成交，收到 %s" % trade.side)
        pos = self.positions.get(trade.ts_code)
        if pos is None or pos.qty <= 0:
            raise BacktestError("无持仓可卖：%s" % trade.ts_code)
        if trade.qty > pos.available:
            raise BacktestError(
                "T+1 或可卖数量不足：%s 可卖 %d，委托 %d"
                % (trade.ts_code, pos.available, trade.qty)
            )
        realized = (trade.price - pos.avg_cost) * trade.qty - trade.total_cost
        self.cash += trade.cash_flow
        pos.qty -= trade.qty
        pos.available -= trade.qty
        pos.realized_pnl += realized
        if pos.qty == 0:
            del self.positions[trade.ts_code]
        return realized

    def credit_dividend(self, ts_code, cash_per_share):
        """
        登记现金分红：按持有数量直接入现金，返回入账金额

        【口径】只对**除权除息日之前已持有**的数量发放（引擎在当日开盘前调用）；
        送股、转增暂不建模（公司行为数据尚未接入，见 AGENT.md 数据缺口）。
        """
        pos = self.positions.get(ts_code)
        if pos is None or pos.qty <= 0:
            return 0.0
        amount = pos.qty * float(cash_per_share)
        self.cash += amount
        return amount

    def market_value(self, price_map):
        """按给定价格表计值；任何持仓缺价都直接抛错（不静默按 0 计）"""
        total = 0.0
        for code, pos in self.positions.items():
            if pos.qty <= 0:
                continue
            if code not in price_map:
                raise BacktestError("持仓 %s 没有价格，无法计值" % code)
            total += pos.value(price_map[code])
        return total

    def equity(self, price_map):
        """权益 = 现金 + 持仓市值"""
        return self.cash + self.market_value(price_map)

    def snapshot(self):
        """当前持仓快照（按代码排序）"""
        return [
            self.positions[code].to_dict()
            for code in sorted(self.positions)
            if self.positions[code].qty > 0
        ]
