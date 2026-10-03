#!/usr/bin/env python3
"""
==============================================================================
StockLab - 因子登记表 (stocklab.factor.registry)
==============================================================================

【模块职责】
   因子的全局登记与检索：登记（register）、按名取（get）、按分类列举（list_factors）、
   批量计算（compute）。

【设计原则】
   - 登记是**显式调用**（不使用装饰器），由各分类包在 import 时执行一次；
   - 同名因子重复登记直接拒绝，避免两条口径悄悄覆盖同一名字；
   - registry 自身不 import 任何分类包（否则循环依赖），
     由 stocklab/factor/__init__.py 统一触发登记；
   - 因子名即对外契约：筛选条件、研究快照里的 factor 字段都用它。
"""

import pandas as pd

from stocklab.factor.base import FactorDataError

__all__ = [
    "register",
    "get",
    "list_factors",
    "compute",
]

# 因子名 -> Factor（模块级私有，禁止外部直接改）
_REGISTRY = {}


def register(factor):
    """
    登记一个因子

    Args:
        factor (Factor): 因子定义

    Returns:
        Factor: 原对象（便于模块内联调用）

    Raises:
        FactorDataError: 同名因子重复登记
    """
    if factor.name in _REGISTRY:
        raise FactorDataError("因子 %s 已登记，禁止重复定义" % factor.name)
    _REGISTRY[factor.name] = factor
    return factor


def get(name):
    """
    按名字取因子

    Args:
        name (str): 因子名，如 "pe_ttm"

    Returns:
        Factor: 因子定义

    Raises:
        FactorDataError: 未登记的名字（并列出可用因子）
    """
    if name not in _REGISTRY:
        raise FactorDataError(
            "未登记的因子 %s，可用因子: %s" % (name, list_factors())
        )
    return _REGISTRY[name]


def list_factors(category=None):
    """
    列举已登记因子

    Args:
        category (str, optional): 按分类过滤（value/quality/growth/momentum/dividend/risk）

    Returns:
        list: 升序排列的因子名列表
    """
    names = sorted(_REGISTRY)
    if category is None:
        return names
    return [n for n in names if category in _REGISTRY[n].categories]


def compute(frame, names):
    """
    在同一输入帧上批量计算若干因子

    Args:
        frame (pd.DataFrame): 因子输入帧
        names (list): 因子名列表

    Returns:
        pd.DataFrame: 列为 names 的因子值表（索引与帧一致）

    Raises:
        FactorDataError: 因子未登记，或其输入列不在帧中
    """
    values = {}
    for name in names:
        values[name] = get(name).values(frame)
    if not values:
        return pd.DataFrame(index=frame.index)
    return pd.DataFrame(values, index=frame.index)
