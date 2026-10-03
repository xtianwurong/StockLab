#!/usr/bin/env python3
"""
==============================================================================
StockLab - 归一化原语 (stocklab.normalization.base)
==============================================================================

【模块职责】
   提供各数据源 Normalizer 共用的列级原语（本层的「地基」，无外部依赖）：
     - normalize_ts_code  纯数字代码 → 标准 ts_code（含交易所后缀推断）
     - pick_column        多候选列名中取第一个存在的列（同一数据源列名常改版）
     - to_numeric_column  安全取数值列（缺失时返回全 NaN，保持列结构完整）
     - to_date_series     日期列解析（无法解析的值置 NaT）
     - clean_text_value   文本字段清洗（None / NaN / 占位符 → 空串）
     - clean_date_value   日期字段清洗（无法解析 → None）

【设计原则】
   - 只依赖 pandas，不 import datasource / persistence / facade；
   - 「缺列」必须由调用方用 require_columns 显式声明为必需，本层不猜；
   - 数据源不提供的列一律保留并写 NULL，绝不省略列。
"""

import pandas as pd

__all__ = [
    "normalize_ts_code",
    "pick_column",
    "to_numeric_column",
    "to_date_series",
    "clean_text_value",
    "clean_date_value",
]


def normalize_ts_code(code):
    """
    将纯数字代码转换为标准 ts_code 格式

    【后缀规则】
       6/5/90 开头 → 上交所；4/8/92 开头 → 北交所；其余 → 深交所。
       注：920/4/8 属北交所，5 开头为沪市 ETF/债券，故归入 .SH。

    Args:
        code (str): 纯数字代码，如 "600519"

    Returns:
        str: 标准格式，如 "600519.SH"
    """
    code = str(code).strip()
    if code.startswith(("6", "5", "90")):
        return code + ".SH"
    if code.startswith(("4", "8", "92")):
        return code + ".BJ"
    return code + ".SZ"


def pick_column(frame, candidate_names):
    """
    从多个候选列名中取第一个存在的列

    【为何需要】
       同一交易所的接口在不同版本间会改列名（如「上市日期」与「A股上市日期」），
       逐个探测比要求数据源永远不改名更现实，缺失时仍由 require_columns 兜底。

    Args:
        frame (pd.DataFrame): 源数据表
        candidate_names (list): 候选列名，按优先级排列

    Returns:
        pd.Series 或 None: 命中的列；全部缺失时返回 None
    """
    for name in candidate_names:
        if name in frame.columns:
            return frame[name]
    return None


def to_numeric_column(frame, column_name, fallback=None):
    """
    安全提取 DataFrame 中的数值列

    Args:
        frame (pd.DataFrame): 源数据表
        column_name (str): 目标列名
        fallback: 列缺失时的替代值，None 表示返回全 NaN 序列

    Returns:
        pd.Series: 数值列；列缺失且 fallback 为 None 时返回全 NaN 序列
    """
    if column_name not in frame.columns:
        return pd.Series(fallback, index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column_name], errors="coerce")


def to_date_series(values):
    """
    把任意来源的一列解析为日期（无法解析的值为 NaT）

    Args:
        values (pd.Series): 原始列

    Returns:
        pd.Series: datetime64 类型的日期列
    """
    return pd.to_datetime(values, errors="coerce")


def clean_text_value(value):
    """
    清洗文本字段：None / NaN / 占位符统一归一为空串

    Args:
        value: 原始值

    Returns:
        str: 清洗后的文本；缺失时为 ""
    """
    if value is None or value != value:
        return ""
    text = str(value).strip()
    if text in ("None", "nan", "NaT", "-", "--"):
        return ""
    return text


def clean_date_value(value):
    """
    清洗日期字段：无法解析时返回 None（落库即 NULL）

    Args:
        value: 原始值

    Returns:
        datetime.date 或 None: 解析成功返回日期对象，否则 None
    """
    if value is None or value != value:
        return None
    text = str(value).strip()
    if text in ("", "-", "--", "None", "nan", "NaT"):
        return None
    try:
        return pd.to_datetime(text).date()
    except (ValueError, TypeError):
        return None
