#!/usr/bin/env python3
"""
==============================================================================
StockLab - 统一同步入口 (app/scripts/sync_all.py)
==============================================================================

【功能用途】
   统一调度所有同步脚本，提供单一入口：
     python app/scripts/sync_all.py

【运行方式】
   # 全量同步（按依赖顺序执行，跳过可选阶段）
   python app/scripts/sync_all.py

   # 仅同步核心行情数据（securities -> prices -> valuations -> indexes -> lifecycle）
   python app/scripts/sync_all.py --core-only

   # 仅同步基本面数据（需先有 securities）
   python app/scripts/sync_all.py --fundamentals --workers 8

   # 仅同步大宗商品
   python app/scripts/sync_all.py --commodities

   # 仅同步投资人观点
   python app/scripts/sync_all.py --insight

   # 仅同步公告（需显式指定起始日期首次运行）
   python app/scripts/sync_all.py --announcements --start-date 2015-01-01

   # 仅同步基金域（基金信息/净值/持仓 + 申万口径资金流）
   python app/scripts/sync_all.py --funds
   python app/scripts/sync_all.py --funds --fund-codes 000001.OF,161725.OF

   # 指定数据库路径
   python app/scripts/sync_all.py --db-path /path/to/stocklab.duckdb

【执行顺序与依赖】
   1. securities    （基础信息，所有后续阶段的前提）
   2. prices        （日 K 行情，依赖 securities）
   3. valuations    （估值快照，依赖 prices）
   4. indexes       （指数成分，依赖 securities）
   5. lifecycle     （生命周期，依赖 securities）
   6. fundamentals  （基本面，依赖 securities；可选，耗时最长）
   7. commodities   （大宗商品，无依赖；可选）
   8. insight       （投资人观点，依赖 securities；可选）
   9. funds          （基金域：信息/净值/持仓/资金流；可选）
  10. announcements （公告，依赖 securities；可选，首次需 --start-date）

【退出码语义】
   0  全部成功
   1  有阶段失败（详见日志）
   2  参数错误
"""

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta

# 将项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from stocklab.common.http_client import install_browser_user_agent
from stocklab.persistence.storage import initialize_database

_logger = logging.getLogger(__name__)

__all__ = [
    "parse_args",
    "run_subcommand",
    "main",
]


SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description=(
            "StockLab 统一数据同步入口\n\n"
            "不带参数 = 核心全量同步（securities/prices/valuations/indexes/lifecycle）\n"
            "可选阶段通过 --xxx 标志启用"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 核心开关
    parser.add_argument(
        "--core-only",
        action="store_true",
        help="仅执行核心阶段（securities/prices/valuations/indexes/lifecycle）",
    )

    # 可选阶段开关
    parser.add_argument(
        "--fundamentals",
        action="store_true",
        help="同步基本面（三大报表 + 财务指标，耗时数小时）",
    )
    parser.add_argument(
        "--commodities",
        action="store_true",
        help="同步大宗商品价格",
    )
    parser.add_argument(
        "--insight",
        action="store_true",
        help="同步投资人观点（load-sources + collect + manual）",
    )
    parser.add_argument(
        "--announcements",
        action="store_true",
        help="同步巨潮公告索引（增量）",
    )
    parser.add_argument(
        "--funds",
        action="store_true",
        help="同步基金域（信息/净值/持仓 + 申万口径资金流）",
    )
    parser.add_argument(
        "--fund-codes",
        type=str,
        default=None,
        help="基金域限定基金代码（逗号分隔）；缺省同步库中已登记的基金",
    )

    # 通用参数
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="fundamentals / valuation-history 阶段并发数（默认 8）",
    )
    parser.add_argument(
        "--period",
        type=str,
        default="近五年",
        help="valuation-history 历史区间（近五年/近十年/全部）",
    )
    parser.add_argument(
        "--ts-code",
        type=str,
        default=None,
        help="fundamentals / announcements 限定标的（逗号分隔）",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="announcements 起始日期（首次同步必填，如 2015-01-01）",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="同步结束日期（默认今天）",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="prices 阶段增量模式（从最新交易日同步到当前）",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="DuckDB 库路径（缺省读 config.ini）",
    )
    parser.add_argument(
        "--skip-on-failure",
        action="store_true",
        help="某阶段失败时继续执行后续阶段（默认遇到失败即停止）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印将要执行的命令，不实际运行",
    )

    return parser.parse_args()


def run_subcommand(script_name, args_list, db_path=None, dry_run=False):
    """
    运行子脚本

    Args:
        script_name (str): 脚本文件名（如 sync_market_data.py）
        args_list (list): 传给子脚本的参数列表
        db_path (str): 数据库路径（透传给子脚本）
        dry_run (bool): True 时仅打印命令

    Returns:
        tuple: (success: bool, returncode: int)
    """
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    cmd = [sys.executable, script_path] + args_list
    if db_path:
        cmd.extend(["--db-path", db_path])

    cmd_str = " ".join(cmd)
    _logger.info(">>> 执行: %s", cmd_str)

    if dry_run:
        _logger.info("    [dry-run] 跳过实际执行")
        return True, 0

    try:
        # 子进程继承父进程的环境变量（含 STOCKLAB_XUEQIU_COOKIE 等凭证）
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=7200,  # 2 小时超时（基本面全市场可能更久）
        )
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                _logger.info("    %s", line)
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                _logger.warning("    %s", line)

        if result.returncode == 0:
            _logger.info("    [OK] %s", script_name)
            return True, 0
        else:
            _logger.error("    [FAIL] %s (退出码 %d)", script_name, result.returncode)
            return False, result.returncode

    except subprocess.TimeoutExpired:
        _logger.error("    [TIMEOUT] %s 超过 2 小时", script_name)
        return False, -1
    except Exception as e:
        _logger.exception("    [EXCEPTION] %s: %s", script_name, e)
        return False, -2


def main():
    """主入口"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = parse_args()

    # 全局安装浏览器 UA 补丁
    install_browser_user_agent()

    # 初始化数据库（跑迁移）
    db_path = initialize_database(args.db_path)
    _logger.info("数据库路径: %s", db_path)

    # 构建执行计划
    plan = []

    # 1. 核心阶段（固定顺序）
    core_phases = [
        ("sync_market_data.py", ["securities"], "证券基础信息"),
        ("sync_market_data.py", ["prices", "--incremental"] if args.incremental else
         ["prices", "--start-date", args.start_date or (datetime.now().replace(day=1) - timedelta(days=30)).strftime("%Y-%m-%d"), "--end-date", args.end_date or datetime.now().strftime("%Y-%m-%d")], "日 K 行情"),
        ("sync_market_data.py", ["valuations"], "估值快照"),
        ("sync_market_data.py", ["indexes"], "指数成分"),
        ("sync_market_data.py", ["lifecycle"], "生命周期"),
    ]

    # 2. 可选阶段
    optional_phases = []

    if args.fundamentals or (not args.core_only and not any([args.fundamentals, args.commodities, args.insight, args.announcements])):
        # 如果显式指定了 --fundamentals，或者没有指定任何可选阶段且不是 --core-only
        # 默认包含 fundamentals（但这是最耗时的，所以建议显式指定）
        pass  # 默认不包含，除非显式指定

    if args.fundamentals:
        ts_codes = [c.strip() for c in (args.ts_code or "").split(",")] if args.ts_code else None
        fa = ["fundamentals", "--workers", str(args.workers)]
        if ts_codes:
            fa.extend(["--ts-code", ",".join(ts_codes)])
        optional_phases.append(("sync_market_data.py", fa, "基本面"))

    if args.commodities:
        optional_phases.append(("sync_commodities.py", ["sync"], "大宗商品"))

    if args.insight:
        # insight 需要先 load-sources，再 collect
        optional_phases.append(("sync_investor_insight.py", ["load-sources"], "投资人登记"))
        optional_phases.append(("sync_investor_insight.py", ["collect", "--platform", "xueqiu", "--limit", "200"], "投资人采集-雪球"))
        optional_phases.append(("sync_investor_insight.py", ["collect", "--platform", "guba", "--limit", "200"], "投资人采集-股吧"))

    if args.funds:
        fa = ["sync"]
        if args.fund_codes:
            fa.extend(["--codes", args.fund_codes])
        optional_phases.append(("sync_fund.py", fa, "基金域（信息/净值/持仓/资金流）"))

    if args.announcements:
        an = ["announcements"]
        if args.start_date:
            an.extend(["--start-date", args.start_date])
        if args.end_date:
            an.extend(["--end-date", args.end_date])
        if args.ts_code:
            an.extend(["--ts-code", args.ts_code])
        optional_phases.append(("sync_market_data.py", an, "公告索引"))

    # 如果没有显式指定任何可选阶段且不是 --core-only，默认只跑核心
    if not args.core_only and not optional_phases:
        _logger.info("未指定可选阶段（--fundamentals/--commodities/--insight/--announcements/--funds），仅执行核心同步")
        plan = core_phases
    elif args.core_only:
        plan = core_phases
    else:
        plan = core_phases + optional_phases

    _logger.info("=" * 60)
    _logger.info("同步计划：共 %d 个阶段", len(plan))
    for i, (script, args_list, desc) in enumerate(plan, 1):
        _logger.info("  %d. %s (%s)", i, desc, " ".join(args_list))
    _logger.info("=" * 60)

    # 执行
    all_ok = True
    start_time = datetime.now()

    for i, (script, args_list, desc) in enumerate(plan, 1):
        _logger.info("=" * 60)
        _logger.info("阶段 %d/%d: %s", i, len(plan), desc)
        _logger.info("=" * 60)

        ok, code = run_subcommand(script, args_list, args.db_path, args.dry_run)
        if not ok:
            all_ok = False
            if not args.skip_on_failure:
                _logger.error("阶段失败且未设置 --skip-on-failure，终止同步")
                break
            _logger.warning("阶段失败，继续执行后续阶段（--skip-on-failure）")

    elapsed = datetime.now() - start_time
    _logger.info("=" * 60)
    if all_ok:
        _logger.info("全部同步完成，耗时 %s", elapsed)
    else:
        _logger.error("同步存在失败，耗时 %s", elapsed)
    _logger.info("=" * 60)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())