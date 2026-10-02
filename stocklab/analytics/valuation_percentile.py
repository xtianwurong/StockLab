#!/usr/bin/env python3
"""
==============================================================================
StockLab - 历史估值分位计算 (stocklab.analytics.valuation_percentile)
==============================================================================

【模块职责】
   依据个股的历史估值序列，计算「当前值在过去 N 年中的位置」，
   产出可量化的分位结论，替换人工定性描述。

【分位口径（唯一实现，不可与其他工具混用）】
   分位 = (历史区间内低于当前值的样本数 / 有效样本总数) x 100%

   例：PE 分位 20%，表示当前价格比过去 80% 的时间都便宜。

   注意：这是「经验分布函数 CDF」口径，与 pandas 的 Series.quantile()
   （线性插值分位点）**不是同一件事**：
     - quantile(0.25) 返回「第 25 百分位的数值」；
     - 本模块返回「第 25 百分位的时间占比」。
   两者用途不同，绝不可互相替代或混报。

【为何需要单独成模块】
   历史分位是「判断当前贵贱」的唯一量化依据。若用定性描述（「处于低位」）
   则跨次不可比、无法复核；量化后每次结论都可复算、可追溯到具体样本。

【亏损样本处理（关键）】
   估值序列中的非正数（PE <= 0）代表亏损，该期间不具备估值比较意义，
   必须排除出分母。否则一家亏损股的历史分位会被虚高/虚低地扭曲。
   负值保留在原始序列中（不篡改数据），仅在统计口径上排除。

【依赖清单】
   标准库：logging
   第三方库：pandas
==============================================================================
"""

import logging

import pandas as pd

_logger = logging.getLogger(__name__)

__all__ = [
    "VALUATION_INDICATOR_PE_TTM",
    "VALUATION_INDICATOR_PE_STATIC",
    "VALUATION_INDICATOR_PB",
    "VALUATION_INDICATOR_PS",
    "VALUATION_INDICATOR_PCF",
    "VALUATION_LEVEL_LOW",
    "VALUATION_LEVEL_MIDDLE",
    "VALUATION_LEVEL_HIGH",
    "ValuationPercentileResult",
    "ValuationPercentileAnalyzer",
]


# ============================================================================
# 口径常量
# ============================================================================

# 可计算分位的估值指标（列名与 market.valuation_history 一致）
VALUATION_INDICATOR_PE_TTM = "pe_ttm"
VALUATION_INDICATOR_PE_STATIC = "pe_static"
VALUATION_INDICATOR_PB = "pb"
VALUATION_INDICATOR_PS = "ps"
VALUATION_INDICATOR_PCF = "pcf"

# 分位高低档位划分（相对分位百分比）
# 低于 30% 视为相对便宜，高于 70% 视为相对贵，中间为中性
VALUATION_LEVEL_LOW = "相对低位"
VALUATION_LEVEL_MIDDLE = "中性"
VALUATION_LEVEL_HIGH = "相对高位"

_LOW_PERCENTILE = 30.0
_HIGH_PERCENTILE = 70.0

# 结果实体中的空值占位
_EMPTY_TEXT = "-"


class ValuationPercentileResult:
    """
    单个指标的估值分位结果（简单实体类 / POD）
    """

    def __init__(self, indicator, current_value, percentile, sample_count,
                 median_value, min_value, max_value, interval_text, level):
        """
        初始化分位结果

        Args:
            indicator (str): 指标列名，如 pe_ttm
            current_value (float): 当前值；不可用时为 None
            percentile (float): 分位百分比（0 ~ 100）；不可用时为 None
            sample_count (int): 参与计算的有效样本数（已排除亏损与非正值）
            median_value (float): 区间中位数；不可用时为 None
            min_value (float): 区间最小值；不可用时为 None
            max_value (float): 区间最大值；不可用时为 None
            interval_text (str): 计算区间描述，含起止日期与样本频率
            level (str): 分位档位，取值为相对低位 / 中性 / 相对高位
        """
        self.indicator = indicator
        self.current_value = current_value
        self.percentile = percentile
        self.sample_count = sample_count
        self.median_value = median_value
        self.min_value = min_value
        self.max_value = max_value
        self.interval_text = interval_text
        self.level = level

    def is_available(self):
        """
        本指标是否成功算出分位

        Returns:
            bool: True 表示分位有效
        """
        return self.percentile is not None

    def to_dict(self):
        """转换为可序列化的字典"""
        return {
            "indicator": self.indicator,
            "current_value": self.current_value,
            "percentile": self.percentile,
            "sample_count": self.sample_count,
            "median_value": self.median_value,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "interval_text": self.interval_text,
            "level": self.level,
        }


class ValuationPercentileAnalyzer:
    """
    历史估值分位分析器

    【职责】
      1. 从历史估值序列中提取指定区间的有效样本
      2. 按 CDF 口径计算当前值的分位百分比
      3. 输出区间统计与高低档位判定
    """

    def analyze(self, history_df, current_values=None, start_date=None, end_date=None):
        """
        计算各估值指标的历史分位

        Args:
            history_df (pd.DataFrame): 历史估值表，需含 trade_date 与各估值列；
                                       通常来自 ValuationHistoryRepository
            current_values (dict, optional): {指标列名: 当前值}；
                                             为空时取区间内最新一个交易日的值
            start_date (str, optional): 计算区间起始日期 YYYY-MM-DD，缺省为全部历史
            end_date (str, optional): 计算区间结束日期 YYYY-MM-DD，缺省为最新

        Returns:
            list: ValuationPercentileResult 列表（每个指标一项）
        """
        results = []

        if history_df is None or history_df.empty:
            _logger.warning("历史估值序列为空，无法计算分位")
            return self._build_unavailable_results(current_values)

        sample_df = self._slice_interval(history_df, start_date, end_date)
        if sample_df.empty:
            _logger.warning("指定区间内无历史估值样本，无法计算分位")
            return self._build_unavailable_results(current_values)

        interval_text = self._build_interval_text(sample_df)

        # 候选指标中，有数据的正常计算；无数据的也要显式标记为不可用，
        # 否则调用方无法区分「该指标算不出来」与「该指标不存在」
        indicators = self._resolve_indicator_candidates(sample_df)
        for indicator in indicators:
            results.append(
                self._analyze_indicator(
                    sample_df, indicator, current_values, interval_text
                )
            )

        return results

    def _analyze_indicator(self, sample_df, indicator, current_values, interval_text):
        """
        计算单个指标的分位

        Args:
            sample_df (pd.DataFrame): 区间内的历史估值表
            indicator (str): 指标列名
            current_values (dict): 当前值字典，可能为空
            interval_text (str): 区间描述文本

        Returns:
            ValuationPercentileResult: 分位结果
        """
        series = pd.to_numeric(sample_df[indicator], errors="coerce")
        # 亏损期（PE<=0）与缺失值不具备估值比较意义，排除出分母
        valid_series = series[series > 0]
        sample_count = len(valid_series)

        if sample_count == 0:
            _logger.info(
                "指标 [%s] 在区间内无有效样本（可能长期亏损或数据源未提供）", indicator
            )
            return ValuationPercentileResult(
                indicator, None, None, 0, None, None, None, interval_text, _EMPTY_TEXT
            )

        current_value = self._resolve_current_value(
            series, current_values, indicator
        )
        if current_value is None or current_value <= 0:
            return ValuationPercentileResult(
                indicator, current_value, None, sample_count,
                float(valid_series.median()), float(valid_series.min()),
                float(valid_series.max()), interval_text, _EMPTY_TEXT,
            )

        # CDF 口径：低于当前值的样本占比。current_value 自身在样本内时用严格小于，
        # 避免把「恰好等于当前值」的历史样本算作「低于」，导致分位偏高一档。
        lower_count = int((valid_series < current_value).sum())
        percentile = lower_count / sample_count * 100.0

        return ValuationPercentileResult(
            indicator,
            current_value,
            percentile,
            sample_count,
            float(valid_series.median()),
            float(valid_series.min()),
            float(valid_series.max()),
            interval_text,
            self._classify_level(percentile),
        )

    def _slice_interval(self, history_df, start_date, end_date):
        """
        按起止日期裁剪历史序列

        Args:
            history_df (pd.DataFrame): 完整历史序列
            start_date (str, optional): 起始日期
            end_date (str, optional): 结束日期

        Returns:
            pd.DataFrame: 裁剪后的序列（按时间升序）
        """
        result = history_df.copy()

        if "trade_date" in result.columns:
            result = result.copy()
            # DuckDB DATE 列读回为 datetime64（带时间分量），统一规整为纯日期，
            # 避免下游做字符串格式化时出现 "2026-10-01 00:00:00"
            result["trade_date"] = pd.to_datetime(result["trade_date"]).dt.date
            # 入参为 YYYY-MM-DD 字符串，须先转成 date 对象再比较，
            # 否则 date 与 str 直接比较会抛 TypeError
            if start_date:
                result = result[result["trade_date"] >= self._to_date(start_date)]
            if end_date:
                result = result[result["trade_date"] <= self._to_date(end_date)]
            result = result.sort_values(by="trade_date")

        return result

    def _to_date(self, date_text):
        """
        把 YYYY-MM-DD 字符串转为 date 对象

        Args:
            date_text (str): 日期字符串

        Returns:
            datetime.date: 日期对象；无法解析时原样返回（交由 pandas 报错）
        """
        try:
            return pd.to_datetime(date_text).date()
        except (ValueError, TypeError):
            return date_text

    def _resolve_indicator_candidates(self, sample_df):
        """
        列出待计算的指标列（表结构中实际存在的估值列）

        Args:
            sample_df (pd.DataFrame): 区间内的历史估值表

        Returns:
            list: 指标列名列表
        """
        candidates = [
            VALUATION_INDICATOR_PE_TTM,
            VALUATION_INDICATOR_PE_STATIC,
            VALUATION_INDICATOR_PB,
            VALUATION_INDICATOR_PS,
            VALUATION_INDICATOR_PCF,
        ]

        found = []
        for indicator in candidates:
            if indicator in sample_df.columns:
                found.append(indicator)
        return found

    def _resolve_current_value(self, series, current_values, indicator):
        """
        确定用于计算分位的当前值

        【优先级】
           显式传入的当前值 > 区间内最新交易日的值。
           传入当前值可避免「用最新一日当基准」与「用当日实际值」之间的口径差异。

        Args:
            series (pd.Series): 指标序列
            current_values (dict): 当前值字典，可能为空
            indicator (str): 指标列名

        Returns:
            float: 当前值；无法确定时返回 None
        """
        if current_values and indicator in current_values:
            explicit_value = pd.to_numeric(
                pd.Series([current_values[indicator]]), errors="coerce"
            ).iloc[0]
            if explicit_value == explicit_value:  # 过滤 NaN
                return float(explicit_value)

        valid_series = series.dropna()
        if valid_series.empty:
            return None
        return float(valid_series.iloc[-1])

    def _build_interval_text(self, sample_df):
        """
        生成计算区间描述（skill 要求注明区间与样本频率）

        Args:
            sample_df (pd.DataFrame): 区间内的历史估值表

        Returns:
            str: 形如 "2021-10-03 ~ 2026-10-01（913 个交易日）" 的描述
        """
        if "trade_date" not in sample_df.columns or sample_df.empty:
            return _EMPTY_TEXT

        start = sample_df["trade_date"].iloc[0]
        end = sample_df["trade_date"].iloc[-1]
        return "%s ~ %s（%d 个交易日）" % (start, end, len(sample_df))

    def _classify_level(self, percentile):
        """
        按分位判定高低档位

        Args:
            percentile (float): 分位百分比

        Returns:
            str: 档位名称
        """
        if percentile <= _LOW_PERCENTILE:
            return VALUATION_LEVEL_LOW
        if percentile >= _HIGH_PERCENTILE:
            return VALUATION_LEVEL_HIGH
        return VALUATION_LEVEL_MIDDLE

    def _build_unavailable_results(self, current_values):
        """
        构造全部指标均不可用的结果列表

        Args:
            current_values (dict): 当前值字典，可能为空

        Returns:
            list: ValuationPercentileResult 列表
        """
        indicators = [
            VALUATION_INDICATOR_PE_TTM,
            VALUATION_INDICATOR_PE_STATIC,
            VALUATION_INDICATOR_PB,
            VALUATION_INDICATOR_PS,
            VALUATION_INDICATOR_PCF,
        ]
        if current_values:
            indicators = list(current_values.keys())

        results = []
        for indicator in indicators:
            results.append(
                ValuationPercentileResult(
                    indicator, None, None, 0, None, None, None, _EMPTY_TEXT, _EMPTY_TEXT
                )
            )
        return results
