#!/usr/bin/env python3
"""
==============================================================================
StockLab - 研究快照 Repository (stocklab.persistence.repository.research_snapshot)
==============================================================================

【模块职责】
   封装研究快照两张表的读写：
     - ResearchSnapshotRepository  research.snapshots（快照元数据 + 可复现 spec）
     - SnapshotResultRepository    research.snapshot_results（逐股判定结果）

【不可变语义】
   快照只 INSERT、不 UPDATE：复现结论必须建立在不可变记录之上。
   同一 snapshot_id 重复写入视为 bug，直接拒绝并记 ERROR（不覆盖旧结果）。
"""

import logging

import duckdb

from stocklab.domain import (
    SNAPSHOT_COLUMNS,
    SNAPSHOT_RESULT_COLUMNS,
    align_columns,
    DataContractError,
)
from stocklab.persistence.repository.base import BaseRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "ResearchSnapshotRepository",
    "SnapshotResultRepository",
]


class ResearchSnapshotRepository(BaseRepository):
    """研究快照元数据数据访问类（只插入，不可覆盖）"""

    _TABLE_NAME = "research.snapshots"
    _COLUMNS = SNAPSHOT_COLUMNS

    def insert(self, snapshot_df):
        """
        写入一条研究快照（不可变：重复 snapshot_id 直接拒绝）

        Args:
            snapshot_df (pd.DataFrame): 快照行，列序可任意，须含全部契约列

        Returns:
            int: 实际写入的行数；空数据、契约违约或主键冲突返回 0
        """
        if snapshot_df is None or snapshot_df.empty:
            _logger.warning("%s: insert 接收到空数据，跳过写入", self._TABLE_NAME)
            return 0

        try:
            aligned = align_columns(snapshot_df, self._COLUMNS, self._TABLE_NAME)
        except DataContractError as error:
            _logger.error("%s 写入被拒绝: %s", self._TABLE_NAME, error)
            return 0

        column_list = ", ".join(self._COLUMNS)
        conn = self._db.get_connection()
        tmp_table = "_research_snapshots_tmp"
        conn.register(tmp_table, aligned)
        try:
            conn.execute(
                f"INSERT INTO {self._TABLE_NAME} ({column_list}) "
                f"SELECT {column_list} FROM {tmp_table}"
            )
            return len(aligned)
        except duckdb.Error as error:
            _logger.error(
                "%s 写入失败（可能 snapshot_id 重复，快照禁止覆盖）: %s",
                self._TABLE_NAME,
                error,
            )
            return 0
        finally:
            conn.unregister(tmp_table)

    def find(self, snapshot_id):
        """
        按 snapshot_id 查询一条快照

        Args:
            snapshot_id (str): 快照编号

        Returns:
            dict: 快照行（spec_json 保持字符串，解析交给 research 层）；不存在返回 None
        """
        conn = self._db.get_connection()
        row = conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE snapshot_id = ?",
            [snapshot_id],
        ).fetchdf()
        if row.empty:
            return None
        return row.iloc[0].to_dict()

    def find_all(self):
        """
        查询全部快照（按创建时间倒序）

        Returns:
            pd.DataFrame: 快照列表
        """
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} ORDER BY created_at DESC"
        ).fetchdf()

    def count(self):
        """快照总数"""
        conn = self._db.get_connection()
        result = conn.execute(f"SELECT COUNT(*) FROM {self._TABLE_NAME}").fetchone()
        return int(result[0]) if result else 0


class SnapshotResultRepository(BaseRepository):
    """研究快照逐股结果数据访问类（只插入，不可覆盖）"""

    _TABLE_NAME = "research.snapshot_results"
    _COLUMNS = SNAPSHOT_RESULT_COLUMNS

    def insert(self, results_df):
        """
        批量写入逐股判定结果

        Args:
            results_df (pd.DataFrame): 结果表，须含全部契约列

        Returns:
            int: 实际写入的行数
        """
        if results_df is None or results_df.empty:
            _logger.warning("%s: insert 接收到空数据，跳过写入", self._TABLE_NAME)
            return 0

        try:
            aligned = align_columns(results_df, self._COLUMNS, self._TABLE_NAME)
        except DataContractError as error:
            _logger.error("%s 写入被拒绝: %s", self._TABLE_NAME, error)
            return 0

        column_list = ", ".join(self._COLUMNS)
        conn = self._db.get_connection()
        tmp_table = "_snapshot_results_tmp"
        conn.register(tmp_table, aligned)
        try:
            conn.execute(
                f"INSERT INTO {self._TABLE_NAME} ({column_list}) "
                f"SELECT {column_list} FROM {tmp_table}"
            )
            return len(aligned)
        except duckdb.Error as error:
            _logger.error("%s 写入失败（可能与已有快照重复）: %s", self._TABLE_NAME, error)
            return 0
        finally:
            conn.unregister(tmp_table)

    def find_by_snapshot(self, snapshot_id):
        """
        查询某个快照的全部逐股结果

        Args:
            snapshot_id (str): 快照编号

        Returns:
            pd.DataFrame: 逐股结果（按 ts_code 升序）
        """
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE snapshot_id = ? ORDER BY ts_code",
            [snapshot_id],
        ).fetchdf()

    def count(self, snapshot_id):
        """某个快照的逐股结果条数"""
        conn = self._db.get_connection()
        result = conn.execute(
            f"SELECT COUNT(*) FROM {self._TABLE_NAME} WHERE snapshot_id = ?",
            [snapshot_id],
        ).fetchone()
        return int(result[0]) if result else 0
