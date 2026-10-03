#!/usr/bin/env python3
"""
==============================================================================
StockLab - 公司披露领域契约 (stocklab.domain.disclosure)
==============================================================================

【模块职责】
   定义公司披露域的列契约（列名 + 列序，与 005_announcements.sql 严格同序）：
     - ANNOUNCEMENT_COLUMNS corporate.announcements（巨潮公告索引）

【写入语义】
   公告索引只 INSERT、不 UPDATE：抓到的披露记录是既成事实，
   重复同步按 announcement_id 忽略冲突，再按 (ts_code, 日期, 标题) 二次去重。
"""

__all__ = [
    "ANNOUNCEMENT_COLUMNS",
]

# 公告索引表全部列（与 005_announcements.sql 同序）
ANNOUNCEMENT_COLUMNS = (
    "announcement_id",
    "ts_code",
    "announcement_date",
    "publish_time",
    "title",
    "category",
    "pdf_url",
    "source",
    "source_url",
    "crawl_time",
    "content_hash",
)
