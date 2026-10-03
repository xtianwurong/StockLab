#!/usr/bin/env python3
"""
==============================================================================
StockLab - Repository 基类 (stocklab.persistence.repository.base)
==============================================================================

【模块职责】
   封装 Repository 层的通用逻辑，减少重复代码。
   所有 Repository 类应继承本基类。

【设计原则】
   - 模板方法基类：子类只需指定表名、契约列序、冲突列与更新列
   - 显式字段映射：写入前用 align_columns 对齐领域契约，INSERT 显式列出全部列名，
     **不再依赖 DataFrame 列序**（禁止 INSERT INTO t SELECT *）
   - 契约防线：缺列直接拒绝写入并记 ERROR，数据源改版不会静默污染数据库
   - 统一异常处理与日志格式：避免各 Repository 重复编写 try/except
"""

import logging

import duckdb

from stocklab.domain import align_columns, DataContractError
from stocklab.persistence.storage.duckdb import Database

_logger = logging.getLogger(__name__)

__all__ = [
    "BaseRepository",
]


class BaseRepository:
    """
    Repository 基类

    【职责】
      1. 封装通用的 UPSERT 逻辑（契约对齐 + 显式列名写入）
      2. 统一异常处理与日志格式
      3. 管理数据库连接

    【子类约定】
      子类必须定义以下类属性：
        - _TABLE_NAME: 目标表名（如 "reference.securities"）
        - _COLUMNS:    目标表全部列的契约元组（来自 stocklab.domain，与 DDL 同序）
    """

    _TABLE_NAME = ""
    _COLUMNS = ()

    def __init__(self, database=None):
        """
        初始化 Repository

        Args:
            database (Database, optional): 数据库管理器实例，默认创建新实例
        """
        self._db = database if database else Database()

    def upsert(self, df, conflict_columns, update_columns):
        """
        通用 UPSERT 方法：契约对齐 → 显式列名 INSERT ... ON CONFLICT DO UPDATE

        Args:
            df (pd.DataFrame): 待写入的数据表（任意列序，写入前按 _COLUMNS 重排）
            conflict_columns (list): 冲突检测列名列表（必须属于 _COLUMNS）
            update_columns (list): 需要更新的列名列表（必须属于 _COLUMNS）

        Returns:
            int: 实际写入的行数；空数据或契约违约返回 0
        """
        if df is None or df.empty:
            _logger.warning("%s: upsert 接收到空数据，跳过写入", self._TABLE_NAME)
            return 0

        if not self._COLUMNS:
            _logger.error(
                "%s 未声明数据契约 _COLUMNS，拒绝写入", self._TABLE_NAME
            )
            return 0

        invalid_columns = [
            column
            for column in list(conflict_columns) + list(update_columns)
            if column not in self._COLUMNS
        ]
        if invalid_columns:
            _logger.error(
                "%s 的冲突/更新列不属于契约: %s",
                self._TABLE_NAME,
                ", ".join(invalid_columns[:8]),
            )
            return 0

        try:
            aligned = align_columns(df, self._COLUMNS, self._TABLE_NAME)
        except DataContractError as error:
            _logger.error("%s 写入被拒绝: %s", self._TABLE_NAME, error)
            return 0

        column_list = ", ".join(self._COLUMNS)
        select_list = ", ".join(self._COLUMNS)
        conn = self._db.get_connection()
        tmp_table = f"_{self._TABLE_NAME.replace('.', '_')}_tmp"
        conn.register(tmp_table, aligned)

        try:
            conflict_str = ", ".join(conflict_columns)
            update_str = ", ".join(
                f"{col} = excluded.{col}" for col in update_columns
            )
            conn.execute(f"""
                INSERT INTO {self._TABLE_NAME} ({column_list})
                SELECT {select_list} FROM {tmp_table}
                ON CONFLICT ({conflict_str}) DO UPDATE SET {update_str}
            """)
            row_count = len(aligned)
            _logger.info("%s 表 UPSERT 完成: %d 条记录", self._TABLE_NAME, row_count)
            return row_count
        except duckdb.Error as error:
            _logger.error("%s 表 UPSERT 数据库错误: %s", self._TABLE_NAME, error)
            return 0
        except Exception as error:
            _logger.error("%s 表 UPSERT 未知错误: %s", self._TABLE_NAME, error)
            return 0
        finally:
            conn.unregister(tmp_table)
