#!/usr/bin/env python3
"""
==============================================================================
StockLab - 证券基础信息 Repository (stocklab.datasource.repository.security)
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

from stocklab.datasource.storage.duckdb import Database

_logger = logging.getLogger(__name__)

__all__ = [
    "SecurityRepository",
]


class SecurityRepository:
    """
    证券基础信息数据访问类

    【职责】
      1. 批量写入 / 更新证券基础信息
      2. 查询证券列表
      3. 按代码查询单只证券信息
    """

    _TABLE_NAME = "reference.securities"

    def __init__(self, database=None):
        """
        初始化 Repository

        Args:
            database (Database, optional): 数据库管理器实例，默认创建新实例
        """
        self._db = database if database else Database()

    def upsert(self, securities_df):
        """
        批量写入或更新证券基础信息（幂等操作）

        使用 DuckDB 的 INSERT INTO ... ON CONFLICT DO UPDATE 语法实现 UPSERT，
        重复执行不会产生重复记录。

        Args:
            securities_df (pd.DataFrame): 证券基础信息表，必须包含 ts_code 列

        Returns:
            int: 实际写入的行数
        """
        if securities_df is None or securities_df.empty:
            _logger.warning("upsert 接收到空数据，跳过写入")
            return 0

        conn = self._db.get_connection()

        # 注册 DataFrame 为临时表，然后执行 UPSERT
        conn.register("_securities_tmp", securities_df)

        try:
            conn.execute(
                f"""
                INSERT INTO {self._TABLE_NAME}
                SELECT * FROM _securities_tmp
                ON CONFLICT (ts_code) DO UPDATE SET
                    symbol = excluded.symbol,
                    name = excluded.name,
                    exchange = excluded.exchange,
                    market = excluded.market,
                    industry = excluded.industry,
                    area = excluded.area,
                    list_date = excluded.list_date,
                    delist_date = excluded.delist_date,
                    list_status = excluded.list_status,
                    is_hs = excluded.is_hs
                """
            )
            row_count = len(securities_df)
            _logger.info("securities 表 UPSERT 完成: %d 条记录", row_count)
            return row_count
        except Exception as error:
            _logger.error("securities 表 UPSERT 失败: %s", error)
            return 0
        finally:
            conn.unregister("_securities_tmp")

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
