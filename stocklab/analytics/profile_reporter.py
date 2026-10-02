#!/usr/bin/env python3
"""
==============================================================================
StockLab - 市盈率分布报告渲染器 (stocklab.analytics.profile_reporter)
==============================================================================

【模块职责】
   把 ValuationDistributionProfile 排版成纯文本统计报告，
   支持直接打印到控制台或落盘为 .txt 文件。

【为何与计算分离】
   计算与呈现是两种关注点：本模块只负责「读结果、拼字符串」，
   不做任何统计运算，也不依赖 pandas。更换输出形态（HTML / 数据库落盘）
   时只需另写渲染层，分析器无需改动。

【排版约定】
   控制台里的中文字符占两格，直接用 % 对齐会错位，
   因此本模块用 east_asian_width 计算视觉宽度后再补空格，保证列对齐。

【依赖清单】
   标准库：logging、os、unicodedata
==============================================================================
"""

import logging
import os
import unicodedata

from stocklab.analytics.valuation_distribution import (
    PE_MIN_REASONABLE_SAMPLE_COUNT,
    ValuationDistributionProfile,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "ValuationDistributionReporter",
]

# 报告宽度（视觉列数）
_REPORT_WIDTH = 78

# 直方图条形长度（视觉列数）
_BAR_WIDTH = 30

# 空值占位符，避免报告里出现 "None"
_EMPTY_TEXT = "-"


class ValuationDistributionReporter:
    """
    市盈率分布文本报告渲染器
    """

    def __init__(self, title="A 股全市场市盈率分布统计"):
        """
        初始化报告渲染器

        Args:
            title (str, optional): 报告标题
        """
        self._title = title

    def format_text_report(self, profile, source_note=""):
        """
        将统计结果排版为纯文本报告

        Args:
            profile (ValuationDistributionProfile): 分析器产出的统计结果实体
            source_note (str, optional): 数据来源说明，会原样打印在报告头部

        Returns:
            str: 完整的多行文本报告
        """
        lines = []

        self._append_header(lines, profile, source_note)

        if profile.is_empty():
            lines.append("")
            lines.append("[统计失败] %s" % profile.status)
            lines.append("")
            return "\n".join(lines)

        self._append_sample_breakdown(lines, profile)
        self._append_central_tendency(lines, profile)
        self._append_quantiles(lines, profile)
        self._append_bucket_histogram(lines, profile)
        self._append_market_breakdown(lines, profile)
        self._append_rank_entries(lines, profile, "PE 最低 %d 只" % len(profile.lowest_pe_entries),
                                  profile.lowest_pe_entries)
        self._append_rank_entries(lines, profile, "PE 最高 %d 只" % len(profile.highest_pe_entries),
                                  profile.highest_pe_entries)
        self._append_footer(lines, profile)

        # 逐行去掉补齐列宽留下的行尾空格，避免报告被单独转发时携带无意义空白
        return "\n".join(line.rstrip() for line in lines)

    def write_text_report(self, profile, output_path, source_note=""):
        """
        将统计结果落盘为 UTF-8 文本报告

        Args:
            profile (ValuationDistributionProfile): 统计结果实体
            output_path (str): 输出文件路径；父目录不存在时自动创建
            source_note (str, optional): 数据来源说明

        Returns:
            str: 实际写入的文件路径；写入失败返回空字符串
        """
        content = self.format_text_report(profile, source_note)

        parent_dir = os.path.dirname(os.path.abspath(output_path))
        try:
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as file_handle:
                file_handle.write(content + "\n")
        except OSError as error:
            _logger.error("市盈率分布报告写入失败 [%s]: %s", output_path, error)
            return ""

        _logger.info("市盈率分布报告已保存至: %s", output_path)
        return output_path

    # =========================================================================
    # 报告分段（每个方法负责一个区块，保证主流程只体现结构不掺杂排版细节）
    # =========================================================================

    def _append_header(self, lines, profile, source_note):
        """报告头部：标题、数据口径与样本总量"""
        lines.append("=" * _REPORT_WIDTH)
        lines.append(" " + self._title)
        lines.append("=" * _REPORT_WIDTH)
        lines.append(self._field("统计口径", profile.pe_column))
        lines.append(self._field("数据日期", profile.trade_date or _EMPTY_TEXT))
        if source_note:
            lines.append(self._field("取数通路", source_note))
        lines.append(self._field("样本总数", "%d 只" % profile.total_stock_count))
        if not profile.is_empty() and not profile.is_representative():
            lines.append(
                self._field("样本代表性", "不足（有效样本 %d 只 < %d 只），分布结论不成立"
                            % (profile.valid_pe_count, PE_MIN_REASONABLE_SAMPLE_COUNT))
            )
        lines.append("")

    def _append_sample_breakdown(self, lines, profile):
        """样本构成区块：说明哪些样本被计入统计、哪些被排除"""
        lines.append("-" * _REPORT_WIDTH)
        lines.append("【样本构成】")
        lines.append(
            "  "
            + self._field("有效 PE 样本", "%d 只 (%.1f%%)"
                          % (profile.valid_pe_count, profile.coverage_ratio() * 100))
        )
        lines.append(
            "  "
            + self._field("无有效 PE", "%d 只 (%.1f%%)  <- 亏损或数据源未提供"
                          % (profile.invalid_pe_count,
                             self._ratio_percent(profile.invalid_pe_count, profile.total_stock_count)))
        )
        lines.append(
            "  "
            + self._field("PE <= 0", "%d 只  <- 已排除出分位数统计"
                          % profile.non_positive_pe_count)
        )
        lines.append(
            "  "
            + self._field("PE > %.0f 倍" % profile.extreme_high_threshold,
                          "%d 只 (%.1f%%)  <- 已排除出截尾均值"
                          % (profile.extreme_high_count,
                             self._ratio_percent(profile.extreme_high_count, profile.valid_pe_count)))
        )
        lines.append("")

    def _append_central_tendency(self, lines, profile):
        """集中趋势区块：突出中位数才是分布中心，均值仅作参考"""
        lines.append("-" * _REPORT_WIDTH)
        lines.append("【集中趋势】（仅统计 PE > 0 的有效样本）")
        lines.append("  " + self._field("最小值", self._ratio_text(profile.min_pe)))
        lines.append("  " + self._field("最大值", self._ratio_text(profile.max_pe)))
        lines.append("  " + self._field("算术均值", self._ratio_text(profile.mean_pe)
                                        + "   <- 右偏严重，仅供参考"))
        lines.append("  " + self._field("中位数", self._ratio_text(profile.median_pe)
                                        + "   <- 分布中心，优先参考此值"))
        lines.append("  " + self._field("截尾均值", self._ratio_text(profile.trimmed_mean_pe)
                                        + "   <- 已剔除 > %.0f 倍极端值"
                                        % profile.extreme_high_threshold))
        lines.append("")

    def _append_quantiles(self, lines, profile):
        """
        分位数区块

        【为何不配条形】
          市盈率右偏极端（全市场最大值常达数千倍），若以最大值为满格做线性映射，
          P5~P95 的条形会全部塌缩成空条，反而误导读者。此处只给数值，
          分布形态由下方「区间分布」的直方图承担。
        """
        lines.append("-" * _REPORT_WIDTH)
        lines.append("【分位数分布】（由低到高，反映分布形态）")
        for item in profile.quantiles:
            lines.append("  " + self._pad(item[0], 8) + self._pad(self._ratio_text(item[1]), 14)
                         + self._render_quantile_note(item[0], profile))
        lines.append("")

    def _render_quantile_note(self, label, profile):
        """
        为关键分位点追加一句解读

        Args:
            label (str): 分位点展示名，如 "P50"
            profile (ValuationDistributionProfile): 统计结果实体

        Returns:
            str: 注释文本；非关键分位点返回空字符串
        """
        if label == "P50":
            return "<- 中位数，即分布中心"
        if label == "P99":
            return "<- 前 1% 样本的上沿"
        return ""

    def _append_bucket_histogram(self, lines, profile):
        """区间分布区块：固定语义分桶的直方图"""
        lines.append("-" * _REPORT_WIDTH)
        lines.append("【区间分布】（左开右闭，如 20-30 表示 (20, 30] 倍）")
        for bucket in profile.buckets:
            ratio = bucket.ratio()
            count_text = "%d 只" % bucket.stock_count
            lines.append(
                "  "
                + self._pad(bucket.label, 10)
                + self._pad(count_text, 10)
                + self._pad("%.1f%%" % (ratio * 100), 8)
                + self._render_ratio_bar(ratio)
            )
        lines.append("")

    def _append_market_breakdown(self, lines, profile):
        """分交易所区块：对比沪 / 深 / 北的估值水平"""
        if not profile.market_distributions:
            return
        lines.append("-" * _REPORT_WIDTH)
        lines.append("【分交易所对比】")
        lines.append("  " + self._pad("交易所", 10) + self._pad("有效样本", 10)
                     + self._pad("中位数", 12) + self._pad("最小值", 12) + "最大值")
        for item in profile.market_distributions:
            lines.append(
                "  "
                + self._pad(item.market, 10)
                + self._pad("%d 只" % item.valid_pe_count, 10)
                + self._pad(self._ratio_text(item.median_pe), 12)
                + self._pad(self._ratio_text(item.min_pe), 12)
                + self._ratio_text(item.max_pe)
            )
        lines.append("")

    def _append_rank_entries(self, lines, profile, title, entries):
        """极值榜单区块：带序号、代码、简称、交易所与 PE"""
        if not entries:
            return
        lines.append("-" * _REPORT_WIDTH)
        lines.append("【%s】" % title)
        for index in range(len(entries)):
            entry = entries[index]
            code_text = "%d. %s %s" % (index + 1, entry.ts_code, entry.name)
            lines.append(
                "  "
                + self._pad(code_text, 34)
                + self._pad(entry.market, 8)
                + self._ratio_text(entry.pe_value)
            )
        lines.append("")

    def _append_footer(self, lines, profile):
        """报告尾部：复述统计口径，避免报告被单独转发时失去上下文"""
        lines.append("=" * _REPORT_WIDTH)
        lines.append("口径说明：PE %s；已排除 PE <= 0 的亏损样本与无数据样本。" % profile.pe_column)
        lines.append("          A 股市盈率分布右偏，判断整体估值高低请以中位数为准。")
        lines.append("=" * _REPORT_WIDTH)

    # =========================================================================
    # 排版基础工具
    # =========================================================================

    def _field(self, label, value):
        """渲染一行「标签 + 值」，标签列宽对齐"""
        return self._pad(label, 16) + value

    def _ratio_text(self, value):
        """把市盈率数值格式化为带「倍」后缀的文本；None 显示为占位符"""
        if value is None:
            return _EMPTY_TEXT
        return "%.2f 倍" % value

    def _ratio_percent(self, count, total):
        """计算占比百分比；分母为 0 时返回 0.0"""
        if not total:
            return 0.0
        return count / total * 100.0

    def _render_ratio_bar(self, ratio):
        """
        按 0 ~ 1 的比例渲染条形

        Args:
            ratio (float): 填充比例，超出 0 ~ 1 时按边界截断

        Returns:
            str: 条形文本
        """
        filled_length = int(ratio * _BAR_WIDTH + 0.5)
        if filled_length < 0:
            filled_length = 0
        if filled_length > _BAR_WIDTH:
            filled_length = _BAR_WIDTH
        return "#" * filled_length + "." * (_BAR_WIDTH - filled_length)

    def _pad(self, text, visual_width):
        """
        按视觉宽度右侧补空格，使中英文混排的列能够对齐

        Args:
            text (str): 待补齐的文本
            visual_width (int): 目标视觉列数

        Returns:
            str: 补齐后的文本；已超宽时原样返回
        """
        current_width = self._visual_width(text)
        if current_width >= visual_width:
            return text
        return text + " " * (visual_width - current_width)

    def _visual_width(self, text):
        """计算字符串的视觉列数：东亚全角字符按 2 列计"""
        width = 0
        for char in text:
            if unicodedata.east_asian_width(char) in ("W", "F"):
                width += 2
            else:
                width += 1
        return width
