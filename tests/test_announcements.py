#!/usr/bin/env python3
"""
==============================================================================
StockLab - 公告索引落库与同步测试 (tests/test_announcements.py)
==============================================================================

【功能用途】
  覆盖免费数据源扩展需求 §25~§28 / §49~§51 的持久化与同步阶段：
    1. 迁移 005：corporate.announcements 建表，列名与列序等于 ANNOUNCEMENT_COLUMNS
    2. AnnouncementRepository：只插入（主键冲突忽略）/ 水位 / 区间去重键
    3. 阶段九同步：表为空且未给 --start-date 时拒绝执行（禁止无限回溯）
    4. 首次同步：全市场抓取 -> 非 A 股过滤 -> 落库 -> 记录水位
    5. 增量同步：起点取水位、重复公告二次去重（跨 announcement_id）
    6. --ts-code 逐只模式

【运行方式】
  ./venv/bin/python tests/test_announcements.py
  （全部使用临时数据库，不触碰 data/stocklab.duckdb；注入离线公告桩，不联网）
==============================================================================
"""

import os
import shutil
import sys
import tempfile
from datetime import date, datetime

# 将项目根目录加入模块搜索路径，保证直接运行本脚本时能 import stocklab
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb
import pandas as pd

from app.scripts.sync_market_data import sync_announcements
from stocklab.domain import ANNOUNCEMENT_COLUMNS
from stocklab.persistence import AnnouncementRepository, Database, initialize_database
from stocklab.persistence.migrations import MIGRATIONS_DIR

MS_20260815 = 1786723200000


def _row(ts_code, announcement_id, title, day, pdf="finalpage/x.PDF"):
    """构造一条符合客户端契约（9 列）的公告行"""
    return {
        "announcement_id": announcement_id,
        "ts_code": ts_code,
        "announcement_date": day,
        "publish_time": MS_20260815,
        "title": title,
        "category": None,
        "pdf_url": "http://static.cninfo.com.cn/" + pdf,
        "source": "cninfo",
        "source_url": "http://www.cninfo.com.cn/new/hisAnnouncement/query",
    }


class FakeCninfoClient:
    """离线公告桩：模拟真实客户端的去重逻辑（announcement_id + (ts_code, 日期, 标题)）"""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def fetch_announcements(self, symbol, start_date, end_date, max_pages=200):
        self.calls.append((symbol, start_date, end_date, max_pages))
        kept = []
        seen_ids = set()
        seen_keys = set()
        for row in self.rows:
            if symbol is not None and row["ts_code"] != symbol:
                continue
            d = str(row["announcement_date"])
            if not (start_date <= d <= end_date):
                continue
            # 去重：announcement_id 优先 + (ts_code, 日期, 标题) 兜底
            if row["announcement_id"] in seen_ids:
                continue
            key = (row["ts_code"], row["announcement_date"], row["title"])
            if key in seen_keys:
                continue
            seen_ids.add(row["announcement_id"])
            seen_keys.add(key)
            kept.append(row)
        if not kept:
            return pd.DataFrame()
        return pd.DataFrame(kept)


def _seed_securities(conn):
    """向临时库注入 A 股股票池（用于全市场模式的非 A 股过滤）"""
    for ts_code, symbol, name in (
        ("600519.SH", "600519", "贵州茅台"),
        ("000001.SZ", "000001", "平安银行"),
    ):
        conn.execute(
            "INSERT INTO reference.securities (ts_code, symbol, name) VALUES (?, ?, ?)",
            [ts_code, symbol, name],
        )


def _table_columns(db_path, schema, table):
    """读取表列名（保持列序）"""
    conn = None
    try:
        conn = duckdb.connect(db_path, read_only=True)
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [schema, table],
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        if conn is not None:
            conn.close()


def run_migration_test():
    """测试迁移 005：建表成功且列序与领域契约一致"""
    print("\n" + "=" * 65)
    print("【阶段一：测试迁移 005 与列契约】")
    print("=" * 65)

    temp_dir = tempfile.mkdtemp(prefix="sl_ann_mig_")
    db_path = os.path.join(temp_dir, "ann.duckdb")
    try:
        initialize_database(db_path)
        columns = _table_columns(db_path, "corporate", "announcements")
        assert columns == list(ANNOUNCEMENT_COLUMNS), (columns, ANNOUNCEMENT_COLUMNS)

        # 迁移必须由目录自动发现（新增 NNN_*.sql 无需手改测试数字）
        migrations = sorted(
            name for name in os.listdir(MIGRATIONS_DIR)
            if name.endswith(".sql")
        )
        assert "005_announcements.sql" in migrations, migrations

        # 幂等：重复初始化不报错，表结构不变
        initialize_database(db_path)
        assert _table_columns(db_path, "corporate", "announcements") == list(ANNOUNCEMENT_COLUMNS)
        print("  -> corporate.announcements 列名与列序 == ANNOUNCEMENT_COLUMNS")
        print("  -> 005_announcements.sql 被自动发现，重复初始化幂等")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_repository_test():
    """测试 Repository：插入、主键冲突忽略、水位、区间去重键"""
    print("\n" + "=" * 65)
    print("【阶段二：测试 AnnouncementRepository】")
    print("=" * 65)

    temp_dir = tempfile.mkdtemp(prefix="sl_ann_repo_")
    db_path = os.path.join(temp_dir, "ann.duckdb")
    try:
        initialize_database(db_path)
        with Database(db_path) as database:
            repository = AnnouncementRepository(database)

            # 空数据 -> 0，不报错
            assert repository.insert(pd.DataFrame()) == 0

            frame = pd.DataFrame([
                _row("600519.SH", "id-1", "半年度报告", date(2026, 8, 15)),
                _row("000001.SZ", "id-2", "股东大会决议", date(2026, 9, 1)),
            ])
            # 抓取元数据由同步阶段补齐（阶段三会走完整链路）
            frame["crawl_time"] = datetime.now()
            frame["content_hash"] = None
            assert repository.insert(frame) == 2
            assert repository.latest_date() == date(2026, 9, 1)

            # 主键冲突：重复插入被忽略，不覆盖也不报错
            repository.insert(frame)
            conn = database.get_connection()
            count = conn.execute(
                "SELECT COUNT(*) FROM corporate.announcements"
            ).fetchone()[0]
            assert count == 2, count

            # 区间去重键
            keys = repository.existing_keys(date(2026, 8, 1), date(2026, 12, 31))
            assert ("600519.SH", date(2026, 8, 15), "半年度报告") in keys
            assert ("000001.SZ", date(2026, 9, 1), "股东大会决议") in keys
            assert len(keys) == 2

            # 缺契约列的 DataFrame 必须被拒绝（返回 0），不能写脏数据
            bad = pd.DataFrame([{"announcement_id": "x", "ts_code": "600519.SH"}])
            assert repository.insert(bad) == 0
            count = conn.execute(
                "SELECT COUNT(*) FROM corporate.announcements"
            ).fetchone()[0]
            assert count == 2, count

            # 表为空时水位为 None
            conn.execute("DELETE FROM corporate.announcements")
            assert repository.latest_date() is None

        print("  -> 插入 / 主键冲突忽略 / 水位 / 区间去重键 / 契约防线 全部正确")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_sync_test():
    """测试阶段九同步：空表拒绝、首次同步、增量与二次去重、逐只模式"""
    print("\n" + "=" * 65)
    print("【阶段三：测试阶段九同步（水位 / 过滤 / 去重 / 逐只）】")
    print("=" * 65)

    temp_dir = tempfile.mkdtemp(prefix="sl_ann_sync_")
    db_path = os.path.join(temp_dir, "ann.duckdb")
    try:
        initialize_database(db_path)

        # 预置股票池：113050.SH 不在池内（可转债），全市场模式必须剔除
        with Database(db_path) as database:
            _seed_securities(database.get_connection())

        rows = [
            _row("600519.SH", "a-1", "贵州茅台2026年半年度报告", date(2026, 8, 15)),
            _row("000001.SZ", "a-2", "平安银行2026年半年度报告", date(2026, 9, 1)),
            _row("113050.SH", "a-3", "可转债转股价格调整公告", date(2026, 9, 1)),
        ]
        client = FakeCninfoClient(rows)

        with Database(db_path) as database:
            # 1) 表为空且未给 --start-date -> 拒绝执行（禁止默认无限回溯）
            assert sync_announcements(database, client=client) is False
            assert client.calls == [], "拒绝执行时不应发起任何抓取"

            # 2) 首次同步（显式给出起始日期）
            assert sync_announcements(
                database, start_date="2026-08-01", end_date="2026-09-30", client=client
            ) is True
            assert client.calls[-1][0] is None, "缺省应为全市场模式"
            assert client.calls[-1][1] == "2026-08-01"
            assert client.calls[-1][2] == "2026-09-30"

            repository = AnnouncementRepository(database)
            conn = database.get_connection()
            count = conn.execute(
                "SELECT COUNT(*) FROM corporate.announcements"
            ).fetchone()[0]
            assert count == 2, "非 A 股（113050.SH）应被剔除，实际 %s" % count
            assert repository.latest_date() == date(2026, 9, 1)
            print("  -> 首次同步：2 条入账、非 A 股 1 条被剔除、水位记录正确")

            # 3) 增量同步：未给 --start-date 时起点取水位；重复公告二次去重
            rows.append(
                _row("600519.SH", "a-4", "贵州茅台2026年三季度报告", date(2026, 10, 1))
            )
            # 同内容不同 announcementId：必须被 (ts_code, 日期, 标题) 去重
            rows.append(
                _row("600519.SH", "a-5", "贵州茅台2026年三季度报告", date(2026, 10, 1))
            )
            client = FakeCninfoClient(rows)
            assert sync_announcements(database, end_date="2026-10-31", client=client) is True
            assert client.calls[-1][1] == "2026-09-01", client.calls[-1]

            count = conn.execute(
                "SELECT COUNT(*) FROM corporate.announcements"
            ).fetchone()[0]
            assert count == 3, "应只新增 1 条（重复 1 条被去重），实际 %s" % count
            titles = [
                row[0] for row in conn.execute(
                    "SELECT title FROM corporate.announcements ORDER BY announcement_date"
                ).fetchall()
            ]
            assert titles.count("贵州茅台2026年三季度报告") == 1, titles
            assert repository.latest_date() == date(2026, 10, 1)
            print("  -> 增量同步：起点取水位 2026-09-01、重复内容去重、仅新增 1 条")

            # 4) 重复执行同一区间：全部命中既有键 -> 新增 0 条
            client = FakeCninfoClient(rows)
            assert sync_announcements(
                database, start_date="2026-08-01", end_date="2026-10-31", client=client
            ) is True
            count = conn.execute(
                "SELECT COUNT(*) FROM corporate.announcements"
            ).fetchone()[0]
            assert count == 3, count
            print("  -> 重复执行同一区间新增 0 条（幂等）")

            # 5) --ts-code 逐只模式：按 symbol 分别抓取
            client = FakeCninfoClient(rows)
            assert sync_announcements(
                database, start_date="2026-08-01", end_date="2026-10-31",
                ts_codes=["600519.SH"], client=client,
            ) is True
            assert client.calls[-1][0] == "600519.SH"
            print("  -> --ts-code 逐只模式按 symbol 调用")

            # 6) 起始晚于结束 -> 失败
            client = FakeCninfoClient(rows)
            assert sync_announcements(
                database, start_date="2026-12-01", end_date="2026-10-31", client=client
            ) is False
            print("  -> 起止日期非法时失败返回，不发起抓取")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    """运行全部公告持久化与同步测试"""
    run_migration_test()
    run_repository_test()
    run_sync_test()
    print("\n" + "=" * 65)
    print("公告索引落库与同步测试全部通过！")
    print("=" * 65)


if __name__ == "__main__":
    main()
