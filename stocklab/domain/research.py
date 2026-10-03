#!/usr/bin/env python3
"""
==============================================================================
StockLab - 研究快照领域契约 (stocklab.domain.research)
==============================================================================

【模块职责】
   定义研究快照域的列契约（列名 + 列序，与 004_research.sql 严格同序）：
     - SNAPSHOT_COLUMNS        research.snapshots（一次研究运行的元数据 + 可复现 spec）
     - SNAPSHOT_RESULT_COLUMNS research.snapshot_results（每只股票的判定结果）

【不可变语义】
   快照一经写入不再更新：复现结论必须建立在不可变记录之上，
   重复执行同一次研究应产生**新的** snapshot_id，而不是覆盖旧结果。
"""

__all__ = [
    "SNAPSHOT_COLUMNS",
    "SNAPSHOT_RESULT_COLUMNS",
]

# 研究快照主表全部列（与 004_research.sql 同序）
SNAPSHOT_COLUMNS = (
    "snapshot_id",
    "created_at",
    "as_of_date",
    "universe",
    "universe_size",
    "data_version",
    "factor_version",
    "config_version",
    "condition",
    "passed_count",
    "result_count",
    "spec_json",
)

# 快照逐股结果全部列（与 004_research.sql 同序）
SNAPSHOT_RESULT_COLUMNS = (
    "snapshot_id",
    "ts_code",
    "passed",
    "failed_rules",
    "factor_values",
)
