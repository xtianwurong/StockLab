#!/usr/bin/env python3
"""
==============================================================================
StockLab - 回测引擎 (stocklab.backtest.engine)
==============================================================================

【模块职责】
   V2 需求 §9 的 Backtest Engine（Phase 3 第 10 项）：
     - BacktestEngine  按日推进：信号 → 委托 → **下一交易日**收盘成交 → 计值
     - BacktestResult  回测产物：权益曲线 / 成交 / 委托（含拒绝原因）/ 指标

【撮合时序（一个交易日内固定四步，顺序不可换）】
       1. unlock()          新交易日开始，昨日买入的股份今日可卖（T+1）
       2. 分红入账          除权除息日按**当日开盘前**持仓发放现金
       3. 撮合昨日委托      先卖后买（先腾出现金），一律按当日**收盘价**
       4. 计值 → 下单       按当日收盘算权益，再由当日信号生成委托，等明日成交

【设计原则】
   - **前视偏差在结构上不可能**：信号只能用当日及之前的数据，委托固定在下一
     交易日成交，「当天算的信号当天成交」不存在；
   - **买不进、卖不出都必须说话**：涨停不可买、跌停不可卖、停牌 / 退市无价格、
     现金不足一手、T+1 不可卖 —— 原因逐条写进委托的 reason，绝不静默丢单；
   - **退市不清零**：仍持仓的退市标的按最近价格计值并在 equity.stale_count 里
     计数，卖出被拒绝并写明原因，权益里如实体现，不会被悄悄剔除；
   - 引擎只吃 DataFrame（行情 / 信号 / 分红 / 基准），不联网、不查库，
     数据由调用方（research 层）准备。
"""

import pandas as pd

from stocklab.backtest.cost import CostModel
from stocklab.backtest.metrics import compute_metrics
from stocklab.backtest.order import BUY, SELL, BacktestError, Order
from stocklab.backtest.portfolio import Portfolio
from stocklab.backtest.trade import Trade

__all__ = ["BacktestEngine", "BacktestResult"]

EQUITY_COLUMNS = (
    "trade_date",
    "cash",
    "market_value",
    "total_equity",
    "returns",
    "stale_count",
    "dividend_income",
)
TRADE_COLUMNS = (
    "trade_date",
    "ts_code",
    "side",
    "qty",
    "price",
    "amount",
    "commission",
    "stamp_duty",
    "slippage",
    "total_cost",
    "cash_flow",
    "realized_pnl",
)
ORDER_COLUMNS = (
    "created_date",
    "fill_date",
    "ts_code",
    "side",
    "qty",
    "filled_qty",
    "price",
    "status",
    "reason",
)
POSITION_COLUMNS = (
    "ts_code",
    "qty",
    "available",
    "avg_cost",
    "realized_pnl",
)

# 涨跌停价按 0.01 取整后的边界比较余量
_PRICE_EPSILON = 1e-9


def _require_columns(frame, required, name):
    """入参必须是含指定列的 DataFrame"""
    if frame is None or not isinstance(frame, pd.DataFrame):
        raise BacktestError("%s 必须是 pandas.DataFrame，收到 %r" % (name, frame))
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise BacktestError(
            "%s 缺少列 %s，实际列 %s" % (name, missing, list(frame.columns))
        )


def _with_dates(frame, column, name):
    """把日期列统一解析成时间戳（无法解析直接报错，绝不静默丢弃）"""
    _require_columns(frame, (column,), name)
    parsed = pd.to_datetime(frame[column], errors="coerce")
    if parsed.isna().any():
        sample = frame[column][parsed.isna()].iloc[0]
        raise BacktestError("%s.%s 含无法解析的日期：%r" % (name, column, sample))
    result = frame.copy()
    result[column] = parsed
    return result


def _check_unique(frame, keys, name):
    """同一 (标的, 日期) 只能出现一次：重复行会让撮合口径含糊"""
    duplicated = frame.duplicated(keys)
    if duplicated.any():
        sample = frame.loc[duplicated, keys].iloc[0]
        raise BacktestError("%s 存在重复行 %s：%s" % (name, keys, dict(sample)))


def _empty_frame(columns):
    """列齐全的空帧（保证下游 colnames 恒定，空结果也不缺列）"""
    return pd.DataFrame(columns=list(columns))


class BacktestResult:
    """
    回测产物

    【字段】
       equity       权益曲线：cash / market_value / total_equity / returns /
                     stale_count（按最近价计值的持仓数）/ dividend_income
       trades       全部成交（含费用三段与已实现盈亏）
       orders       全部委托（含被拒绝的与拒绝原因）
       positions    回测结束时的持仓快照
       initial_cash 初始现金
    """

    def __init__(
        self,
        equity,
        trades,
        orders,
        positions,
        initial_cash,
        benchmark_equity=None,
    ):
        self.equity = equity
        self.trades = trades
        self.orders = orders
        self.positions = positions
        self.initial_cash = initial_cash
        self.benchmark_equity = benchmark_equity

    def metrics(self, risk_free=0.0):
        """§9.3 的全部指标（run(benchmark=...) 传了基准时 excess_return 才非 NaN）"""
        return compute_metrics(
            self.equity["total_equity"],
            trades=self.trades,
            benchmark=self.benchmark_equity,
            risk_free=risk_free,
        )

    @property
    def final_equity(self):
        """期末权益"""
        if self.equity.empty:
            return float(self.initial_cash)
        return float(self.equity["total_equity"].iloc[-1])

    @property
    def rejected(self):
        """被拒绝的委托（为什么没成交，必须查得到）"""
        if self.orders.empty:
            return self.orders
        return self.orders[self.orders["status"] == "rejected"]


class BacktestEngine:
    """
    回测引擎

    Args:
        initial_cash (float): 初始现金
        cost (CostModel): 交易成本模型，缺省用 A 股常见费率
        lot_size (int): 每手股数（买入按手取整，卖出可清仓）
        limit_rate (float 或 dict): 涨跌停幅度，默认 0.10；传 dict 按 ts_code
            覆盖（创业板 / 科创板 0.20、北交所 0.30）；行情里若已有
            limit_up / limit_down / suspended 布尔列则以它们为准。
    """

    def __init__(
        self,
        initial_cash=1_000_000.0,
        cost=None,
        lot_size=100,
        limit_rate=0.10,
    ):
        if initial_cash <= 0:
            raise BacktestError("初始现金必须为正数，收到 %r" % (initial_cash,))
        quantity = int(lot_size)
        if quantity <= 0:
            raise BacktestError("每手股数必须为正整数，收到 %r" % (lot_size,))
        self.initial_cash = float(initial_cash)
        self.cost = cost if cost is not None else CostModel()
        self.lot_size = quantity
        self.limit_rate = self._check_limit_rate(limit_rate)

    @staticmethod
    def _check_limit_rate(limit_rate):
        """涨跌停幅度校验（单值或按 ts_code 的映射，区间必须在 (0, 1)）"""
        if isinstance(limit_rate, dict):
            for code, rate in limit_rate.items():
                if not 0 < rate < 1:
                    raise BacktestError(
                        "%s 的涨跌停幅度必须在 (0, 1) 之间，收到 %r" % (code, rate)
                    )
            return dict(limit_rate)
        if not 0 < limit_rate < 1:
            raise BacktestError(
                "涨跌停幅度必须在 (0, 1) 之间，收到 %r" % (limit_rate,)
            )
        return float(limit_rate)

    # ------------------------------------------------------------------
    # 入参校验
    # ------------------------------------------------------------------
    def _validate(self, prices, signals, dividends, benchmark):
        _require_columns(prices, ("ts_code", "trade_date", "close"), "prices")
        _require_columns(signals, ("ts_code", "trade_date", "weight"), "signals")
        if prices.empty:
            raise BacktestError("prices 没有任何行情行，无法回测")

        close = pd.to_numeric(prices["close"], errors="coerce")
        if close.isna().any():
            raise BacktestError("prices.close 含缺失或非数值")
        if (close <= 0).any():
            sample = prices["close"][close <= 0].iloc[0]
            raise BacktestError("prices.close 必须为正数，收到 %r" % (sample,))
        _check_unique(prices, ["ts_code", "trade_date"], "prices")
        _check_unique(signals, ["ts_code", "trade_date"], "signals")

        weights = pd.to_numeric(signals["weight"], errors="coerce")
        if weights.isna().any():
            raise BacktestError("signals.weight 含缺失或非数值")
        if (weights < 0).any():
            raise BacktestError("A 股现货不支持做空，signals.weight 不得为负数")
        if (weights > 1).any():
            sample = float(weights[weights > 1].iloc[0])
            raise BacktestError("单只目标权重不得大于 1，收到 %r" % sample)
        totals = weights.groupby(signals["trade_date"]).sum()
        over = totals[totals > 1 + 1e-9]
        if not over.empty:
            detail = {
                str(day): round(float(value), 6)
                for day, value in over.items()
            }
            raise BacktestError("同一信号日目标权重合计超过 1：%s" % detail)

        if dividends is not None:
            _require_columns(
                dividends, ("ts_code", "ex_date", "cash_per_share"), "dividends"
            )
            _check_unique(dividends, ["ts_code", "ex_date"], "dividends")
            per_share = pd.to_numeric(dividends["cash_per_share"], errors="coerce")
            if per_share.isna().any() or (per_share < 0).any():
                raise BacktestError(
                    "dividends.cash_per_share 必须是非负数值（每股现金分红）"
                )

        if benchmark is not None:
            _require_columns(benchmark, ("trade_date", "close"), "benchmark")
            _check_unique(benchmark, ["trade_date"], "benchmark")
            bench_close = pd.to_numeric(benchmark["close"], errors="coerce")
            if bench_close.isna().any() or (bench_close <= 0).any():
                raise BacktestError("benchmark.close 必须是正数")

    # ------------------------------------------------------------------
    # 行情面板
    # ------------------------------------------------------------------
    def _pivot(self, frame, column):
        """宽表面板：行=交易日，列=ts_code；缺行即该日无行情（停牌 / 退市）"""
        panel = frame.pivot(index="trade_date", columns="ts_code", values=column)
        return panel.sort_index().sort_index(axis=1)

    def _pivot_flag(self, prices, close_raw, column):
        """显式布尔列转面板；行情里没有该列时返回 None"""
        if column not in prices.columns:
            return None
        panel = self._pivot(prices, column).reindex(
            index=close_raw.index, columns=close_raw.columns
        )
        return panel.fillna(False).astype(bool)

    def _rates(self, codes):
        """按 ts_code 的涨跌停幅度（dict 里没列出的按默认 0.10）"""
        if isinstance(self.limit_rate, dict):
            return pd.Series(
                {code: float(self.limit_rate.get(code, 0.10)) for code in codes}
            )
        return pd.Series({code: float(self.limit_rate) for code in codes})

    def _false_panel(self, close_raw):
        return pd.DataFrame(False, index=close_raw.index, columns=close_raw.columns)

    def _limit_panels(self, prices, close_raw):
        """
        涨跌停面板

        行情里显式给的 limit_up / limit_down 列优先；否则用
        pre_close × (1±limit_rate) 按 0.01 取整推导（A 股价格笼子）；
        两者都没有 → 全 False（不假设存在限制，事实是「没给就按无限价处理」）。
        """
        up = self._pivot_flag(prices, close_raw, "limit_up")
        down = self._pivot_flag(prices, close_raw, "limit_down")
        if up is not None and down is not None:
            return up, down
        if "pre_close" not in prices.columns:
            return (
                up if up is not None else self._false_panel(close_raw),
                down if down is not None else self._false_panel(close_raw),
            )
        pre_close = self._pivot(prices, "pre_close").reindex(
            index=close_raw.index, columns=close_raw.columns
        )
        rates = self._rates(close_raw.columns)
        upper = pre_close.mul(1 + rates, axis=1).round(2)
        lower = pre_close.mul(1 - rates, axis=1).round(2)
        valid = pre_close.notna() & close_raw.notna()
        return (
            up if up is not None else (valid & (close_raw >= upper - _PRICE_EPSILON)),
            down
            if down is not None
            else (valid & (close_raw <= lower + _PRICE_EPSILON)),
        )

    def _suspended_panel(self, prices, close_raw):
        """停牌面板：显式 suspended 列优先，否则按「有行情但成交量为 0」推导"""
        panel = self._pivot_flag(prices, close_raw, "suspended")
        if panel is not None:
            return panel
        if "volume" not in prices.columns:
            return self._false_panel(close_raw)
        volume = self._pivot(prices, "volume").reindex(
            index=close_raw.index, columns=close_raw.columns
        )
        return volume.fillna(0) <= 0

    # ------------------------------------------------------------------
    # 数量
    # ------------------------------------------------------------------
    def _affordable_qty(self, price, cash):
        """按现金与成本模型算得起的股数（向下取整到手）"""
        if price <= 0 or cash <= 0:
            return 0
        unit_rate = self.cost.commission_rate + self.cost.slippage_rate
        qty = int(cash // (price * (1 + unit_rate)))
        qty = (qty // self.lot_size) * self.lot_size
        while qty > 0:
            fees = self.cost.apply(BUY, price, qty)["total"]
            if qty * price + fees <= cash:
                return qty
            qty -= self.lot_size
        return 0

    def _to_lot(self, qty):
        """向下取整到整手"""
        return (int(qty) // self.lot_size) * self.lot_size

    # ------------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------------
    def _build_orders(self, rows, date, price_row, portfolio, equity, orders_log):
        """
        由当日信号生成委托（成交日 = 下一个交易日）

        信号给的是**当日完整目标权重**：持仓里没出现的标的按 0 处理（清仓）。
        算不出目标仓位的情况（信号日无价格）当场记成被拒绝的委托，理由写清楚。
        """
        weights = {}
        for code, weight in zip(rows["ts_code"], rows["weight"]):
            weights[code] = float(weight)

        pending = []
        for code in sorted(set(weights) | set(portfolio.positions)):
            pos = portfolio.position(code)
            current_qty = pos.qty if pos is not None else 0
            weight = weights.get(code, 0.0)
            price = price_row.get(code)
            has_price = price is not None and not pd.isna(price)

            if weight > 0 and not has_price:
                rejected = Order(code, BUY, max(current_qty, self.lot_size), date)
                rejected.reject(
                    "信号日无价格（停牌 / 未上市 / 已退市），无法计算目标仓位"
                )
                orders_log.append(rejected)
                continue

            if weight > 0:
                target_qty = self._to_lot(float(weight) * equity / price)
            else:
                target_qty = 0

            diff = target_qty - current_qty
            if diff == 0:
                continue
            if diff > 0:
                qty = self._to_lot(diff)
                if qty <= 0:
                    continue
                # 只按目标算数量，**不在这里扣现金**：同日的卖出先成交，
                # 现金够不够要等撮合时（卖单之后）再判断
                order = Order(code, BUY, qty, date)
            else:
                # 清仓不需要信号日的价格（目标数量恒为 0）；成交日没有价格
                # 会被撮合明确拒绝，退市持仓因此绝不会被静默剔除
                order = Order(code, SELL, current_qty - target_qty, date)
            # 一律登记：成交 / 拒绝的状态由撮合回填，事后能查「为什么没成交」
            orders_log.append(order)
            pending.append(order)
        return pending

    # ------------------------------------------------------------------
    # 撮合
    # ------------------------------------------------------------------
    def _fill(
        self,
        order,
        date,
        price_row,
        suspended_row,
        limit_up_row,
        limit_down_row,
        portfolio,
        trades_log,
    ):
        """按当日收盘价撮合一笔委托；失败一律写 reason 拒绝"""
        price = price_row.get(order.ts_code)
        if price is None or pd.isna(price):
            order.reject("无价格（停牌 / 未上市 / 已退市），当日无法成交")
            return
        if bool(suspended_row.get(order.ts_code, False)):
            order.reject("停牌，当日无法成交")
            return

        if order.side == BUY:
            if bool(limit_up_row.get(order.ts_code, False)):
                order.reject("涨停，收盘价封板买不进")
                return
            qty = min(order.qty, self._affordable_qty(price, portfolio.cash))
            if qty <= 0:
                order.reject("现金不足一手（%d 股）" % self.lot_size)
                return
            costs = self.cost.apply(BUY, price, qty)
            trade = Trade(
                order.ts_code,
                date,
                BUY,
                qty,
                price,
                commission=costs["commission"],
                stamp_duty=costs["stamp_duty"],
                slippage=costs["slippage"],
            )
            portfolio.apply_buy(trade)
            trades_log.append(trade)
            note = "" if qty == order.qty else "部分成交：现金不足以买满"
            order.fill(trade, note)
            return

        if bool(limit_down_row.get(order.ts_code, False)):
            order.reject("跌停，收盘价封板卖不出")
            return
        pos = portfolio.position(order.ts_code)
        available = 0 if pos is None else pos.available
        qty = min(order.qty, available)
        if qty <= 0:
            order.reject(
                "T+1：当日买入的股份不可卖出（可卖 0 股）"
                if pos is not None
                else "无可卖持仓"
            )
            return
        costs = self.cost.apply(SELL, price, qty)
        trade = Trade(
            order.ts_code,
            date,
            SELL,
            qty,
            price,
            commission=costs["commission"],
            stamp_duty=costs["stamp_duty"],
            slippage=costs["slippage"],
        )
        # 已实现盈亏由组合按「平均成本 + 全部费用」算出，成交只负责记账
        trade.realized_pnl = portfolio.apply_sell(trade)
        trades_log.append(trade)
        note = "" if qty == order.qty else "部分成交：T+1 可卖数量不足"
        order.fill(trade, note)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self, prices, signals, dividends=None, benchmark=None):
        """
        执行回测

        Args:
            prices (pd.DataFrame): 行情，必需列 ts_code / trade_date / close；
                可选 pre_close（推导涨跌停）、volume（推导停牌）、
                limit_up / limit_down / suspended（直接给定时优先）
            signals (pd.DataFrame): 目标权重 ts_code / trade_date / weight，
                每个信号日给**完整目标**，未出现的持仓按 0（清仓）；
                信号日必须是行情里的交易日
            dividends (pd.DataFrame): 现金分红 ts_code / ex_date / cash_per_share
            benchmark (pd.DataFrame): 基准 trade_date / close（算超额收益用）

        Returns:
            BacktestResult
        """
        prices = _with_dates(prices, "trade_date", "prices")
        signals = _with_dates(signals, "trade_date", "signals")
        if dividends is not None:
            dividends = _with_dates(dividends, "ex_date", "dividends")
        if benchmark is not None:
            benchmark = _with_dates(benchmark, "trade_date", "benchmark")
        self._validate(prices, signals, dividends, benchmark)

        close_raw = self._pivot(prices, "close")
        dates = list(close_raw.index)
        date_set = set(dates)
        missing_signal_dates = sorted(
            set(signals["trade_date"]) - date_set
        )
        if missing_signal_dates:
            raise BacktestError(
                "信号日不在行情交易日内：%s"
                % [str(day.date()) for day in missing_signal_dates[:5]]
            )
        if dividends is not None:
            missing_dividend_dates = sorted(set(dividends["ex_date"]) - date_set)
            if missing_dividend_dates:
                raise BacktestError(
                    "除权除息日不在行情交易日内：%s"
                    % [str(day.date()) for day in missing_dividend_dates[:5]]
                )

        close_value = close_raw.ffill()
        limit_up, limit_down = self._limit_panels(prices, close_raw)
        suspended = self._suspended_panel(prices, close_raw)
        price_rows = {day: close_raw.loc[day] for day in dates}
        value_rows = {day: close_value.loc[day] for day in dates}
        up_rows = {day: limit_up.loc[day] for day in dates}
        down_rows = {day: limit_down.loc[day] for day in dates}
        suspended_rows = {day: suspended.loc[day] for day in dates}
        signal_groups = dict(tuple(signals.groupby("trade_date")))
        dividend_groups = (
            {}
            if dividends is None or dividends.empty
            else dict(tuple(dividends.groupby("ex_date")))
        )
        benchmark_equity = (
            None if benchmark is None else self._align_benchmark(benchmark, dates)
        )

        portfolio = Portfolio(self.initial_cash)
        pending = []
        equity_rows = []
        trades_log = []
        orders_log = []

        for day in dates:
            # 1) 新交易日：昨日买入的股份今日可卖（T+1）
            portfolio.unlock()

            # 2) 除权除息：按当日开盘前的持仓发现金
            dividend_income = 0.0
            if day in dividend_groups:
                rows = dividend_groups[day]
                for code, per_share in zip(rows["ts_code"], rows["cash_per_share"]):
                    dividend_income += portfolio.credit_dividend(code, per_share)

            # 3) 撮合昨日委托：先卖后买，腾出的现金当天就能用
            price_row = price_rows[day]
            fill_orders, pending = pending, []
            for order in fill_orders:
                if order.side == SELL:
                    self._fill(
                        order,
                        day,
                        price_row,
                        suspended_rows[day],
                        up_rows[day],
                        down_rows[day],
                        portfolio,
                        trades_log,
                    )
            for order in fill_orders:
                if order.side == BUY:
                    self._fill(
                        order,
                        day,
                        price_row,
                        suspended_rows[day],
                        up_rows[day],
                        down_rows[day],
                        portfolio,
                        trades_log,
                    )

            # 4) 计值（停牌 / 退市按最近价，缺价直接报错不静默计 0）
            holdings = portfolio.held_codes()
            price_map = {}
            stale_count = 0
            for code in holdings:
                if price_row.get(code) is None or pd.isna(price_row.get(code)):
                    stale_count += 1
                carried = value_rows[day].get(code)
                if carried is None or pd.isna(carried):
                    raise BacktestError(
                        "持仓 %s 没有价格历史，无法计值（行情区间是否覆盖该标的？）"
                        % code
                    )
                price_map[code] = carried
            market_value = portfolio.market_value(price_map)
            equity_rows.append(
                {
                    "trade_date": day,
                    "cash": portfolio.cash,
                    "market_value": market_value,
                    "total_equity": portfolio.cash + market_value,
                    "stale_count": stale_count,
                    "dividend_income": dividend_income,
                }
            )

            # 5) 当日信号下单（明天才可能成交）
            if day in signal_groups:
                pending = self._build_orders(
                    signal_groups[day],
                    day,
                    price_row,
                    portfolio,
                    portfolio.cash + market_value,
                    orders_log,
                )

        # 回测区间结束后还没轮到成交日的委托：明确记成拒绝，不假装成交
        for order in pending:
            order.reject("回测区间内没有下一个交易日可成交")

        equity = pd.DataFrame(equity_rows, columns=list(EQUITY_COLUMNS))
        if not equity.empty:
            equity["returns"] = equity["total_equity"].pct_change()
        trades = (
            pd.DataFrame([trade.to_dict() for trade in trades_log])
            if trades_log
            else _empty_frame(TRADE_COLUMNS)
        )
        orders = (
            pd.DataFrame([order.to_dict() for order in orders_log])
            if orders_log
            else _empty_frame(ORDER_COLUMNS)
        )
        snapshot = portfolio.snapshot()
        positions = (
            pd.DataFrame(snapshot)
            if snapshot
            else _empty_frame(POSITION_COLUMNS)
        )
        return BacktestResult(
            equity=equity,
            trades=trades,
            orders=orders,
            positions=positions,
            initial_cash=self.initial_cash,
            benchmark_equity=benchmark_equity,
        )

    def _align_benchmark(self, benchmark, dates):
        """把基准收盘价对齐到回测交易日（前向填充，起始晚于回测的部分丢掉）"""
        lookup = benchmark.drop_duplicates("trade_date").set_index("trade_date")[
            "close"
        ]
        aligned = lookup.reindex(dates).ffill().dropna()
        if aligned.empty:
            return None
        return aligned.astype("float64")
