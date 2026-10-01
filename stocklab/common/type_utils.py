#!/usr/bin/env python3
"""
==============================================================================
StockLab - 类型安全转换基础工具模块 (stocklab.common.type_utils)
==============================================================================
"""

import math

# 明确对外暴露契约
__all__ = ["safe_float", "safe_int"]


def safe_float(val, default=None):
    """
    安全转换为浮点数 (float)

    【处理场景】
      - None / 空字符串 / 常见占位符 ("-", "--") -> default
      - 字符串浮点数 / 科学计数法 -> float
      - math.isnan 的浮点数 -> default
      - 转换异常 (ValueError / TypeError) -> default

    Args:
        val: 待转换的值
        default: 转换失败时返回的备选默认值（默认为 None）

    Returns:
        float | default: 转换成功返回浮点数，失败返回 default
    """
    if val is None:
        return default

    if isinstance(val, str):
        val = val.strip()
        if val == "" or val == "-" or val == "--":
            return default

    try:
        f_val = float(val)
        if math.isnan(f_val):
            return default
        return f_val
    except (ValueError, TypeError):
        return default


def safe_int(val, default=None):
    """
    安全转换为整数 (int)

    【说明】
      先通过 safe_float 解析，再转为 int，可无缝处理 "1045357.0" 这类带小数点的整数字符串。

    Args:
        val: 待转换的值
        default: 转换失败时返回的备选默认值（默认为 None）

    Returns:
        int | default: 转换成功返回整数，失败返回 default
    """
    float_val = safe_float(val, default=None)
    if float_val is not None:
        return int(float_val)
    return default
