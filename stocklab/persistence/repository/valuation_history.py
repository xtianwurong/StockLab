#!/usr/bin/env python3
"""
==============================================================================
StockLab - 历史估值序列 Repository (stocklab.persistence.repository.valuation_history)
==============================================================================

【模块职责】
   封装 market.valuation_history 表的读写操作，对外提供简洁的数据访问接口。
   不涉及任何外部数据源，只负责 DataFrame <-> DuckDB 的映射。

【与 market.daily_valuations 的区别（务必分清）】
     daily_valuations  = 全市场「单日快照」，每只股票每个交易日一行，
                         用于当下估值横截面比较（如全市场 PE 分布）；
     valuation_history = 单只股票「跨年序列」，一次可含数千个交易日，
                         用于计算历史分位（当前值在过去 N 年中的位置）。
   二者服务的分析问题不同，不可互相替代。

【设计原则】
   - 批量写入：利用 DuckDB 对 pandas 的原生支持进行批量 UPSERT
   - 幂等性：重复写入同一批数据不会产生重复记录
   - NULL 语义保留：估值字段允许 NULL（亏损或无数据），不强制填充默认值
==============================================================================
"""

import logging

import pandas as pd

from stocklab.domain import VALUATION_HISTORY_COLUMNS
from stocklab.persistence.repository.base import BaseRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "ValuationHistoryRepository",
]


class ValuationHistoryRepository(BaseRepository):
    """
    历史估值序列数据访问类
    """

    _TABLE_NAME = "market.valuation_history"
    _COLUMNS = VALUATION_HISTORY_COLUMNS

    def upsert(self, history_df):
        """
        批量写入或更新历史估值序列（幂等操作）

        使用 (ts_code, trade_date) 复合主键，重复执行不会产生重复记录。

        Args:
            history_df (pd.DataFrame): 历史估值表，必须包含 ts_code 与 trade_date 列

        Returns:
            int: 实际写入的行数
        """
        return super().upsert(
            history_df,
            conflict_columns=["ts_code", "trade_date"],
            update_columns=["pe_ttm", "pe_static", "pb", "ps", "pcf"]
        )

    def find_by_code(self, ts_code, start_date=None, end_date=None):
        """
        按证券代码查询历史估值序列（按时间升序）

        Args:
            ts_code (str): 证券代码，如 "600519.SH"
            start_date (str, optional): 起始日期，格式 "YYYY-MM-DD"
            end_date (str, optional): 结束日期，格式 "YYYY-MM-DD"

        Returns:
            pd.DataFrame: 历史估值表
        """
        conn = self._db.get_connection()
        sql = f"SELECT * FROM {self._TABLE_NAME} WHERE ts_code = ?"
        params = [ts_code]

        if start_date:
            sql += " AND trade_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND trade_date <= ?"
            params.append(end_date)

        sql += " ORDER BY trade_date"

        return conn.execute(sql, params).fetchdf()

    def find_latest_date(self, ts_code):
        """
        查询某只证券最新一个有估值记录的交易日

        Args:
            ts_code (str): 证券代码，如 "600519.SH"

        Returns:
            str: YYYY-MM-DD 形式的日期；无数据时返回空字符串
        """
        conn = self._db.get_connection()
        result = conn.execute(
            f"SELECT MAX(trade_date) FROM {self._TABLE_NAME} WHERE ts_code = ?",
            [ts_code],
        ).fetchone()
        if result is None or result[0] is None:
            return ""
        return str(result[0])

    def count(self):
        """
        查询历史估值记录总数

        Returns:
            int: 记录总数
        """
        conn = self._db.get_connection()
        result = conn.execute(f"SELECT COUNT(*) FROM {self._TABLE_NAME}").fetchone()
        return int(result[0]) if result else 0
