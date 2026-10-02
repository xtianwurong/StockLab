#!/usr/bin/env python3
"""
==============================================================================
StockLab - A 股全市场市盈率分布统计 CLI (app/scripts/analyze_pe_distribution.py)
==============================================================================

【功能用途】
   统计 A 股全市场当前的市盈率（PE）分布范围：样本构成、集中趋势、分位数、
   区间分布、分交易所对比，以及 PE 最低 / 最高的具体标的。

【输出形态】
   - 控制台：纯文本报告，便于交互式快速查看；
   - `--output`   : 纯文本报告落盘；
   - `--markdown` : Markdown 报告落盘，适合长期归档与被其他文档引用。

【执行流程】
   1. 经 stocklab.facade.MarketDataFacade 按配置优先级取「全市场估值快照」
      与「全市场证券基础信息」（简称、交易所靠 ts_code 关联）。
   2. 把两张表交给 stocklab.analytics 做纯统计，产出统计结果实体。
   3. 由 stocklab.analytics 的渲染器输出：控制台文本报告，
      并可按参数额外落盘纯文本 / Markdown 两种归档文件。

【为什么市盈率分布必须看中位数】
   A 股市盈率分布严重右偏，少数高成长股 PE 可达数千倍，算术均值被极端值拉偏，
   不代表「典型股票的估值水平」。本脚本同时输出均值与中位数，
   并在报告里明确标注应以中位数为准。

【运行方式】
   # 默认统计 PE-TTM 的最新快照（优先级取自 config.ini）
   ./venv/bin/python scripts/analyze_pe_distribution.py

   # 统计市盈率(动态)口径
   ./venv/bin/python scripts/analyze_pe_distribution.py --pe-column dynamic

   # 强制走远端接口取最新快照（本地库快照过旧时使用）
   ./venv/bin/python scripts/analyze_pe_distribution.py --priority remote_first

   # 指定统计交易日并把报告落盘
   ./venv/bin/python scripts/analyze_pe_distribution.py \
       --trade-date 2026-09-30 --output output/pe_distribution.txt

   # 只归档 Markdown 报告（长期留存、可被 grep 与引用）
   ./venv/bin/python scripts/analyze_pe_distribution.py \
       --markdown output/pe_distribution.md

   # 同时输出两种形态
   ./venv/bin/python scripts/analyze_pe_distribution.py \
       --output output/pe_distribution.txt --markdown output/pe_distribution.md
==============================================================================
"""

import argparse
import logging
import os
import sys

# 将项目根目录加入模块搜索路径，保证直接运行脚本时能 import stocklab
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.analytics import (
    PE_MIN_REASONABLE_SAMPLE_COUNT,
    PE_VALUE_COLUMN_DYNAMIC,
    PE_VALUE_COLUMN_TTM,
    ValuationDistributionAnalyzer,
    ValuationDistributionMarkdownReporter,
    ValuationDistributionReporter,
)
from stocklab.common.config import load_data_source_priority
from stocklab.facade import MarketDataFacade

_logger = logging.getLogger("StockLab.AnalyzePeDistribution")

# 命令行 PE 口径选项到实际列名的映射
_PE_COLUMN_CHOICES = {
    "ttm": PE_VALUE_COLUMN_TTM,
    "dynamic": PE_VALUE_COLUMN_DYNAMIC,
}


def parse_args():
    """
    解析命令行参数

    Returns:
        argparse.Namespace: 解析结果
    """
    parser = argparse.ArgumentParser(
        description="A 股全市场市盈率分布统计工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--pe-column",
        type=str,
        choices=list(_PE_COLUMN_CHOICES.keys()),
        default="ttm",
        help="统计口径：ttm = 市盈率 TTM（默认，跨期可比）；dynamic = 市盈率(动态)",
    )
    parser.add_argument(
        "--trade-date",
        type=str,
        default=None,
        help="统计交易日，格式 YYYY-MM-DD；缺省时取本地库最新交易日的快照",
    )
    parser.add_argument(
        "--priority",
        type=str,
        choices=["local_first", "remote_first"],
        default=None,
        help="取数优先级；缺省时读取 config.ini 的 [data_source] priority",
    )
    parser.add_argument(
        "--extreme-threshold",
        type=float,
        default=1000.0,
        help="极端高 PE 阈值（倍），超过者不计入截尾均值（默认 1000）",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="PE 最低 / 最高各展示多少只标的（默认 10）",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="纯文本报告落盘路径；缺省时仅打印到控制台",
    )
    parser.add_argument(
        "--markdown",
        type=str,
        default=None,
        help="Markdown 报告落盘路径，适合长期归档；缺省时不生成",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.ini",
        help="配置文件路径（取数优先级），默认 config.ini",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="本地 DuckDB 文件路径（默认 data/stocklab.duckdb）",
    )
    return parser.parse_args()


def describe_priority(priority):
    """
    生成取数通路的可读说明，写入报告头部

    Args:
        priority (str): 实际生效的取数优先级

    Returns:
        str: 中文说明文本
    """
    if priority == "remote_first":
        return "远端接口优先（本地库仅作回退）"
    if priority == "local_first":
        return "本地 DuckDB 优先（未命中回退远端并回写）"
    return "未知优先级 [%s]" % priority


def warn_if_sample_not_representative(profile):
    """
    样本量不足时给出告警并说明补救动作

    【为何仍需在入口侧告警】
       代表性判定已下沉到库层（ValuationDistributionProfile.is_representative），
       报告本身也会标注。此处补充「怎么补救」——库层只描述事实，
       不该知道 sync_market_data.py 这个具体脚本的存在。

    Args:
        profile (ValuationDistributionProfile): 统计结果实体
    """
    if profile.is_empty() or profile.is_representative():
        return

    _logger.warning(
        "有效样本仅 %d 只（低于代表性量级 %d 只），本次统计不具全市场代表性：",
        profile.valid_pe_count,
        PE_MIN_REASONABLE_SAMPLE_COUNT,
    )
    _logger.warning(
        "  本地估值快照可能不完整。请先执行 "
        "./venv/bin/python scripts/sync_market_data.py valuations，"
        "或改用 --priority remote_first 直接取远端快照。"
    )


def main():
    """
    主入口函数

    Returns:
        None: 成功退出码 0，失败退出码 1
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = parse_args()
    pe_column = _PE_COLUMN_CHOICES[args.pe_column]

    # 取数优先级来源：命令行显式指定 > config.ini 的 [data_source] > 库内默认值
    facade_priority = args.priority
    if facade_priority is None:
        facade_priority = load_data_source_priority(args.config)

    with MarketDataFacade(priority=facade_priority, db_path=args.db_path) as facade:
        _logger.info("取数优先级: %s | 统计口径: %s", facade.priority, pe_column)

        valuation_df = facade.fetch_valuations(args.trade_date)
        _logger.info("已获取全市场估值快照: %d 条记录", len(valuation_df))

        securities_df = facade.fetch_securities()
        _logger.info("已获取全市场证券基础信息: %d 条记录", len(securities_df))

        source_note = describe_priority(facade.priority)

    analyzer = ValuationDistributionAnalyzer(
        pe_column=pe_column,
        extreme_high_threshold=args.extreme_threshold,
        extreme_entry_count=args.top,
    )
    profile = analyzer.analyze(valuation_df, securities_df)
    warn_if_sample_not_representative(profile)

    reporter = ValuationDistributionReporter()
    print(reporter.format_text_report(profile, source_note))

    if args.output:
        reporter.write_text_report(profile, args.output, source_note)

    if args.markdown:
        markdown_reporter = ValuationDistributionMarkdownReporter()
        saved_path = markdown_reporter.write_markdown_report(profile, args.markdown, source_note)
        if saved_path:
            print("\n[成功] 市盈率分布 Markdown 报告已生成: %s" % saved_path)

    if profile.is_empty():
        sys.exit(1)


if __name__ == "__main__":
    main()
