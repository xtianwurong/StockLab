#!/usr/bin/env python3
"""
==============================================================================
StockLab - DuckDB 数据库初始化入口 (stocklab.persistence.storage.schema)
==============================================================================

【模块职责】
   1. 定义默认数据库文件路径 DEFAULT_DB_PATH；
   2. 提供初始化入口 initialize_database()：创建目录 + 触发结构迁移。

【结构变更规则（V2 起强制）】
   全部 DDL 已迁移到 stocklab/persistence/migrations/NNN_*.sql，
   本文件不再定义任何表结构。修改线上数据库结构的唯一方式是新增迁移文件，
   由 SchemaMigrator 比对 sys.schema_version 后按序执行。
"""

import logging
import os

from stocklab.persistence.migrations import SchemaMigrator

_logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DB_PATH",
    "initialize_database",
]


# 默认数据库文件路径（相对于项目根目录）
DEFAULT_DB_PATH = os.path.join("data", "stocklab.duckdb")


def initialize_database(db_path=None):
    """
    初始化 DuckDB 数据库：创建目录，并按版本表执行未应用的结构迁移

    幂等设计：重复执行不会报错；已是最新版本时不做任何 DDL 变更。

    Args:
        db_path (str, optional): 数据库文件路径，默认使用 data/stocklab.duckdb

    Returns:
        str: 实际使用的数据库文件路径
    """
    actual_path = db_path if db_path else DEFAULT_DB_PATH

    # 自动创建父目录
    parent_dir = os.path.dirname(actual_path)
    if parent_dir and not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)
        _logger.info("已创建数据目录: %s", parent_dir)

    applied = SchemaMigrator(actual_path).apply()
    if applied:
        _logger.info("数据库初始化完成: %s（应用 %d 个迁移）", actual_path, applied)

    return actual_path
