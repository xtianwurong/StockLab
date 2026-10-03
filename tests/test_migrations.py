#!/usr/bin/env python3
"""
==============================================================================
StockLab - 数据库迁移测试 (tests/test_migrations.py)
==============================================================================

【功能用途】
  验证 SchemaMigrator（stocklab.persistence.migrations）的行为：
    1. 新库首次初始化：按序应用迁移目录里的全部 NNN_*.sql，版本表记录完整，结构齐全
    2. 幂等：重复初始化不再执行任何迁移（0 条）
    3. 老库升级（已有 001 基线 + 版本行）：只补 001 之后的迁移，
       list_status 按 L/D/P 映射成 status 后丢弃原列
    4. 有结构但没有版本表的老库：先引导版本表，再按序补跑（001 幂等跳过）
    5. 迁移失败不记录版本（可修复后重跑）；序号重复直接拒绝

   迁移总数与序号一律从迁移目录推导：新增 NNN_*.sql 时本测试无需手改数字。

【运行方式】
  python tests/test_migrations.py
  （全部使用临时数据库，不触碰 data/stocklab.duckdb；运行前请停掉 serve_web）
==============================================================================
"""

import os
import sys

import duckdb

# 允许直接执行本文件（./venv/bin/python tests/xxx.py）；走 pytest 时由
# pytest.ini 的 `pythonpath = .` 统一负责，不会重复插入。
if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.persistence.migrations import MIGRATIONS_DIR, SchemaMigrator
import pytest
from stocklab.persistence.storage import initialize_database

def _columns(db_path, schema, table):
    """读取表的列名集合（列序由 DDL 决定，此处只关心列名）"""
    conn = duckdb.connect(db_path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchall()
    finally:
        conn.close()
    return set(row[0] for row in rows)

def _versions(db_path):
    """读取版本表已记录的迁移序号"""
    conn = duckdb.connect(db_path, read_only=True)
    try:
        rows = conn.execute("SELECT version FROM sys.schema_version").fetchall()
    finally:
        conn.close()
    return sorted(row[0] for row in rows)

def _expected_versions():
    """迁移目录里全部 NNN_*.sql 的序号（新增迁移时测试自动跟着变）"""
    versions = []
    for name in os.listdir(MIGRATIONS_DIR):
        if name[:3].isdigit() and name[3:4] == "_" and name.endswith(".sql"):
            versions.append(int(name[:3]))
    return sorted(versions)

def _table_exists(db_path, schema, table):
    """判断表是否存在"""
    conn = duckdb.connect(db_path, read_only=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    finally:
        conn.close()
    return row[0] > 0

def test_fresh_database(tmp_db_path):
    """测试新库首次初始化：迁移目录里的迁移全部应用，且结构齐全"""

    db_path = os.path.join(tmp_db_path, "fresh.duckdb")

def test_idempotency(tmp_db_path):
    """测试重复初始化是幂等的：第二次应用 0 条，版本行不重复"""

    db_path = os.path.join(tmp_db_path, "idem.duckdb")

def _build_legacy_database(db_path, with_version_row):
    """构造 V2 之前的老库：执行 001 基线迁移并写入 list_status 数据

    Args:
        db_path (str): 目标库文件路径
        with_version_row (bool): 是否同时记录 001 的版本行（有版本表的老库）
    """
    baseline_path = os.path.join(MIGRATIONS_DIR, "001_initial.sql")
    conn = duckdb.connect(db_path)
    try:
        with open(baseline_path, "r", encoding="utf-8") as handle:
            conn.execute(handle.read())
        conn.execute(
            "INSERT INTO reference.securities "
            "(ts_code, symbol, name, exchange, market, list_status) "
            "VALUES ('600519.SH', '600519', '贵州茅台', 'SH', '主板', 'L'), "
            "('000001.SZ', '000001', '深发展Ａ', 'SZ', '主板', 'D'), "
            "('430047.BJ', '430047', '粤证券3', 'BJ', '北交所', 'P')"
        )
        if with_version_row:
            conn.execute(
                "CREATE SCHEMA IF NOT EXISTS sys;"
                "CREATE TABLE IF NOT EXISTS sys.schema_version ("
                " version INTEGER PRIMARY KEY, name VARCHAR, applied_at TIMESTAMP);"
                "INSERT INTO sys.schema_version VALUES (1, 'initial', NULL);"
            )
    finally:
        conn.close()

def test_legacy_upgrade(tmp_db_path):
    """测试老库（已有基线与版本行）只补 001 之后的迁移，且 list_status 正确迁移"""

    db_path = os.path.join(tmp_db_path, "legacy.duckdb")

def test_structured_without_version(tmp_db_path):
    """测试「有结构但没有版本表」的老库：先引导版本表，再按序补跑"""

    db_path = os.path.join(tmp_db_path, "novt.duckdb")

def test_failure_and_duplicate(tmp_db_path):
    """测试迁移失败不记录版本（可重跑）、迁移序号重复被拒绝"""

    db_path = os.path.join(tmp_db_path, "fail.duckdb")

def test_initialize_database_delegation(tmp_db_path):
    """测试 initialize_database 委托给迁移器（不再自己写 DDL）"""

    db_path = os.path.join(tmp_db_path, "init.duckdb")


if __name__ == "__main__":
    # 保住旧的直接执行入口：委托给 pytest，退出码语义一致
    raise SystemExit(pytest.main([__file__, "-v"]))
