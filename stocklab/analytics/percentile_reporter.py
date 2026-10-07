#!/usr/bin/env python3
"""
==============================================================================
StockLab - 历史估值分位报告渲染器 (stocklab.analytics.percentile_reporter)
==============================================================================

【模块职责】
   把 ValuationPercentileResult 列表排版为控制台文本与 Markdown 两种报告。
   与 profile_reporter / markdown_reporter 并列，是「同一份结果、多份呈现」
   扩展点在本层的第三个实例。

【为何单独成模块】
   分位报告需要固定列宽表格、中文标签、指标中英文映射与档位解读，
   与全市场分布报告的结构完全不同，塞进既有渲染器会产生互相干扰的分支。

【排版约定】
   中文字符在控制台占两格，直接用 % 对齐会错位，
   因此按 east_asian_width 计算视觉宽度后再补空格。

【依赖清单】
   标准库：logging
==============================================================================
"""

import logging
from stocklab.analytics.text_format import visual_width, pad_text

from stocklab.analytics.valuation_percentile import (
    VALUATION_INDICATOR_PB,
    VALUATION_INDICATOR_PCF,
    VALUATION_INDICATOR_PE_STATIC,
    VALUATION_INDICATOR_PE_TTM,
    VALUATION_INDICATOR_PS,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "ValuationPercentileReporter",
]

# 报告宽度（视觉列数）
_REPORT_WIDTH = 78

# 指标列名 -> 中文标签
_INDICATOR_LABELS = {
    VALUATION_INDICATOR_PE_TTM: "PE-TTM",
    VALUATION_INDICATOR_PE_STATIC: "PE(静)",
    VALUATION_INDICATOR_PB: "市净率 PB",
    VALUATION_INDICATOR_PS: "市销率 PS",
    VALUATION_INDICATOR_PCF: "市现率 PCF",
}

# 控制台表格列宽（视觉列数）
_COL_INDICATOR = 14
_COL_CURRENT = 10
_COL_PERCENTILE = 10
_COL_SAMPLE = 8
_COL_MEDIAN = 12
_COL_LEVEL = 10

# 空值占位符
_EMPTY_TEXT = "-"


class ValuationPercentileReporter:
    """
    历史估值分位报告渲染器
    """

    # 兼容性包装：旧代码/测试可能直接调用这些私有方法
    def _visual_width(self, text):
        return visual_width(text)

    def _pad(self, text, visual_width):
        return pad_text(text, visual_width)


    def format_console_report(self, ts_code, results, source_note=""):
        """
        渲染控制台文本报告

        Args:
            ts_code (str): 证券代码
            results (list): ValuationPercentileResult 列表
            source_note (str, optional): 取数通路说明

        Returns:
            str: 完整报告文本
        """
        lines = []
        lines.append("=" * _REPORT_WIDTH)
        lines.append(" " + ts_code + " 历史估值分位")
        lines.append("=" * _REPORT_WIDTH)
        if source_note:
            lines.append(pad_text("取数通路", 12) + source_note)
        lines.append("")
        lines.append(pad_text("分位口径", 12) + "(区间内低于当前值的样本数 / 有效样本总数) x 100%")
        lines.append(" " * 12 + "亏损期(PE<=0)不计入样本")

        available = self._filter_available(results)
        if not available:
            lines.append("")
            lines.append("[失败] 无任何指标可计算出分位（可能长期亏损或本地无历史数据）")
            return "\n".join(lines)

        lines.append("")
        lines.append("-" * _REPORT_WIDTH)
        lines.append(
            pad_text("指标", _COL_INDICATOR)
            + pad_text("当前值", _COL_CURRENT)
            + pad_text("历史分位", _COL_PERCENTILE)
            + pad_text("样本数", _COL_SAMPLE)
            + pad_text("区间中位数", _COL_MEDIAN)
            + "档位"
        )
        lines.append("-" * _REPORT_WIDTH)

        for item in available:
            lines.append(
                pad_text(self.indicator_label(item.indicator), _COL_INDICATOR)
                + pad_text(self._format_number(item.current_value), _COL_CURRENT)
                + pad_text("%.1f%%" % item.percentile, _COL_PERCENTILE)
                + pad_text(str(item.sample_count), _COL_SAMPLE)
                + pad_text(self._format_number(item.median_value), _COL_MEDIAN)
                + item.level
            )

        lines.append("-" * _REPORT_WIDTH)
        lines.append(pad_text("计算区间", 12) + available[0].interval_text)
        lines.append("")
        lines.append("解读：分位越低表示当前估值相对历史越便宜。")
        lines.append("      分位 <= 30% 判为相对低位，>= 70% 判为相对高位。")
        lines.append("      注：单个指标分位不等于「低估值」结论，须再做行业与同业横向比较。")
        return "\n".join(lines)

    def format_markdown_report(self, ts_code, results, source_note=""):
        """
        渲染 Markdown 报告

        Args:
            ts_code (str): 证券代码
            results (list): ValuationPercentileResult 列表
            source_note (str, optional): 取数通路说明

        Returns:
            str: Markdown 文本
        """
        lines = []
        lines.append("# %s 历史估值分位" % ts_code)
        lines.append("")
        if source_note:
            lines.append("> 取数通路：%s" % source_note)
            lines.append(">")
        lines.append("> 分位口径：`(区间内低于当前值的样本数 / 有效样本总数) x 100%`，亏损期不计入样本")
        lines.append("")

        available = self._filter_available(results)
        if not available:
            lines.append("**失败**：无任何指标可计算出分位。")
            lines.append("")
            return "\n".join(lines)

        lines.append("## 估值分位")
        lines.append("")
        lines.append("| 指标 | 当前值 | 历史分位 | 样本数 | 区间中位数 | 区间最小 | 区间最大 | 档位 |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
        for item in available:
            lines.append(
                "| %s | %s | **%.1f%%** | %d | %s | %s | %s | %s |"
                % (self.indicator_label(item.indicator),
                   self._format_number(item.current_value),
                   item.percentile,
                   item.sample_count,
                   self._format_number(item.median_value),
                   self._format_number(item.min_value),
                   self._format_number(item.max_value),
                   item.level)
            )
        lines.append("")

        lines.append("## 口径说明")
        lines.append("")
        lines.append("- 计算区间：%s" % available[0].interval_text)
        lines.append("- 分位定义：当前值越低分位越低；分位 20% 表示比过去 80% 的时间都便宜")
        lines.append("- 亏损期（估值 <= 0）不参与计算，避免亏损期扭曲分位")
        lines.append("- 档位划分：<= 30% 相对低位，30%~70% 中性，>= 70% 相对高位")
        lines.append("- 单指标分位不等于「低估值」结论：需与行业、同业横向比较后再判断")
        return "\n".join(lines)

    # =========================================================================
    # 工具方法
    # =========================================================================

    def indicator_label(self, indicator):
        """
        把指标列名转为中文标签

        Args:
            indicator (str): 指标列名

        Returns:
            str: 中文标签；未知指标回退为原始列名
        """
        return _INDICATOR_LABELS.get(indicator, indicator)

    def _filter_available(self, results):
        """
        筛出成功算出分位的结果

        Args:
            results (list): ValuationPercentileResult 列表

        Returns:
            list: 有效结果列表
        """
        available = []
        for item in results:
            if item.is_available():
                available.append(item)
        return available

    def _format_number(self, value):
        """
        格式化数值；None 显示为占位符

        Args:
            value (float): 待格式化数值

        Returns:
            str: 格式化后的文本
        """
        if value is None:
            return _EMPTY_TEXT
        return "%.2f" % value

