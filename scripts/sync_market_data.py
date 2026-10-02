#!/usr/bin/env python3
"""
 ==============================================================================
StockLab - A 股市场数据同步 CLI (scripts/sync_market_data.py)
 ==============================================================================

【功能用途】
   独立的数据同步入口，负责将全市场 A 股数据从公开数据源同步到本地 DuckDB。
   三个阶段通过子命令分开操作，也可不带子命令一键全跑。

【运行方式】
   # 一键全跑（阶段一 -> 阶段二 -> 阶段三）
   python scripts/sync_market_data.py --start-date 2025-01-01 --end-date 2026-09-30
   python scripts/sync_market_data.py --incremental

   # 阶段一：仅同步股票基础信息
   python scripts/sync_market_data.py securities

   # 阶段二：仅同步日 K 行情（指定日期范围 / 增量）
   python scripts/sync_market_data.py prices --start-date 2025-01-01 --end-date 2026-09-30
   python scripts/sync_market_data.py prices --incremental

   # 阶段三：仅同步估值快照
   python scripts/sync_market_data.py valuations

   # 阶段四：仅同步历史估值序列（逐只抓取，耗时最长；不参与一键全跑）
   python scripts/sync_market_data.py valuation-history --period 近五年
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

# 将项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.datasource.market_provider import MarketDataProvider
from stocklab.persistence import (
    DailyPriceRepository,
    DailyValuationRepository,
    Database,
    SecurityRepository,
    ValuationHistoryRepository,
    initialize_database,
)

_logger = logging.getLogger("StockLab.Sync")


def parse_args():
    """
    解析命令行参数（支持子命令分开执行，也支持无子命令一键全跑）

    【实现说明】
       子命令用可选位置参数实现（而非 add_subparsers），原因是子解析器会用
       自身默认值覆盖父解析器已解析的结果，导致公共参数放在子命令前面时丢失。
    """
    parser = argparse.ArgumentParser(
        description=(
            "A 股市场数据同步工具 - 将全市场数据同步到本地 DuckDB\n\n"
            "子命令（缺省 = 一键全跑前三个阶段）：\n"
            "  securities         阶段一：同步股票基础信息（约 18 次子请求，几十秒）\n"
            "  prices             阶段二：同步日 K 行情（逐只抓取，全历史约 5400 次请求）\n"
            "  valuations         阶段三：同步最新全市场估值快照（1 次请求）\n"
            "  valuation-history  阶段四：同步历史估值序列（逐只抓取，用于历史分位；耗时最长）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["securities", "prices", "valuations", "valuation-history"],
        default=None,
        help="要执行的同步阶段（不传则按顺序全跑）",
    )
    parser.add_argument(
        "--period",
        type=str,
        default="近五年",
        help="valuation-history 阶段的历史区间：近五年 / 近十年 / 全部（默认 近五年）",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="同步起始日期，格式 YYYY-MM-DD（默认近 30 天）",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="同步结束日期，格式 YYYY-MM-DD（默认今天）",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="增量同步模式：自动从最新交易日期同步到当前",
    )
    parser.add_argument(
        "--securities-only",
        action="store_true",
        help="（兼容旧用法）仅同步股票基础信息，等价于 securities 子命令",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="数据库文件路径（默认 data/stocklab.duckdb）",
    )
    return parser.parse_args()


def sync_securities(database):
    """阶段一：同步股票基础信息"""
    _logger.info("=" * 60)
    _logger.info("阶段一：同步股票基础信息")
    _logger.info("=" * 60)

    provider = MarketDataProvider()
    repository = SecurityRepository(database)

    df = provider.fetch_securities()
    if df.empty:
        _logger.error("获取股票基础信息失败，跳过")
        return False

    count = repository.upsert(df)
    _logger.info("股票基础信息同步完成: %d 条记录", count)
    return True


def resolve_price_dates(args, database):
    """
    确定日 K 行情的同步日期范围

    Returns:
        tuple: (start_date, end_date, skip)
               日期格式均为 "YYYYMMDD"；skip 为 True 表示本地已是最新无需同步
    """
    if args.incremental:
        # 增量同步：从最新交易日期同步到当前
        price_repo = DailyPriceRepository(database)
        max_date = price_repo.get_max_trade_date()
        if not max_date:
            # 本地无数据：默认回补近 30 天
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
            end_date = datetime.now().strftime("%Y%m%d")
            return start_date, end_date, False

        next_date = (datetime.strptime(max_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")
        # 本地数据已是最新（下一个起始日已超过今天），跳过日 K 同步
        if next_date > end_date:
            _logger.info(
                "本地日 K 数据已是最新（最新交易日 %s），跳过日 K 同步", max_date
            )
            return next_date, end_date, True
        return next_date, end_date, False

    # 指定日期范围
    start_date = (
        args.start_date.replace("-", "")
        if args.start_date
        else (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
    )
    end_date = (
        args.end_date.replace("-", "")
        if args.end_date
        else datetime.now().strftime("%Y%m%d")
    )
    return start_date, end_date, False


def sync_daily_prices(database, start_date, end_date):
    """阶段二：同步指定日期范围的日 K 行情"""
    _logger.info("=" * 60)
    _logger.info("阶段二：同步日 K 行情 (%s ~ %s)", start_date, end_date)
    _logger.info("=" * 60)

    provider = MarketDataProvider()
    repository = DailyPriceRepository(database)

    # 获取全部证券代码
    sec_repo = SecurityRepository(database)
    securities = sec_repo.find_all()
    if securities.empty:
        _logger.error("证券列表为空，请先执行: sync_market_data.py securities")
        return False

    total_count = 0
    failed_count = 0

    for _, row in securities.iterrows():
        ts_code = row["ts_code"]
        df = provider.fetch_daily_prices(ts_code, start_date, end_date)
        if df.empty:
            failed_count += 1
            continue

        count = repository.upsert(df)
        total_count += count

    _logger.info(
        "日 K 行情同步完成: 成功 %d 只，失败 %d 只，共 %d 条记录",
        len(securities) - failed_count,
        failed_count,
        total_count,
    )

    # 全部股票抓取失败时视为整体失败
    if failed_count == len(securities):
        _logger.error("全部 %d 只股票日 K 抓取失败", len(securities))
        return False

    return True


def sync_valuation_history(database, period, ts_code_list=None):
    """
    阶段四：同步历史估值序列（用于计算历史估值分位）

    【与阶段三的区别】
       阶段三 valuations        = 全市场单日快照，横截面比较用；
       阶段四 valuation-history = 单只跨年序列，历史分位计算用。
       逐只抓取，全市场约 5572 次请求，耗时远长于阶段二。

    Args:
        database (Database): 数据库连接管理器
        period (str): 历史区间，如 近五年 / 近十年 / 全部
        ts_code_list (list, optional): 指定同步的证券代码列表；
                                        为空时同步全市场

    Returns:
        bool: 是否同步成功
    """
    _logger.info("=" * 60)
    _logger.info("阶段四：同步历史估值序列（区间: %s）", period)
    _logger.info("=" * 60)

    provider = MarketDataProvider()
    repository = ValuationHistoryRepository(database)

    securities = SecurityRepository(database).find_all()
    if securities.empty:
        _logger.error("证券列表为空，请先执行: sync_market_data.py securities")
        return False

    if ts_code_list:
        targets = list(ts_code_list)
        _logger.info("限定同步 %d 只指定标的", len(targets))
    else:
        targets = securities["ts_code"].tolist()
        _logger.info("全市场 %d 只，逐只抓取中（耗时较长，可按批次分次执行）", len(targets))

    total_count = 0
    failed_count = 0

    for index, ts_code in enumerate(targets):
        df = provider.fetch_valuation_history(ts_code, period)
        if df.empty:
            failed_count += 1
        else:
            total_count += repository.upsert(df)

        # 每 100 只打印一次进度：全市场同步耗时数小时，无进度反馈难以判断是否卡死
        if (index + 1) % 100 == 0:
            _logger.info(
                "进度 %d/%d | 已写入 %d 条 | 失败 %d 只",
                index + 1, len(targets), total_count, failed_count,
            )

    _logger.info(
        "历史估值同步完成: 成功 %d 只，失败 %d 只，共 %d 条记录",
        len(targets) - failed_count, failed_count, total_count,
    )

    if failed_count == len(targets):
        _logger.error("全部 %d 只标的历史估值抓取失败", len(targets))
        return False

    return True


def sync_valuations(database):
    """阶段三：同步最新全市场估值快照"""
    _logger.info("=" * 60)
    _logger.info("阶段三：同步最新全市场估值快照")
    _logger.info("=" * 60)

    provider = MarketDataProvider()
    repository = DailyValuationRepository(database)

    df = provider.fetch_realtime_valuations()
    if df.empty:
        _logger.error("获取估值快照失败，跳过")
        return False

    count = repository.upsert(df)
    _logger.info("估值快照同步完成: %d 条记录", count)
    return True


def run_prices_stage(args, database):
    """执行阶段二：确定日期范围并同步日 K 行情，返回是否成功"""
    start_date, end_date, skip = resolve_price_dates(args, database)
    if skip:
        return True
    return sync_daily_prices(database, start_date, end_date)


def main():
    """主入口函数（无子命令 = 一键全跑；带子命令 = 仅执行对应阶段）"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = parse_args()
    command = args.command  # None 表示无子命令，一键全跑

    # 初始化数据库
    db_path = initialize_database(args.db_path)
    _logger.info("数据库路径: %s", db_path)

    with Database(db_path) as database:
        # 阶段一：同步股票基础信息（全跑模式与 securities 子命令均执行）
        if command in (None, "securities"):
            if not sync_securities(database):
                _logger.error("股票基础信息同步失败，终止")
                sys.exit(1)
            if command == "securities":
                return

        # 兼容旧用法：--securities-only
        if command is None and args.securities_only:
            _logger.info("仅同步股票基础信息模式，退出")
            return

        # 阶段二：同步日 K 行情（全跑模式与 prices 子命令均执行）
        if command in (None, "prices"):
            if not run_prices_stage(args, database):
                _logger.error("日 K 行情同步失败")
                sys.exit(1)
            if command == "prices":
                return

        # 阶段三：同步估值快照
        if command in (None, "valuations"):
            if not sync_valuations(database):
                # 单独执行估值子命令时失败即退出码 1；全跑模式下容忍失败
                if command == "valuations":
                    _logger.error("估值快照同步失败")
                    sys.exit(1)
                _logger.warning("估值快照同步失败（日 K 数据已写入，可稍后重试）")
            if command == "valuations":
                return

        # 阶段四：同步历史估值序列（默认不参与一键全跑：逐只抓取耗时过长）
        if command == "valuation-history":
            if not sync_valuation_history(database, args.period):
                _logger.error("历史估值序列同步失败")
                sys.exit(1)
            return

    _logger.info("=" * 60)
    _logger.info("同步任务完成")
    _logger.info("=" * 60)


if __name__ == "__main__":
    main()
