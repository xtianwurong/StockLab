#!/usr/bin/env python3
"""
==============================================================================
StockLab - 市盈率分布 Markdown 报告渲染器 (stocklab.analytics.markdown_reporter)
==============================================================================

【模块职责】
   把 ValuationDistributionProfile 渲染为 Markdown 格式的归档报告。
   与 profile_reporter（纯文本报告）并列，是同一份统计结果的第二种呈现形态。

【为何独立成模块而不是给文本渲染器加开关】
   两种输出的结构差异很大：纯文本靠视觉宽度对齐与 ASCII 条形，
   Markdown 靠表格语法与代码块。塞进同一个类会出现大量互相干扰的分支。
   「同一份结果、多份呈现」的扩展点应落在渲染层，而不是计算层。

【归档场景考虑】
   本渲染器的目标是产出「可长期留存、可被 grep、可被其他文档引用」的记录文件：
     - 表格化统计，便于检索「多少只 PE 在 20-30 区间」这类问题；
     - 顶部给出自动生成的中文结论句，读文件第一行即可掌握全貌；
     - 保留口径说明，避免脱离上下文的报告被误引用。

【依赖清单】
   标准库：logging、os
==============================================================================
"""

import logging
import os
from datetime import datetime

from stocklab.analytics.valuation_distribution import (
    PE_MIN_REASONABLE_SAMPLE_COUNT,
    ValuationDistributionProfile,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "ValuationDistributionMarkdownReporter",
]

# 极值榜单展示条数（Markdown 表格比控制台宽，适当放宽）
_DEFAULT_RANK_ENTRY_COUNT = 15

# 空值占位符
_EMPTY_TEXT = "-"


class ValuationDistributionMarkdownReporter:
    """
    市盈率分布 Markdown 归档报告渲染器
    """

    def __init__(self, title="A 股全市场市盈率分布统计", rank_entry_count=_DEFAULT_RANK_ENTRY_COUNT):
        """
        初始化 Markdown 报告渲染器

        Args:
            title (str, optional): 报告标题（一级标题）
            rank_entry_count (int, optional): 极值榜单在报告中展示的条数上限
        """
        self._title = title
        self._rank_entry_count = rank_entry_count

    def format_markdown_report(self, profile, source_note=""):
        """
        将统计结果渲染为 Markdown 报告

        Args:
            profile (ValuationDistributionProfile): 分析器产出的统计结果实体
            source_note (str, optional): 数据来源说明

        Returns:
            str: 完整的 Markdown 文本
        """
        lines = []

        lines.append("# " + self._title)
        lines.append("")
        lines.append("> 统计时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        lines.append(">")
        lines.append("> 统计口径：`%s` ｜ 数据日期：`%s` ｜ 取数通路：%s"
                     % (profile.pe_column, profile.trade_date or _EMPTY_TEXT,
                        source_note if source_note else _EMPTY_TEXT))

        if profile.is_empty():
            lines.append(">")
            lines.append("> **统计失败**：%s" % profile.status)
            lines.append("")
            return "\n".join(lines)

        if not profile.is_representative():
            lines.append(">")
            lines.append("> ⚠️ **样本代表性不足**：有效样本 %d 只，低于全市场量级 %d 只，"
                         "本报告的分布结论不成立，请先补齐本地估值快照。"
                         % (profile.valid_pe_count, PE_MIN_REASONABLE_SAMPLE_COUNT))

        lines.append("")
        lines.append(self._build_conclusion(profile))
        lines.append("")
        self._append_sample_breakdown(lines, profile)
        self._append_central_tendency(lines, profile)
        self._append_quantiles(lines, profile)
        self._append_bucket_histogram(lines, profile)
        self._append_market_breakdown(lines, profile)
        self._append_rank_table(lines, profile, "PE 最低 %d 只" % self._rank_entry_count,
                                profile.lowest_pe_entries)
        self._append_rank_table(lines, profile, "PE 最高 %d 只" % self._rank_entry_count,
                                profile.highest_pe_entries)
        self._append_footer(lines, profile)

        return "\n".join(lines)

    def write_markdown_report(self, profile, output_path, source_note=""):
        """
        将统计结果落盘为 UTF-8 Markdown 报告

        Args:
            profile (ValuationDistributionProfile): 统计结果实体
            output_path (str): 输出文件路径；父目录不存在时自动创建
            source_note (str, optional): 数据来源说明

        Returns:
            str: 实际写入的文件路径；写入失败返回空字符串
        """
        content = self.format_markdown_report(profile, source_note)

        parent_dir = os.path.dirname(os.path.abspath(output_path))
        try:
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as file_handle:
                file_handle.write(content + "\n")
        except OSError as error:
            _logger.error("市盈率分布 Markdown 报告写入失败 [%s]: %s", output_path, error)
            return ""

        _logger.info("市盈率分布 Markdown 报告已保存至: %s", output_path)
        return output_path

    # =========================================================================
    # 报告分段
    # =========================================================================

    def _build_conclusion(self, profile):
        """
        自动生成一句话结论

        【设计意图】
           归档报告常被单独翻阅，读者不该必须读到表格末尾才能明白核心信息。
           结论只陈述统计事实，不做投资判断。

        Args:
            profile (ValuationDistributionProfile): 统计结果实体

        Returns:
            str: 结论文本（含分位数集中区间与极端值占比）
        """
        p25 = self._quantile_value(profile, "P25")
        p50 = self._quantile_value(profile, "P50")
        p75 = self._quantile_value(profile, "P75")
        p90 = self._quantile_value(profile, "P90")

        parts = []
        parts.append("全市场有效样本 **%d 只**（占全市场 %.1f%%）"
                     % (profile.valid_pe_count, profile.coverage_ratio() * 100))
        if p50 is not None:
            parts.append("市盈率中位数为 **%.2f 倍**" % p50)
        if p25 is not None and p75 is not None:
            parts.append("中间 50%% 的个股集中在 **%.2f ~ %.2f 倍**区间" % (p25, p75))
        if p90 is not None:
            parts.append("90%% 的个股低于 **%.2f 倍**" % p90)
        parts.append("均值 %.2f 倍因受极端值拉偏仅供参考"
                     % profile.mean_pe if profile.mean_pe is not None else "均值不可用")
        if profile.invalid_pe_count > 0:
            parts.append("另有 **%d 只** 因亏损或数据缺失无有效 PE，已排除统计"
                         % profile.invalid_pe_count)

        return "**结论**：" + "；".join(parts) + "。"

    def _append_sample_breakdown(self, lines, profile):
        """样本构成表格"""
        lines.append("## 一、样本构成")
        lines.append("")
        lines.append("| 样本类别 | 只数 | 占比 | 说明 |")
        lines.append("| --- | ---: | ---: | --- |")
        lines.append("| 全市场样本 | %d | 100.0%% | 证券全集 |" % profile.total_stock_count)
        lines.append("| **有效 PE 样本** | **%d** | **%.1f%%** | 参与全部统计 |"
                     % (profile.valid_pe_count, profile.coverage_ratio() * 100))
        lines.append("| 无有效 PE | %d | %.1f%% | 亏损或数据源未提供该字段 |"
                     % (profile.invalid_pe_count,
                        self._percent_of(profile.invalid_pe_count, profile.total_stock_count)))
        lines.append("| PE ≤ 0 | %d | %.1f%% | 已排除出分位数统计 |"
                     % (profile.non_positive_pe_count,
                        self._percent_of(profile.non_positive_pe_count, profile.total_stock_count)))
        lines.append("| PE > %.0f 倍 | %d | %.1f%% | 极端高值，已排除出截尾均值 |"
                     % (profile.extreme_high_threshold, profile.extreme_high_count,
                        self._percent_of(profile.extreme_high_count, profile.valid_pe_count)))
        lines.append("")

    def _append_central_tendency(self, lines, profile):
        """集中趋势表格"""
        lines.append("## 二、集中趋势")
        lines.append("")
        lines.append("> 仅统计 `PE > 0` 的有效样本。")
        lines.append("")
        lines.append("| 指标 | 数值 | 含义 |")
        lines.append("| --- | ---: | --- |")
        lines.append("| 最小值 | %s | 全市场最低 |" % self._ratio_text(profile.min_pe))
        lines.append("| 最大值 | %s | 全市场最高 |" % self._ratio_text(profile.max_pe))
        lines.append("| 算术均值 | %s | 右偏严重，**仅供参考** |" % self._ratio_text(profile.mean_pe))
        lines.append("| **中位数** | **%s** | **分布中心，判断估值高低以此为准** |"
                     % self._ratio_text(profile.median_pe))
        lines.append("| 截尾均值 | %s | 已剔除 > %.0f 倍极端值后的均值 |"
                     % (self._ratio_text(profile.trimmed_mean_pe), profile.extreme_high_threshold))
        lines.append("")

    def _append_quantiles(self, lines, profile):
        """分位数表格"""
        lines.append("## 三、分位数分布")
        lines.append("")
        lines.append("| 分位点 | PE-TTM | 累计含义 |")
        lines.append("| --- | ---: | --- |")
        for item in profile.quantiles:
            label = item[0]
            level = self._quantile_ratio(label)
            lines.append("| %s | %.2f 倍 | %.0f%% 的个股低于此值 |"
                         % (label, item[1], level * 100))
        lines.append("")

    def _append_bucket_histogram(self, lines, profile):
        """区间分布表格 —— 本报告的核心结论区"""
        lines.append("## 四、市盈率区间分布")
        lines.append("")
        lines.append("> 区间语义为左开右闭，如 `20-30` 表示 `(20, 30]` 倍。")
        lines.append("")
        lines.append("| PE 区间 | 只数 | 占比 | 分布 |")
        lines.append("| --- | ---: | ---: | --- |")
        for bucket in profile.buckets:
            ratio = bucket.ratio()
            lines.append("| %s | %d | %.1f%% | `%s` |"
                         % (bucket.label, bucket.stock_count, ratio * 100,
                            self._render_ratio_bar(ratio)))
        lines.append("")
        lines.append("**合计**：有效样本 %d 只" % profile.valid_pe_count)
        lines.append("")

    def _append_market_breakdown(self, lines, profile):
        """分交易所对比表格"""
        if not profile.market_distributions:
            return
        lines.append("## 五、分交易所对比")
        lines.append("")
        lines.append("| 交易所 | 有效样本 | 中位数 | 最小值 | 最大值 |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for item in profile.market_distributions:
            lines.append("| %s | %d | %.2f 倍 | %.2f 倍 | %.2f 倍 |"
                         % (item.market, item.valid_pe_count, item.median_pe,
                            item.min_pe, item.max_pe))
        lines.append("")

    def _append_rank_table(self, lines, profile, title, entries):
        """极值榜单表格"""
        if not entries:
            return
        lines.append("## %s" % title)
        lines.append("")
        lines.append("| 排名 | 证券代码 | 证券简称 | 交易所 | PE |")
        lines.append("| ---: | --- | --- | --- | ---: |")
        for index in range(len(entries)):
            entry = entries[index]
            lines.append("| %d | `%s` | %s | %s | %.2f 倍 |"
                         % (index + 1, entry.ts_code, self._escape(entry.name),
                            entry.market if entry.market else _EMPTY_TEXT,
                            entry.pe_value))
        lines.append("")

    def _append_footer(self, lines, profile):
        """口径说明"""
        lines.append("## 口径说明")
        lines.append("")
        lines.append("- 统计口径：`%s`（`pe` 为市盈率动态、`pe_ttm` 为市盈率 TTM，两者不可混用）"
                     % profile.pe_column)
        lines.append("- 数据日期 `%s` 取自估值快照实际样本，**不代表真实交易日**"
                     % (profile.trade_date if profile.trade_date else _EMPTY_TEXT))
        lines.append("- 已排除 `PE ≤ 0` 的亏损样本与无数据样本，二者均不参与分位数与分桶统计")
        lines.append("- A 股市盈率分布严重右偏，**判断整体估值高低请以中位数为准**，均值不可直接引用")
        lines.append("- 分位数由 pandas 线性插值得出，同一份数据可稳定复现")
        lines.append("")

    # =========================================================================
    # 排版工具
    # =========================================================================

    def _quantile_value(self, profile, label):
        """按分位点名称取值；不存在时返回 None"""
        for item in profile.quantiles:
            if item[0] == label:
                return item[1]
        return None

    def _quantile_ratio(self, label):
        """把分位点名称（如 P25）换算成累计比例（0.25）"""
        try:
            return int(label.lstrip("P")) / 100.0
        except ValueError:
            return 0.0

    def _ratio_text(self, value):
        """格式化市盈率为带「倍」的文本；None 显示为占位符"""
        if value is None:
            return _EMPTY_TEXT
        return "%.2f 倍" % value

    def _percent_of(self, count, total):
        """计算占比百分比；分母为 0 时返回 0.0"""
        if not total:
            return 0.0
        return count / total * 100.0

    def _render_ratio_bar(self, ratio):
        """把占比渲染为代码块内的 ASCII 条形"""
        width = 24
        filled_length = int(ratio * width + 0.5)
        if filled_length < 0:
            filled_length = 0
        if filled_length > width:
            filled_length = width
        return "#" * filled_length + "." * (width - filled_length)

    def _escape(self, text):
        """
        转义 Markdown 表格单元内的特殊字符

        【为何需要】
           证券简称可能含竖线（如「某某|转债」），不转义会破坏表格列结构。

        Args:
            text (str): 原始文本

        Returns:
            str: 转义后的文本
        """
        if text is None:
            return _EMPTY_TEXT
        return str(text).replace("|", "\\|")
