#!/usr/bin/env python3
"""
==============================================================================
StockLab - Schema 迁移执行器 (stocklab.persistence.migrations.runner)
==============================================================================

【模块职责】
   读取本目录下的 NNN_*.sql 迁移文件，比对 sys.schema_version 版本表，
   按序执行未应用的迁移并记录版本，实现「启动时检查版本 → 自动执行 migration
   → 升级数据库」。

【设计原则】
   - 迁移文件是数据库结构的唯一真相：任何结构变更新增 NNN_*.sql，
     禁止再修改 storage/schema.py 直接改变线上库结构；
   - 每个迁移只执行一次（版本表记录），执行顺序按三位序号升序；
   - 001 是基线迁移（= 机制上线前的既有结构），全部 IF NOT EXISTS，
     因此老库补跑不会报错、新库与老库走完全相同的升级路径；
   - 迁移执行失败即中止并抛出，不记录版本，修复后重跑即可（须保证文件自身幂等）。
"""

import datetime
import logging
import os
import re

import duckdb

_logger = logging.getLogger(__name__)

__all__ = [
    "SchemaMigrator",
    "MIGRATIONS_DIR",
]

# 迁移文件所在目录（与本模块同目录）
MIGRATIONS_DIR = os.path.dirname(os.path.abspath(__file__))

# 版本表引导 DDL：版本表必须先于任何迁移文件存在
_BOOTSTRAP_DDL = """
CREATE SCHEMA IF NOT EXISTS sys;
CREATE TABLE IF NOT EXISTS sys.schema_version (
    version    INTEGER PRIMARY KEY,
    name       VARCHAR,
    applied_at TIMESTAMP
);
"""

# 迁移文件命名：三位序号 + 下划线 + 小写描述，如 001_initial.sql
_MIGRATION_PATTERN = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class SchemaMigrator:
    """
    数据库结构迁移执行器

    【职责】
      1. 引导 sys.schema_version 版本表；
      2. 扫描迁移目录，找出未应用的迁移；
      3. 按序执行并记录版本，返回本次应用的迁移条数。

    【使用方式】
        migrator = SchemaMigrator("data/stocklab.duckdb")
        applied = migrator.apply()
    """

    def __init__(self, db_path, migrations_dir=None):
        """
        初始化迁移执行器

        Args:
            db_path (str): 数据库文件路径
            migrations_dir (str, optional): 迁移文件目录，默认为包内 migrations 目录
        """
        self._db_path = db_path
        self._migrations_dir = migrations_dir if migrations_dir else MIGRATIONS_DIR

    def apply(self):
        """
        检查版本并执行全部未应用的迁移

        Returns:
            int: 本次实际应用的迁移条数（已是最新时为 0）

        Raises:
            RuntimeError: 迁移文件序号重复时抛出
            duckdb.Error: 迁移 SQL 执行失败时抛出（版本不记录，修复后可重跑）
        """
        conn = duckdb.connect(self._db_path)
        try:
            conn.execute(_BOOTSTRAP_DDL)
            applied_versions = self._applied_versions(conn)
            pending = [
                item for item in self._discover() if item[0] not in applied_versions
            ]

            for version, name, file_path in pending:
                with open(file_path, "r", encoding="utf-8") as handle:
                    sql = handle.read()
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO sys.schema_version (version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    [version, name, datetime.datetime.now()],
                )
                _logger.info("已应用迁移 %03d_%s", version, name)

            if not pending:
                _logger.debug("数据库结构已是最新版本（version=%d）", max(applied_versions) if applied_versions else 0)
            else:
                _logger.info(
                    "数据库结构迁移完成: 应用 %d 个迁移，当前版本 %d",
                    len(pending),
                    max([version for version, _, _ in self._discover()]),
                )
            return len(pending)
        finally:
            conn.close()

    def _applied_versions(self, conn):
        """
        读取版本表中已应用的迁移序号

        Args:
            conn (duckdb.DuckDBPyConnection): 已建好版本表的连接

        Returns:
            set: 已应用的迁移序号集合
        """
        rows = conn.execute("SELECT version FROM sys.schema_version").fetchall()
        return set(row[0] for row in rows)

    def _discover(self):
        """
        扫描迁移目录，按序号升序返回全部可用迁移

        Returns:
            list: [(version, name, file_path), ...]，序号升序

        Raises:
            RuntimeError: 存在重复迁移序号时抛出
        """
        entries = []
        seen_versions = {}
        for file_name in sorted(os.listdir(self._migrations_dir)):
            match = _MIGRATION_PATTERN.match(file_name)
            if match is None:
                continue
            version = int(match.group(1))
            if version in seen_versions:
                raise RuntimeError(
                    "迁移序号重复: %03d 已被 %s 占用，%s 再次使用"
                    % (version, seen_versions[version], file_name)
                )
            seen_versions[version] = file_name
            name = file_name[4:-4]  # 去掉 "001_" 前缀与 ".sql" 后缀
            entries.append((version, name, os.path.join(self._migrations_dir, file_name)))
        entries.sort(key=lambda item: item[0])
        return entries
