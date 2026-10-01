"""
StockLab 数据存储层 (stocklab.persistence.storage)

【模块职责】
   本地 DuckDB 的连接管理与 Schema 定义，不含任何业务数据映射逻辑。
"""

from .duckdb import Database
from .schema import DEFAULT_DB_PATH, initialize_database

__all__ = [
    "Database",
    "DEFAULT_DB_PATH",
    "initialize_database",
]
