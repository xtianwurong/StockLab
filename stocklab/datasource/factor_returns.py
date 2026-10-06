#!/usr/bin/env python3
"""
==============================================================================
StockLab - 因子收益率数据源 (stocklab.datasource.factor_returns)
==============================================================================

【模块职责】
  提供因子收益率数据，用于 RBSA 多因子暴露分解（P1增强）。

【数据来源】
  目前为模拟/示例因子收益率，用于演示多因子分解框架。
  生产环境建议接入：
  - Barra 风格因子库
  - Wind 因子收益率
  - 自建因子库（如基于阿尔法因子回测得到的因子收益率）
  - 开源因子库（如因子工厂、WorldQuant 等）

【因子分类】
  style:   风格因子（规模、价值、动量、质量、波动率等）
  macro:   宏观因子（利率、汇率、商品、信用利差等）
  industry: 行业因子（申万一级行业收益率）

【接口】
  fetch_factor_returns(since: date) -> Dict[str, DataFrame(trade_date, factor_return)]

【后续扩展建议】
  1. 接入真实因子数据源（Barra、Wind、自建）
  2. 增加因子正交化处理
  3. 增加因子收益率的预测模型（用于前瞻性分解）
  4. 增加因子暴露度的约束（如行业中性、风格中性）
==============================================================================
"""

import logging
from datetime import date, timedelta
from typing import Dict, Optional

import numpy as np
import pandas as pd

_logger = logging.getLogger(__name__)

__all__ = [
    "fetch_factor_returns",
]


def _generate_factor_returns(
    trade_dates: list,
    factor_name: str,
    mean: float = 0.0,
    std: float = 0.01,
    seed: int = 42,
) -> pd.DataFrame:
    """生成单个因子的模拟收益率序列"""
    np.random.seed(seed)
    returns = np.random.normal(mean, std, len(trade_dates))
    return pd.DataFrame({
        "trade_date": trade_dates,
        "factor_return": returns
    })


def fetch_factor_returns(since: Optional[date] = None) -> Dict[str, pd.DataFrame]:
    """
    获取因子收益率数据（当前为模拟数据，用于演示框架）

    Args:
        since: 起始日期，默认 1 年前

    Returns:
        Dict[factor_name, DataFrame(trade_date, factor_return)]
    """
    if since is None:
        since = date.today() - timedelta(days=365)

    # 生成交易日序列（简化：工作日）
    dates = pd.date_range(start=since, end=date.today(), freq="B")
    trade_dates = [d.date() for d in dates]

    np.random.seed(42)  # 固定种子，保证可复现

    factors = {}

    # ============ 风格因子 ============
    # 规模因子：小市值 - 大市值 (SMB)
    factors["size"] = _generate_factor_returns(
        trade_dates, "size", mean=0.0001, std=0.008, seed=1
    )

    # 价值因子：高账面市值比 - 低账面市值比 (HML)
    factors["value"] = _generate_factor_returns(
        trade_dates, "value", mean=0.0002, std=0.009, seed=2
    )

    # 动量因子：过去 12 月赢家 - 过去 12 月输家 (WML)
    factors["momentum"] = _generate_factor_returns(
        trade_dates, "momentum", mean=0.0003, std=0.012, seed=3
    )

    # 质量因子：高盈利/低杠杆 - 低盈利/高杠杆 (QMJ)
    factors["quality"] = _generate_factor_returns(
        trade_dates, "quality", mean=0.00015, std=0.007, seed=4
    )

    # 低波动因子：低波动 - 高波动
    factors["low_vol"] = _generate_factor_returns(
        trade_dates, "low_vol", mean=0.0001, std=0.006, seed=5
    )

    # ============ 宏观因子 ============
    # 利率因子：10年期国债收益率变化
    factors["interest_rate"] = _generate_factor_returns(
        trade_dates, "interest_rate", mean=-0.00005, std=0.005, seed=10
    )

    # 汇率因子：美元指数变化
    factors["fx"] = _generate_factor_returns(
        trade_dates, "fx", mean=0.00002, std=0.004, seed=11
    )

    # 商品因子：CRB指数收益率
    factors["commodity"] = _generate_factor_returns(
        trade_dates, "commodity", mean=0.0002, std=0.015, seed=12
    )

    # 信用利差因子
    factors["credit_spread"] = _generate_factor_returns(
        trade_dates, "credit_spread", mean=-0.00003, std=0.006, seed=13
    )

    # ============ 行业因子（已由行业指数覆盖，这里可选） ============
    # 如需行业因子，可在行业指数层面处理

    return factors
