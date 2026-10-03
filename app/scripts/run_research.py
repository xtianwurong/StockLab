#!/usr/bin/env python3
"""
 ==============================================================================
StockLab - 研究筛选 CLI (app/scripts/run_research.py)
 ==============================================================================

【功能用途】
   V2 需求 §6 / §8 的研究入口：按结构化条件筛选股票，并把这次研究
   连同它的全部可复现要素（时点、股票池、条件、预处理、版本）写成研究快照，
   之后可以按 snapshot_id **重新生成**并逐行比对，证明结果可复现。

【运行方式】
   # 执行一次筛选并写入研究快照（默认子命令 screen）
   python app/scripts/run_research.py --as-of 2024-06-30 --config configs/screen_value.json

   # 只看条件、不落快照
   python app/scripts/run_research.py screen --as-of 2024-06-30 \
       --config configs/screen_value.json --no-snapshot

   # 按快照重新生成并比对（不一致时退出码为 1）
   python app/scripts/run_research.py rerun --snapshot-id RS20261003120000-a1b2c3

   # 列出全部研究快照
   python app/scripts/run_research.py list

   # 列出已登记因子（含所需输入列与口径说明）
   python app/scripts/run_research.py factors

【配置文件】
   JSON（YAML 同构；本项目未引入 PyYAML，故只接受 JSON），结构与 §8.1 一致：
   {
     "rules": [
       {"factor": "pe_ttm", "operator": "lt", "value": 20},
       {"factor": "roe", "operator": "gt", "value": 0.12},
       {"logic": "or", "rules": [
         {"factor": "dividend_yield", "operator": "gt", "value": 0.03},
         {"factor": "momentum_12m", "operator": "gt", "value": 0.2}
       ]}
     ],
     "preprocess": [{"method": "winsorize", "columns": ["pe_ttm"]}],
     "factors": ["pb"]
   }
"""

import argparse
import datetime
import json
import logging
import os
import sys

# 将项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from stocklab.factor import FACTOR_VERSION, registry as factor_engine
from stocklab.persistence.storage import Database
from stocklab.research import (
    SnapshotError,
    build_factor_frame,
    config_version,
    create_snapshot,
    data_version,
    list_snapshots,
    rerun_snapshot,
)
from stocklab.screener import ScreenError, ScreenPipeline

# 结果表打印上限
_MAX_PRINT_ROWS = 20
_MAX_EXAMPLES = 5


def parse_args():
    """
    解析命令行参数（子命令用可选位置参数实现，公共参数放在子命令前后都生效）

    Returns:
        argparse.Namespace: 命令行参数
    """
    parser = argparse.ArgumentParser(
        description=(
            "研究筛选工具 - 按结构化条件筛选股票并生成可复现的研究快照\n\n"
            "子命令（缺省 = screen）：\n"
            "  screen    执行一次筛选并写入研究快照（用 --config 指定 JSON 条件）\n"
            "  rerun     按 --snapshot-id 重新生成结果并与存档逐行比对\n"
            "  list      列出全部研究快照\n"
            "  factors   列出已登记因子（分类 / 输入列 / 口径）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["screen", "rerun", "list", "factors"],
        default="screen",
        help="要执行的研究动作（默认 screen）",
    )
    parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        help="研究时点 YYYY-MM-DD（默认今天；screen 与帧构造都按它做 Point-in-Time 取数）",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="筛选条件 JSON 文件路径（screen 必填，结构见文件头说明）",
    )
    parser.add_argument(
        "--universe",
        type=str,
        default="A股全市场",
        help="股票池标签，写进快照（默认 A股全市场）",
    )
    parser.add_argument(
        "--ts-codes",
        type=str,
        default=None,
        help="限定证券代码，逗号分隔（如 600519.SH,000001.SZ）；缺省为 as-of 全市场",
    )
    parser.add_argument(
        "--snapshot-id",
        type=str,
        default=None,
        help="rerun 子命令要复现的快照编号",
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="screen 子命令不写研究快照（只看结果）",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=_MAX_PRINT_ROWS,
        help="结果表打印行数上限（默认 20）",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="数据库文件路径（默认 data/stocklab.duckdb）",
    )
    return parser.parse_args()


def load_spec(path):
    """
    读取筛选条件 JSON 文件

    Args:
        path (str): JSON 文件路径

    Returns:
        dict: 条件 spec
    """
    if not path:
        raise ScreenError("screen 子命令必须提供 --config 指定条件文件")
    if not os.path.exists(path):
        raise ScreenError("条件文件不存在: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except ValueError as error:
        raise ScreenError("条件文件 %s 不是合法 JSON: %s" % (path, error))


def resolve_as_of(value):
    """
    解析研究时点（缺省取今天）

    Args:
        value (str): "YYYY-MM-DD" 或 None

    Returns:
        datetime.date

    Raises:
        ScreenError: 格式非法
    """
    text = value if value else datetime.date.today().isoformat()
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        raise ScreenError("研究时点格式必须是 YYYY-MM-DD，实际: %s" % value)


def run_screen(args):
    """执行一次筛选：取数 -> 因子 -> 筛选 -> （可选）写快照 -> 打印结果"""
    as_of = resolve_as_of(args.as_of)
    pipeline = ScreenPipeline.from_spec(load_spec(args.config))
    database = Database(args.db_path) if args.db_path else Database()

    ts_codes = (
        [code.strip() for code in args.ts_codes.split(",") if code.strip()]
        if args.ts_codes
        else None
    )
    frame = build_factor_frame(
        as_of,
        ts_codes=ts_codes,
        factor_names=pipeline.factor_names,
        extra_columns=pipeline.input_columns(),
        database=database,
    )
    result = pipeline.run(frame)

    snapshot_id = None
    if not args.no_snapshot:
        snapshot_id = create_snapshot(
            as_of, args.universe, pipeline, result.summary, database
        )

    _print_result(args, as_of, pipeline, result, snapshot_id, database)
    return 0


def _print_result(args, as_of, pipeline, result, snapshot_id, database):
    """打印筛选结果：版本信息 + 通过名单 + 被排除的示例及原因"""
    counts = result.counts
    config = config_version(pipeline.spec())

    print("=" * 78)
    print("研究筛选结果")
    print("=" * 78)
    print("条件    : %s" % result.condition)
    print("时点    : %s    股票池: %s（%d 只）" % (as_of, args.universe, counts["total"]))
    print("通过    : %d / %d" % (counts["passed"], counts["total"]))
    print(
        "版本    : data=%s  factor=%s  config=%s"
        % (data_version(database, as_of), FACTOR_VERSION, config)
    )
    if snapshot_id:
        print("快照    : %s（rerun 用它复现）" % snapshot_id)
    print("-" * 78)

    if counts["total"] == 0:
        print("没有可用标的（股票池为空）。")
        return

    passed_rows = result.summary[result.summary["passed"]]
    columns = ["ts_code", "name"] + [
        name for name in pipeline.factor_names if name in result.summary.columns
    ]
    print("通过名单（前 %d 只）:" % min(args.top, counts["passed"]))
    if counts["passed"]:
        print(passed_rows[columns].head(args.top).to_string(index=False))
    else:
        print("  （无）")

    print("-" * 78)
    failed_rows = result.summary[~result.summary["passed"]]
    print("被排除示例（最多 %d 只，含原因）:" % _MAX_EXAMPLES)
    if len(failed_rows):
        for row in failed_rows.head(_MAX_EXAMPLES).to_dict(orient="records"):
            reasons = "；".join(row["failed_rules"]) if row["failed_rules"] else "-"
            print("  %s %s: %s" % (row["ts_code"], row.get("name", ""), reasons))
    else:
        print("  （无）")


def run_rerun(args):
    """按快照编号重新生成结果并与存档比对；不一致返回 1"""
    if not args.snapshot_id:
        raise ScreenError("rerun 子命令必须提供 --snapshot-id")

    database = Database(args.db_path) if args.db_path else Database()
    outcome = rerun_snapshot(args.snapshot_id, database)

    print("=" * 78)
    print("研究快照复现")
    print("=" * 78)
    print("快照    : %s" % outcome["snapshot_id"])
    print("条件    : %s" % outcome["condition"])
    if outcome["identical"]:
        counts = outcome["result"].counts
        print(
            "结果    : 与存档逐行一致（%d 只标的，通过 %d 只）"
            % (counts["total"], counts["passed"])
        )
        return 0

    print("结果    : 与存档不一致（共 %d 处差异）" % len(outcome["differences"]))
    for difference in outcome["differences"][:_MAX_EXAMPLES]:
        print("  - %s" % difference)
    if len(outcome["differences"]) > _MAX_EXAMPLES:
        print("  ... 其余 %d 处省略" % (len(outcome["differences"]) - _MAX_EXAMPLES))
    return 1


def run_list(args):
    """列出全部研究快照"""
    database = Database(args.db_path) if args.db_path else Database()
    snapshots = list_snapshots(database)
    print("=" * 78)
    print("研究快照（共 %d 条）" % len(snapshots))
    print("=" * 78)
    if not snapshots.empty:
        columns = [
            name
            for name in [
                "snapshot_id", "created_at", "as_of_date", "universe",
                "passed_count", "result_count", "condition",
                "data_version", "factor_version", "config_version",
            ]
            if name in snapshots.columns
        ]
        print(snapshots[columns].to_string(index=False))
    return 0


def run_factors(args):
    """列出已登记因子：分类、输入列与口径说明"""
    print("=" * 78)
    print("已登记因子（%d 个，版本 %s）" % (len(factor_engine.list_factors()), FACTOR_VERSION))
    print("=" * 78)
    for category in ("value", "quality", "growth", "momentum", "dividend", "risk"):
        names = factor_engine.list_factors(category)
        if not names:
            continue
        print("\n[%s]" % category)
        for name in names:
            factor = factor_engine.get(name)
            print(
                "  %-20s 需要列: %s\n      %s"
                % (name, ", ".join(factor.columns), factor.description)
            )
    return 0


def main():
    """命令行入口"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    pandas_options()
    args = parse_args()

    handlers = {
        "screen": run_screen,
        "rerun": run_rerun,
        "list": run_list,
        "factors": run_factors,
    }
    try:
        return handlers[args.command](args) or 0
    except (ScreenError, SnapshotError) as error:
        # 业务性错误（配置非法 / 快照不存在）：只报原因，不刷堆栈
        logging.getLogger(__name__).error("%s", error)
        return 1


def pandas_options():
    """统一结果表打印样式，避免列被折行截断"""
    pd.set_option("display.max_columns", 60)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_colwidth", 40)


if __name__ == "__main__":
    sys.exit(main())
