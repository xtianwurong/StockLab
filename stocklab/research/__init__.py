#!/usr/bin/env python3
"""
==============================================================================
StockLab - 研究层 (stocklab.research)
==============================================================================

【模块职责】
   V2 需求 §6 / §2.5 的研究引擎编排层：
     - frame      把本地库按 Point-in-Time 口径拼成因子输入帧（唯一取数入口）
     - snapshot   研究快照：创建 / 读取 / 按 spec 重新生成并比对

【分层约定】
   本包是**编排层**：允许依赖 persistence（查数落库）、factor、screener（纯计算）；
   与 facade 同级，属于「取数 / 编排」这一侧，不被 datasource 或 persistence 反向依赖。
   因子与筛选本身仍是纯计算，绝不直接访问数据库。

【对外用法】
   from stocklab.research import build_factor_frame, create_snapshot, rerun_snapshot

   frame = build_factor_frame("2024-06-30")
   result = pipeline.run(frame)
   snapshot_id = create_snapshot("2024-06-30", "A股全市场", pipeline, result.summary)
   rerun_snapshot(snapshot_id)   # 重新生成并比对，identical=True 表示可复现
"""

from .frame import build_factor_frame
from .snapshot import (
    SnapshotError,
    config_version,
    create_snapshot,
    data_version,
    list_snapshots,
    load_snapshot,
    rerun_snapshot,
)

__all__ = [
    "build_factor_frame",
    "create_snapshot",
    "load_snapshot",
    "list_snapshots",
    "rerun_snapshot",
    "SnapshotError",
    "config_version",
    "data_version",
]
