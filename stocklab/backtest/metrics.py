#!/usr/bin/env python3
"""
==============================================================================
StockLab - 回测指标 (stocklab.backtest.metrics)
==============================================================================

【模块职责】
   V2 需求 §9.3 的指标（至少 11 个）：
     CAGR / Annual Return / Volatility / Sharpe / Sortino / Calmar /
     Max Drawdown / Win Rate / Profit Factor / Turnover / Excess Return

【设计原则】
   - 年化口径统一：**一年 252 个交易日**，所有年化都从日频推出，
     不混用「自然日 365」，否则两个指标之间没法互相验算；
   - CAGR 是几何（复合）年化，Annual Return 是算术年化（日均收益 × 252），
     两者不同、都要给，正是一条研究结论里最容易被含糊掉的差别；
   - 胜率 / 盈亏比只认**卖出的已实现盈亏**（平均成本法、已扣全部费用），
     不用浮动盈亏凑数；
   - 分母为 0（无波动 / 无下行 / 无回撤 / 无亏损交易）时比率**无定义 → NaN**，
     绝不用 inf 冒充「表现很好」，也绝不悄悄改成 0；
   - 输入为空一律返回 NaN，不抛异常：指标是结果的一部分，缺数据就如实缺。
"""

import math

import pandas as pd

from stocklab.backtest.order import SELL, BacktestError

__all__ = [
    "TRADING_DAYS_PER_YEAR",
    "daily_returns",
    "cagr",
    "annual_return",
    "volatility",
    "sharpe",
    "sortino",
    "max_drawdown",
    "calmar",
    "win_rate",
    "profit_factor",
    "turnover",
    "excess_return",
    "compute_metrics",
]

TRADING_DAYS_PER_YEAR = 252


def _series(values):
    """统一成 float Series（去索引依赖，只看数值顺序）"""
    if values is None:
        return pd.Series(dtype="float64")
    if isinstance(values, pd.DataFrame):
        raise BacktestError("指标入参应是一维序列（权益曲线），收到 DataFrame")
    return pd.Series(values, dtype="float64").reset_index(drop=True)


def daily_returns(equity):
    """日收益率（首日无基准，剔除）"""
    series = _series(equity)
    if len(series) < 2:
        return pd.Series(dtype="float64")
    return series.pct_change().dropna()


def cagr(equity):
    """几何年化收益率：(期末/期初) ** (252/交易日数) - 1"""
    series = _series(equity)
    if len(series) < 2:
        return float("nan")
    start, end = float(series.iloc[0]), float(series.iloc[-1])
    if start <= 0 or end <= 0:
        return float("nan")
    return (end / start) ** (TRADING_DAYS_PER_YEAR / (len(series) - 1)) - 1


def annual_return(equity):
    """算术年化收益率：日均收益 × 252"""
    values = daily_returns(equity)
    if values.empty:
        return float("nan")
    return float(values.mean() * TRADING_DAYS_PER_YEAR)


def volatility(values):
    """年化波动率：日收益样本标准差 × sqrt(252)"""
    values = _series(values)
    if len(values) < 2:
        return float("nan")
    return float(values.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))


def sharpe(values, risk_free=0.0):
    """
    夏普比率：(算术年化收益 − 无风险利率) / 年化波动率

    risk_free 为**年化**利率（如 0.02 表示 2%）。
    """
    values = _series(values)
    vol = volatility(values)
    if math.isnan(vol) or vol == 0:
        return float("nan")
    excess = float(values.mean()) * TRADING_DAYS_PER_YEAR - risk_free
    return excess / vol


def sortino(values, risk_free=0.0):
    """索提诺比率：只用下行波动（低于目标收益的样本）作分母"""
    values = _series(values)
    if len(values) < 2:
        return float("nan")
    daily_target = risk_free / TRADING_DAYS_PER_YEAR
    downside = values[values < daily_target] - daily_target
    if downside.empty:
        return float("nan")
    down_dev = float(
        math.sqrt((downside ** 2).mean()) * math.sqrt(TRADING_DAYS_PER_YEAR)
    )
    if down_dev == 0:
        return float("nan")
    excess = float(values.mean()) * TRADING_DAYS_PER_YEAR - risk_free
    return excess / down_dev


def max_drawdown(equity):
    """最大回撤：相对历史高点的最深跌幅（返回负数或 0）"""
    series = _series(equity)
    if series.empty:
        return float("nan")
    running_max = series.cummax()
    drawdown = series / running_max - 1.0
    return float(drawdown.min())


def calmar(equity):
    """卡玛比率：CAGR / |最大回撤|"""
    drawdown = max_drawdown(equity)
    if math.isnan(drawdown) or drawdown == 0:
        return float("nan")
    return cagr(equity) / abs(drawdown)


def _closed_pnls(trades):
    """取卖出成交的已实现盈亏（胜率与盈亏比的唯一口径）"""
    if trades is None or len(trades) == 0:
        return pd.Series(dtype="float64")
    frame = trades[trades["side"] == SELL]
    if frame.empty or "realized_pnl" not in frame.columns:
        return pd.Series(dtype="float64")
    return pd.Series(frame["realized_pnl"], dtype="float64").dropna()


def win_rate(trades):
    """胜率：已实现盈利的平仓笔数 / 全部平仓笔数"""
    pnls = _closed_pnls(trades)
    if pnls.empty:
        return float("nan")
    return float((pnls > 0).mean())


def profit_factor(trades):
    """盈亏比：盈利总额 / |亏损总额|（无亏损交易时分母为 0 → 无定义）"""
    pnls = _closed_pnls(trades)
    if pnls.empty:
        return float("nan")
    gains = float(pnls[pnls > 0].sum())
    losses = float(pnls[pnls < 0].sum())
    if losses == 0:
        return float("nan")
    return gains / abs(losses)


def turnover(trades, equity):
    """
    区间换手率：累计成交金额 / 平均权益（未年化）

    权益为空或均值非正 → NaN（无从谈起）；权益有值但一笔没交易 → 0。
    """
    series = _series(equity)
    if series.empty or float(series.mean()) <= 0:
        return float("nan")
    if trades is None or len(trades) == 0:
        return 0.0
    traded = float(pd.Series(trades["amount"], dtype="float64").sum())
    return traded / float(series.mean())


def excess_return(equity, benchmark_equity):
    """超额年化收益：策略 CAGR − 基准 CAGR（几何口径）"""
    return cagr(equity) - cagr(benchmark_equity)


def compute_metrics(equity, trades=None, benchmark=None, risk_free=0.0):
    """
    一次性算出 §9.3 要求的全部指标

    Args:
        equity: 权益曲线（一维序列）
        trades: 成交帧（可空，胜率 / 盈亏比 / 换手由此算）
        benchmark: 对齐后的基准权益曲线（可空 → excess_return 为 NaN）
        risk_free (float): 无风险利率（年化）

    Returns:
        dict: 11 个指标，键名与 §9.3 一致
    """
    values = daily_returns(equity)
    result = {
        "cagr": cagr(equity),
        "annual_return": annual_return(equity),
        "volatility": volatility(values),
        "sharpe": sharpe(values, risk_free),
        "sortino": sortino(values, risk_free),
        "calmar": calmar(equity),
        "max_drawdown": max_drawdown(equity),
        "win_rate": win_rate(trades),
        "profit_factor": profit_factor(trades),
        "turnover": turnover(trades, equity),
        "excess_return": (
            float("nan")
            if benchmark is None
            else excess_return(equity, benchmark)
        ),
    }
    return result
