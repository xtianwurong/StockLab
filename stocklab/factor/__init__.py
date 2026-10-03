#!/usr/bin/env python3
"""
==============================================================================
StockLab - 因子引擎 (stocklab.factor)
==============================================================================

【模块职责】
   V2 需求 §7 的 Factor Engine：因子定义、登记表、预处理。
   本包**纯计算**——只吃 DataFrame 返回 Series，不请求网络、不读数据库、
   不改全局状态（因子输入帧由 stocklab.research.frame 负责拼装）。

【目录与需求对应】
   base.py            Factor / FactorDataError / divide（安全除法）
   registry.py        register / get / list_factors / compute（显式登记，无装饰器）
   value/             §7.1  PE PB PS EV/EBITDA FCF Yield
   quality/           §7.2  ROE ROIC 毛利率 营业利润率 CFO/净利润 Debt/Equity
   growth/            §7.3  营收/利润/EPS/自由现金流增长、ROE 趋势
   momentum/          §7.4  1M 3M 6M 12M
   dividend/          §7.5  股息率 分红增长 分红率 分红稳定性（股息率同时属 value）
   risk/              §7 目录  区间波动率与最大回撤
   preprocessing/     §7.6  winsorize / rank / zscore / 行业中性化 / 市值中性化 / 缺失值

【对外用法】
   import stocklab.factor as factor
   factor.list_factors("value")
   factor.compute(frame, ["pe_ttm", "roe"])
   factor.apply_preprocessing(frame, [{"method": "winsorize", "columns": ["pe_ttm"]}])
"""

from .base import FACTOR_CATEGORIES, Factor, FactorDataError, divide, log_positive
from .registry import compute, get, list_factors, register
from .preprocessing import (
    MISSING_ACTIONS,
    PREPROCESS_METHODS,
    apply_preprocessing,
)

# 各分类包在 import 时完成因子登记（显式 register 调用，不使用装饰器）
from . import dividend, growth, momentum, quality, risk, value  # noqa: F401

# 因子库版本号：研究快照用它记录 factor_version，因子口径变更时必须递增
FACTOR_VERSION = "factor_v1"

__all__ = [
    "Factor",
    "FactorDataError",
    "FACTOR_CATEGORIES",
    "FACTOR_VERSION",
    "divide",
    "log_positive",
    "register",
    "get",
    "list_factors",
    "compute",
    "apply_preprocessing",
    "PREPROCESS_METHODS",
    "MISSING_ACTIONS",
]
