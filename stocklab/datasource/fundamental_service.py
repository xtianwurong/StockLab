#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基本面取数模块 (stocklab.datasource.fundamental_service)
==============================================================================

【模块职责】
   从东方财富拉取三大报表（利润表 / 资产负债表 / 现金流量表），
   经 normalizer.eastmoney 归一化为 Point-in-Time 契约帧返回。
   与 market_service 一样：本层只取数与归一化，不落库。

【数据源说明】
   - 接口：akshare 的 stock_profit/balance/cash_flow_sheet_by_report_em
   - 返回 REPORT_DATE（报告期）+ NOTICE_DATE（公告日期），
     是当前唯一能同时拿到「报告期 + 公告日期」的免费口径，
     Point-in-Time 依赖的 announce_date 即来源于此；
   - 单只股票约 60 次 HTTP 请求（按报告期分批），故同步耗时较长，
     由调用方做并发取数、串行落库（见 app/scripts/sync_market_data.py）。

【失败降级】
   取数失败记录日志并返回空 DataFrame，由调用方决定是否中止；
   源列缺失（数据源改版）由 normalizer 抛 DataContractError，属不可重试错误，
   同样降级为空帧但记 ERROR —— 绝不返回列不齐的帧去写库。
"""

import logging
import time

import akshare as ak
import pandas as pd

from stocklab.domain import DataContractError
from stocklab.normalization.eastmoney import (
    normalize_balance_sheets,
    normalize_cashflow_statements,
    normalize_income_statements,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "FundamentalService",
]


class FundamentalService:
    """
    基本面取数类：东方财富三大报表 → Point-in-Time 契约帧

    【方法与库表对应】
      fetch_income_statement    -> fundamental.income_statements
      fetch_balance_sheet       -> fundamental.balance_sheets
      fetch_cashflow_statement  -> fundamental.cashflow_statements
      （financial_indicators 由 stocklab.fundamental 纯计算派生，不取数）
    """

    def __init__(self, retry_count=2, retry_interval_seconds=2, interval_seconds=0.3):
        """
        初始化基本面取数服务

        Args:
            retry_count (int, optional): 失败重试次数
            retry_interval_seconds (int, optional): 重试间隔秒数
            interval_seconds (float, optional): 两次调用之间的最小间隔（防风控）
        """
        self._retry_count = retry_count
        self._retry_interval_seconds = retry_interval_seconds
        self._interval_seconds = interval_seconds

    def fetch_income_statement(self, ts_code):
        """
        获取单只证券的利润表（Point-in-Time）

        Args:
            ts_code (str): 证券代码，如 "600519.SH"

        Returns:
            pd.DataFrame: INCOME_STATEMENT_COLUMNS 契约列序；失败时为空表
        """
        return self._fetch(
            ts_code,
            ak.stock_profit_sheet_by_report_em,
            normalize_income_statements,
        )

    def fetch_balance_sheet(self, ts_code):
        """
        获取单只证券的资产负债表（Point-in-Time）

        Args:
            ts_code (str): 证券代码

        Returns:
            pd.DataFrame: BALANCE_SHEET_COLUMNS 契约列序；失败时为空表
        """
        return self._fetch(
            ts_code,
            ak.stock_balance_sheet_by_report_em,
            normalize_balance_sheets,
        )

    def fetch_cashflow_statement(self, ts_code):
        """
        获取单只证券的现金流量表（Point-in-Time）

        Args:
            ts_code (str): 证券代码

        Returns:
            pd.DataFrame: CASHFLOW_STATEMENT_COLUMNS 契约列序；失败时为空表
        """
        return self._fetch(
            ts_code,
            ak.stock_cash_flow_sheet_by_report_em,
            normalize_cashflow_statements,
        )

    def _fetch(self, ts_code, fetcher, normalizer):
        """
        取数 + 归一化的公共流程（重试、限流、契约错误处理）

        Args:
            ts_code (str): 证券代码
            fetcher: akshare 接口函数（symbol 参数为 市场前缀 + 代码）
            normalizer: 对应的归一化函数 (raw, ts_code) -> 契约帧

        Returns:
            pd.DataFrame: 契约列序帧；失败或源列不匹配时为空表
        """
        symbol = _to_em_symbol(ts_code)
        raw = pd.DataFrame()

        for attempt in range(1, self._retry_count + 1):
            if attempt > 1 and self._interval_seconds:
                time.sleep(self._interval_seconds)
            try:
                fetched = fetcher(symbol=symbol)
                if fetched is None or fetched.empty:
                    _logger.warning("[%s] 财报接口返回空数据", ts_code)
                    return pd.DataFrame()
                raw = fetched
                break
            except Exception as error:
                _logger.debug(
                    "%s [%s] 第 %d/%d 次失败: %s",
                    getattr(fetcher, "__name__", "fetcher"),
                    ts_code, attempt, self._retry_count, error,
                )
                if attempt < self._retry_count:
                    time.sleep(self._retry_interval_seconds)

        if raw.empty:
            return pd.DataFrame()

        if self._interval_seconds:
            time.sleep(self._interval_seconds)

        try:
            return normalizer(raw, ts_code)
        except DataContractError as error:
            # 源列改版属不可重试错误：记 ERROR 并拒绝产出，绝不写入缺列数据
            _logger.error("[%s] 财报数据契约违约，本批次不入库: %s", ts_code, error)
            return pd.DataFrame()


def _to_em_symbol(ts_code):
    """
    把标准 ts_code 转成东财财报接口的市场前缀格式

    Args:
        ts_code (str): 标准证券代码，如 "600519.SH"

    Returns:
        str: 东财格式，如 "SH600519"
    """
    parts = str(ts_code).split(".")
    if len(parts) != 2:
        raise ValueError("非法证券代码: %s" % ts_code)
    return parts[1].upper() + parts[0]
