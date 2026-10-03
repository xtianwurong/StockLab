#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基本面 Repository (stocklab.persistence.repository.fundamental)
==============================================================================

【模块职责】
   封装 fundamental 域四张表的读写，核心是 Point-in-Time 查询：
     - find_as_of(ts_code, as_of_date)  返回 as-of 时点**已公告**的全部报告期
     - latest_as_of(ts_code, as_of_date) 返回 as-of 时点最新可见的一期
     - cross_section_as_of(as_of_date)  返回 as-of 时点的全市场横截面

【为何所有查询都必须带 available_date 条件】
   报告期早于 as-of 不代表当时已知：一家公司 4 月才公告一季报，
   在 3 月底做历史回测时若读到该期数据，即为未来信息泄漏（look-ahead bias）。
   本层把 available_date <= as_of 作为不可省略的前置条件。

【主键】
   (ts_code, report_period, source)：同一期允许多个数据源并存，
   便于后续交叉验证（data quality），互不覆盖。
"""

import logging

from stocklab.domain import (
    BALANCE_SHEET_COLUMNS,
    CASHFLOW_STATEMENT_COLUMNS,
    FINANCIAL_INDICATOR_COLUMNS,
    INCOME_STATEMENT_COLUMNS,
)
from stocklab.persistence.repository.base import BaseRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "IncomeStatementRepository",
    "BalanceSheetRepository",
    "CashflowStatementRepository",
    "FinancialIndicatorRepository",
]


class _StatementRepository(BaseRepository):
    """
    基本面表通用 Repository（内部基类）

    【职责】
      提供四张基本面表共用的 UPSERT 与 Point-in-Time 查询，
      子类只需声明 _TABLE_NAME 与 _COLUMNS。

    【查询约定】
      所有 as-of 查询都以 available_date 为可见性判据，禁止只按 report_period 过滤。
    """

    _TABLE_NAME = ""
    _COLUMNS = ()

    def upsert(self, statement_df):
        """
        批量写入或更新基本面数据（幂等操作）

        Args:
            statement_df (pd.DataFrame): 基本面表，列序可任意，须含全部契约列

        Returns:
            int: 实际写入的行数
        """
        update_columns = [c for c in self._COLUMNS if c not in ("ts_code", "report_period", "source")]
        return super().upsert(
            statement_df,
            conflict_columns=["ts_code", "report_period", "source"],
            update_columns=update_columns
        )

    def find_by_code(self, ts_code, source=None):
        """
        查询某只证券的全部基本面数据（按报告期升序）

        Args:
            ts_code (str): 证券代码，如 "600519.SH"
            source (str, optional): 数据源过滤，如 "eastmoney"

        Returns:
            pd.DataFrame: 基本面表
        """
        conn = self._db.get_connection()
        sql = f"SELECT * FROM {self._TABLE_NAME} WHERE ts_code = ?"
        params = [ts_code]
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY report_period"
        return conn.execute(sql, params).fetchdf()

    def find_as_of(self, ts_code, as_of_date, source=None):
        """
        查询 as-of 时点已公告的全部报告期（Point-in-Time 安全）

        Args:
            ts_code (str): 证券代码
            as_of_date (str 或 datetime.date): 历史时点
            source (str, optional): 数据源过滤

        Returns:
            pd.DataFrame: available_date <= as_of_date 的行，按报告期升序
        """
        conn = self._db.get_connection()
        sql = f"""
            SELECT * FROM {self._TABLE_NAME}
            WHERE ts_code = ?
              AND available_date <= ?
        """
        params = [ts_code, as_of_date]
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY report_period"
        return conn.execute(sql, params).fetchdf()

    def latest_as_of(self, ts_code, as_of_date, source=None):
        """
        查询 as-of 时点最新可见的一期（Point-in-Time 安全）

        Args:
            ts_code (str): 证券代码
            as_of_date (str 或 datetime.date): 历史时点
            source (str, optional): 数据源过滤

        Returns:
            pd.DataFrame: 0 或 1 行；无可见数据时为空表
        """
        conn = self._db.get_connection()
        sql = f"""
            SELECT * FROM {self._TABLE_NAME}
            WHERE ts_code = ?
              AND available_date <= ?
        """
        params = [ts_code, as_of_date]
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY report_period DESC, announce_date DESC LIMIT 1"
        return conn.execute(sql, params).fetchdf()

    def cross_section_as_of(self, as_of_date, report_period=None, source=None):
        """
        查询 as-of 时点全市场已公告的横截面（因子/筛选的取数入口）

        Args:
            as_of_date (str 或 datetime.date): 历史时点
            report_period (str, optional): 限定报告期，如 "2024-03-31"
            source (str, optional): 数据源过滤

        Returns:
            pd.DataFrame: 每个证券一行的最新可见数据
        """
        conn = self._db.get_connection()
        sql = f"""
            SELECT * FROM {self._TABLE_NAME}
            WHERE available_date <= ?
        """
        params = [as_of_date]
        if report_period:
            sql += " AND report_period = ?"
            params.append(report_period)
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += """
            QUALIFY row_number() OVER (
                PARTITION BY ts_code
                ORDER BY report_period DESC, announce_date DESC, source
            ) = 1
        """
        return conn.execute(sql, params).fetchdf()


class IncomeStatementRepository(_StatementRepository):
    """利润表数据访问类"""

    _TABLE_NAME = "fundamental.income_statements"
    _COLUMNS = INCOME_STATEMENT_COLUMNS


class BalanceSheetRepository(_StatementRepository):
    """资产负债表数据访问类"""

    _TABLE_NAME = "fundamental.balance_sheets"
    _COLUMNS = BALANCE_SHEET_COLUMNS


class CashflowStatementRepository(_StatementRepository):
    """现金流量表数据访问类"""

    _TABLE_NAME = "fundamental.cashflow_statements"
    _COLUMNS = CASHFLOW_STATEMENT_COLUMNS


class FinancialIndicatorRepository(_StatementRepository):
    """财务指标（质量 / 成长）数据访问类"""

    _TABLE_NAME = "fundamental.financial_indicators"
    _COLUMNS = FINANCIAL_INDICATOR_COLUMNS
