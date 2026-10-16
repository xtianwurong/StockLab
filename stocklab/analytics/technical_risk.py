#!/usr/bin/env python3
"""
==============================================================================
StockLab - 技术面与风险指标 (stocklab.analytics.technical_risk)
==============================================================================

【模块职责】
   在**日线收盘价序列**上做纯计算，产出两类结果：

     1. 技术面：均线（MA）、相对强弱（RSI）、指数平滑异同（MACD）、动量收益
     2. 风险面：年化收益/波动、夏普/索提诺/卡玛比率、最大回撤（含峰谷与修复）、
        相对基准的 Beta / 年化 Alpha

   本模块是**纯计算叶子**：不吃数据库、不发请求、不 import 其它层，
   输入输出只有 pandas / numpy 对象，便于在测试里用合成序列直接验证。

【口径与约束】
   - 全部指标**只用到当日及之前的数据**（滚动窗口 / 递推），无未来函数。
   - 年化折算统一用 252 个交易日。
   - 输入长度不足、分母为 0、序列常数等情况一律返回 None 或 NaN，
     绝不返回 inf，也绝不用 0 冒充「无波动」——那会把「没数据」变成结论。
   - 收益率一律为**小数**（0.05 表示 5%），不在这里做百分号换算。
"""

import logging

import numpy as np
import pandas as pd

_logger = logging.getLogger(__name__)

__all__ = [
    "TRADING_DAYS_PER_YEAR",
    "to_returns",
    "annualized_volatility",
    "max_drawdown",
    "risk_metrics",
    "moving_averages",
    "relative_strength_index",
    "macd",
    "momentum",
    "technical_snapshot",
]

TRADING_DAYS_PER_YEAR = 252


def _close_series(closes):
    """
    把各种入参归一为「以日期为索引的收盘价 Series」

    Args:
        closes: pd.Series 或带 close 列的 pd.DataFrame

    Returns:
        pd.Series: 数值型收盘价，已按索引排序、去除缺失值
    """
    if closes is None:
        return pd.Series(dtype="float64")
    if isinstance(closes, pd.DataFrame):
        if "close" not in closes.columns:
            _logger.warning("收盘价表缺少 close 列，按空序列处理")
            return pd.Series(dtype="float64")
        series = closes["close"]
        if "trade_date" in closes.columns:
            series = series.copy()
            series.index = pd.to_datetime(closes["trade_date"]).values
    else:
        series = closes
    series = pd.to_numeric(series, errors="coerce").dropna()
    return series.sort_index()


def to_returns(closes):
    """
    计算简单日收益率：r_t = close_t / close_{t-1} - 1

    Args:
        closes (pd.Series): 收盘价序列

    Returns:
        pd.Series: 日收益率（首日为 NaN，已剔除）；不足两点时返回空序列
    """
    series = _close_series(closes)
    if len(series) < 2:
        return pd.Series(dtype="float64")
    return series.pct_change().dropna()


def annualized_volatility(returns):
    """
    年化波动率 = 日收益率标准差 × sqrt(252)

    Args:
        returns (pd.Series): 日收益率序列

    Returns:
        float 或 None: 样本少于 2 个或标准差不可用时返回 None
    """
    values = pd.to_numeric(returns, errors="coerce").dropna()
    if len(values) < 2:
        return None
    deviation = float(values.std(ddof=1))
    if not np.isfinite(deviation):
        return None
    return deviation * np.sqrt(TRADING_DAYS_PER_YEAR)


def max_drawdown(closes):
    """
    最大回撤及其峰、谷、修复信息

    【定义】回撤 = 当前净值 / 历史最高净值 - 1，取最小值。修复日为回撤谷底
    之后净值首次回到前高的交易日，若尚未修复则为 None。

    Args:
        closes (pd.Series): 收盘价序列

    Returns:
        dict: 含以下键（样本不足时 value 为 None）
            max_drawdown (float): 负数，如 -0.35 表示最深回撤 35%
            peak_date / trough_date (str): 前高日与谷底日
            recovery_date (str 或 None): 修复日
            drawdown_days (int 或 None): 峰到谷的交易日数
            recovery_days (int 或 None): 谷到修复的交易日数
    """
    series = _close_series(closes)
    empty = {
        "max_drawdown": None,
        "peak_date": None,
        "trough_date": None,
        "recovery_date": None,
        "drawdown_days": None,
        "recovery_days": None,
    }
    if len(series) < 2:
        return empty

    running_peak = series.cummax()
    drawdown = series / running_peak - 1.0
    trough_pos = int(np.argmin(drawdown.values))
    trough_value = float(drawdown.iloc[trough_pos])
    if not np.isfinite(trough_value) or trough_value >= 0:
        # 全程单调不回落：最大回撤为 0，没有峰谷可报
        empty["max_drawdown"] = 0.0
        return empty

    # 谷底之前最后一次创出运行新高的位置即为前高
    peak_pos = int(np.argmax(running_peak.values[: trough_pos + 1]))

    recovery_pos = None
    peak_value = float(series.iloc[peak_pos])
    for pos in range(trough_pos + 1, len(series)):
        if float(series.iloc[pos]) >= peak_value:
            recovery_pos = pos
            break

    result = {
        "max_drawdown": trough_value,
        "peak_date": _date_text(series.index[peak_pos]),
        "trough_date": _date_text(series.index[trough_pos]),
        "recovery_date": _date_text(series.index[recovery_pos]) if recovery_pos is not None else None,
        "drawdown_days": trough_pos - peak_pos,
        "recovery_days": (recovery_pos - trough_pos) if recovery_pos is not None else None,
    }
    return result


def risk_metrics(closes, benchmark_closes=None, risk_free_rate=0.02):
    """
    综合风险收益指标

    Args:
        closes (pd.Series): 标的收盘价序列
        benchmark_closes (pd.Series, optional): 基准收盘价；给出时才计算 Beta/Alpha
        risk_free_rate (float): 年化无风险利率（小数），默认 0.02

    Returns:
        dict: 含年化收益/波动、夏普、索提诺、卡玛、最大回撤，以及可算时的
              beta / alpha / r_squared；不可算项一律为 None
    """
    series = _close_series(closes)
    result = {
        "annual_return": None,
        "annual_volatility": None,
        "sharpe": None,
        "sortino": None,
        "calmar": None,
        "max_drawdown": None,
        "beta": None,
        "alpha": None,
        "r_squared": None,
        "sample_days": int(len(series)),
    }
    if len(series) < 2:
        return result

    returns = to_returns(series)
    if returns.empty:
        return result

    total_return = float(series.iloc[-1] / series.iloc[0] - 1.0)
    years = len(returns) / TRADING_DAYS_PER_YEAR
    annual_return = (1.0 + total_return) ** (1.0 / years) - 1.0 if years > 0 else None
    annual_vol = annualized_volatility(returns)

    result["annual_return"] = _finite_or_none(annual_return)
    result["annual_volatility"] = _finite_or_none(annual_vol)

    if annual_vol and annual_vol > 0:
        result["sharpe"] = _finite_or_none((annual_return - risk_free_rate) / annual_vol)

        downside = returns[returns < 0.0]
        downside_vol = annualized_volatility(downside)
        if downside_vol and downside_vol > 0:
            result["sortino"] = _finite_or_none((annual_return - risk_free_rate) / downside_vol)

    drawdown = max_drawdown(series)["max_drawdown"]
    result["max_drawdown"] = drawdown
    if drawdown and drawdown < 0 and annual_return is not None:
        result["calmar"] = _finite_or_none(annual_return / abs(drawdown))

    if benchmark_closes is not None:
        result.update(_beta_alpha(returns, benchmark_closes))
    return result


def moving_averages(closes, windows=(5, 10, 20, 60)):
    """
    多条简单移动均线

    Args:
        closes (pd.Series): 收盘价序列
        windows (tuple): 均线窗口天数

    Returns:
        pd.DataFrame: 索引同收盘价，列名形如 ma5 / ma20，窗口不足处为 NaN
    """
    series = _close_series(closes)
    frame = pd.DataFrame(index=series.index)
    for window in windows:
        frame["ma%d" % window] = series.rolling(window, min_periods=window).mean()
    return frame


def relative_strength_index(closes, window=14):
    """
    相对强弱指标 RSI（Wilder 平滑）

    Args:
        closes (pd.Series): 收盘价序列
        window (int): 回看窗口，默认 14

    Returns:
        pd.Series: 取值 0~100；窗口不足处为 NaN
    """
    series = _close_series(closes)
    if len(series) <= window:
        return pd.Series(index=series.index, dtype="float64")
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder 平滑等价于 alpha = 1/window 的指数加权
    avg_gain = gain.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    # RS = 平均涨幅 / 平均跌幅；跌幅为 0 时 RS 无定义，需单独兜底
    rs = avg_gain / avg_loss.where(avg_loss > 0)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = rsi.where(avg_loss > 0, 100.0)  # 只涨不跌：RSI 顶格 100
    rsi = rsi.where((avg_loss > 0) | (avg_gain > 0), 50.0)  # 完全不动：中性 50
    return rsi.where((avg_gain + avg_loss).notna())  # 窗口不足处保持 NaN


def macd(closes, fast=12, slow=26, signal=9):
    """
    MACD 指标（DIF / DEA / 柱）

    Args:
        closes (pd.Series): 收盘价序列
        fast (int): 快线 EMA 窗口
        slow (int): 慢线 EMA 窗口
        signal (int): 信号线 EMA 窗口

    Returns:
        pd.DataFrame: 列 dif / dea / hist，hist = (dif - dea) × 2
    """
    series = _close_series(closes)
    frame = pd.DataFrame(index=series.index)
    if len(series) < slow:
        frame["dif"] = np.nan
        frame["dea"] = np.nan
        frame["hist"] = np.nan
        return frame
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    frame["dif"] = dif
    frame["dea"] = dea
    frame["hist"] = (dif - dea) * 2.0
    return frame


def momentum(closes, windows=(5, 20, 60, 120, 250)):
    """
    区间动量收益：ret_<window> = close_last / close_{last-window} - 1

    Args:
        closes (pd.Series): 收盘价序列
        windows (tuple): 回看天数

    Returns:
        dict: 键形如 ret_20d；样本不足以覆盖该窗口时值为 None
    """
    series = _close_series(closes)
    result = {}
    for window in windows:
        key = "ret_%dd" % window
        if len(series) <= window:
            result[key] = None
            continue
        base = float(series.iloc[-1 - window])
        if base == 0:
            result[key] = None
            continue
        result[key] = _finite_or_none(float(series.iloc[-1]) / base - 1.0)
    return result


def technical_snapshot(closes):
    """
    把技术面各指标的最新值汇总成一个扁平字典（供页面直接展示）

    Args:
        closes (pd.Series): 收盘价序列

    Returns:
        dict: 含 as_of、close、ma5/ma20/ma60、rsi14、macd_dif/dea/hist、
              以及各区间动量；不可算项为 None
    """
    series = _close_series(closes)
    if series.empty:
        return {"as_of": None, "close": None}

    averages = moving_averages(series).iloc[-1]
    rsi = relative_strength_index(series)
    macd_frame = macd(series).iloc[-1]

    snapshot = {
        "as_of": _date_text(series.index[-1]),
        "close": _finite_or_none(float(series.iloc[-1])),
        "ma5": _latest(averages.get("ma5")),
        "ma20": _latest(averages.get("ma20")),
        "ma60": _latest(averages.get("ma60")),
        "rsi14": _latest(rsi.iloc[-1] if len(rsi) else None),
        "macd_dif": _latest(macd_frame.get("dif")),
        "macd_dea": _latest(macd_frame.get("dea")),
        "macd_hist": _latest(macd_frame.get("hist")),
    }
    snapshot.update(momentum(series))
    return snapshot


def _beta_alpha(returns, benchmark_closes):
    """
    CAPM 口径的 Beta / 年化 Alpha / 拟合优度

    Args:
        returns (pd.Series): 标的日收益率（以日期为索引）
        benchmark_closes (pd.Series): 基准收盘价序列

    Returns:
        dict: beta / alpha / r_squared；对齐样本不足或基准无波动时为 None
    """
    benchmark = _close_series(benchmark_closes)
    result = {"beta": None, "alpha": None, "r_squared": None}
    if len(benchmark) < 2:
        return result
    bench_returns = to_returns(benchmark)
    aligned = pd.concat([returns, bench_returns], axis=1, join="inner").dropna()
    if len(aligned) < 3:
        return result
    fund_values = aligned.iloc[:, 0].values
    bench_values = aligned.iloc[:, 1].values
    variance = float(np.var(bench_values, ddof=1))
    if not np.isfinite(variance) or variance <= 0:
        # 基准零波动：Beta 无定义，不用 0 或 1 冒充
        return result
    covariance = float(np.cov(fund_values, bench_values, ddof=1)[0, 1])
    beta = covariance / variance
    alpha = (float(np.mean(fund_values)) - beta * float(np.mean(bench_values))) * TRADING_DAYS_PER_YEAR
    correlation = float(np.corrcoef(fund_values, bench_values)[0, 1])
    result["beta"] = _finite_or_none(beta)
    result["alpha"] = _finite_or_none(alpha)
    result["r_squared"] = _finite_or_none(correlation ** 2) if np.isfinite(correlation) else None
    return result


def _finite_or_none(value):
    """把非有限值（None / NaN / inf）统一成 None，保证下游 JSON 合法"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _latest(value):
    """取 Series 末值；缺失或非有限时返回 None"""
    if value is None:
        return None
    return _finite_or_none(value)


def _date_text(value):
    """把索引项格式化为 YYYY-MM-DD；不可解析时原样转字符串"""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    text = str(value)
    return text[:10]
