#!/usr/bin/env python3
"""
==============================================================================
StockLab - 大宗商品同步 CLI (app/scripts/sync_commodities.py)
==============================================================================

【功能用途】
   把大宗商品主力连续日线同步到 commodity.price_history。

【运行方式】
   # 同步全部登记品种（缺省行为）
   python app/scripts/sync_commodities.py sync

   # 只同步指定品种（可重复）
   python app/scripts/sync_commodities.py sync --symbol crude_oil --symbol gold

   # 只看会抓哪些、以及库里现在什么样（不联网）
   python app/scripts/sync_commodities.py status

   # 抓了但不写库（验证源站列名与归一化是否还对）
   python app/scripts/sync_commodities.py sync --dry-run

【退出码语义】
   0  全部成功（含「本次无变化」这种正常情况）
   1  有品种采集失败（网络 / 源站改版 / 归一化剔空任一）
   2  配置或输入错误（品种代号未登记、库路径无效等）

   【为什么失败不一定是 1】
      一个品种的源站改版，不该让其余九个品种的同步结果作废。退出码只回答
      「这一轮是否**有**品种失败」，逐品种原因在下方结果表里列出来。
      调用方（定时任务 / CI）据此决定是否告警，而不是按退出码 1 全盘判失败。

【为什么是单独一个脚本而不是 sync_market_data.py 的子命令】
   sync_market_data 走的是「同一套行情契约、同一套归一化」的增量同步，
   每个阶段共享参数与状态；大宗商品是**另一类数据**（另一张表、另一套归一化、
   另一组失败模式），塞进去只会让两边的参数表互相污染。
   与 sync_investor_insight.py 同样的取舍：一域一脚本，各自可独立重跑。
"""

import argparse
import logging
import os
import sys
from datetime import date

import pandas as pd

# 将项目根目录加入模块搜索路径（与其它 CLI 一致，否则直接执行会 ModuleNotFoundError）
sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from stocklab.common.config import find_config_path
from stocklab.common.http_client import install_browser_user_agent
from stocklab.datasource.commodity import (
    CommoditySourceError,
    by_symbol,
    commodities,
    fetch_main_history,
)
from stocklab.persistence.repository.commodity import CommodityPriceRepository
from stocklab.persistence.storage import initialize_database
from stocklab.persistence.storage.duckdb import Database

_logger = logging.getLogger(__name__)

__all__ = [
    "parse_args",
    "sync",
    "show_status",
    "main",
]


def parse_args(argv=None):
    """
    解析命令行参数

    Args:
        argv (list, optional): 参数列表（测试注入用）

    Returns:
        argparse.Namespace
    """
    parser = argparse.ArgumentParser(
        description=(
            "大宗商品价格同步工具\n\n"
            "子命令：\n"
            "  sync    抓取主力连续日线并写库（缺省）\n"
            "  status  查看登记品种与本地库存，不联网"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["sync", "status"],
        default="sync",
        help="要执行的动作（缺省 = sync）",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        default=None,
        metavar="SYMBOL",
        help="限定品种代号，可重复；缺省 = 全部登记品种",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="抓取但不写库，只报告将写入多少行",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="DuckDB 库路径（缺省读 config.ini）",
    )
    return parser.parse_args(argv)


def _resolve_specs(symbols):
    """
    把 --symbol 列表解析成登记项

    Args:
        symbols (list | None): 代号列表；None 表示全部

    Returns:
        list[CommoditySpec]

    Raises:
        ValueError: 代号未登记（由 main 转成退出码 2）
    """
    if not symbols:
        return list(commodities())

    specs = []
    unknown = []
    for symbol in symbols:
        spec = by_symbol(symbol)
        if spec is None:
            unknown.append(symbol)
        else:
            specs.append(spec)
    if unknown:
        raise ValueError(
            "未登记的大宗商品代号: %s（可用: %s）"
            % (", ".join(unknown),
               ", ".join(spec.symbol for spec in commodities()))
        )
    # 保持登记顺序，同时去掉重复传入
    seen, ordered = set(), []
    for spec in commodities():
        if spec.symbol in {item.symbol for item in specs} and spec.symbol not in seen:
            ordered.append(spec)
            seen.add(spec.symbol)
    return ordered


def sync(database, specs, dry_run=False):
    """
    逐品种抓取并写库

    【为什么一个品种失败不中断其余品种】
      十个品种走的是十个独立的 HTTP 请求。某个合约改名、某个交易所接口抖动，
      不该让另外九个已经抓回来的数据也丢掉 —— 那会让「同步失败」的成本
      从一个品种放大到全部。所以循环内部 try/except，失败只记结果。

    Args:
        database (Database): 数据库句柄
        specs (list[CommoditySpec]): 待同步品种
        dry_run (bool): True 时抓取但不写库

    Returns:
        tuple[int, int]: (成功品种数, 失败品种数)
    """
    repository = CommodityPriceRepository(database)
    before = repository.counts_by_symbol()

    rows = []
    ok, failed = 0, 0
    for spec in specs:
        try:
            frame = fetch_main_history(spec)
        except CommoditySourceError as error:
            failed += 1
            rows.append((spec, "失败", str(error)))
            _logger.error("%s", error)
            continue

        except Exception as error:  # akshare 抛什么都有可能，不当成系统崩溃
            failed += 1
            rows.append((spec, "失败", "%s: %s" % (type(error).__name__, error)))
            _logger.exception("品种 [%s] 采集异常", spec.symbol)
            continue

        written = len(frame)
        if dry_run:
            rows.append((spec, "dry-run", "%d 行待写入" % written))
            ok += 1
            continue

        repository.insert(frame)
        ok += 1
        after = repository.counts_by_symbol()
        added = after.get(spec.symbol, 0) - before.get(spec.symbol, 0)
        rows.append((
            spec, "成功",
            "%d 行（新增 %d / 覆盖 %d）" % (written, added, written - added),
        ))

    _report(rows, before)
    return ok, failed


def _report(rows, before_counts):
    """打印逐品种结果表"""
    if not rows:
        print("  （没有匹配的品种）")
        return

    print("")
    print("  %-10s %-16s %-8s %s" % ("品种", "代号", "状态", "结果"))
    print("  " + "-" * 74)
    for spec, state, detail in rows:
        local = before_counts.get(spec.symbol, 0)
        print("  %-10s %-16s %-8s %s  [本地 %d 条]"
              % (spec.name, spec.symbol, state, detail, local))


def show_status(database):
    """
    打印登记品种与本地库存（不联网）

    Args:
        database (Database): 数据库句柄
    """
    repository = CommodityPriceRepository(database)
    counts = repository.counts_by_symbol()
    latest = repository.latest_by_symbol()
    latest_map = {}
    if latest is not None and not latest.empty:
        for row in latest.itertuples(index=False):
            latest_map[row.symbol] = str(row.trade_date)

    today = date.today()
    print("")
    print("  %-10s %-16s %-20s %-12s %-7s %s"
          % ("品种", "代号", "交易所", "末次数据", "滞后", "本地条数"))
    print("  " + "-" * 82)
    for spec in commodities(include_inactive=True):
        last = latest_map.get(spec.symbol)
        if last:
            lag = (today - date.fromisoformat(last)).days
            lag_text = "%d 天" % lag
        else:
            last, lag_text = "—", "—"
        flag = "" if spec.is_active else "（已停用）"
        print("  %-10s %-16s %-20s %-12s %-7s %d"
              % (spec.name + flag, spec.symbol, spec.exchange, last,
                 lag_text, counts.get(spec.symbol, 0)))
    print("")
    as_of = repository.as_of()
    print("  数据截止: %s" % (as_of or "（表为空，先跑 sync）"))
    print("  判陈旧阈值: 超过 15 天无新数据的品种页面不给评级")


def main(argv=None):
    """
    主入口

    Args:
        argv (list, optional): 参数列表（测试注入用）

    Returns:
        int: 退出码（0 成功 / 1 有品种失败 / 2 配置或输入错误）
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)

    # 全局安装浏览器 UA 补丁（与其它 CLI 一致），仅在入口显式调用一次
    install_browser_user_agent()

    try:
        specs = _resolve_specs(args.symbols)
    except ValueError as error:
        _logger.error("%s", error)
        return 2

    # 必须走 initialize_database：它每次都会检查版本并应用未生效的迁移。
    # 直接 Database(db_path) 不会迁移，在全新库上会立刻撞上
    # 「表 commodity.price_history 不存在」（与其它同步脚本一致）。
    db_path = find_config_path(args.db_path) if args.db_path else args.db_path
    db_path = initialize_database(db_path)
    _logger.info("数据库路径: %s", db_path)

    with Database(db_path) as database:
        if args.command == "status":
            show_status(database)
            return 0

        _logger.info(
            "开始同步 %d 个品种%s", len(specs),
            "（dry-run，不写库）" if args.dry_run else "",
        )
        _, failed = sync(database, specs, dry_run=args.dry_run)
        if failed:
            _logger.warning("%d 个品种失败", failed)
            return 1
        _logger.info("同步完成")
        return 0


if __name__ == "__main__":
    sys.exit(main())
