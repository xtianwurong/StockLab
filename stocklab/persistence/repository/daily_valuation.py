#!/usr/bin/env python3
"""
==============================================================================
StockLab - 每日估值 Repository (stocklab.persistence.repository.daily_valuation)
==============================================================================

【模块职责】
   封装 market.daily_valuations 表的读写操作，对外提供简洁的数据访问接口。
   不涉及任何外部数据源，只负责 DataFrame <-> DuckDB 的映射。

【设计原则】
   - 批量写入：利用 DuckDB 对 pandas 的原生支持进行批量 UPSERT
   - 幂等性：重复写入同一批数据不会产生重复记录
   - NULL 语义保留：PE / PB 等估值字段允许 NULL，不强制填充默认值
"""

import logging

import pandas as pd

from stocklab.persistence.repository.base import BaseRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "DailyValuationRepository",
]


class DailyValuationRepository(BaseRepository):
    """
    每日估值数据访问类

    【职责】
      1. 批量写入 / 更新每日估值
      2. 查询单只股票历史估值
      3. 查询某交易日全市场估值
      4. 按估值条件筛选股票
    """

    _TABLE_NAME = "market.daily_valuations"

    def upsert(self, valuations_df):
        """
        批量写入或更新每日估值（幂等操作）

        使用 (ts_code, trade_date) 复合主键，重复执行不会产生重复记录。

        Args:
            valuations_df (pd.DataFrame): 每日估值表，必须包含 ts_code 和 trade_date 列

        Returns:
            int: 实际写入的行数
        """
        return super().upsert(
            valuations_df,
            conflict_columns=["ts_code", "trade_date"],
            update_columns=[
                "turnover_rate", "turnover_rate_f", "pe", "pe_ttm", "pb",
                "ps", "ps_ttm", "dv_ratio", "dv_ttm", "total_share",
                "float_share", "free_share", "total_mv", "circ_mv"
            ]
        )

    def find_by_code(self, ts_code, start_date=None, end_date=None):
        """
        按股票代码查询历史估值

        Args:
            ts_code (str): 证券代码，如 "600519.SH"
            start_date (str, optional): 起始日期，格式 "YYYY-MM-DD"
            end_date (str, optional): 结束日期，格式 "YYYY-MM-DD"

        Returns:
            pd.DataFrame: 每日估值表
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

        result = conn.execute(sql, params).fetchdf()
        return result

    def find_by_date(self, trade_date):
        """
        按交易日期查询全市场估值

        Args:
            trade_date (str): 交易日期，格式 "YYYY-MM-DD"

        Returns:
            pd.DataFrame: 全市场每日估值表
        """
        conn = self._db.get_connection()
        result = conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE trade_date = ? ORDER BY ts_code",
            [trade_date],
        ).fetchdf()
        return result

    def count(self):
        """
        查询估值记录总数

        Returns:
            int: 记录总数
        """
        conn = self._db.get_connection()
        result = conn.execute(f"SELECT COUNT(*) FROM {self._TABLE_NAME}").fetchone()
        return int(result[0]) if result else 0
