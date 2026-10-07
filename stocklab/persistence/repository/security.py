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


from stocklab.domain import SECURITY_COLUMNS
from stocklab.persistence.repository.base import BaseRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "SecurityRepository",
]


class SecurityRepository(BaseRepository):
    """
    证券基础信息数据访问类

    【职责】
      1. 批量写入 / 更新证券基础信息（含生命周期字段 list_date / delist_date / status）
      2. 查询证券列表与按代码查询
      3. 推导 as-of 历史股票池 universe(as_of_date)（防幸存者偏差）
    """

    _TABLE_NAME = "reference.securities"
    _COLUMNS = SECURITY_COLUMNS

    def upsert(self, securities_df):
        """
        批量写入或更新证券身份信息（幂等操作）

        【字段归属】
           本方法对应「证券名录」阶段（东财代码表），只更新身份字段：
           symbol / name / exchange / market / industry / area / is_hs。
           list_date / delist_date / status 归生命周期阶段（交易所日历）所有，
           名录不提供这三列的值，若在此一并更新会把生命周期阶段回填的
           上市日与退市日重新抹成 NULL（名录帧里这两列恒为空）。

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
                "area", "is_hs"
            ]
        )

    def upsert_lifecycle(self, lifecycle_df):
        """
        批量写入或更新证券生命周期字段（幂等操作）

        【字段归属】
           对应「证券生命周期」阶段（沪深北上市/退市日历）：
           只更新 list_date / delist_date / status；退市而不在名录中的证券
           会以完整行插入（否则退市股永远进不了股票池，产生幸存者偏差）。

        Args:
            lifecycle_df (pd.DataFrame): 含全部契约列的证券帧（通常由
                                         normalization.exchange.merge_lifecycle 产出）

        Returns:
            int: 实际写入的行数
        """
        return super().upsert(
            lifecycle_df,
            conflict_columns=["ts_code"],
            update_columns=["list_date", "delist_date", "status"]
        )

    def universe(self, as_of_date):
        """
        推导某个历史时点真实存在的证券集合（as-of 股票池）

        【口径】
           上市日 <= as_of 且（未定退市日 或 退市日 > as_of）。
           - 绝不按「当前仍在市」过滤：退市股票只要在 as_of 时点仍上市就必须
             进入样本，否则历史研究产生幸存者偏差；
           - list_date 未知（NULL）时视为「无法证明其尚未上市」，保守纳入样本
             并依赖 delist_date 排除，避免把老股误踢出历史样本。

        Args:
            as_of_date (str 或 datetime.date): 历史时点，如 "2018-01-01"

        Returns:
            pd.DataFrame: 该时点的证券集合（列与契约一致）
        """
        conn = self._db.get_connection()
        return conn.execute(
            f"""
            SELECT {", ".join(self._COLUMNS)}
            FROM {self._TABLE_NAME}
            WHERE (list_date IS NULL OR list_date <= ?)
              AND (delist_date IS NULL OR delist_date > ?)
            ORDER BY ts_code
            """,
            [as_of_date, as_of_date],
        ).fetchdf()

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
