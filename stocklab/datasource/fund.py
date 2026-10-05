#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金数据源 (stocklab.datasource.fund)
==============================================================================

【模块职责】
   公募基金基本信息与净值历史采集
   数据源：AKShare fund_* 系列接口
"""


import logging
from typing import Optional
from datetime import date

import pandas as pd

from stocklab.domain import (
    FUND_INFO_COLUMNS,
    FUND_NAV_HISTORY_COLUMNS,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "FUND_SOURCE_AKSHARE",
    "fetch_fund_info",
    "fetch_fund_nav_history",
    "fetch_all_fund_codes",
]

FUND_SOURCE_AKSHARE = "akshare:fund"


def fetch_all_fund_codes() -> pd.DataFrame:
    """
    获取全市场基金代码列表

    Returns:
        pd.DataFrame: fund_code, fund_name, fund_type
    """
    import akshare as ak

    # 获取所有基金列表
    df = ak.fund_name_em()
    if df is None or df.empty:
        _logger.warning("获取基金列表失败")
        return pd.DataFrame(columns=["fund_code", "fund_name", "fund_type"])

    # 标准化列名
    # AKShare 返回：基金代码、基金简称、基金类型
    df = df.rename(columns={
        "基金代码": "fund_code",
        "基金简称": "fund_name",
        "基金类型": "fund_type",
    })

    # 标准化代码格式：补全后缀 .OF
    df["fund_code"] = df["fund_code"].astype(str).str.zfill(6) + ".OF"

    return df[["fund_code", "fund_name", "fund_type"]].drop_duplicates()


def fetch_fund_info(fund_code: str) -> pd.DataFrame:
    """
    获取单只基金的基本信息

    Args:
        fund_code: 基金代码，如 "000001.OF"

    Returns:
        pd.DataFrame: FUND_INFO_COLUMNS 契约列序，单行
    """
    import akshare as ak

    # 去掉后缀
    code = fund_code.replace(".OF", "").replace(".OF", "")

    try:
        df = ak.fund_individual_basic_info_xq(symbol=code)
    except Exception as e:
        _logger.warning("获取基金 [%s] 基本信息失败: %s", fund_code, e)
        return pd.DataFrame(columns=list(FUND_INFO_COLUMNS))

    if df is None or df.empty:
        _logger.warning("基金 [%s] 无基本信息", fund_code)
        return pd.DataFrame(columns=list(FUND_INFO_COLUMNS))

    # df 结构：item, value
    info = dict(zip(df["item"], df["value"]))

    from datetime import datetime
    frame = pd.DataFrame([{
        "fund_code": fund_code,
        "fund_name": info.get("基金名称", ""),
        "fund_short_name": info.get("基金简称", ""),
        "fund_type": info.get("基金类型", ""),
        "manager_name": info.get("基金经理", ""),
        "company_name": info.get("基金公司", ""),
        "establish_date": _parse_date(info.get("成立日期", "")),
        "benchmark": info.get("业绩比较基准", ""),
        "status": "active" if info.get("基金状态", "") != "清盘" else "terminated",
        "source": "akshare:fund_individual",
        "fetched_at": datetime.now(),
    }])

    frame = frame[list(FUND_INFO_COLUMNS)]
    return frame


def fetch_fund_nav_history(fund_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """
    获取单只基金的净值历史

    Args:
        fund_code: 基金代码，如 "000001.OF"
        start_date: 起始日期 YYYY-MM-DD，None 表示不限
        end_date: 结束日期 YYYY-MM-DD，None 表示今天

    Returns:
        pd.DataFrame: FUND_NAV_HISTORY_COLUMNS 契约列序
    """
    import akshare as ak

    code = fund_code.replace(".OF", "")

    try:
        df = ak.fund_open_fund_info_em(fund=code, indicator="单位净值走势")
    except Exception as e:
        _logger.warning("获取基金 [%s] 净值历史失败: %s", fund_code, e)
        return pd.DataFrame(columns=list(FUND_NAV_HISTORY_COLUMNS))

    if df is None or df.empty:
        return pd.DataFrame(columns=list(FUND_NAV_HISTORY_COLUMNS))

    # 列：净值日期、单位净值、累计净值、日增长率
    df = df.rename(columns={
        "净值日期": "nav_date",
        "单位净值": "nav",
        "累计净值": "acc_nav",
        "日增长率": "change_pct",
    })

    # 类型转换
    df["nav_date"] = pd.to_datetime(df["nav_date"], errors="coerce").dt.date
    df["nav"] = pd.to_numeric(df["nav"], errors="coerce")
    df["acc_nav"] = pd.to_numeric(df["acc_nav"], errors="coerce")
    df["change_pct"] = pd.to_numeric(df["change_pct"].astype(str).str.replace("%", ""), errors="coerce")

    # 日期过滤
    if start_date:
        start = pd.to_datetime(start_date).date()
        df = df[df["nav_date"] >= start]
    if end_date:
        end = pd.to_datetime(end_date).date()
        df = df[df["nav_date"] <= end]

    df = df.dropna(subset=["nav_date", "nav"])
    df = df[df["nav"] > 0]

    if df.empty:
        return pd.DataFrame(columns=list(FUND_NAV_HISTORY_COLUMNS))

    from datetime import datetime
    now = datetime.now()
    df["fund_code"] = fund_code
    df["source"] = "akshare:fund_nav"
    df["fetched_at"] = now

    df = df[list(FUND_NAV_HISTORY_COLUMNS)]
    df = df.sort_values("nav_date").reset_index(drop=True)

    _logger.info("基金 [%s] 净值历史采集: %d 条，%s ~ %s",
                 fund_code, len(df), df["nav_date"].iloc[0], df["nav_date"].iloc[-1])
    return df


def _parse_date(date_str: str):
    """解析各种日期格式"""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return date.fromisoformat(date_str) if "-" in date_str else date_str
        except Exception:
            continue
    return None