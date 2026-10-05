#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基准指数行业权重域契约 (stocklab.domain.benchmark)
==============================================================================

【模块职责】
   定义「基准指数 -> 行业权重快照」的列契约，与建表 DDL 同序。

【口径】
   weight 为小数（0.1234 = 12.34%），同 (benchmark_code, level) 下合计为 1；
   coverage 为已映射到申万行业的成分券权重占基准总权重的比例（0~1）。
"""

__all__ = [
    "BENCHMARK_INDUSTRY_WEIGHT_COLUMNS",
    "BENCHMARK_INDEX_LEVEL1",
]

# reference.benchmark_industry_weights 全部列（与建表 DDL 同序）
BENCHMARK_INDUSTRY_WEIGHT_COLUMNS = (
    "benchmark_code",
    "level",
    "sector_code",
    "sector_name",
    "weight",
    "as_of_date",
    "coverage",
    "source",
    "fetched_at",
)

# 可选基准指数（代码 -> 中文名），与 datasource.benchmark_index 保持一致
BENCHMARK_INDEX_LEVEL1 = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000906.SH": "中证800",
    "000985.SH": "中证全指",
    "399001.SZ": "深证成指",
    "000001.SH": "上证指数",
    "399106.SZ": "深证综指",
}
