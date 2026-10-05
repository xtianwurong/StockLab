#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金域同步 (app/scripts/sync_fund.py)
==============================================================================

【功能用途】
  给「基金域」补上写方。此前 fund.fund_info / fund.nav_history / fund.fund_holding /
  capital.flow_daily 四张表**只有读方**：采集函数写了、接口读了，但全项目没有一个
  脚本调用它们，表里只有手工种子数据，接口能返回数据纯属侥幸。

  同步内容：
    1. 基金信息  fund.fund_info          <- 雪球 danjuanfunds（字段与早期设想不同，已对齐）
    2. 净值历史  fund.nav_history        <- 东财 lsjz 主源 + akshare pingzhongdata 备源
    3. 持仓明细  fund.fund_holding       <- 东财 F10 jjcc 页面（可 --skip-holdings）
    4. 资金流    capital.flow_daily      <- 新浪个股资金流按申万映射聚合（可 --skip-flow）

【为什么资金流要按「最近交易日」落库】
  新浪给的是「即时」快照，不含日期。假日（如国庆休市）跑同步时，把快照记成今天
  就等于凭空造了一个不存在的交易日。这里用本地行情表的最新交易日归属，
  拿不到（数据陈旧超过 7 天）才退回今天并告警。

【历史序列怎么来】
  东财 push2 系域名取不到历史资金流，新浪也只有即时快照 —— **历史只能靠每日运行
  积累，不回填**。RBSA 的资金流佐证需要序列，刚上线时会提示「资金流历史不足」，
  跑够几天才逐步有相关性。这是数据现实，不是 bug。

【运行方式】
  python app/scripts/sync_fund.py --codes 000001.OF,000002.OF   # 指定基金全量同步
  python app/scripts/sync_fund.py --register --codes 000001.OF   # 先登记清单再同步
  python app/scripts/sync_fund.py status                         # 看各表现状
  python app/scripts/sync_fund.py --flow-levels 1 --skip-holdings

【退出码】
  0 全部成功 / 1 有失败（详见日志）/ 2 参数或前置条件不满足
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from typing import List, Optional

import pandas as pd

# 项目根目录加入模块搜索路径（与其它 CLI 一致）
sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from stocklab.common.http_client import install_browser_user_agent
from stocklab.datasource.capital_flow import (
    aggregate_stock_flow_to_sw,
    fetch_stock_fund_flow_individual,
)
from stocklab.datasource.fund import fetch_all_fund_codes, fetch_fund_info, fetch_fund_nav_history
from stocklab.datasource.fund_holding import fetch_fund_holding_history
from stocklab.persistence import Database
from stocklab.persistence.repository.fund_analysis import (
    CapitalFlowDailyRepository,
    FundInfoRepository,
    FundNavHistoryRepository,
)
from stocklab.persistence.repository.fund_holding import (
    FundHoldingRepository,
    StockIndustryMappingRepository,
)
from stocklab.persistence.storage import initialize_database

_logger = logging.getLogger(__name__)

__all__ = [
    "parse_args",
    "resolve_codes",
    "latest_trade_date",
    "sync",
    "show_status",
    "main",
]


def parse_args(argv: Optional[List[str]] = None):
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="StockLab 基金域同步（信息 / 净值 / 持仓 / 资金流）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?", default="sync", choices=["sync", "status"],
                        help="sync=同步（缺省） status=只读统计")

    parser.add_argument("--codes", type=str, default=None,
                        help="基金代码，逗号分隔（000001.OF 或 000001 均可）")
    parser.add_argument("--register", action="store_true",
                        help="先用东财基金清单登记 fund.fund_info（只补新代码，不覆盖已有详情）")
    parser.add_argument("--nav-days", type=int, default=730,
                        help="净值回看天数（默认 730）")
    parser.add_argument("--holding-years", type=int, default=2,
                        help="持仓回看年数（默认 2，每年 4 个报告期）")
    parser.add_argument("--flow-levels", type=str, default="1,2",
                        help="资金流聚合的申万层级，逗号分隔（默认 1,2）")
    parser.add_argument("--skip-holdings", action="store_true", help="不同步持仓")
    parser.add_argument("--skip-flow", action="store_true", help="不同步资金流")
    parser.add_argument("--skip-nav", action="store_true", help="不同步净值")
    parser.add_argument("--db-path", type=str, default=None, help="DuckDB 库路径（缺省读 config.ini）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划不执行")
    return parser.parse_args(argv)


def _normalize_codes(raw: Optional[str]) -> List[str]:
    """
    000001 / 1.OF / 000001.OF 都收，统一成「6 位数字 + .OF」

    任何不是数字的东西一律丢掉并告警 —— 拼错的代码一路传到采集层，
    只会变成一条含糊的「抓取失败」，在入口处拒绝才查得出来。
    """
    if not raw:
        return []
    codes = []
    for item in str(raw).split(","):
        token = item.strip()
        if not token:
            continue
        core = token[:-3] if token.upper().endswith(".OF") else token
        if not core.isdigit():
            _logger.warning("忽略非法基金代码: %s", item)
            continue
        codes.append(f"{core.zfill(6)}.OF")
    # 去重，保持顺序
    return list(dict.fromkeys(codes))


def resolve_codes(args, database) -> List[str]:
    """
    确定本次要同步哪些基金

    优先级：--codes > 库里已登记的 fund_info
    """
    explicit = _normalize_codes(args.codes)
    if explicit:
        return explicit
    registered = FundInfoRepository(database).find_all()
    if registered.empty:
        return []
    return registered["fund_code"].astype(str).tolist()


def latest_trade_date(database, max_stale_days: int = 7):
    """
    取本地已知的最近交易日（资金流快照归属日）

    Returns:
        date: 取不到或数据陈旧超过 max_stale_days 时退回今天（并告警）
    """
    conn = database.get_connection()
    candidates = []
    for table in ("market.daily_prices", "sw.index_daily"):
        try:
            row = conn.execute(f"SELECT MAX(trade_date) FROM {table}").fetchone()
        except Exception as exc:  # 表还不存在时不应中断同步
            _logger.debug("读取 %s 最新交易日失败: %s", table, exc)
            continue
        if row and row[0] is not None:
            value = row[0]
            if isinstance(value, str):
                value = pd.to_datetime(value).date()
            elif not isinstance(value, date):
                value = pd.Timestamp(value).date()
            candidates.append(value)

    if not candidates:
        _logger.warning("本地无行情记录，资金流快照按今天 %s 归属", date.today())
        return date.today()

    latest = max(candidates)
    stale = (date.today() - latest).days
    if stale > max_stale_days:
        _logger.warning("本地最近交易日 %s 已陈旧 %d 天（> %d），资金流快照按今天归属",
                        latest, stale, max_stale_days)
        return date.today()
    return latest


def _register_fund_list(database) -> int:
    """登记全市场基金清单：只补新代码，已有行的详情一个字段都不动"""
    repo = FundInfoRepository(database)
    listing = fetch_all_fund_codes()
    if listing.empty:
        _logger.error("基金清单抓取为空（东财 fund_name_em 域名在本机不稳），跳过登记")
        return 0

    existing = set(repo.find_all()["fund_code"].astype(str))
    fresh = listing[~listing["fund_code"].astype(str).isin(existing)]
    if fresh.empty:
        _logger.info("基金清单已全部登记（共 %d 只），无新增", len(existing))
        return 0

    now = datetime.now()
    frame = pd.DataFrame({
        "fund_code": fresh["fund_code"].astype(str),
        "fund_name": fresh["fund_name"].astype(str),
        "fund_short_name": None,
        "fund_type": fresh["fund_type"].astype(str),
        "manager_name": None,
        "company_name": None,
        "establish_date": None,
        "benchmark": None,
        "status": "active",
        "source": "akshare:fund_name_em",
        "fetched_at": now,
    })
    written = repo.upsert(frame)
    _logger.info("基金清单登记 %d 只（新增 %d，已有 %d 保持不变）",
                 written, len(fresh), len(existing))
    return written


def sync(args) -> int:
    """执行基金域同步，返回退出码（0 全成 / 1 有失败 / 2 前置条件不满足）"""
    if args.dry_run:
        _logger.info("[dry-run] codes=%s register=%s nav=%s holdings=%s flow=%s",
                     args.codes, args.register, not args.skip_nav,
                     not args.skip_holdings, not args.skip_flow)
        _logger.info("[dry-run] nav_days=%d holding_years=%d flow_levels=%s",
                     args.nav_days, args.holding_years, args.flow_levels)
        return 0

    db_path = initialize_database(args.db_path)
    database = Database(db_path)
    failures: List[str] = []

    try:
        if args.register:
            _logger.info("=== 登记基金清单 ===")
            try:
                _register_fund_list(database)
            except Exception as exc:
                _logger.error("基金清单登记失败: %s", exc)
                failures.append("fund_list")

        codes = resolve_codes(args, database)
        if not codes:
            _logger.error("没有可同步的基金：请用 --codes 000001.OF 指定，或先 --register 登记清单")
            return 2

        _logger.info("同步基金 %d 只: %s", len(codes),
                     ", ".join(codes[:8]) + ("…" if len(codes) > 8 else ""))

        info_repo = FundInfoRepository(database)
        nav_repo = FundNavHistoryRepository(database)
        start_date = (date.today() - timedelta(days=int(args.nav_days))).strftime("%Y-%m-%d")
        end_date = date.today().strftime("%Y-%m-%d")

        for code in codes:
            # 1) 基本信息（雪球）：失败只告警，不阻断净值 —— 两者是不同上游
            try:
                info = fetch_fund_info(code)
                if info.empty:
                    _logger.warning("基金 %s 基本信息为空（雪球可能无此代码）", code)
                    failures.append(f"{code}:info")
                else:
                    info_repo.upsert(info)
            except Exception as exc:
                _logger.error("基金 %s 基本信息同步失败: %s", code, exc)
                failures.append(f"{code}:info")

            # 2) 净值历史（RBSA 的输入，失败等于该基金无法分析）
            if args.skip_nav:
                continue
            try:
                nav = fetch_fund_nav_history(code, start_date=start_date, end_date=end_date)
                if nav.empty:
                    _logger.error("基金 %s 净值历史为空（lsjz 与 pingzhongdata 两源均失败）", code)
                    failures.append(f"{code}:nav")
                else:
                    written = nav_repo.upsert(nav)
                    _logger.info("基金 %s 净值 %d 条（%s ~ %s，源 %s）",
                                 code, written, nav["nav_date"].min(),
                                 nav["nav_date"].max(), nav["source"].iloc[0])
            except Exception as exc:
                _logger.error("基金 %s 净值同步失败: %s", code, exc)
                failures.append(f"{code}:nav")

            # 3) 持仓明细（暴露与归因的输入）
            if args.skip_holdings:
                continue
            try:
                holdings = fetch_fund_holding_history(code, years=int(args.holding_years))
                if holdings.empty:
                    _logger.warning("基金 %s 持仓为空（报告期页面无数据）", code)
                    failures.append(f"{code}:holding")
                else:
                    written = FundHoldingRepository(database).upsert(holdings)
                    periods = holdings["report_date"].nunique()
                    _logger.info("基金 %s 持仓 %d 行 / %d 个报告期（写入 %d）",
                                 code, len(holdings), periods, written)
            except Exception as exc:
                _logger.error("基金 %s 持仓同步失败: %s", code, exc)
                failures.append(f"{code}:holding")

        # 4) 资金流（与基金无关，按申万口径全市场同步一次）
        if not args.skip_flow:
            try:
                failures.extend(_sync_capital_flow(database, args))
            except Exception as exc:
                _logger.error("资金流同步失败: %s", exc)
                failures.append("capital_flow")

        return 1 if failures else 0
    finally:
        database.close()


def _sync_capital_flow(database, args) -> List[str]:
    """申万口径行业资金流：新浪个股快照 -> 按本地映射聚合 -> 落库"""
    levels = []
    for item in str(args.flow_levels).split(","):
        item = item.strip()
        if item:
            if item not in ("1", "2"):
                _logger.error("--flow-levels 只支持 1 或 2，收到 %r", item)
                return ["capital_flow:level"]
            levels.append(int(item))
    if not levels:
        return []

    mapping = StockIndustryMappingRepository(database).find_all()
    if mapping.empty:
        _logger.error("股票行业映射为空，无法聚合申万资金流（先跑 sync_benchmark.py --with-mapping）")
        return ["capital_flow:mapping"]

    trade_date = latest_trade_date(database)
    _logger.info("=== 资金流快照（归属交易日 %s）===", trade_date)

    individual = fetch_stock_fund_flow_individual()
    if individual.empty:
        _logger.error("新浪个股资金流采集失败")
        return ["capital_flow:fetch"]

    repo = CapitalFlowDailyRepository(database)
    failures = []
    for level in levels:
        frame, meta = aggregate_stock_flow_to_sw(
            individual, mapping, level=level, trade_date=trade_date
        )
        if frame.empty:
            _logger.error("申万 L%d 资金流聚合为空", level)
            failures.append(f"capital_flow:l{level}")
            continue
        written = repo.upsert(frame)
        _logger.info(
            "申万 L%d 资金流 %d 个行业写入 %d 行，个股 join 覆盖率 %.2f%%（%d/%d）",
            level, meta["sector_count"], written,
            meta["stock_coverage"] * 100, meta["stock_matched"], meta["stock_total"],
        )
    return failures


def show_status(args) -> int:
    """只读统计：各表现状（不联网）"""
    db_path = initialize_database(args.db_path)
    database = Database(db_path)
    try:
        conn = database.get_connection()
        queries = [
            ("基金登记 fund.fund_info", "SELECT COUNT(*) FROM fund.fund_info"),
            ("净值 fund.nav_history",
             "SELECT COUNT(*), MIN(nav_date), MAX(nav_date) FROM fund.nav_history"),
            ("持仓 fund.fund_holding",
             "SELECT COUNT(*), COUNT(DISTINCT fund_code), COUNT(DISTINCT report_date) FROM fund.fund_holding"),
            ("资金流 capital.flow_daily",
             "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM capital.flow_daily"),
            ("行业映射 fund.stock_industry_mapping",
             "SELECT COUNT(*) FROM fund.stock_industry_mapping"),
        ]
        for label, sql in queries:
            try:
                row = conn.execute(sql).fetchone()
                _logger.info("%-42s %s", label, row)
            except Exception as exc:
                _logger.warning("%-42s 查询失败: %s", label, exc)
        return 0
    finally:
        database.close()


def main(argv: Optional[List[str]] = None) -> int:
    """主入口"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)
    install_browser_user_agent()

    if args.command == "status":
        return show_status(args)
    if args.nav_days <= 0 or args.holding_years <= 0:
        _logger.error("--nav-days / --holding-years 必须为正整数")
        return 2
    return sync(args)


if __name__ == "__main__":
    sys.exit(main())
