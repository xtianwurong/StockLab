#!/usr/bin/env python3
"""
==============================================================================
StockLab - 公告索引 Repository (stocklab.persistence.repository.announcement)
==============================================================================

【模块职责】
   封装 corporate.announcements 的读写：
     - AnnouncementRepository.insert()            只插入（冲突忽略）
     - AnnouncementRepository.latest_date()       增量同步水位
     - AnnouncementRepository.existing_keys()     区间内既有去重键（二次去重）

【写入语义】
   公告是既成事实：只 INSERT、不 UPDATE。
   冲突处理分两层：
     1. 主键 announcement_id 冲突 -> ON CONFLICT DO NOTHING；
     2. 跨 announcement_id 的同内容公告 -> 写入前按
        (ts_code, announcement_date, title) 过滤（existing_keys）。
"""

import logging

from stocklab.domain import ANNOUNCEMENT_COLUMNS
from stocklab.persistence.repository.base import BaseRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "AnnouncementRepository",
]


class AnnouncementRepository(BaseRepository):
    """公司公告索引数据访问类（只插入，不可覆盖）"""

    _TABLE_NAME = "corporate.announcements"
    _COLUMNS = ANNOUNCEMENT_COLUMNS

    def insert(self, frame):
        """
        插入公告索引（主键冲突自动忽略）

        Args:
            frame (pd.DataFrame): 公告行，列序可任意，须含全部契约列

        Returns:
            int: 提交写入的行数；空数据或契约违约返回 0
        """
        return self._insert_ignore_conflict(frame, ["announcement_id"])

    def latest_date(self):
        """
        查询已同步的最新公告日期（增量同步水位）

        Returns:
            datetime.date | None: 表为空返回 None
        """
        conn = self._db.get_connection()
        row = conn.execute(
            f"SELECT MAX(announcement_date) FROM {self._TABLE_NAME}"
        ).fetchone()
        if not row or row[0] is None:
            return None
        return row[0]

    def existing_keys(self, start_date, end_date):
        """
        查询区间内已存在的去重键，用于跨 announcement_id 的二次去重

        Args:
            start_date: 起始日期（含）
            end_date: 结束日期（含）

        Returns:
            set: {(ts_code, datetime.date, title), ...}
        """
        conn = self._db.get_connection()
        rows = conn.execute(
            f"SELECT ts_code, announcement_date, title FROM {self._TABLE_NAME} "
            "WHERE announcement_date >= ? AND announcement_date <= ?",
            [start_date, end_date],
        ).fetchall()
        return {(row[0], row[1], row[2]) for row in rows}
