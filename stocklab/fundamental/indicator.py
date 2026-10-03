#!/usr/bin/env python3
"""
==============================================================================
StockLab - 财务指标派生 (stocklab.fundamental.indicator)
==============================================================================

【模块职责】
   由已归一化的利润表与资产负债表**纯计算**派生质量 / 成长指标，
   输出 fundamental.financial_indicators 契约帧。
   本模块不取数、不落库、不 import datasource / persistence，可离线单测。

【指标口径】
   质量（分母为 0 或缺失时返回 NaN，不臆造数值）：
     gross_margin     = (revenue - operating_cost) / revenue
     operating_margin = operating_profit / revenue
     net_margin       = net_profit / revenue
     roe              = net_profit_attributable / equity
     roa              = net_profit / total_assets
     roic             = NOPAT / (equity + interest_bearing_debt)
                       NOPAT = operating_profit * (1 - 有效税率)，
                       有效税率 = income_tax / total_profit（利润总额 <= 0 时取 0）

   成长（同比 = 与去年同一报告期比较，按报告期平移一年做关联，
   不假设报告期连续，缺去年同期即为 NaN）：
     revenue_yoy = (revenue - 上年同期 revenue) / |上年同期 revenue|
     profit_yoy  = 同上，口径 net_profit
     eps_yoy     = 同上，口径 eps

【Point-in-Time】
   指标同时依赖利润表与资产负债表，announce_date 取两者的**较晚者**：
   任何一方尚未公告时，该指标都不应出现在历史研究里。
"""

import pandas as pd

from stocklab.domain import FINANCIAL_INDICATOR_COLUMNS, align_columns

__all__ = [
    "build_financial_indicators",
]


def build_financial_indicators(income_df, balance_df):
    """
    由利润表与资产负债表派生财务指标帧

    Args:
        income_df (pd.DataFrame): 利润表契约帧（INCOME_STATEMENT_COLUMNS）
        balance_df (pd.DataFrame): 资产负债表契约帧（BALANCE_SHEET_COLUMNS），
                                   可为空表（此时资产负债类指标为 NaN）

    Returns:
        pd.DataFrame: FINANCIAL_INDICATOR_COLUMNS 契约列序，按报告期升序；
                      利润表为空时返回空表
    """
    if income_df is None or income_df.empty:
        return pd.DataFrame(columns=list(FINANCIAL_INDICATOR_COLUMNS))

    frame = income_df.reset_index(drop=True).copy()
    # 归一化帧与库内帧的报告期类型不同（date 对象 vs datetime64），
    # 关联前统一为 datetime64，避免 pandas 因键类型不一致拒绝合并
    frame["report_period"] = pd.to_datetime(frame["report_period"])
    frame = frame.merge(_balance_part(balance_df), on=["ts_code", "report_period"], how="left")
    frame = frame.merge(_previous_year_part(income_df), on=["ts_code", "report_period"], how="left")

    revenue = _numeric(frame, "revenue")
    operating_cost = _numeric(frame, "operating_cost")
    operating_profit = _numeric(frame, "operating_profit")
    net_profit = _numeric(frame, "net_profit")
    net_profit_attributable = _numeric(frame, "net_profit_attributable")
    eps = _numeric(frame, "eps")
    equity = _numeric(frame, "equity")
    total_assets = _numeric(frame, "total_assets")
    interest_bearing_debt = _numeric(frame, "interest_bearing_debt")
    total_profit = _numeric(frame, "total_profit")
    income_tax = _numeric(frame, "income_tax").fillna(0.0)

    tax_rate = _safe_divide(income_tax, total_profit).fillna(0.0)
    nopat = operating_profit * (1.0 - tax_rate)

    announce_date = [
        _later_date(left, right)
        for left, right in zip(frame["announce_date"], frame["balance_announce_date"])
    ]

    result = pd.DataFrame(
        {
            "ts_code": frame["ts_code"],
            "report_period": frame["report_period"].dt.date,
            "announce_date": announce_date,
            "available_date": announce_date,
            "source": frame["source"],
            "source_record_id": frame["source_record_id"],
            "roe": _safe_divide(net_profit_attributable, equity),
            "roa": _safe_divide(net_profit, total_assets),
            "roic": _safe_divide(nopat, equity + interest_bearing_debt),
            "gross_margin": _safe_divide(revenue - operating_cost, revenue),
            "operating_margin": _safe_divide(operating_profit, revenue),
            "net_margin": _safe_divide(net_profit, revenue),
            "revenue_yoy": _yoy(revenue, _numeric(frame, "revenue_prev")),
            "profit_yoy": _yoy(net_profit, _numeric(frame, "net_profit_prev")),
            "eps_yoy": _yoy(eps, _numeric(frame, "eps_prev")),
        }
    )
    result = align_columns(result, FINANCIAL_INDICATOR_COLUMNS, "fundamental.financial_indicators")
    return result.sort_values(by="report_period").reset_index(drop=True)


def _balance_part(balance_df):
    """
    取资产负债表中派生指标所需的部分（重命名公告日期以避免列名冲突）

    Args:
        balance_df (pd.DataFrame): 资产负债表契约帧

    Returns:
        pd.DataFrame: ts_code / report_period / balance_announce_date /
                      equity / total_assets / interest_bearing_debt
    """
    columns = ["ts_code", "report_period", "announce_date", "equity",
               "total_assets", "interest_bearing_debt"]
    if balance_df is None or balance_df.empty:
        empty = pd.DataFrame(columns=[column.replace("announce_date", "balance_announce_date")
                                      for column in columns])
    else:
        empty = balance_df[columns].rename(
            columns={"announce_date": "balance_announce_date"}
        )
    empty["report_period"] = pd.to_datetime(empty["report_period"])
    return empty


def _previous_year_part(income_df):
    """
    生成按报告期平移的「去年同期」帧（用于同比计算）

    【平移方向】
       本期 P 需要关联到原始报告期为 P-1 年的行；关联键来自去年同期帧，
       因此要把该帧的报告期**加一年**（原始 P' 的行标成 P'+1 年 = 本期 P），
       这样左侧本期 P 才能命中右侧 P-1 年的数据。方向搞反会算成「与明年比」。

    Args:
        income_df (pd.DataFrame): 利润表契约帧

    Returns:
        pd.DataFrame: ts_code / report_period(=原报告期 + 1 年) / revenue_prev /
                      net_profit_prev / eps_prev
    """
    previous = income_df[["ts_code", "report_period", "revenue", "net_profit", "eps"]].copy()
    previous["report_period"] = (
        pd.to_datetime(previous["report_period"]) + pd.DateOffset(years=1)
    )
    return previous.rename(
        columns={
            "revenue": "revenue_prev",
            "net_profit": "net_profit_prev",
            "eps": "eps_prev",
        }
    )


def _numeric(frame, column):
    """取数值列，列不存在时返回全 NaN（保证算式可运行）"""
    if column not in frame.columns:
        return pd.Series(float("nan"), index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce")


def _safe_divide(numerator, denominator):
    """
    安全除法：分母为 0 或缺失时返回 NaN（不产生 inf，也不触发除零警告）

    Args:
        numerator (pd.Series): 分子
        denominator (pd.Series): 分母

    Returns:
        pd.Series: 比值
    """
    clean_denominator = denominator.mask(denominator == 0)
    return numerator / clean_denominator


def _yoy(current, previous):
    """
    同比增长率：(本期 - 去年同期) / |去年同期|

    Args:
        current (pd.Series): 本期值
        previous (pd.Series): 去年同期值

    Returns:
        pd.Series: 同比；去年同期缺失或为 0 时为 NaN
    """
    return _safe_divide(current - previous, previous.abs())


def _later_date(left, right):
    """
    取两个日期中较晚的一个（缺失值视为不存在）

    Args:
        left: 第一个日期（date / Timestamp / NaT / None 均可）
        right: 第二个日期（同上）

    Returns:
        datetime.date 或 None: 两者都有时取较晚者，只有一方有效时取该方
    """
    left = _as_date(left)
    right = _as_date(right)
    if left is not None and right is not None:
        return left if left >= right else right
    if left is not None:
        return left
    return right


def _as_date(value):
    """
    把任意日期形态归一为 date；缺失或无法解析时返回 None

    Args:
        value: date、Timestamp、字符串、NaT、NaN、None

    Returns:
        datetime.date 或 None
    """
    if value is None:
        return None
    if value != value:  # NaN / NaT 与自身不相等
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if parsed is None or parsed != parsed:
        return None
    return parsed.date()
