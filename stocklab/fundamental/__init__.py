"""
StockLab 基本面域 (stocklab.fundamental)

【模块职责】
   Point-in-Time 基本面域的**纯计算**部分：由已归一化的报表派生质量 / 成长指标。
   本包不取数、不落库，可离线单测。

【与文档 §3.1 建议目录的对应关系】
   需求文档建议 fundamental/ 下含 models / datasource / normalizer / repository，
   为避免与既有分层重复建设，按「各归其位」落位如下：
     - models          -> stocklab/domain/fundamental.py（列契约，数据源与持久化共用）
     - datasource      -> stocklab/datasource/fundamental_service.py（东财三大报表取数）
     - normalizer      -> stocklab/normalization/eastmoney.py（源列 → PIT 契约帧）
     - repository      -> stocklab/persistence/repository/fundamental.py（含 as-of 查询）
     - 纯指标派生       -> 本包 indicator.py（文档未指明归属的计算逻辑）
   这样 Datasource / Persistence 依旧互不依赖，验收标准 §17 的架构约束才成立。
"""

from .indicator import build_financial_indicators

__all__ = [
    "build_financial_indicators",
]
