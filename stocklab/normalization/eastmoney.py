#!/usr/bin/env python3
"""
==============================================================================
StockLab - 东方财富财报归一化器 (stocklab.normalization.eastmoney)
==============================================================================

【模块职责】
   把东方财富三大报表（利润表 / 资产负债表 / 现金流量表）的源列结构
   归一化为 fundamental 域的 Point-in-Time 契约帧。

【Point-in-Time 规则】
   - announce_date  <- NOTICE_DATE（源接口的公告日期）
   - available_date <- announce_date（公告即可见，研究视角从公告日起才能看到该期数据）
   - 公告日期无法解析的行**直接丢弃并告警**：宁可缺数据，
     也不能入库「只有报告期、没有公告期」的行（否则历史回测必然泄漏未来信息）
   - source_record_id = SECURITY_CODE|REPORT_DATE，便于回源核对

【源列命名】
   东财报表列全大写英文（OPERATE_INCOME / TOTAL_PARENT_EQUITY ...），
   与契约的 snake_case 不同名，映射关系集中在本模块，改名时只需改这里。
"""

import logging

import pandas as pd

from stocklab.domain import (
    BALANCE_SHEET_COLUMNS,
    CASHFLOW_STATEMENT_COLUMNS,
    INCOME_STATEMENT_COLUMNS,
    require_columns,
    align_columns,
)
from stocklab.normalization.base import to_date_series, to_numeric_column

_logger = logging.getLogger(__name__)

__all__ = [
    "normalize_income_statements",
    "normalize_balance_sheets",
    "normalize_cashflow_statements",
    "DEFAULT_SOURCE",
]

# 本层对应的源标识，写入 source 列
DEFAULT_SOURCE = "eastmoney"

# 源表共有的身份列（Point-in-Time 的时间来源）
_SOURCE_IDENTITY_COLUMNS = ("SECURITY_CODE", "REPORT_DATE", "NOTICE_DATE")


def normalize_income_statements(raw, ts_code, source=DEFAULT_SOURCE):
    """
    利润表归一化：东财利润表 → fundamental.income_statements 契约列

    Args:
        raw (pd.DataFrame): 源表
        ts_code (str): 证券代码，如 "600519.SH"
        source (str): 数据源标识，默认 "eastmoney"

    Returns:
        pd.DataFrame: INCOME_STATEMENT_COLUMNS 契约列序，按报告期升序

    Raises:
        DataContractError: 缺少身份列或必需科目列时抛出
    """
    owner = "eastmoney.stock_profit_sheet_by_report_em"
    require_columns(
        raw,
        _SOURCE_IDENTITY_COLUMNS
        + (
            "OPERATE_INCOME", "OPERATE_COST", "OPERATE_PROFIT",
            "TOTAL_PROFIT", "INCOME_TAX", "NETPROFIT", "PARENT_NETPROFIT",
            "BASIC_EPS",
        ),
        owner,
    )

    data = _pit_fields(raw, ts_code, source)
    data.update(
        {
            "revenue": to_numeric_column(raw, "OPERATE_INCOME"),
            "operating_cost": to_numeric_column(raw, "OPERATE_COST"),
            "operating_profit": to_numeric_column(raw, "OPERATE_PROFIT"),
            "total_profit": to_numeric_column(raw, "TOTAL_PROFIT"),
            "income_tax": to_numeric_column(raw, "INCOME_TAX"),
            "net_profit": to_numeric_column(raw, "NETPROFIT"),
            "net_profit_attributable": to_numeric_column(raw, "PARENT_NETPROFIT"),
            "eps": to_numeric_column(raw, "BASIC_EPS"),
        }
    )
    return _assemble(data, INCOME_STATEMENT_COLUMNS, owner)


def normalize_balance_sheets(raw, ts_code, source=DEFAULT_SOURCE):
    """
    资产负债表归一化：东财资产负债表 → fundamental.balance_sheets 契约列

    【有息负债口径】
       interest_bearing_debt = 短期借款 + 一年内到期非流动负债 + 长期借款 + 应付债券。
       东财对「没有该项负债」的报表返回空值，单个缺失项按 0 计入，四项全缺同样按 0
       （视为无有息负债），否则无借款公司的 ROIC 会整列缺失。

    Args:
        raw (pd.DataFrame): 源表
        ts_code (str): 证券代码
        source (str): 数据源标识

    Returns:
        pd.DataFrame: BALANCE_SHEET_COLUMNS 契约列序，按报告期升序

    Raises:
        DataContractError: 缺少身份列或必需科目列时抛出
    """
    owner = "eastmoney.stock_balance_sheet_by_report_em"
    require_columns(
        raw,
        _SOURCE_IDENTITY_COLUMNS
        + (
            "TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_PARENT_EQUITY",
            "MONETARYFUNDS", "SHORT_LOAN", "NONCURRENT_LIAB_1YEAR",
            "LONG_LOAN", "BOND_PAYABLE",
        ),
        owner,
    )

    debt_parts = raw[
        ["SHORT_LOAN", "NONCURRENT_LIAB_1YEAR", "LONG_LOAN", "BOND_PAYABLE"]
    ].apply(pd.to_numeric, errors="coerce")

    data = _pit_fields(raw, ts_code, source)
    data.update(
        {
            "total_assets": to_numeric_column(raw, "TOTAL_ASSETS"),
            "total_liabilities": to_numeric_column(raw, "TOTAL_LIABILITIES"),
            "equity": to_numeric_column(raw, "TOTAL_PARENT_EQUITY"),
            "cash": to_numeric_column(raw, "MONETARYFUNDS"),
            "interest_bearing_debt": debt_parts.sum(axis=1),
        }
    )
    return _assemble(data, BALANCE_SHEET_COLUMNS, owner)


def normalize_cashflow_statements(raw, ts_code, source=DEFAULT_SOURCE):
    """
    现金流量表归一化：东财现金流量表 → fundamental.cashflow_statements 契约列

    【自由现金流口径】
       free_cashflow = 经营活动现金流净额 - 购建固定资产/无形资产/长期资产支付的现金
       （资本开支缺失时该行为 NaN，不臆造为 0）。

    Args:
        raw (pd.DataFrame): 源表
        ts_code (str): 证券代码
        source (str): 数据源标识

    Returns:
        pd.DataFrame: CASHFLOW_STATEMENT_COLUMNS 契约列序，按报告期升序

    Raises:
        DataContractError: 缺少身份列或必需科目列时抛出
    """
    owner = "eastmoney.stock_cash_flow_sheet_by_report_em"
    require_columns(
        raw,
        _SOURCE_IDENTITY_COLUMNS
        + ("NETCASH_OPERATE", "NETCASH_INVEST", "NETCASH_FINANCE", "CONSTRUCT_LONG_ASSET"),
        owner,
    )

    operating = to_numeric_column(raw, "NETCASH_OPERATE")
    capital_expense = to_numeric_column(raw, "CONSTRUCT_LONG_ASSET")

    data = _pit_fields(raw, ts_code, source)
    data.update(
        {
            "operating_cashflow": operating,
            "investing_cashflow": to_numeric_column(raw, "NETCASH_INVEST"),
            "financing_cashflow": to_numeric_column(raw, "NETCASH_FINANCE"),
            "free_cashflow": operating - capital_expense,
        }
    )
    return _assemble(data, CASHFLOW_STATEMENT_COLUMNS, owner)


def _pit_fields(raw, ts_code, source):
    """
    构造四张基本面表共有的 Point-in-Time 字段

    Args:
        raw (pd.DataFrame): 源表（含 SECURITY_CODE / REPORT_DATE / NOTICE_DATE）
        ts_code (str): 证券代码
        source (str): 数据源标识

    Returns:
        dict: {ts_code, report_period, announce_date, available_date, source, source_record_id}
    """
    report_period = to_date_series(raw["REPORT_DATE"]).dt.date
    announce_date = to_date_series(raw["NOTICE_DATE"]).dt.date
    record_id = (
        raw["SECURITY_CODE"].astype(str).str.strip()
        + "|"
        + report_period.astype(str)
    )
    return {
        "ts_code": ts_code,
        "report_period": report_period,
        "announce_date": announce_date,
        # 公告即可见：研究视角从公告日起才允许读到该期数据
        "available_date": announce_date,
        "source": source,
        "source_record_id": record_id,
    }


def _assemble(data, contract_columns, owner):
    """
    组装契约帧：丢弃报告期或公告日期缺失的行，并对齐列序

    Args:
        data (dict): 列名字典
        contract_columns (tuple): 目标契约列序
        owner (str): 契约归属名，用于日志

    Returns:
        pd.DataFrame: 契约列序帧，按报告期升序
    """
    frame = pd.DataFrame(data)
    missing_announce = frame["report_period"].isna() | frame["announce_date"].isna()
    if missing_announce.any():
        _logger.warning(
            "%s 丢弃 %d 行缺报告期或缺公告日期的数据（不可入库）",
            owner,
            int(missing_announce.sum()),
        )
        frame = frame[~missing_announce]
    frame = align_columns(frame, contract_columns, owner)
    return frame.sort_values(by="report_period").reset_index(drop=True)
