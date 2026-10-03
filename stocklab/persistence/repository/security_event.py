#!/usr/bin/env python3
"""
==============================================================================
StockLab - 证券生命周期事件 Repository (stocklab.persistence.repository.security_event)
==============================================================================

【模块职责】
   封装 reference.security_events 表的读写：
     - 写入生命周期事件（LISTED / DELISTED / ST / INDEX_ADDED ...）
     - 按时点查询事件，供 as-of 股票池与历史研究复核

【设计原则】
   - 批量 UPSERT（ts_code, event_date, event_type）复合主键，幂等；
   - 事件是「不可变事实」：同一事件重复同步不产生重复记录；
   - 不依赖任何外部数据源，事件帧由数据源层归一化后交给本层。
"""

import logging

from stocklab.domain import SECURITY_EVENT_COLUMNS
from stocklab.persistence.repository.base import BaseRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "SecurityEventRepository",
]


class SecurityEventRepository(BaseRepository):
    """
    证券生命周期事件数据访问类

    【职责】
      1. 批量写入生命周期事件
      2. 查询某只证券 / 某个时点之前的事件
    """

    _TABLE_NAME = "reference.security_events"
    _COLUMNS = SECURITY_EVENT_COLUMNS

    def upsert(self, events_df):
        """
        批量写入或更新生命周期事件（幂等操作）

        Args:
            events_df (pd.DataFrame): 生命周期事件表，
                                      列序可任意，须含全部契约列

        Returns:
            int: 实际写入的行数
        """
        return super().upsert(
            events_df,
            conflict_columns=["ts_code", "event_date", "event_type"],
            update_columns=["detail", "source"]
        )

    def find(self, ts_code=None, as_of_date=None, event_type=None):
        """
        查询生命周期事件（三个条件均为可选，按时间升序）

        Args:
            ts_code (str, optional): 证券代码
            as_of_date (str, optional): 时点，仅返回 event_date <= as_of_date 的事件
            event_type (str, optional): 事件类型，如 "DELISTED"

        Returns:
            pd.DataFrame: 事件表
        """
        conn = self._db.get_connection()
        sql = f"SELECT * FROM {self._TABLE_NAME} WHERE 1 = 1"
        params = []

        if ts_code:
            sql += " AND ts_code = ?"
            params.append(ts_code)
        if as_of_date:
            sql += " AND event_date <= ?"
            params.append(as_of_date)
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)

        sql += " ORDER BY event_date, ts_code"
        return conn.execute(sql, params).fetchdf()

    def count(self):
        """
        查询事件总数

        Returns:
            int: 事件记录总数
        """
        conn = self._db.get_connection()
        result = conn.execute(f"SELECT COUNT(*) FROM {self._TABLE_NAME}").fetchone()
        return int(result[0]) if result else 0
