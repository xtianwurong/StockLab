#!/usr/bin/env python3
"""
==============================================================================
StockLab - 指数成分股 Repository (stocklab.persistence.repository.index_membership)
==============================================================================

【模块职责】
   封装 reference.index_memberships 表的读写操作。
   记录「某只股票在某个时点属于哪些指数」，不涉及任何外部数据源。

【本表解决的选股问题】
   行业分类数据（reference.securities.industry）在部分数据源不可用时，
   指数成分可作为「同业分组」的近似替代维度：
     - 同属沪深300 的 300 只股票构成一个可比样本池；
     - 低估值判断可做指数内横向比较，替代「行业平均估值」；
     - 全市场预筛时可先在指数成分内缩样本，显著降低计算量。

【为何按 effective_date 分区而非只存当前状态】
   指数成分会随定期调样变化（沪深300 每年调整两次）。
   保留生效日期可回溯「某只股票在历史某时点属于哪个指数」，
   避免用今天的成分去解释历史估值分位。

【设计原则】
   - 幂等：同一批数据重复写入行数不变
   - 保留多次调样：同一 ts_code + index_code 可有多条记录（不同生效日）
==============================================================================
"""

import logging

import pandas as pd

from stocklab.persistence.repository.base import BaseRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "IndexMembershipRepository",
]


class IndexMembershipRepository(BaseRepository):
    """
    指数成分股数据访问类
    """

    _TABLE_NAME = "reference.index_memberships"

    def upsert(self, membership_df):
        """
        批量写入或更新指数成分（幂等操作）

        使用 (ts_code, index_code, effective_date) 复合主键，
        重复同步同一期成分不会产生重复记录。

        Args:
            membership_df (pd.DataFrame): 指数成分表，
                                          必须包含 ts_code / index_code / effective_date

        Returns:
            int: 实际写入的行数
        """
        return super().upsert(
            membership_df,
            conflict_columns=["ts_code", "index_code", "effective_date"],
            update_columns=["index_name"]
        )

    def find_by_index(self, index_code, effective_date=None):
        """
        查询某指数的成分股

        Args:
            index_code (str): 指数代码，如 "000300"
            effective_date (str, optional): 指定生效日期 YYYY-MM-DD；
                                            缺省时取该指数最新的生效日

        Returns:
            pd.DataFrame: 指数成分表，按 ts_code 升序
        """
        conn = self._db.get_connection()

        if effective_date is None:
            latest = self.find_latest_date(index_code)
            if not latest:
                return pd.DataFrame()
            effective_date = latest

        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} "
            "WHERE index_code = ? AND effective_date = ? ORDER BY ts_code",
            [index_code, effective_date],
        ).fetchdf()

    def find_by_code(self, ts_code):
        """
        查询某只股票所属的全部指数（跨所有生效期）

        Args:
            ts_code (str): 证券代码，如 "600519.SH"

        Returns:
            pd.DataFrame: 该股票的指数归属记录
        """
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE ts_code = ? "
            "ORDER BY index_code, effective_date DESC",
            [ts_code],
        ).fetchdf()

    def find_latest_date(self, index_code):
        """
        查询某指数最新的成分生效日期

        Args:
            index_code (str): 指数代码

        Returns:
            str: YYYY-MM-DD 形式的日期；无数据时返回空字符串
        """
        conn = self._db.get_connection()
        result = conn.execute(
            f"SELECT MAX(effective_date) FROM {self._TABLE_NAME} WHERE index_code = ?",
            [index_code],
        ).fetchone()
        if result is None or result[0] is None:
            return ""
        return str(result[0])

    def list_indexes(self):
        """
        列出已入库的全部指数及其最新生效日

        Returns:
            pd.DataFrame: 列含 index_code / index_name / effective_date / stock_count
        """
        conn = self._db.get_connection()
        return conn.execute(
            f"""
            SELECT index_code,
                   MAX(index_name) AS index_name,
                   MAX(effective_date) AS effective_date,
                   COUNT(*) AS stock_count
            FROM {self._TABLE_NAME}
            GROUP BY index_code
            ORDER BY stock_count DESC
            """
        ).fetchdf()

    def count(self):
        """
        查询指数成分记录总数

        Returns:
            int: 记录总数
        """
        conn = self._db.get_connection()
        result = conn.execute(f"SELECT COUNT(*) FROM {self._TABLE_NAME}").fetchone()
        return int(result[0]) if result else 0
