#!/usr/bin/env python3
"""
==============================================================================
StockLab - 个股历史估值分位 CLI (app/scripts/analyze_valuation_percentile.py)
==============================================================================

【功能用途】
   计算单只股票当前 PE / PB / PCF 在历史区间中的分位，替代「处于低位 / 高位」
   这类无法复核的定性描述。

【分位口径】
   分位 = (区间内低于当前值的样本数 / 有效样本总数) x 100%
   例：PE 分位 20%，表示当前价格比过去 80% 的时间都便宜。
   亏损期（PE <= 0）不参与计算。

【执行流程】
   1. 经 stocklab.facade.MarketDataFacade 取历史估值序列（本地未命中时回退远端并回写）
   2. 交给 stocklab.analytics 的 ValuationPercentileAnalyzer 做纯计算
   3. 由 ValuationPercentileReporter 渲染为控制台文本与可选 Markdown

【运行方式】
   # 查某只股票的历史估值分位
   ./venv/bin/python app/scripts/analyze_valuation_percentile.py 600519.SH

   # 指定区间与当前值（当前值缺省取历史序列中最新的一个）
   ./venv/bin/python app/scripts/analyze_valuation_percentile.py 600519.SH \
       --start-date 2021-01-01 --pe-ttm 19.32

   # 导出 Markdown
   ./venv/bin/python app/scripts/analyze_valuation_percentile.py 600519.SH \
       --markdown output/600519_percentile.md
==============================================================================
"""

import argparse
import logging
import os
import sys

# 将项目根目录加入模块搜索路径，保证直接运行脚本时能 import stocklab
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from stocklab.analytics import (
    VALUATION_INDICATOR_PB,
    VALUATION_INDICATOR_PE_TTM,
    ValuationPercentileAnalyzer,
    ValuationPercentileReporter,
)
from stocklab.common.config import load_data_source_priority
from stocklab.common.http_client import install_browser_user_agent
from stocklab.facade import MarketDataFacade

_logger = logging.getLogger("StockLab.ValuationPercentile")


def parse_args():
    """
    解析命令行参数

    Returns:
        argparse.Namespace: 解析结果
    """
    parser = argparse.ArgumentParser(
        description="个股历史估值分位分析工具（替代定性的高低位描述）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("ts_code", type=str, help="证券代码，如 600519.SH")
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="计算区间起始日期 YYYY-MM-DD；缺省为全部历史",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="计算区间结束日期 YYYY-MM-DD；缺省为最新交易日",
    )
    parser.add_argument(
        "--period",
        type=str,
        default="全部",
        help="向远端请求的历史区间：近五年 / 近十年 / 全部（默认 全部）",
    )
    parser.add_argument(
        "--pe-ttm",
        type=float,
        default=None,
        help="当前 PE-TTM；缺省时取历史序列中最新的一个值",
    )
    parser.add_argument(
        "--pb",
        type=float,
        default=None,
        help="当前 PB；缺省时取历史序列中最新的一个值",
    )
    parser.add_argument(
        "--priority",
        type=str,
        choices=["local_first", "remote_first"],
        default=None,
        help="取数优先级；缺省时读取 config.ini 的 [data_source] priority",
    )
    parser.add_argument(
        "--markdown",
        type=str,
        default=None,
        help="Markdown 报告落盘路径；缺省时不生成",
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
    生成取数通路的可读说明

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


def build_current_values(args):
    """
    组装命令行显式传入的当前值

    Args:
        args (argparse.Namespace): 解析后的命令行参数

    Returns:
        dict: {指标列名: 当前值}；未传入任何当前值时返回空字典
    """
    current_values = {}
    if args.pe_ttm is not None:
        current_values[VALUATION_INDICATOR_PE_TTM] = args.pe_ttm
    if args.pb is not None:
        current_values[VALUATION_INDICATOR_PB] = args.pb
    return current_values


def write_markdown(path, content):
    """
    将 Markdown 报告落盘

    Args:
        path (str): 输出文件路径
        content (str): Markdown 文本

    Returns:
        str: 实际写入的路径；失败返回空字符串
    """
    parent_dir = os.path.dirname(os.path.abspath(path))
    try:
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as file_handle:
            file_handle.write(content + "\n")
    except OSError as error:
        _logger.error("分位报告写入失败 [%s]: %s", path, error)
        return ""
    return path


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

    # 全局安装浏览器 UA 补丁（规避东财 WAF 反爬阻断），仅在入口显式调用一次
    install_browser_user_agent()

    args = parse_args()

    facade_priority = args.priority
    if facade_priority is None:
        facade_priority = load_data_source_priority(args.config)

    with MarketDataFacade(priority=facade_priority, db_path=args.db_path) as facade:
        _logger.info("查询 %s 的历史估值序列 ...", args.ts_code)
        history_df = facade.fetch_valuation_history(args.ts_code, args.period)
        source_note = describe_priority(facade.priority)

    _logger.info("历史估值样本: %d 行", len(history_df))

    analyzer = ValuationPercentileAnalyzer()
    results = analyzer.analyze(
        history_df,
        current_values=build_current_values(args),
        start_date=args.start_date,
        end_date=args.end_date,
    )

    reporter = ValuationPercentileReporter()
    print(reporter.format_console_report(args.ts_code, results, source_note))

    if args.markdown:
        content = reporter.format_markdown_report(args.ts_code, results, source_note)
        saved_path = write_markdown(args.markdown, content)
        if saved_path:
            print("\n[成功] 分位 Markdown 报告已生成: %s" % saved_path)

    if not any(item.is_available() for item in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
