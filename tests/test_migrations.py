#!/usr/bin/env python3
"""
==============================================================================
StockLab - 数据库迁移测试 (tests/test_migrations.py)
==============================================================================

【功能用途】
  验证 SchemaMigrator（stocklab.persistence.migrations）的行为：
    1. 新库首次初始化：按序应用 001/002/003，版本表记录完整，结构齐全
    2. 幂等：重复初始化不再执行任何迁移（0 条）
    3. 老库升级（已有 001 基线 + 版本行）：只补 002/003，
       list_status 按 L/D/P 映射成 status 后丢弃原列
    4. 有结构但没有版本表的老库：先引导版本表，再按序补跑（001 幂等跳过）
    5. 迁移失败不记录版本（可修复后重跑）；序号重复直接拒绝

【运行方式】
  python tests/test_migrations.py
  （全部使用临时数据库，不触碰 data/stocklab.duckdb；运行前请停掉 serve_web）
==============================================================================
"""

import os
import shutil
import sys
import tempfile

# 将项目根目录加入模块搜索路径，保证直接运行本脚本时能 import stocklab
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb

from stocklab.persistence.migrations import MIGRATIONS_DIR, SchemaMigrator
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


def run_fresh_database_test():
    """测试新库首次初始化：3 个迁移全部应用，且结构齐全"""
    print("\n" + "=" * 65)
    print("【阶段一：测试新库首次初始化】")
    print("=" * 65)

    temp_dir = tempfile.mkdtemp(prefix="sl_mig_new_")
    db_path = os.path.join(temp_dir, "fresh.duckdb")
    try:
        applied = SchemaMigrator(db_path).apply()
        assert applied == 3, applied
        assert _versions(db_path) == [1, 2, 3], _versions(db_path)

        # 生命周期字段：只有语义化的 status，list_status 已被 003 取代
        columns = _columns(db_path, "reference", "securities")
        assert "status" in columns and "list_status" not in columns, columns

        # Point-in-Time 基本面四张表 + 生命周期事件表
        for schema, table in [
            ("fundamental", "income_statements"),
            ("fundamental", "balance_sheets"),
            ("fundamental", "cashflow_statements"),
            ("fundamental", "financial_indicators"),
            ("reference", "security_events"),
        ]:
            assert _table_exists(db_path, schema, table), "%s.%s 缺失" % (schema, table)

        print("  -> 首次初始化应用 3 个迁移，版本 [1, 2, 3]，结构齐全")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_idempotency_test():
    """测试重复初始化是幂等的：第二次应用 0 条，版本行不重复"""
    print("\n" + "=" * 65)
    print("【阶段二：测试迁移幂等】")
    print("=" * 65)

    temp_dir = tempfile.mkdtemp(prefix="sl_mig_idem_")
    db_path = os.path.join(temp_dir, "idem.duckdb")
    try:
        SchemaMigrator(db_path).apply()
        for _ in range(2):
            applied = SchemaMigrator(db_path).apply()
            assert applied == 0, applied
        assert _versions(db_path) == [1, 2, 3], _versions(db_path)
        print("  -> 连续重复初始化均应用 0 个迁移，版本行不重复")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


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


def run_legacy_upgrade_test():
    """测试老库（已有基线与版本行）只补跑 002/003，且 list_status 正确迁移"""
    print("\n" + "=" * 65)
    print("【阶段三：测试老库升级（已有 001 版本行）】")
    print("=" * 65)

    temp_dir = tempfile.mkdtemp(prefix="sl_mig_old_")
    db_path = os.path.join(temp_dir, "legacy.duckdb")
    try:
        _build_legacy_database(db_path, with_version_row=True)
        assert _versions(db_path) == [1]

        applied = SchemaMigrator(db_path).apply()
        assert applied == 2, applied
        assert _versions(db_path) == [1, 2, 3], _versions(db_path)

        conn = duckdb.connect(db_path, read_only=True)
        try:
            # L/D/P 必须映射为领域语义，并且原列被丢弃（同一含义不许两列并存）
            rows = conn.execute(
                "SELECT ts_code, status FROM reference.securities ORDER BY ts_code"
            ).fetchall()
            assert rows == [
                ("000001.SZ", "DELISTED"),
                ("430047.BJ", "PAUSED"),
                ("600519.SH", "LISTED"),
            ], rows
            columns = set(
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='reference' AND table_name='securities'"
                ).fetchall()
            )
        finally:
            conn.close()
        assert "list_status" not in columns and "status" in columns, columns

        print("  -> 只补跑 2 个迁移；L/D/P → LISTED/DELISTED/PAUSED，原列已丢弃")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_structured_without_version_test():
    """测试「有结构但没有版本表」的老库：先引导版本表，再按序补跑"""
    print("\n" + "=" * 65)
    print("【阶段四：测试无版本表的老库（先引导再补跑）】")
    print("=" * 65)

    temp_dir = tempfile.mkdtemp(prefix="sl_mig_novt_")
    db_path = os.path.join(temp_dir, "novt.duckdb")
    try:
        _build_legacy_database(db_path, with_version_row=False)
        assert _table_exists(db_path, "reference", "securities")

        applied = SchemaMigrator(db_path).apply()
        # 001 全部 IF NOT EXISTS → 结构已在时跳过执行但同样记录版本
        assert applied == 3, applied
        assert _versions(db_path) == [1, 2, 3], _versions(db_path)

        # 已存在的表不能被重建（否则老数据会丢）
        conn = duckdb.connect(db_path, read_only=True)
        try:
            count = conn.execute("SELECT COUNT(*) FROM reference.securities").fetchone()[0]
        finally:
            conn.close()
        assert count == 3, count
        print("  -> 版本表被引导，3 个迁移补跑，老数据完好")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_failure_and_duplicate_test():
    """测试迁移失败不记录版本（可重跑）、迁移序号重复被拒绝"""
    print("\n" + "=" * 65)
    print("【阶段五：测试失败不记录版本与序号重复检测】")
    print("=" * 65)

    temp_dir = tempfile.mkdtemp(prefix="sl_mig_fail_")
    db_path = os.path.join(temp_dir, "fail.duckdb")
    migrations_dir = os.path.join(temp_dir, "migrations")
    os.makedirs(migrations_dir)
    try:
        # 迁移 1 合法，迁移 2 语法错误 → 执行到 2 抛出且不记录版本 2
        with open(os.path.join(migrations_dir, "001_ok.sql"), "w", encoding="utf-8") as handle:
            handle.write("CREATE SCHEMA IF NOT EXISTS probe; CREATE TABLE probe.t (a INT);")
        with open(os.path.join(migrations_dir, "002_bad.sql"), "w", encoding="utf-8") as handle:
            handle.write("SELECT this_is_not_a_table;")

        try:
            SchemaMigrator(db_path, migrations_dir).apply()
            raise AssertionError("非法迁移 SQL 必须抛出异常")
        except duckdb.Error:
            pass
        assert _versions(db_path) == [1], _versions(db_path)
        print("  -> 迁移失败中止，版本 2 未记录（修复后可重跑）")

        # 修复后重跑：只补 002
        with open(os.path.join(migrations_dir, "002_bad.sql"), "w", encoding="utf-8") as handle:
            handle.write("CREATE TABLE IF NOT EXISTS probe.t2 (b INT);")
        assert SchemaMigrator(db_path, migrations_dir).apply() == 1
        assert _versions(db_path) == [1, 2], _versions(db_path)
        print("  -> 修复后重跑只补应用 1 个迁移，已完成的不重复执行")

        # 序号重复 → 直接拒绝，避免执行顺序不可预测
        with open(os.path.join(migrations_dir, "001_dup.sql"), "w", encoding="utf-8") as handle:
            handle.write("CREATE TABLE IF NOT EXISTS probe.t3 (c INT);")
        try:
            SchemaMigrator(db_path, migrations_dir).apply()
            raise AssertionError("迁移序号重复必须抛出 RuntimeError")
        except RuntimeError as error:
            print("  -> 序号重复被拒绝: %s" % error)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_initialize_database_delegation_test():
    """测试 initialize_database 委托给迁移器（不再自己写 DDL）"""
    print("\n" + "=" * 65)
    print("【阶段六：测试 initialize_database 走迁移器】")
    print("=" * 65)

    temp_dir = tempfile.mkdtemp(prefix="sl_mig_init_")
    db_path = os.path.join(temp_dir, "init.duckdb")
    try:
        path = initialize_database(db_path)
        assert path == db_path, path
        assert _versions(db_path) == [1, 2, 3]

        # 再次调用不重复应用
        initialize_database(db_path)
        assert _versions(db_path) == [1, 2, 3]
        print("  -> initialize_database 与迁移器共用同一套版本记录")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    """运行全部迁移测试"""
    run_fresh_database_test()
    run_idempotency_test()
    run_legacy_upgrade_test()
    run_structured_without_version_test()
    run_failure_and_duplicate_test()
    run_initialize_database_delegation_test()
    print("\n" + "=" * 65)
    print("数据库迁移测试全部通过！")
    print("=" * 65)


if __name__ == "__main__":
    main()
