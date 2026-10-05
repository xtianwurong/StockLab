"""
StockLab 归一化层 (stocklab.normalization)

【模块职责】
   「源 DataFrame → 领域契约 DataFrame」的唯一转换层：
   数据源返回的中文/改版列名与列序在这里被映射为 stocklab.domain 的契约列名与列序。

   - base.py     归一化原语：代码后缀推断、候选列名探测、数值/日期/文本清洗
   - akshare.py  akshare 各接口的 7 个 normalize_*（证券 / 日K / 估值快照 /
                 历史估值 / 行业估值 / 指数成分 / 公司概况）
   - exchange.py 交易所上市与退市日历 → 证券生命周期（status / 生命周期事件）
   - eastmoney.py 东方财富三大报表 → Point-in-Time 基本面契约帧

【分层约束】
   本层只依赖 pandas 与 stocklab.domain，不 import datasource / persistence / facade，
   因此可脱离网络与数据库用合成数据做契约测试（tests/contract/）。
"""

from .base import (
    clean_date_value,
    clean_text_value,
    normalize_ts_code,
    pick_column,
    to_date_series,
    to_numeric_column,
)

__all__ = [
    "normalize_ts_code",
    "pick_column",
    "to_numeric_column",
    "to_date_series",
    "clean_text_value",
    "clean_date_value",
]
