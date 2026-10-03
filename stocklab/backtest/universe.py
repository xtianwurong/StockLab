#!/usr/bin/env python3
"""
==============================================================================
StockLab - 幸存者安全股票池 (stocklab.backtest.universe)
==============================================================================

【模块职责】
   V2 需求 Phase 3 第 13 项 Survivorship-safe Universe：
     - universe_as_of  按历史时点推导「当时真实存在」的股票池

【设计原则】
   - 口径与 persistence 的 SecurityRepository.universe(as_of) **逐字一致**：
     上市日 <= as_of 且（未定退市日 或 退市日 > as_of）；退市股票只要在 as_of
     时点仍上市就必须进样本，否则历史研究产生幸存者偏差；
   - list_date 未知（NaN）时视为「无法证明其尚未上市」，保守纳入样本，
     并依赖 delist_date 排除，避免把老股误踢出历史样本；
   - 本函数吃**内存里的帧**，不碰数据库：回测层保持纯计算（不联网、不查库），
     取数由 research / 调用方负责；
   - 幸存者安全还有一半在引擎侧：**持仓里已退市的标的不会被静默清零**，
     它继续按最近价格计值、卖出会被明确拒绝并写明原因。
"""

import pandas as pd

from stocklab.backtest.order import BacktestError

__all__ = ["universe_as_of"]

# 必需列：ts_code + list_date；delist_date 缺失视为「尚未定退市日」
REQUIRED_COLUMNS = ("ts_code", "list_date")


def universe_as_of(securities, as_of_date):
    """
    推导某个历史时点真实存在的证券集合（as-of 股票池）

    Args:
        securities (pd.DataFrame): 证券生命周期帧，至少含 ts_code / list_date，
            可选 delist_date（缺失列 = 全部未定退市日）
        as_of_date: 历史时点（str / datetime.date / pandas.Timestamp）

    Returns:
        pd.DataFrame: 该时点在市的证券（按 ts_code 排序，新副本，不改入参）
    """
    if securities is None or not isinstance(securities, pd.DataFrame):
        raise BacktestError("securities 必须是 DataFrame，收到 %r" % (securities,))
    missing = [c for c in REQUIRED_COLUMNS if c not in securities.columns]
    if missing:
        raise BacktestError("股票池帧缺少列 %s，实际列 %s" % (missing, list(securities.columns)))

    frame = securities.copy()
    list_date = pd.to_datetime(frame["list_date"], errors="coerce")
    as_of = pd.to_datetime(as_of_date, errors="coerce")
    if pd.isna(as_of):
        raise BacktestError("as_of_date 无法解析为日期，收到 %r" % (as_of_date,))

    alive = list_date.isna() | (list_date <= as_of)
    if "delist_date" in frame.columns:
        delist_date = pd.to_datetime(frame["delist_date"], errors="coerce")
        alive = alive & (delist_date.isna() | (delist_date > as_of))

    result = frame.loc[alive].sort_values("ts_code").reset_index(drop=True)
    return result
