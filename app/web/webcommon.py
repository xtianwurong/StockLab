#!/usr/bin/env python3
"""
==============================================================================
StockLab - Web 层横切公共模块 (app.web.webcommon)
==============================================================================

【模块职责】
  只放**多个接口模块长得一模一样**的横切逻辑，三类：
    - json_num / json_text            DataFrame 单元格 -> JSON 安全值
    - parse_int_arg / parse_paging    整数查询参数的格式与范围校验
    - looks_like_date                 严格 YYYY-MM-DD 校验

【什么**不**放进来】
  - 业务口径（分位、档位、统计）：那是 store / stocklab.facade 的事；
  - 取数与数据库：本模块无状态，不碰连接；
  - 错误响应的形状：各接口的文案本身就不同，统一成 bad_request() 反而
    会把「哪句话回给用户」这一层判断藏起来，所以保持各模块就地
    `jsonify({"error": ...}), 400` —— 形状已由 tests 全部钉住。

【为什么必须有 json_num】
  pandas 的 NaN / ±inf 直接进 flask.jsonify 会产出 `NaN` / `Infinity`，
  两者都**不是合法 JSON**：浏览器侧 JSON.parse 会直接抛错，整页数据渲染
  失败。因此序列化边界上必须先把它们归一成 null。
  这份逻辑此前在 store / compare_api / screener_api 各有一份拷贝，
  且 store 那份漏了 inf —— 这正是它该只存在一次的理由。

【依赖方向】
  只依赖 flask 与 pandas，**不 import stocklab.***（含 persistence）。
  app.web 各接口模块的取数统一走 stocklab.facade，本模块不参与。
==============================================================================
"""

import math
import re

import pandas as pd
from flask import request

__all__ = [
    "json_num",
    "json_text",
    "parse_int_arg",
    "parse_paging",
    "looks_like_date",
]


# ---------------------------------------------------------------------------
# JSON 安全转换
# ---------------------------------------------------------------------------

def json_num(value, digits=None):
    """
    把 pandas / numpy 数值转成 JSON 安全的数：NaN 与 ±inf 归一为 None

    Args:
        value: 单元格原始值（可为 None / NaN / numpy 标量 / 字符串数字）
        digits (int | None): 保留小数位；None 表示不截断，保留原精度

            传 4 是因为 SQL 返回的中位数常是 37.88999999999999 这类全长
            浮点，全市场五千多行直接下发会让 payload 白白变大，而页面
            展示时反正还要再格式化一次。

    Returns:
        float | None: JSON 可序列化数值

    Examples:
        >>> json_num(float("nan")) is None
        True
        >>> json_num(float("inf")) is None
        True
        >>> json_num("37.88999999999999", 4)
        37.89
        >>> json_num("abc") is None
        True
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # NaN 自比较不等；inf 用 math.isinf 一次同时挡掉正负两端
    if number != number or math.isinf(number):
        return None
    if digits is None:
        return number
    return round(number, digits)


def json_text(value):
    """
    把可能为 NaN 的字符串转成空串

    Args:
        value: 单元格原始值（None / NaN / 任意可 str 的对象）

    Returns:
        str: 非空字符串

    Examples:
        >>> json_text(float("nan"))
        ''
        >>> json_text(None)
        ''
        >>> json_text(600519)
        '600519'
    """
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    if isinstance(value, str):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# 请求参数校验
# ---------------------------------------------------------------------------

_INTEGER_RE = re.compile(r"-?\d+")


def parse_int_arg(name, raw, default, minimum=None, maximum=None):
    """
    严格解析一个整数查询参数

    Args:
        name (str): 参数名，只用于拼错误文案
        raw (str | None): 从 request.args 取到的原始值
        default (int): 缺省或空串时回退的值
        minimum (int | None): 下界（含）
        maximum (int | None): 上界（含）

    Returns:
        tuple: (值, None) 或 (None, 错误文案)；调用方据后者回 400

    【为什么空串当缺省、其余一律 400】
      空串来自表单提交空字段，语义就是「没填」，回默认值是诚实的；
      而 `--5` / `1e2` / `abc` 是**写了值但写错了**，静默回退会让调用方
      拿到一批条数完全不同的数据，却以为自己的 limit 生效了 —— 假阴性
      比 400 难查得多，所以这里宁可报错。

    Examples:
        >>> parse_int_arg("limit", "20", 30)
        (20, None)
        >>> parse_int_arg("limit", "", 30)
        (30, None)
        >>> parse_int_arg("limit", "abc", 30)[0] is None
        True
        >>> parse_int_arg("offset", "-1", 0, minimum=0)[0] is None
        True
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        return default, None
    if not _INTEGER_RE.fullmatch(text):
        return None, "参数 [%s] 必须是整数" % name
    value = int(text)
    if minimum is not None and value < minimum:
        return None, "参数 [%s] 不能小于 %d" % (name, minimum)
    if maximum is not None and value > maximum:
        return None, "参数 [%s] 取值范围 %s~%d" % (
            name, minimum if minimum is not None else "-", maximum)
    return value, None


def parse_paging(default_limit, max_limit):
    """
    读取并校验 offset / limit 分页参数

    Args:
        default_limit (int): limit 缺省时取的值
        max_limit (int): limit 上限

    Returns:
        tuple: (offset, limit) 或 (None, 错误文案)；调用方据后者回 400

    Examples:
        >>> parse_paging(20, 200)
        (0, 20)
    """
    offset, error = parse_int_arg("offset", request.args.get("offset"), 0,
                                  minimum=0)
    if error:
        return None, error
    limit, error = parse_int_arg("limit", request.args.get("limit"),
                                 default_limit, minimum=1, maximum=max_limit)
    if error:
        return None, error
    return (offset, limit), None


def looks_like_date(text):
    """
    严格判断是否为 YYYY-MM-DD

    【为什么要严格】
      只看长度 <= 10 会放行 "abcdefghij" 这类 10 位垃圾值，它一路走到 SQL
      才变成「本地无数据」—— 调用方拿到的是一个**业务上正确的空结果**，
      而不是「你传错了参数」。假阴性比报错难查得多。

    Args:
        text (str): 待校验文本

    Returns:
        bool: 是否为合法日期

    Examples:
        >>> looks_like_date("2026-01-31")
        True
        >>> looks_like_date("2026-13-01")
        False
        >>> looks_like_date("20260101")
        False
        >>> looks_like_date("")
        False
    """
    if not isinstance(text, str) or len(text) != 10:
        return False
    try:
        return not pd.isna(pd.to_datetime(text, format="%Y-%m-%d",
                                          errors="raise"))
    except (ValueError, TypeError):
        return False
