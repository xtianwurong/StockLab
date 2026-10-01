#!/usr/bin/env python3
"""
==============================================================================
StockLab - A 股核心板块纯正 ETF 与主板 10 年月线走势网页生成器 (scripts/generate_sector_trend.py)
==============================================================================

【说明】
  本脚本为命令行顶层快捷入口，底层核心逻辑已分层封装在 stocklab.visualizer 模块中。
"""

import argparse
import logging
import os
import sys

# 将项目根目录加入模块搜索路径，保证直接运行脚本时能 import stocklab
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.common.config import load_ini_config
from stocklab.visualizer import SectorTrendVisualizer


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # 从 config.ini 读取默认配置供命令行参数回退与提示
    _, default_months, default_output, _ = load_ini_config()

    parser = argparse.ArgumentParser(
        description="A 股核心板块纯正行业 ETF 与主板 10 年月线走势网页生成器"
    )
    parser.add_argument(
        "--months",
        type=int,
        default=default_months,
        help=f"历史月数（默认从 config.ini 读取: {default_months} 个月）",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=default_output,
        help=f"输出 HTML 文件名（默认从 config.ini 读取: {default_output}）",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.ini",
        help="配置文件路径（默认: config.ini）",
    )

    args = parser.parse_args()

    app = SectorTrendVisualizer(num_months=args.months, config_path=args.config)
    result_path = app.generate(output_filename=args.output)
    if result_path:
        print(f"\n[成功] 10 年月线交互式走势网页已生成: {result_path}")
        sys.exit(0)
    else:
        print("\n[失败] 网页生成失败，请检查网络或数据源接口")
        sys.exit(1)


if __name__ == "__main__":
    main()
