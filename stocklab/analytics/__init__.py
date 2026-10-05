"""
StockLab 统计分析层 (stocklab.analytics)

【模块职责】
   对已取到的数据表做统计聚合与派生计算，产出不可变的统计结果实体，
   交给渲染层排版。本层是纯变换层：

     - 只接收 pandas DataFrame，不发网络请求、不连接数据库；
     - 不 import datasource / persistence / facade，取数由上层负责；
     - 因此可完全脱离网络与数据库独立单测。

【分层约束】
   - 上层（入口脚本）应先经 stocklab.facade 取数，再交给本层分析，
     不要在本层内直接访问数据源或数据库。

【渲染层扩展方式】
   计算与呈现分离：分析器只出统计结果实体，呈现由渲染层承担。
   全市场市盈率分布有两种并列形态：
     - ValuationDistributionReporter          -> 纯文本报告（控制台阅读）
     - ValuationDistributionMarkdownReporter  -> Markdown 报告（长期归档）
   历史估值分位另有专用渲染器（表格列宽与分布报告差异大，不复用）：
     - ValuationPercentileReporter            -> 控制台文本 + Markdown 双形态
   新增输出形态应另写渲染器并在本文件导出，不要给既有渲染器加格式开关。
"""

from .fund_attribution import (
    AttributionConfig,
    AttributionResult,
    BrinsonAttribution,
    FundAttributionEngine,
)
from .markdown_reporter import ValuationDistributionMarkdownReporter
from .percentile_reporter import ValuationPercentileReporter
from .profile_reporter import ValuationDistributionReporter
from .valuation_percentile import (
    VALUATION_INDICATOR_PB,
    VALUATION_INDICATOR_PCF,
    VALUATION_INDICATOR_PE_STATIC,
    VALUATION_INDICATOR_PE_TTM,
    VALUATION_INDICATOR_PS,
    VALUATION_LEVEL_HIGH,
    VALUATION_LEVEL_LOW,
    VALUATION_LEVEL_MIDDLE,
    ValuationPercentileAnalyzer,
    ValuationPercentileResult,
)
from .valuation_distribution import (
    PE_DISTRIBUTION_BUCKET_EDGES,
    PE_DISTRIBUTION_BUCKET_LABELS,
    PE_DISTRIBUTION_QUANTILES,
    PE_EXTREME_HIGH_THRESHOLD,
    PE_MIN_REASONABLE_SAMPLE_COUNT,
    PE_VALUE_COLUMN_DYNAMIC,
    PE_VALUE_COLUMN_TTM,
    DistributionBucket,
    MarketValuationDistribution,
    PeRankEntry,
    ValuationDistributionAnalyzer,
    ValuationDistributionProfile,
)

__all__ = [
    "PE_VALUE_COLUMN_TTM",
    "PE_VALUE_COLUMN_DYNAMIC",
    "PE_EXTREME_HIGH_THRESHOLD",
    "PE_MIN_REASONABLE_SAMPLE_COUNT",
    "PE_DISTRIBUTION_QUANTILES",
    "PE_DISTRIBUTION_BUCKET_EDGES",
    "PE_DISTRIBUTION_BUCKET_LABELS",
    "DistributionBucket",
    "PeRankEntry",
    "MarketValuationDistribution",
    "ValuationDistributionProfile",
    "ValuationDistributionAnalyzer",
    "ValuationDistributionReporter",
    "ValuationDistributionMarkdownReporter",
    "VALUATION_INDICATOR_PE_TTM",
    "VALUATION_INDICATOR_PE_STATIC",
    "VALUATION_INDICATOR_PB",
    "VALUATION_INDICATOR_PS",
    "VALUATION_INDICATOR_PCF",
    "VALUATION_LEVEL_LOW",
    "VALUATION_LEVEL_MIDDLE",
    "VALUATION_LEVEL_HIGH",
    "ValuationPercentileAnalyzer",
    "ValuationPercentileResult",
    "ValuationPercentileReporter",
    "ValuationDistributionReporter",
    "ValuationDistributionMarkdownReporter",
    "AttributionConfig",
    "AttributionResult",
    "BrinsonAttribution",
    "FundAttributionEngine",
]
