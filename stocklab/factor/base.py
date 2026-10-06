#!/usr/bin/env python3
"""
==============================================================================
StockLab - 因子定义基元 (stocklab.factor.base)
==============================================================================

【模块职责】
   因子引擎的最小抽象：
     - Factor           因子定义（名字 / 分类 / 所需输入列 / 计算函数 / 口径说明）
     - FactorDataError  输入帧不满足因子契约（缺列、返回值类型/长度不对、分类非法）
     - divide           安全除法（分母为 0 或缺失 -> NaN，不产生 inf）

【设计原则】
   - 纯计算：只吃 DataFrame、返回 Series，不取数、不落库、不碰全局状态；
   - 契约先行：因子声明自己需要的输入列，帧里缺列直接抛异常而不是静默出 NaN，
     「数据缺失」与「帧构造漏列」必须是两种不同的故障；
   - 一个因子只对应一个口径，口径差异（如 EBITDA 是否加回折旧）写在 description 里。
"""

import math

import pandas as pd

__all__ = [
    "Factor",
    "FactorDataError",
    "FACTOR_CATEGORIES",
    "divide",
    "log_positive",
    "pass_through",
]


def pass_through(column):
    """
    取输入帧的一列作为因子值（原样透传，不改口径）

    这是最常用的因子构造方式：直接取 PIT 帧中的某一列作为因子值。
    6 大因子分类（quality/momentum/value/growth/dividend/risk）全都复用它，
    因此放在基础层避免重复定义。

    Args:
        column (str): 因子值对应的列名

    Returns:
        callable: (frame) -> frame[column].astype("float64")
    """
    def _compute(frame):
        return frame[column].astype("float64")
    return _compute

# 因子分类（V2 需求 §7 的六个子域）
FACTOR_CATEGORIES = (
    "value",
    "quality",
    "growth",
    "momentum",
    "dividend",
    "risk",
)


class FactorDataError(Exception):
    """因子输入帧不满足因子契约"""


def divide(numerator, denominator):
    """
    安全除法：分母为 0、为负数不特判、或为缺失时返回 NaN

    Args:
        numerator (pd.Series): 分子
        denominator (pd.Series): 分母

    Returns:
        pd.Series: 逐元素除法结果，分母为 0 / 缺失的元素为 NaN
    """
    left = numerator.astype("float64")
    right = denominator.astype("float64")
    valid = (right != 0) & right.notna() & left.notna()
    result = pd.Series(float("nan"), index=left.index, dtype="float64")
    result[valid] = left[valid] / right[valid]
    return result


class Factor:
    """
    因子定义

    【职责】
      1. 声明因子的输入帧契约（columns）
      2. 校验并执行计算（values），保证输出是与帧同长的 Series
      3. 记录口径说明，供筛选结果与文档复用

    【说明】
      categories 允许多个（如 Dividend Yield 既属 value 也属 dividend，
      对应 V2 需求 §7.1 与 §7.5 都列了它）。
    """

    def __init__(self, name, categories, columns, compute, description=""):
        if not name:
            raise FactorDataError("因子名不能为空")
        if not columns:
            raise FactorDataError("因子 %s 必须声明至少一个输入列" % name)
        unknown = [c for c in categories if c not in FACTOR_CATEGORIES]
        if unknown:
            raise FactorDataError(
                "因子 %s 的分类 %s 非法，可选: %s"
                % (name, unknown, list(FACTOR_CATEGORIES))
            )
        if not callable(compute):
            raise FactorDataError("因子 %s 的计算必须是可调用对象" % name)

        self.name = name
        self.categories = tuple(categories)
        self.columns = tuple(columns)
        self.compute = compute
        self.description = description

    def values(self, frame):
        """
        在输入帧上计算因子值

        Args:
            frame (pd.DataFrame): 因子输入帧，必须含 self.columns

        Returns:
            pd.Series: 与帧同长，索引与帧一致，名字为因子名

        Raises:
            FactorDataError: 缺输入列，或计算结果不是等长 Series
        """
        missing = [column for column in self.columns if column not in frame.columns]
        if missing:
            raise FactorDataError(
                "因子 %s 缺少输入列 %s" % (self.name, missing)
            )

        result = self.compute(frame)
        if not isinstance(result, pd.Series):
            raise FactorDataError(
                "因子 %s 返回了 %s，必须是 pd.Series"
                % (self.name, type(result).__name__)
            )
        if len(result) != len(frame) or not result.index.equals(frame.index):
            raise FactorDataError(
                "因子 %s 返回长度 %d，与输入帧 %d 不一致"
                % (self.name, len(result), len(frame))
            )
        result.name = self.name
        return result.astype("float64")

    def describe(self):
        """返回可 JSON 化的因子元信息（供研究快照记录 factor_version 明细）"""
        return {
            "name": self.name,
            "categories": list(self.categories),
            "columns": list(self.columns),
            "description": self.description,
        }


def log_positive(value):
    """
    正数取自然对数；0、负数、缺失一律返回 NaN（中性化等场景的输入变换）

    Args:
        value (float): 原始值

    Returns:
        float: log(value)，不合法输入为 NaN
    """
    if value is None or value != value:
        return float("nan")
    if value <= 0:
        return float("nan")
    return math.log(value)
