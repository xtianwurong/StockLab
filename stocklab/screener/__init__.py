#!/usr/bin/env python3
"""
==============================================================================
StockLab - 选股器 (stocklab.screener)
==============================================================================

【模块职责】
   V2 需求 §8 的 Stock Screener：用组合条件筛选股票。

【设计原则】
   - 条件必须结构化（§8.1）：ScreenRule / ScreenGroup / ScreenPipeline 三层，
     spec 用字典描述（YAML/JSON 同构），不写 if xxx 链；
   - 结果必须可解释（§8.2）：summary 每只股票给 symbol/name/passed/failed_rules，
     detail 给（股票 × 规则）的 factor_value/threshold/passed/reason；
   - 因子值缺失一律判不通过，原因写「无数据」，不静默放行；
   - 纯计算：本包不取数、不落库（因子输入帧由 stocklab.research.frame 拼装）。

【对外用法】
   import stocklab.screener as screener

   pipeline = screener.ScreenPipeline.from_spec({
       "rules": [
           {"factor": "pe_ttm", "operator": "lt", "value": 20},
           {"factor": "roe", "operator": "gt", "value": 0.12},
       ],
   })
   result = pipeline.run(frame)
   result.summary   # 每只股票一行
   result.detail    # 每只股票每条规则一行
"""

from .pipeline import ScreenPipeline, ScreenResult
from .rules import OPERATORS, ScreenError, ScreenGroup, ScreenRule

__all__ = [
    "ScreenRule",
    "ScreenGroup",
    "ScreenPipeline",
    "ScreenResult",
    "ScreenError",
    "OPERATORS",
]
