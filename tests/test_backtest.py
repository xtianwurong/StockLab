#!/usr/bin/env python3
"""
==============================================================================
StockLab - 回测引擎测试 (tests/test_backtest.py)
==============================================================================

【功能用途】
  覆盖 V2 需求 §9 与 Phase 3（回测 / 组合 / 交易成本 / 幸存者安全股票池）：
    1. 交易成本：佣金最低值、印花税只对卖出、滑点、非法费率拒绝
    2. 组合与 T+1：买入当日不可卖、平均成本含费用、已实现盈亏、缺价拒绝计值
    3. 指标：CAGR / 年化 / 波动率 / 夏普 / 索提诺 / 卡玛 / 最大回撤 /
       胜率 / 盈亏比 / 换手 / 超额收益，以及分母为 0 时一律 NaN
    4. 幸存者安全：as-of 股票池口径 + 退市持仓不清零、卖出被明确拒绝
    5. 引擎正常路径：手算核对（部分成交、含费用的已实现盈亏、现金分红、
       基准超额），以及「信号日当天绝不成交」的前视偏差防护
    6. 拒绝原因：涨停不可买、跌停不可卖、停牌不可成交、现金不足一手
    7. 非法输入：重复行情、非正价格、非交易日信号、权重越界、缺列全拒

【运行方式】
  python tests/test_backtest.py
  （纯合成数据，不联网、不碰数据库）
"""

import math
import os
import sys


import pandas as pd

# 允许直接执行本文件（./venv/bin/python tests/xxx.py）；走 pytest 时由
# pytest.ini 的 `pythonpath = .` 统一负责，不会重复插入。
if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.backtest import (
    BacktestEngine,
    BacktestError,
    CostModel,
    Order,
    Portfolio,
    Trade,
    compute_metrics,
    universe_as_of,
)
import pytest
from stocklab.backtest.metrics import cagr

A = "000001.SZ"
B = "600519.SH"
D1, D2, D3, D4 = (
    "2024-01-02",
    "2024-01-03",
    "2024-01-04",
    "2024-01-05",
)

# 零成本模型：把手算核对与费用核对分开，互不干扰
FREE_COST = CostModel(
    commission_rate=0.0, min_commission=0.0, stamp_duty_rate=0.0, slippage_bps=0.0
)

def _close(actual, expected, tolerance=1e-6, note=""):
    """数值断言（带上下文，失败时能直接看到期望与实际）"""
    assert abs(actual - expected) <= tolerance, "%s 期望 %s，实际 %s" % (
        note,
        expected,
        actual,
    )

def _price_frame(rows):
    """行情帧：[(ts_code, trade_date, close), ...]"""
    return pd.DataFrame(rows, columns=["ts_code", "trade_date", "close"])

def _signal_frame(rows):
    """信号帧：[(ts_code, trade_date, weight), ...]"""
    return pd.DataFrame(rows, columns=["ts_code", "trade_date", "weight"])

def _engine(cash=10000.0, cost=None, **kwargs):
    return BacktestEngine(initial_cash=cash, cost=cost, **kwargs)

def _equity_list(result):
    return [round(value, 6) for value in result.equity["total_equity"]]

# ==============================================================================
# 阶段一：交易成本
# ==============================================================================
def test_cost():

    cost = CostModel()  # 佣金 0.025%（最低 5 元）、印花税 0.05%（卖出）
    fees = cost.apply("BUY", 10.0, 1000)  # 金额 1 万：佣金不足最低值 -> 5
    _close(fees["commission"], 5.0, note="佣金最低值")
    _close(fees["stamp_duty"], 0.0, note="买入不收印花税")
    _close(fees["slippage"], 0.0, note="默认无滑点")
    _close(fees["total"], 5.0, note="买入总费用")

    fees = cost.apply("BUY", 10.0, 10000)  # 金额 10 万：25 元 > 最低 5 元
    _close(fees["commission"], 25.0, note="佣金按费率")
    print("  -> 佣金：不足最低值取 5 元，够时按 0.025% 计")

    fees = cost.apply("SELL", 10.0, 10000)  # 卖出 10 万：印花税 50 元
    _close(fees["stamp_duty"], 50.0, note="卖出印花税")
    _close(fees["total"], 75.0, note="卖出总费用 = 佣金 + 印花税")
    print("  -> 印花税：仅卖出征收（0.05%）")

    slippy = CostModel(commission_rate=0.0, min_commission=0.0, slippage_bps=10.0)
    fees = slippy.apply("BUY", 10.0, 10000)
    _close(fees["slippage"], 100.0, note="滑点 10bps")
    _close(fees["total"], 100.0, note="只有滑点时的总费用")
    print("  -> 滑点：按成交金额 × bps，买卖双边都计")

    for bad in (
        lambda: CostModel(commission_rate=-0.1),
        lambda: CostModel(min_commission=-1.0),
        lambda: CostModel(stamp_duty_rate=-0.01),
        lambda: CostModel(slippage_bps=-1.0),
        lambda: cost.apply("HOLD", 10.0, 100),
        lambda: cost.apply("BUY", 0.0, 100),
        lambda: cost.apply("BUY", 10.0, 0),
    ):
        message = ""
        try:
            bad()
        except BacktestError as error:
            message = str(error)
        assert message, "非法成本入参必须抛 BacktestError"
    print("  -> 非法入参（负费率 / 未知方向 / 价格非正 / 数量非正）全部拒绝")

    print("  交易成本模型核对通过")

# ==============================================================================
# 阶段二：组合与 T+1
# ==============================================================================
def test_portfolio():

    portfolio = Portfolio(2000.0)
    buy = Trade(A, pd.Timestamp(D1), "BUY", 100, 10.0, commission=5.0)
    portfolio.apply_buy(buy)
    _close(portfolio.cash, 995.0, note="买入后现金")
    pos = portfolio.position(A)
    assert pos.qty == 100, pos.qty
    assert pos.available == 0, "买入当日 T+1，可卖数量必须为 0"
    _close(pos.avg_cost, 10.05, note="平均成本含买入费用")
    print("  -> 买入：现金 -1005（含 5 元佣金），avg_cost=10.05，可卖=0（T+1）")

    sell = Trade(A, pd.Timestamp(D1), "SELL", 50, 11.0, commission=5.0, stamp_duty=0.55)
    try:
        portfolio.apply_sell(sell)
    except BacktestError as error:
        message = str(error)
    assert "T+1" in message, "未解锁前卖出必须被拒绝：%s" % message
    print("  -> 未解锁卖出拒绝: %s" % message)

    portfolio.unlock()
    assert pos.available == 100, pos.available
    realized = portfolio.apply_sell(sell)
    _close(realized, 41.95, note="已实现盈亏 = (11-10.05)×50 - 5.55")
    _close(portfolio.cash, 1539.45, note="卖出后现金")
    assert portfolio.position(A).qty == 50
    print("  -> 解锁后卖出成功：已实现盈亏 41.95，现金 1539.45")

    income = portfolio.credit_dividend(A, 0.1)
    _close(income, 5.0, note="现金分红按持有数量入账")
    _close(portfolio.cash, 1544.45, note="分红后现金")
    print("  -> 现金分红：50 股 × 0.1 = 5.00 直接入现金")

    _close(portfolio.market_value({A: 12.0}), 600.0, note="持仓市值")
    _close(portfolio.equity({A: 12.0}), 2144.45, note="权益 = 现金 + 市值")
    try:
        portfolio.market_value({})
    except BacktestError as error:
        message = str(error)
    assert "没有价格" in message, "缺价持仓不得静默按 0 计值"
    print("  -> 缺价拒绝: %s" % message)

    try:
        Portfolio(-1.0)
    except BacktestError as error:
        message = str(error)
    assert "负数" in message, "初始现金为负必须拒绝"
    print("  组合与 T+1 核对通过")

# ==============================================================================
# 阶段三：指标
# ==============================================================================
def test_metrics():

    # 手算基准：100 -> 110 -> 104.5，收益率 [0.10, -0.05]
    equity = pd.Series([100.0, 110.0, 104.5])
    values = compute_metrics(equity)
    expected_vol = math.sqrt(((0.075 ** 2) + (0.075 ** 2)) / 1) * math.sqrt(252)
    _close(values["volatility"], expected_vol, note="年化波动率 = 样本标准差×√252")
    _close(values["annual_return"], 0.025 * 252, note="算术年化 = 日均×252")
    _close(values["sharpe"], 0.025 * 252 / expected_vol, note="夏普")
    expected_down = 0.05 * math.sqrt(252)
    _close(values["sortino"], 0.025 * 252 / expected_down, note="索提诺")
    _close(values["max_drawdown"], 104.5 / 110 - 1, note="最大回撤")
    _close(values["cagr"], (104.5 / 100.0) ** (252 / 2) - 1, note="CAGR")
    _close(
        values["calmar"],
        values["cagr"] / abs(values["max_drawdown"]),
        note="卡玛 = CAGR / |回撤|",
    )
    print(
        "  -> 手算核对：波动率 %.4f  年化 %.4f  夏普 %.4f  索提诺 %.4f  回撤 %.6f"
        % (
            values["volatility"],
            values["annual_return"],
            values["sharpe"],
            values["sortino"],
            values["max_drawdown"],
        )
    )

    trades = pd.DataFrame(
        [
            {"side": "SELL", "realized_pnl": 100.0, "amount": 1000.0},
            {"side": "SELL", "realized_pnl": -50.0, "amount": 500.0},
            {"side": "SELL", "realized_pnl": 200.0, "amount": 0.0},
            {"side": "SELL", "realized_pnl": -25.0, "amount": 0.0},
            {"side": "BUY", "realized_pnl": None, "amount": 0.0},
        ]
    )
    values = compute_metrics(equity, trades=trades)
    _close(values["win_rate"], 0.5, note="胜率 = 2/4（买入不计入）")
    _close(values["profit_factor"], 4.0, note="盈亏比 = 300/75")
    _close(values["turnover"], 1500.0 / 104.8333333, note="换手 = 成交额/均值", tolerance=1e-4)
    print("  -> 胜率 0.50（只看平仓）  盈亏比 4.00  换手 %.4f" % values["turnover"])

    # 分母为 0：一律 NaN，绝不用 inf 或 0 冒充
    rising = pd.Series([100.0, 110.0, 120.0, 130.0])
    values = compute_metrics(rising)
    _close(values["max_drawdown"], 0.0, note="单调上涨没有回撤")
    assert math.isnan(values["calmar"]), "无回撤时卡玛无定义，必须 NaN"
    assert math.isnan(values["sortino"]), "无下行时索提诺无定义，必须 NaN"
    empty = compute_metrics(pd.Series(dtype="float64"))
    for key in ("cagr", "volatility", "sharpe", "max_drawdown", "win_rate", "turnover"):
        assert math.isnan(empty[key]), "%s 空输入应为 NaN" % key
    # 权益有值但一笔没交易：换手就是 0（这是有意义的 0，不是缺数据）
    idle = compute_metrics(pd.Series([100.0, 100.0, 100.0]))
    assert idle["turnover"] == 0.0, idle["turnover"]
    assert math.isnan(values["excess_return"]), "没给基准时超额收益为 NaN"
    print("  -> 分母为 0 / 空输入：全部如实返回 NaN（换手 0）")

    try:
        compute_metrics(pd.DataFrame({"x": [1.0, 2.0]}))
    except BacktestError as error:
        message = str(error)
    assert "DataFrame" in message, "指标入参必须是一维序列"
    print("  指标核对通过")

# ==============================================================================
# 阶段四：幸存者安全
# ==============================================================================
def test_survivorship():

    securities = pd.DataFrame(
        [
            # 退市股：退市日前一天仍在池，退市当日当天起不在池
            {"ts_code": A, "list_date": "2024-01-01", "delist_date": "2024-01-04"},
            {"ts_code": B, "list_date": "2024-01-01", "delist_date": None},
            # 后上市：早期不在池
            {"ts_code": "300001.SZ", "list_date": "2024-06-01", "delist_date": None},
            # 上市日未知：保守纳入（依赖 delist_date 排除）
            {"ts_code": "000002.SZ", "list_date": None, "delist_date": None},
        ]
    )
    at_d1 = list(universe_as_of(securities, D1)["ts_code"])
    at_d3 = list(universe_as_of(securities, D3)["ts_code"])
    assert A in at_d1, "as-of 时仍上市的退市股必须在池（否则有幸存者偏差）"
    assert A not in at_d3, "退市日之后不得再进池"
    assert B in at_d1 and B in at_d3, "正常标的始终在池"
    assert "300001.SZ" not in at_d1, "as-of 之后才上市的标的不得进池"
    assert "000002.SZ" in at_d1, "上市日未知的标的应保守纳入"
    print("  -> as-of 股票池口径与 persistence 一致：%s" % at_d1)

    try:
        universe_as_of(securities[["ts_code"]], D1)
    except BacktestError as error:
        message = str(error)
    assert "缺少列" in message, "缺 list_date 必须拒绝：%s" % message
    try:
        universe_as_of(securities, "不是日期")
    except BacktestError as error:
        message = str(error)
    assert "无法解析" in message, "非法 as_of 必须拒绝：%s" % message

    # 引擎侧：退市持仓不清零，卖出被明确拒绝
    prices = _price_frame(
        [(A, D1, 10.0), (A, D2, 11.0)]
        + [(B, day, 20.0) for day in (D1, D2, D3, D4)]
    )
    signals = _signal_frame(
        [(A, D1, 0.5), (B, D1, 0.5), (A, D3, 0.0), (B, D3, 0.0)]
    )
    result = _engine(cost=FREE_COST).run(prices, signals)

    assert result.equity["stale_count"].tolist() == [0, 0, 1, 1], (
        "退市后必须计为按最近价计值，实际 %s"
        % result.equity["stale_count"].tolist()
    )
    assert all(
        value > 0 for value in result.equity["total_equity"]
    ), "退市持仓不得被清零：%s" % _equity_list(result)
    assert _equity_list(result) == [10000.0] * 4, _equity_list(result)
    final = result.positions.set_index("ts_code")
    assert int(final.loc[A, "qty"]) == 500, "退市持仓必须留在组合里"
    rejected = list(result.rejected["reason"])
    assert any("无价格" in reason for reason in rejected), rejected
    print("  -> 退市持仓：权益保持 10000，stale_count=1，卖出被拒：%s" % rejected)
    print("  幸存者安全核对通过")

# ==============================================================================
# 阶段五：引擎正常路径（手算核对）
# ==============================================================================
def test_engine():

    # (a) 现金只够买 900 股（1000 股要 1.1 万）-> 部分成交，权益先平后涨
    prices = _price_frame([(A, D1, 10.0), (A, D2, 11.0), (A, D3, 12.0), (A, D4, 13.0)])
    signals = _signal_frame([(A, D1, 1.0)])
    result = _engine(cost=FREE_COST).run(prices, signals)

    assert _equity_list(result) == [10000.0, 10000.0, 10900.0, 11800.0], (
        _equity_list(result)
    )
    trade = result.trades.iloc[0]
    assert int(trade["qty"]) == 900 and float(trade["price"]) == 11.0, dict(trade)
    order = result.orders.iloc[0]
    assert int(order["filled_qty"]) == 900, dict(order)
    assert "部分成交" in order["reason"], order["reason"]
    assert int(result.positions["qty"].iloc[0]) == 900
    assert list(result.equity["stale_count"]) == [0, 0, 0, 0]
    print("  -> 部分成交：1000 股买不起 -> 成交 900 股，权益 10000/10000/10900/11800")

    # 前视偏差防护：信号日当天绝不成交，成交日一律晚于信号日
    assert not (result.trades["trade_date"] == pd.Timestamp(D1)).any(), "信号日当天不得成交"
    filled = result.orders[result.orders["status"] == "filled"]
    assert (filled["fill_date"] > filled["created_date"]).all(), "成交日必须晚于信号日"
    assert len(filled["fill_date"].unique()) == 1
    print("  -> 前视偏差防护：D1 出的信号，成交日只能是 D2（%s）" % D2)

    # (b) 含费用的完整买卖闭环（佣金最低 5 元 + 卖出印花税）
    signals = _signal_frame([(A, D1, 1.0), (A, D3, 0.0)])
    result = _engine().run(prices, signals)
    assert len(result.trades) == 2, len(result.trades)
    _close(result.final_equity, 11784.15, note="期末权益（含全部费用）")
    _close(
        float(result.trades["realized_pnl"].iloc[-1]),
        1784.15,
        note="已实现盈亏 = (13-11.0056)×900 - 10.85",
    )
    assert result.positions.empty, "清仓后不应留持仓"
    _close(
        float(result.equity["cash"].iloc[-1]), 11784.15, note="期末现金 = 权益"
    )
    print(
        "  -> 含费用闭环：期末权益 11784.15（成本合计 %.2f）"
        % float(result.trades["total_cost"].sum())
    )

    # (c) 现金分红：除权日按当日持仓入现金
    prices_flat = _price_frame([(A, D1, 10.0), (A, D2, 10.0), (A, D3, 10.0)])
    dividends = pd.DataFrame(
        [(A, D3, 0.5)], columns=["ts_code", "ex_date", "cash_per_share"]
    )
    result = _engine(cost=FREE_COST).run(
        prices_flat, _signal_frame([(A, D1, 1.0)]), dividends=dividends
    )
    assert _equity_list(result) == [10000.0, 10000.0, 10500.0], _equity_list(result)
    assert list(result.equity["dividend_income"]) == [0.0, 0.0, 500.0]
    print("  -> 现金分红：1000 股 × 0.5 = 500 入现金，权益 10500")

    # (d) 基准与超额收益
    benchmark = _price_frame(
        [(B, D1, 100.0), (B, D2, 101.0), (B, D3, 102.0), (B, D4, 103.0)]
    )
    result = _engine(cost=FREE_COST).run(
        _price_frame(
            [(A, D1, 10.0), (A, D2, 11.0), (A, D3, 12.0), (A, D4, 13.0)]
        ),
        _signal_frame([(A, D1, 1.0)]),
        benchmark=benchmark,
    )
    values = result.metrics()
    expected = cagr(result.equity["total_equity"]) - cagr(result.benchmark_equity)
    _close(values["excess_return"], expected, note="超额收益 = 策略CAGR - 基准CAGR")
    assert values["excess_return"] > 0, values["excess_return"]
    assert not math.isnan(values["cagr"]), "CAGR 必须可算"
    print(
        "  -> 超额收益：策略 CAGR %.4f 对基准，超额 %.4f"
        % (values["cagr"], values["excess_return"])
    )
    print("  引擎正常路径核对通过")

# ==============================================================================
# 阶段六：拒绝原因（涨跌停 / 停牌 / 现金不足）
# ==============================================================================
def test_rejection():

    # 涨停封板：收盘价买不进
    frame = pd.DataFrame(
        [(A, D1, 10.0, False), (A, D2, 11.0, True), (A, D3, 11.0, False), (A, D4, 12.0, False)],
        columns=["ts_code", "trade_date", "close", "limit_up"],
    )
    result = _engine(cost=FREE_COST).run(frame, _signal_frame([(A, D1, 1.0)]))
    assert result.trades.empty, "涨停日不得有成交"
    reason = result.rejected["reason"].iloc[0]
    assert "涨停" in reason, reason
    _close(result.final_equity, 10000.0, note="没买进去，权益不变")
    print("  -> 涨停拒绝: %s" % reason)

    # 停牌：有行情也不能成交
    frame = pd.DataFrame(
        [(A, D1, 10.0, False), (A, D2, 11.0, True), (A, D3, 11.0, False)],
        columns=["ts_code", "trade_date", "close", "suspended"],
    )
    result = _engine(cost=FREE_COST).run(frame, _signal_frame([(A, D1, 1.0)]))
    assert result.trades.empty, "停牌日不得有成交"
    reason = result.rejected["reason"].iloc[0]
    assert "停牌" in reason, reason
    print("  -> 停牌拒绝: %s" % reason)

    # 跌停封板：收盘价卖不出，持仓原样保留
    frame = pd.DataFrame(
        [
            (A, D1, 10.0, False, False),
            (A, D2, 11.0, False, False),
            (A, D3, 11.0, False, False),
            (A, D4, 12.0, False, True),
        ],
        columns=["ts_code", "trade_date", "close", "limit_up", "limit_down"],
    )
    signals = _signal_frame([(A, D1, 1.0), (A, D3, 0.0)])
    result = _engine(cost=FREE_COST).run(frame, signals)
    rejected = result.rejected
    assert len(rejected) == 1, rejected
    reason = rejected["reason"].iloc[0]
    assert "跌停" in reason, reason
    assert int(result.positions["qty"].iloc[0]) == 900, "卖不出去持仓必须保留"
    print("  -> 跌停拒绝: %s（持仓 900 股原样保留）" % reason)

    # 现金不足一手：换仓时卖单被跌停挡住，买单要到撮合时（卖单之后）才算现金
    frame = pd.DataFrame(
        [
            (A, D1, 10.0, False),
            (A, D2, 10.0, False),
            (A, D3, 10.0, False),
            (A, D4, 10.0, False),
            (B, D1, 100.0, False),
            (B, D2, 100.0, False),
            (B, D3, 100.0, False),
            (B, D4, 100.0, True),
        ],
        columns=["ts_code", "trade_date", "close", "limit_down"],
    )
    signals = _signal_frame([(B, D1, 1.0), (A, D3, 1.0), (B, D3, 0.0)])
    result = _engine(cost=FREE_COST).run(frame, signals)
    assert len(result.trades) == 1, result.trades
    reasons = list(result.rejected["reason"])
    assert any("跌停" in reason for reason in reasons), reasons
    assert any("现金不足一手" in reason for reason in reasons), reasons
    assert int(result.positions.set_index("ts_code").loc[B, "qty"]) == 100
    _close(result.final_equity, 10000.0, note="卖不掉也买不进，权益不变")
    print("  -> 卖单跌停 + 买单现金不足，两条原因都留痕: %s" % reasons)

    # 由 pre_close 推导涨跌停（未给显式列时）
    frame = pd.DataFrame(
        [(A, D1, 10.0, 10.0), (A, D2, 11.0, 10.0)],
        columns=["ts_code", "trade_date", "close", "pre_close"],
    )
    result = _engine(cost=FREE_COST).run(frame, _signal_frame([(A, D1, 1.0)]))
    reason = result.rejected["reason"].iloc[0]
    assert "涨停" in reason, reason
    print("  -> pre_close × 1.10 推导涨停: %s" % reason)
    print("  拒绝原因核对通过")

# ==============================================================================
# 阶段七：非法输入
# ==============================================================================
def test_invalid_input():

    engine = _engine(cost=FREE_COST)
    empty_signals = _signal_frame([])

    cases = [
        (
            "重复行情行",
            lambda: engine.run(
                _price_frame([(A, D1, 10.0), (A, D1, 11.0)]), empty_signals
            ),
            "重复行",
        ),
        (
            "收盘价非正",
            lambda: engine.run(
                _price_frame([(A, D1, 0.0)]), empty_signals
            ),
            "必须为正数",
        ),
        (
            "行情为空",
            lambda: engine.run(_price_frame([]), empty_signals),
            "没有任何行情行",
        ),
        (
            "缺 close 列",
            lambda: engine.run(
                pd.DataFrame([(A, D1)], columns=["ts_code", "trade_date"]),
                empty_signals,
            ),
            "缺少列",
        ),
        (
            "信号日不是交易日",
            lambda: engine.run(
                _price_frame([(A, D1, 10.0)]),
                _signal_frame([(A, "2024-01-06", 1.0)]),
            ),
            "不在行情交易日内",
        ),
        (
            "权重合计超过 1",
            lambda: engine.run(
                _price_frame([(A, D1, 10.0), (B, D1, 10.0)]),
                _signal_frame([(A, D1, 0.6), (B, D1, 0.6)]),
            ),
            "合计超过 1",
        ),
        (
            "负权重（做空）",
            lambda: engine.run(
                _price_frame([(A, D1, 10.0)]), _signal_frame([(A, D1, -0.5)])
            ),
            "做空",
        ),
        (
            "权重大于 1",
            lambda: engine.run(
                _price_frame([(A, D1, 10.0)]), _signal_frame([(A, D1, 1.5)])
            ),
            "大于 1",
        ),
        (
            "重复信号行",
            lambda: engine.run(
                _price_frame([(A, D1, 10.0)]),
                _signal_frame([(A, D1, 0.5), (A, D1, 0.5)]),
            ),
            "重复行",
        ),
        (
            "除权日不是交易日",
            lambda: engine.run(
                _price_frame([(A, D1, 10.0)]),
                empty_signals,
                dividends=pd.DataFrame(
                    [(A, "2024-01-06", 0.5)],
                    columns=["ts_code", "ex_date", "cash_per_share"],
                ),
            ),
            "除权除息日不在行情交易日内",
        ),
        (
            "基准日期重复",
            lambda: engine.run(
                _price_frame([(A, D1, 10.0)]),
                empty_signals,
                benchmark=pd.DataFrame(
                    [(D1, 100.0), (D1, 101.0)],
                    columns=["trade_date", "close"],
                ),
            ),
            "重复行",
        ),
        (
            "非法日期字符串",
            lambda: engine.run(
                pd.DataFrame([(A, "2024-02-30", 10.0)],
                             columns=["ts_code", "trade_date", "close"]),
                empty_signals,
            ),
            "无法解析",
        ),
        (
            "初始现金非正",
            lambda: _engine(cash=0.0),
            "初始现金",
        ),
        (
            "每手股数非正",
            lambda: BacktestEngine(initial_cash=10000.0, lot_size=0),
            "每手股数",
        ),
        (
            "涨跌停幅度越界",
            lambda: BacktestEngine(initial_cash=10000.0, limit_rate=1.5),
            "涨跌停幅度",
        ),
        (
            "未知买卖方向",
            lambda: Order(A, "HOLD", 100, D1),
            "未知买卖方向",
        ),
        (
            "委托数量非正",
            lambda: Order(A, "BUY", 0, D1),
            "委托数量",
        ),
    ]
    for name, action, keyword in cases:
        message = ""
        try:
            action()
        except BacktestError as error:
            message = str(error)
        assert keyword in message, "%s 应报「%s」，实际：%s" % (name, keyword, message)
        print("  -> %-16s 拒绝：%s" % (name, message[:56]))

    print("  非法输入核对通过")


if __name__ == "__main__":
    # 保住旧的直接执行入口：委托给 pytest，退出码语义一致
    raise SystemExit(pytest.main([__file__, "-v"]))
