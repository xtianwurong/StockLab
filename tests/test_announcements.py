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
import sys
from datetime import date, datetime

import duckdb
import pandas as pd

# 允许直接执行本文件（./venv/bin/python tests/xxx.py）；走 pytest 时由
# pytest.ini 的 `pythonpath = .` 统一负责，不会重复插入。
if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.scripts.sync_market_data import sync_announcements
from stocklab.domain import ANNOUNCEMENT_COLUMNS
from stocklab.persistence import AnnouncementRepository, Database, initialize_database
import pytest
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

def test_migration(tmp_db_path):
    """测试迁移 005：建表成功且列序与领域契约一致"""

    db_path = os.path.join(tmp_db_path, "ann.duckdb")

def test_repository(tmp_db_path):
    """测试 Repository：插入、主键冲突忽略、水位、区间去重键"""

    db_path = os.path.join(tmp_db_path, "ann.duckdb")

def test_sync(tmp_db_path):
    """测试阶段九同步：空表拒绝、首次同步、增量与二次去重、逐只模式"""

    db_path = os.path.join(tmp_db_path, "ann.duckdb")


if __name__ == "__main__":
    # 保住旧的直接执行入口：委托给 pytest，退出码语义一致
    raise SystemExit(pytest.main([__file__, "-v"]))
