#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基本面数据门面 (stocklab.facade.fundamental_data)
==============================================================================

【模块职责】
   FundamentalDataFacade：基本面数据的**纯本地读**门面。
   语料由 app/scripts/sync_market_data.py fundamentals 落库，门面不发任何远端请求。

   - income_statement(ts_code, as_of_date=None)   单只证券利润表（Point-in-Time）
   - balance_sheet(ts_code, as_of_date=None)      单只证券资产负债表
   - cashflow_statement(ts_code, as_of_date=None) 单只证券现金流量表
   - financial_indicators(ts_code, as_of_date=None) 财务指标（质量/成长）
   - latest_as_of(ts_code)                        最新可见报告期与公告日期
   - cross_section_as_of(as_of_date, report_period=None) 全市场横截面

【为什么是纯本地读】
   基本面数据单只约 60 次 HTTP 请求、全市场数小时，不可能在线查询。
   同步脚本离线抓取落库，门面只负责「按可用时间过滤」与「横截面聚合」。

【Point-in-Time 约束：所有查询必须带 available_date 条件】
   报告期早于 as-of 不代表当时已知：一家公司 4 月才公告一季报，
   在 3 月底做历史回测时若读到该期数据，即为未来信息泄漏（look-ahead bias）。
   门面把 available_date <= as_of 作为不可省略的前置条件。

【与 MarketDataFacade 的区别】
   MarketDataFacade：本地/远端优先级路由 + Cache-Aside 回写（行情、估值）
   FundamentalDataFacade：纯本地读，无远端回退，无回写
"""

import logging

import pandas as pd

from stocklab.persistence import (
    BalanceSheetRepository,
    CashflowStatementRepository,
    Database,
    FinancialIndicatorRepository,
    IncomeStatementRepository,
    initialize_database,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "FundamentalDataFacade",
]


class FundamentalDataFacade:
    """基本面数据门面（纯本地读，无远端回退）"""

    def __init__(self, database=None):
        """
        Args:
            database (Database, None): 数据库实例；None 时由 Repository 自建
                                       （仅限单次性脚本，Web 侧必须传 store.facade_database()）
        """
        self._income_repo = IncomeStatementRepository(database)
        self._balance_repo = BalanceSheetRepository(database)
        self._cashflow_repo = CashflowStatementRepository(database)
        self._indicator_repo = FinancialIndicatorRepository(database)

    # ------------------------------------------------------------------
    # 单只证券查询（Point-in-Time 安全）
    # ------------------------------------------------------------------

    def income_statement(self, ts_code, as_of_date=None):
        """
        获取单只证券的利润表（Point-in-Time）

        Args:
            ts_code (str): 证券代码，如 "600519.SH"
            as_of_date (str, optional): 历史时点，格式 "YYYY-MM-DD"；None 表示取全部历史

        Returns:
            pd.DataFrame: INCOME_STATEMENT_COLUMNS 契约列序；无数据时为空表
        """
        if as_of_date:
            return self._income_repo.find_as_of(ts_code, as_of_date)
        return self._income_repo.find_by_code(ts_code)

    def balance_sheet(self, ts_code, as_of_date=None):
        """
        获取单只证券的资产负债表（Point-in-Time）

        Args:
            ts_code (str): 证券代码
            as_of_date (str, optional): 历史时点

        Returns:
            pd.DataFrame: BALANCE_SHEET_COLUMNS 契约列序
        """
        if as_of_date:
            return self._balance_repo.find_as_of(ts_code, as_of_date)
        return self._balance_repo.find_by_code(ts_code)

    def cashflow_statement(self, ts_code, as_of_date=None):
        """
        获取单只证券的现金流量表（Point-in-Time）

        Args:
            ts_code (str): 证券代码
            as_of_date (str, optional): 历史时点

        Returns:
            pd.DataFrame: CASHFLOW_STATEMENT_COLUMNS 契约列序
        """
        if as_of_date:
            return self._cashflow_repo.find_as_of(ts_code, as_of_date)
        return self._cashflow_repo.find_by_code(ts_code)

    def financial_indicators(self, ts_code, as_of_date=None):
        """
        获取单只证券的财务指标（质量/成长，Point-in-Time）

        Args:
            ts_code (str): 证券代码
            as_of_date (str, optional): 历史时点

        Returns:
            pd.DataFrame: FINANCIAL_INDICATOR_COLUMNS 契约列序
        """
        if as_of_date:
            return self._indicator_repo.find_as_of(ts_code, as_of_date)
        return self._indicator_repo.find_by_code(ts_code)

    def latest_as_of(self, ts_code):
        """
        获取单只证券的最新可见报告期与公告日期（用于页面显示「数据截止」）"""
        latest = {}
        for repo, key in [
            (self._income_repo, "income"),
            (self._balance_repo, "balance"),
            (self._cashflow_repo, "cashflow"),
            (self._indicator_repo, "indicator"),
        ]:
            row = repo.latest_as_of(ts_code, "9999-12-31")
            if not row.empty:
                latest[key] = {
                    "report_period": str(row["report_period"].iloc[0]),
                    "announce_date": str(row["announce_date"].iloc[0]),
                }
        return latest

    # ------------------------------------------------------------------
    # 全市场横截面（用于筛选/因子）
    # ------------------------------------------------------------------

    def cross_section_as_of(self, as_of_date, report_period=None):
        """
        获取 as-of 时点全市场已公告的基本面横截面

        Args:
            as_of_date (str): 历史时点，格式 "YYYY-MM-DD"
            report_period (str, optional): 限定报告期，如 "2024-03-31"

        Returns:
            dict: {
                "income": pd.DataFrame,
                "balance": pd.DataFrame,
                "cashflow": pd.DataFrame,
                "indicators": pd.DataFrame,
            } 每个键对应该表的横截面（每只证券一行最新可见数据）
        """
        return {
            "income": self._income_repo.cross_section_as_of(as_of_date, report_period),
            "balance": self._balance_repo.cross_section_as_of(as_of_date, report_period),
            "cashflow": self._cashflow_repo.cross_section_as_of(as_of_date, report_period),
            "indicators": self._indicator_repo.cross_section_as_of(as_of_date, report_period),
        }