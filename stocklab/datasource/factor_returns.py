#!/usr/bin/env python3
"""
==============================================================================
StockLab - 因子收益率数据源 (stocklab.datasource.factor_returns)
==============================================================================

【模块职责】
  为 RBSA 多因子暴露分解提供**真实**因子收益率。

【当前状态：占位，尚未接入真实源】
  本模块**不再生成模拟数据**。历史上它用 np.random 造了一组「风格/宏观因子
  收益率」直接喂给线上 RBSA 分解，产物（暴露度 / alpha / net_alpha）随之经
  /api/fund/allocation 外泄为看似真实的结论。伪数据混进投研链路比缺数据更糟，
  故一律去除：未接入真实源前，本函数返回空 dict。

  调用方（facade.fund_analysis._load_factor_returns）在拿到空 dict 时会退化
  为「仅行业分解」——这是安全且诚实的行为。

【接入真实源时应做什么】
  - Barra 风格因子库 / Wind 因子收益率 / 自建因子库
  - 返回 {factor_name: DataFrame(trade_date, factor_return)}
  - 严禁在此模块用随机数伪造（且不得调用 np.random.seed 污染全局随机态）
==============================================================================
"""

import logging

_logger = logging.getLogger(__name__)

__all__ = [
    "fetch_factor_returns",
]


def fetch_factor_returns(since=None):
    """
    获取因子收益率数据

    【尚未接入真实数据源】
      当前一律返回空 dict，调用方据此跳过「多因子分解」、退化为「仅行业分解」。
      绝不返回随机数或模拟序列冒充真实因子收益率。

    Args:
        since (datetime.date, optional): 起始日期（待接入真实源后生效）

    Returns:
        dict: {factor_name: DataFrame(trade_date, factor_return)}；当前恒为空
    """
    _logger.warning(
        "因子收益率数据源未接入真实数据（since=%s），本次不产出多因子分解，"
        "RBSA 将退化为仅行业分解",
        since,
    )
    return {}
