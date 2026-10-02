#!/usr/bin/env python3
"""
==============================================================================
StockLab - Repository 基类 (stocklab.persistence.repository.base)
==============================================================================

【模块职责】
   封装 Repository 层的通用逻辑，减少重复代码。
   所有 Repository 类应继承本基类。

【设计原则】
   - 模板方法基类：子类只需指定表名、冲突列与更新列
   - 统一异常处理：避免各 Repository 重复编写 try/except
   - 统一日志格式：便于问题定位
"""

import logging

import pandas as pd
import duckdb

from stocklab.persistence.storage.duckdb import Database

_logger = logging.getLogger(__name__)

__all__ = [
    "BaseRepository",
]


class BaseRepository:
    """
    Repository 基类

    【职责】
      1. 封装通用的 UPSERT 逻辑
      2. 统一异常处理与日志格式
      3. 管理数据库连接

    【子类约定】
      子类必须定义以下类属性：
        - _TABLE_NAME: 目标表名（如 "reference.securities"）
    """

    _TABLE_NAME = ""

    def __init__(self, database=None):
        """
        初始化 Repository

        Args:
            database (Database, optional): 数据库管理器实例，默认创建新实例
        """
        self._db = database if database else Database()

    def upsert(self, df: pd.DataFrame, conflict_columns: list, update_columns: list) -> int:
        """
        通用 UPSERT 方法

        使用 DuckDB 的 INSERT INTO ... ON CONFLICT DO UPDATE 语法实现 UPSERT，
        重复执行不会产生重复记录。

        Args:
            df (pd.DataFrame): 待写入的数据表
            conflict_columns (list): 冲突检测列名列表
            update_columns (list): 需要更新的列名列表

        Returns:
            int: 实际写入的行数；失败返回 0
        """
        if df is None or df.empty:
            _logger.warning("%s: upsert 接收到空数据，跳过写入", self._TABLE_NAME)
            return 0

        conn = self._db.get_connection()
        tmp_table = f"_{self._TABLE_NAME.replace('.', '_')}_tmp"
        conn.register(tmp_table, df)

        try:
            conflict_str = ", ".join(conflict_columns)
            update_str = ", ".join(
                f"{col} = excluded.{col}" for col in update_columns
            )
            conn.execute(f"""
                INSERT INTO {self._TABLE_NAME}
                SELECT * FROM {tmp_table}
                ON CONFLICT ({conflict_str}) DO UPDATE SET {update_str}
            """)
            row_count = len(df)
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
