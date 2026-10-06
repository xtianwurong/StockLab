#!/usr/bin/env python3
"""
==============================================================================
StockLab - 文本排版工具 (stocklab.analytics.text_utils)
==============================================================================

【模块职责】
  仅放置报告渲染层共用的文本排版原语：
    - visual_width: 计算字符串视觉列数（东亚全角字符按 2 列计）
    - pad_text: 按视觉宽度右侧补空格，使中英文混排列对齐

【为何单独成模块】
  percentile_reporter 与 profile_reporter 都需要这两个函数，
  而 analytics/__init__.py 会导入它们 —— 若把函数定义在 __init__.py 里，
  会导致循环导入：__init__.py -> percentile_reporter -> __init__.py。
  独立出 text_utils.py 即可打破循环。
==============================================================================
"""

import unicodedata


def visual_width(text):
    """计算字符串的视觉列数：东亚全角字符按 2 列计

    Args:
        text (str): 输入文本

    Returns:
        int: 视觉宽度（列数）
    """
    width = 0
    for char in text:
        if unicodedata.east_asian_width(char) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def pad_text(text, target_width):
    """按视觉宽度右侧补空格，使中英文混排的列能够对齐

    Args:
        text (str): 待补齐的文本
        target_width (int): 目标视觉列数

    Returns:
        str: 补齐后的文本；已超宽时原样返回
    """
    w = visual_width(text)
    if w >= target_width:
        return text
    return text + " " * (target_width - w)


__all__ = [
    "visual_width",
    "pad_text",
]
