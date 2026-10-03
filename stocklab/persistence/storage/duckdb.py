#!/usr/bin/env python3
"""
==============================================================================
StockLab - DuckDB 连接管理模块 (stocklab.persistence.storage.duckdb)
==============================================================================

【模块职责】
   封装 DuckDB 连接的生命周期管理，提供简洁的连接获取与释放接口。
   不引入 ORM，不引入复杂抽象，仅做最薄的一层连接包装。

【设计原则】
   - 简单可靠：connect / close 语义清晰
   - 上下文管理：支持 with 语句自动释放连接
   - 集中管理：所有数据库连接通过本模块获取，避免散落在各处
"""

import logging

import duckdb

from stocklab.persistence.storage.schema import DEFAULT_DB_PATH, initialize_database

_logger = logging.getLogger(__name__)

__all__ = [
    "Database",
]


class Database:
    """
    DuckDB 数据库连接管理类

    【职责】
      1. 维护数据库文件路径
      2. 提供连接获取与释放
      3. 支持上下文管理 (with 语句)

    【使用方式】
      with Database() as db:
          conn = db.get_connection()
          conn.execute("SELECT ...")
    """

    def __init__(self, db_path=None):
        """
        初始化数据库管理器

        Args:
            db_path (str, optional): 数据库文件路径，默认 data/stocklab.duckdb
        """
        self._db_path = db_path if db_path else DEFAULT_DB_PATH
        self._connection = None

    def _ensure_initialized(self):
        """确保数据库已初始化：建目录 + 比对版本表执行未应用的迁移（幂等）"""
        initialize_database(self._db_path)

    def get_connection(self):
        """
        获取 DuckDB 连接（延迟初始化，首次调用时自动建立连接）

        Returns:
            duckdb.DuckDBPyConnection: 数据库连接对象
        """
        if self._connection is None:
            self._ensure_initialized()
            self._connection = duckdb.connect(self._db_path)
            _logger.debug("已建立数据库连接: %s", self._db_path)
        return self._connection

    def close(self):
        """关闭数据库连接并释放资源"""
        if self._connection is not None:
            self._connection.close()
            self._connection = None
            _logger.debug("已关闭数据库连接: %s", self._db_path)

    def __enter__(self):
        """上下文管理入口"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理出口，确保连接被释放"""
        self.close()
        return False
