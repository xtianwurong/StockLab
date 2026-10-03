#!/usr/bin/env python3
"""
==============================================================================
StockLab - 证券领域契约 (stocklab.domain.security)
==============================================================================

【模块职责】
   定义证券身份域的列契约（列名 + 列序，与建表 DDL 严格同序）：
     - SECURITY_COLUMNS            reference.securities（证券主表，含生命周期字段）
     - SECURITY_EVENT_COLUMNS      reference.security_events（生命周期事件）
     - INDEX_MEMBERSHIP_COLUMNS    reference.index_memberships（指数成分）

【生命周期语义】
   status 取值：LISTED / DELISTED / PAUSED / PRE_LIST。
   历史研究禁止直接拿「当前股票池」，须用 list_date / delist_date 推导
   as-of 股票池（见 stocklab.persistence.repository.security_event）。
"""

__all__ = [
    "SECURITY_COLUMNS",
    "SECURITY_EVENT_COLUMNS",
    "INDEX_MEMBERSHIP_COLUMNS",
    "SECURITY_EVENT_TYPES",
    "SECURITY_STATUSES",
]

# 证券主表全部列（与 001_initial.sql + 003_security_events.sql 终态同序）
SECURITY_COLUMNS = (
    "ts_code",
    "symbol",
    "name",
    "exchange",
    "market",
    "industry",
    "area",
    "list_date",
    "delist_date",
    "status",
    "is_hs",
)

# 生命周期事件表全部列
SECURITY_EVENT_COLUMNS = (
    "ts_code",
    "event_date",
    "event_type",
    "detail",
    "source",
)

# 指数成分表全部列
INDEX_MEMBERSHIP_COLUMNS = (
    "ts_code",
    "index_code",
    "index_name",
    "effective_date",
)

# 允许的生命周期事件类型（V2 需求 §5）
SECURITY_EVENT_TYPES = (
    "LISTED",
    "SUSPENDED",
    "RESUMED",
    "DELISTED",
    "ST",
    "DE_ST",
    "NAME_CHANGED",
    "CODE_CHANGED",
    "INDEX_ADDED",
    "INDEX_REMOVED",
)

# 允许的证券状态取值
SECURITY_STATUSES = (
    "PRE_LIST",
    "LISTED",
    "PAUSED",
    "DELISTED",
)
