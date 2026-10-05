#!/usr/bin/env python3
"""
==============================================================================
StockLab - 资金流数据源 (stocklab.datasource.capital_flow)
==============================================================================

【模块职责】
   板块/行业资金流数据采集
   数据源：AKShare 板块资金流接口
"""


import logging
from typing import Literal
from datetime import date, timedelta

import pandas as pd

from stocklab.domain import CAPITAL_FLOW_DAILY_COLUMNS

_logger = logging.getLogger(__name__)

__all__ = [
    "CAPITAL_FLOW_SOURCE_AKSHARE",
    "fetch_sector_capital_flow",
    "fetch_sw_level1_capital_flow",
    "fetch_sw_level2_capital_flow",
    "fetch_concept_capital_flow",
]

CAPITAL_FLOW_SOURCE_AKSHARE = "akshare:capital_flow"

SectorType = Literal["sw_level1", "sw_level2", "concept", "industry"]


def fetch_sector_capital_flow(
    sector_type: SectorType,
    start_date: str,
    end_date: str = None,
    top_n: int = 100
) -> pd.DataFrame:
    """
    获取板块资金流数据

    Args:
        sector_type: 板块类型
        start_date: 起始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD，默认今天
        top_n: 获取前 N 个板块

    Returns:
        pd.DataFrame: CAPITAL_FLOW_DAILY_COLUMNS 契约列序
    """
    import akshare as ak

    if end_date is None:
        end_date = date.today().strftime("%Y-%m-%d")

    # AKShare 接口映射
    type_map = {
        "sw_level1": ("sw", "sw1"),
        "sw_level2": ("sw", "sw2"),
        "concept": ("concept", "concept"),
        "industry": ("industry", "industry"),
    }

    category, sub_type = type_map.get(sector_type, ("industry", "industry"))

    try:
        df = ak.stock_sector_fund_flow_rank(
            indicator="今日",
            sector_type=sub_type,
        )
    except Exception as e:
        _logger.warning("获取 %s 资金流失败: %s", sector_type, e)
        return pd.DataFrame(columns=list(CAPITAL_FLOW_DAILY_COLUMNS))

    if df is None or df.empty:
        return pd.DataFrame(columns=list(CAPITAL_FLOW_DAILY_COLUMNS))

    # AKShare 返回列：板块名称、板块代码、今日涨跌幅、主力净流入、主力净流入占比、...
    # 标准化列名
    rename_map = {
        "板块名称": "sector_name",
        "板块代码": "sector_code",
        "今日涨跌幅": "change_pct",
        "主力净流入": "main_net_inflow",
        "主力净流入占比": "main_net_inflow_rate",
        "超大单净流入": "super_net_inflow",
        "大单净流入": "large_net_inflow",
        "中单净流入": "medium_net_inflow",
        "小单净流入": "small_net_inflow",
    }

    df = df.rename(columns=rename_map)

    # 只保留需要的列
    keep_cols = ["sector_code", "sector_name"]
    for c in ["change_pct", "main_net_inflow", "main_net_inflow_rate",
              "super_net_inflow", "large_net_inflow", "medium_net_inflow", "small_net_inflow"]:
        if c in df.columns:
            keep_cols.append(c)

    df = df[keep_cols].copy()

    # 类型转换
    for col in ["change_pct", "main_net_inflow", "main_net_inflow_rate",
                "super_net_inflow", "large_net_inflow", "medium_net_inflow", "small_net_inflow"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 计算衍生字段
    if "main_net_inflow" in df.columns and "small_net_inflow" in df.columns:
        df["net_inflow"] = df["main_net_inflow"] + df.get("super_net_inflow", 0) + df.get("large_net_inflow", 0) + df.get("medium_net_inflow", 0) + df.get("small_net_inflow", 0)
    else:
        df["net_inflow"] = df.get("main_net_inflow", 0)

    if "change_pct" in df.columns:
        # 近似计算流入率
        df["net_inflow_rate"] = df["net_inflow"] / 1e8  # 简化

    # 注意：不要在这里 `from datetime import date` —— 函数开头 end_date 默认值
    # 已经在用模块级 date，函数体内再导入会让整函数把 date 当局部变量，
    # end_date=None 时直接 UnboundLocalError
    from datetime import datetime
    trade_date = date.today()
    now = datetime.now()

    df["sector_type"] = sector_type
    df["trade_date"] = trade_date
    df["inflow"] = df.get("main_net_inflow", 0) + df.get("super_net_inflow", 0) + df.get("large_net_inflow", 0)
    df["outflow"] = df.get("medium_net_inflow", 0) + df.get("small_net_inflow", 0)
    df["main_net_inflow"] = df.get("main_net_inflow", 0)
    df["retail_net_inflow"] = df.get("small_net_inflow", 0)
    df["source"] = CAPITAL_FLOW_SOURCE_AKSHARE
    df["fetched_at"] = now

    # 补全缺失列
    for col in CAPITAL_FLOW_DAILY_COLUMNS:
        if col not in df.columns:
            df[col] = None

    df = df[list(CAPITAL_FLOW_DAILY_COLUMNS)]

    # 限制数量
    if top_n and len(df) > top_n:
        df = df.head(top_n)

    return df


def fetch_sw_level1_capital_flow(start_date: str, end_date: str = None) -> pd.DataFrame:
    return fetch_sector_capital_flow("sw_level1", start_date, end_date)


def fetch_sw_level2_capital_flow(start_date: str, end_date: str = None) -> pd.DataFrame:
    return fetch_sector_capital_flow("sw_level2", start_date, end_date)


def fetch_concept_capital_flow(start_date: str, end_date: str = None) -> pd.DataFrame:
    return fetch_sector_capital_flow("concept", start_date, end_date)