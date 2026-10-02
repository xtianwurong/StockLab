#!/usr/bin/env python3
"""
==============================================================================
StockLab - 证券基础信息 Repository (stocklab.persistence.repository.security)
==============================================================================

【模块职责】
   封装 reference.securities 表的读写操作，对外提供简洁的数据访问接口。
   不涉及任何外部数据源（AkShare / BaoStock / Tencent），只负责 DataFrame <-> DuckDB 的映射。

【设计原则】
   - 批量写入：利用 DuckDB 对 pandas 的原生支持进行批量 UPSERT
   - 幂等性：重复写入同一批数据不会产生重复记录
   - NULL 语义保留：不将 NULL / NaN 强制转换为 0
"""

import logging

import pandas as pd

from stocklab.persistence.repository.base import BaseRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "SecurityRepository",
]


class SecurityRepository(BaseRepository):
    """
    证券基础信息数据访问类

    【职责】
      1. 批量写入 / 更新证券基础信息
      2. 查询证券列表
      3. 按代码查询单只证券信息
    """

    _TABLE_NAME = "reference.securities"

    def upsert(self, securities_df):
        """
        批量写入或更新证券基础信息（幂等操作）

        Args:
            securities_df (pd.DataFrame): 证券基础信息表，必须包含 ts_code 列

        Returns:
            int: 实际写入的行数
        """
        return super().upsert(
            securities_df,
            conflict_columns=["ts_code"],
            update_columns=[
                "symbol", "name", "exchange", "market", "industry",
                "area", "list_date", "delist_date", "list_status", "is_hs"
            ]
        )

    def find_all(self):
        """
        查询全部证券基础信息

        Returns:
            pd.DataFrame: 证券基础信息表
        """
        conn = self._db.get_connection()
        result = conn.execute(f"SELECT * FROM {self._TABLE_NAME}").fetchdf()
        return result

    def find_by_code(self, ts_code):
        """
        按证券代码查询单只证券信息

        Args:
            ts_code (str): 证券代码，如 "600519.SH"

        Returns:
            pd.DataFrame: 单只证券信息（0 行或 1 行）
        """
        conn = self._db.get_connection()
        result = conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE ts_code = ?",
            [ts_code],
        ).fetchdf()
        return result

    def count(self):
        """
        查询证券总数

        Returns:
            int: 证券记录总数
        """
        conn = self._db.get_connection()
        result = conn.execute(f"SELECT COUNT(*) FROM {self._TABLE_NAME}").fetchone()
        return int(result[0]) if result else 0
