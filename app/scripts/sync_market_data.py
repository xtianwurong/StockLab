#!/usr/bin/env python3
"""
 ==============================================================================
StockLab - A 股市场数据同步 CLI (app/scripts/sync_market_data.py)
 ==============================================================================

【功能用途】
   独立的数据同步入口，负责将全市场 A 股数据从公开数据源同步到本地 DuckDB。
   九个阶段通过子命令分开操作；不带子命令一键全跑阶段一 / 二 / 三 / 五 / 七，
   阶段四（历史估值）、六（行业估值）、八（基本面）、九（公告）按需显式触发。

【运行方式】
   # 一键全跑（阶段一 -> 二 -> 三 -> 五 -> 七；四 / 六 / 八 / 九需显式子命令）
   python app/scripts/sync_market_data.py --start-date 2025-01-01 --end-date 2026-09-30
   python app/scripts/sync_market_data.py --incremental

   # 阶段一：仅同步股票基础信息
   python app/scripts/sync_market_data.py securities

   # 阶段二：仅同步日 K 行情（指定日期范围 / 增量）
   python app/scripts/sync_market_data.py prices --start-date 2025-01-01 --end-date 2026-09-30
   python app/scripts/sync_market_data.py prices --incremental

   # 阶段三：仅同步估值快照
   python app/scripts/sync_market_data.py valuations

   # 阶段四：仅同步历史估值序列（逐只抓取，耗时最长；不参与一键全跑）
   python app/scripts/sync_market_data.py valuation-history --period 近五年

   # 阶段五：仅同步主流宽基指数成分（同业分组维度，数秒完成）
   python app/scripts/sync_market_data.py indexes

   # 阶段六：仅同步行业估值横截面（板块洼地判断依据）
   python app/scripts/sync_market_data.py industries --stat-date 2026-09-30

   # 阶段七：同步证券生命周期（沪深北上市日历 + 退市日历 -> status / 事件）
   python app/scripts/sync_market_data.py lifecycle

   # 阶段八：同步 Point-in-Time 基本面（三大报表 + 财务指标，耗时较长）
   python app/scripts/sync_market_data.py fundamentals --ts-code 600519.SH
   python app/scripts/sync_market_data.py fundamentals --workers 8

   # 阶段九：同步巨潮公告索引（增量；不参与一键全跑）
   python app/scripts/sync_market_data.py announcements --ts-code 600519.SH --start-date 2026-01-01
   python app/scripts/sync_market_data.py announcements --start-date 2015-01-01   # 首次初始化（耗时长）
   python app/scripts/sync_market_data.py announcements                            # 增量（自动取水位）
"""

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd

# 将项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from stocklab.common.http_client import install_browser_user_agent
from stocklab.datasource.cninfo_client import CninfoClient
from stocklab.datasource.fundamental_service import FundamentalService
from stocklab.datasource.lifecycle_service import LifecycleService
from stocklab.datasource.market_service import MarketService
from stocklab.fundamental import build_financial_indicators
from stocklab.normalization.exchange import build_lifecycle_events, merge_lifecycle
from stocklab.persistence import (
    AnnouncementRepository,
    BalanceSheetRepository,
    CashflowStatementRepository,
    DailyPriceRepository,
    DailyValuationRepository,
    Database,
    FinancialIndicatorRepository,
    IndexMembershipRepository,
    IncomeStatementRepository,
    IndustryValuationRepository,
    SecurityEventRepository,
    SecurityRepository,
    ValuationHistoryRepository,
    initialize_database,
)

_logger = logging.getLogger("StockLab.Sync")

# 主流宽基指数清单（中证官网口径），用于建立「同业分组」的近似维度。
# 行业分类数据不可用时，指数成分可替代「同行业可比样本池」。
_INDEX_CODES = (
    ("000016", "上证50"),
    ("000300", "沪深300"),
    ("000905", "中证500"),
    ("000510", "中证A500"),
    ("000852", "中证1000"),
    ("932000", "中证2000"),
)


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
            "子命令（缺省 = 一键全跑阶段一 / 二 / 三 / 五 / 七）：\n"
            "  securities         阶段一：同步股票基础信息（约 18 次子请求，几十秒）\n"
            "  prices             阶段二：同步日 K 行情（逐只抓取，全历史约 5400 次请求）\n"
            "  valuations         阶段三：同步最新全市场估值快照（1 次请求）\n"
            "  valuation-history  阶段四：同步历史估值序列（逐只抓取，用于历史分位；耗时最长）\n"
            "  indexes             阶段五：同步主流宽基指数成分（同业分组维度；约 6 次请求，数秒）\n"
            "  industries          阶段六：同步行业估值横截面（板块洼地判断依据；默认不参与全跑）\n"
            "  lifecycle           阶段七：同步证券生命周期（上市/退市日历 -> status 与事件）\n"
            "  fundamentals        阶段八：同步 Point-in-Time 基本面（逐只抓取；默认不参与全跑）\n"
            "  announcements       阶段九：同步巨潮公告索引（增量；默认不参与全跑）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=[
            "securities", "prices", "valuations", "valuation-history",
            "indexes", "industries", "lifecycle", "fundamentals",
            "announcements",
        ],
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
        "--stat-date",
        type=str,
        default=None,
        help="industries 阶段的统计日期 YYYY-MM-DD；缺省为今天",
    )
    parser.add_argument(
        "--classification",
        type=str,
        default="国证行业分类",
        help="industries 阶段的行业分类体系：国证行业分类 / 证监会行业分类",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="valuation-history / fundamentals 阶段的取数并发线程数（默认 8；实测 12 仍稳定）",
    )
    parser.add_argument(
        "--ts-code",
        type=str,
        default=None,
        help="fundamentals / announcements 阶段限定的证券代码，逗号分隔（如 600519.SH,000001.SZ）；缺省为全市场",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="同步起始日期，格式 YYYY-MM-DD（价格阶段默认近 30 天；公告阶段为空时取表内水位）",
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

    market_service = MarketService()
    repository = SecurityRepository(database)

    df = market_service.fetch_securities()
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

    market_service = MarketService()
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
        df = market_service.fetch_daily_prices(ts_code, start_date, end_date)
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


def sync_industry_valuation(database, stat_date, classification="国证行业分类"):
    """
    阶段六：同步行业估值横截面

    【用途】
       个股估值分位只能回答「相对自己历史上贵不贵」，回答不了「相对同行业贵不贵」。
       本阶段提供行业级估值，用于定位价值洼地板块与同业比较。

    【为何存三个 PE 口径】
       加权平均 PE 反映龙头主导的估值，中位数 PE 反映「典型公司」的估值。
       三者差异本身即信息：加权远低于中位数说明估值集中在少数权重股上，
       此时只看单一口径会误判洼地程度。

    Args:
        database (Database): 数据库连接管理器
        stat_date (str): 统计日期 YYYY-MM-DD
        classification (str, optional): 行业分类体系（国证 293 个 4 层 / 证监会 120 个 2 层）

    Returns:
        bool: 是否同步成功
    """
    _logger.info("=" * 60)
    _logger.info("阶段六：同步行业估值（%s @ %s）", classification, stat_date)
    _logger.info("=" * 60)

    market_service = MarketService()
    repository = IndustryValuationRepository(database)

    df = market_service.fetch_industry_valuation(stat_date, classification)
    if df.empty:
        _logger.warning("行业估值 [%s @ %s] 获取失败，跳过", classification, stat_date)
        return False

    count = repository.upsert(df)
    _logger.info("行业估值同步完成: %d 条记录", count)
    return count > 0


def sync_index_membership(database):
    """
    阶段五：同步主流宽基指数成分（建立「同业分组」的近似维度）

    【用途】
       reference.securities.industry 在多数免费数据源上不可用，
       指数成分可作为同业的近似替代：同指数成分股构成可比样本池，
       低估值判断可做指数内横向比较，替代「行业平均估值」。

    【规模】
       6 个指数合计约 4350 条记录，每指数 1 次请求，耗时数秒。

    Returns:
        bool: 是否同步成功
    """
    _logger.info("=" * 60)
    _logger.info("阶段五：同步主流宽基指数成分")
    _logger.info("=" * 60)

    market_service = MarketService()
    repository = IndexMembershipRepository(database)

    total_count = 0
    success_count = 0

    for index_code, index_name in _INDEX_CODES:
        df = market_service.fetch_index_membership(index_code)
        if df.empty:
            _logger.warning("指数 [%s] %s 成分获取失败，跳过", index_code, index_name)
            continue
        total_count += repository.upsert(df)
        success_count += 1

    _logger.info(
        "指数成分同步完成: 成功 %d/%d 个指数，共 %d 条记录",
        success_count, len(_INDEX_CODES), total_count,
    )
    return success_count > 0


def sync_valuation_history(database, period, ts_code_list=None, max_workers=8):
    """
    阶段四：同步历史估值序列（用于计算历史估值分位）

    【与阶段三的区别】
       阶段三 valuations        = 全市场单日快照，横截面比较用；
       阶段四 valuation-history = 单只跨年序列，历史分位计算用。

    【并发设计（为何要这么写）】
       单只标的需 5 个指标的独立 HTTP 请求，串行全市场约 7.5 小时；
       实测 12 并发达约 40 分钟且不触发限流。
       但 **DuckDB 连接非线程安全**，因此并发只用于「取数」，
       结果回收到主线程后由单一连接串行落库：
         工作线程池 -> 取数（并发，线程间无共享状态）
         主线程      -> upsert（串行，单连接）
       这是既能并发加速、又不引入数据库并发风险的唯一组合。

    Args:
        database (Database): 数据库连接管理器
        period (str): 历史区间，如 近五年 / 近十年 / 全部
        ts_code_list (list, optional): 指定同步的证券代码列表；为空时同步全市场
        max_workers (int, optional): 取数并发线程数，默认 8（实测 12 仍稳定）

    Returns:
        bool: 是否同步成功
    """
    _logger.info("=" * 60)
    _logger.info("阶段四：同步历史估值序列（区间: %s，并发 %d）", period, max_workers)
    _logger.info("=" * 60)

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
        _logger.info("全市场 %d 只，并发抓取中（预计约 %d 分钟）",
                     len(targets), int(len(targets) * 0.55 / 60))

    total_count = 0
    failed_count = 0

    def fetch_one(ts_code):
        """工作线程任务：只做取数，不触碰数据库"""
        try:
            return ts_code, MarketService().fetch_valuation_history(ts_code, period)
        except Exception as error:
            _logger.debug("并发取数异常 [%s]: %s", ts_code, error)
            return ts_code, pd.DataFrame()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for index, (ts_code, df) in enumerate(executor.map(fetch_one, targets)):
            if df is not None and not df.empty:
                total_count += repository.upsert(df)
            else:
                failed_count += 1

            # 每 200 只打印一次进度：全市场同步耗时数十分钟，无反馈难以判断是否卡死
            if (index + 1) % 200 == 0:
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

    market_service = MarketService()
    repository = DailyValuationRepository(database)

    df = market_service.fetch_realtime_valuations()
    if df.empty:
        _logger.error("获取估值快照失败，跳过")
        return False

    count = repository.upsert(df)
    _logger.info("估值快照同步完成: %d 条记录", count)
    return True


def sync_lifecycle(database):
    """
    阶段七：同步证券生命周期（上市/退市日历 → 主表日期与状态 + 生命周期事件）

    【为什么必须单独一阶段】
       东财名录只含当前在市证券，退市股票不在其中；历史研究若直接拿
       「当前股票池」当样本，会把未来退市的公司从历史里抹掉 → 幸存者偏差。
       本阶段从沪深北交易所官网取上市日历与退市日历，回填 list_date / delist_date、
       推导 status，并写入 reference.security_events，
       使 SecurityRepository.universe(as_of) 能还原任意历史时点的证券集合。

    【失败语义】
       任一来源抓取失败即整体不写入：日历不完整时写入「部分股票有退市日、
       部分没有」的半成品，比不写更危险（会静默污染股票池推导）。

    Args:
        database (Database): 数据库连接管理器

    Returns:
        bool: 是否同步成功
    """
    _logger.info("=" * 60)
    _logger.info("阶段七：同步证券生命周期（沪深北上市日历 + 退市日历）")
    _logger.info("=" * 60)

    service = LifecycleService()

    listing = service.fetch_listing_calendar()
    if listing.empty:
        _logger.error("上市日历获取失败，生命周期同步中止（不写入任何数据）")
        return False

    delisting = service.fetch_delisting_calendar()
    if delisting.empty:
        _logger.error("退市日历获取失败，生命周期同步中止（不写入任何数据）")
        return False

    securities_repository = SecurityRepository(database)
    merged = merge_lifecycle(securities_repository.find_all(), listing, delisting)
    if securities_repository.upsert_lifecycle(merged) == 0:
        _logger.error("证券主表生命周期字段写入失败")
        return False

    events = build_lifecycle_events(listing, delisting)
    event_count = SecurityEventRepository(database).upsert(events)
    if event_count == 0:
        _logger.error("生命周期事件写入失败")
        return False

    _logger.info(
        "生命周期同步完成: 证券 %d 只（上市日历 %d、退市 %d），事件 %d 条",
        len(merged), len(listing), len(delisting), event_count,
    )
    return True


def sync_fundamentals(database, ts_code_list=None, max_workers=8):
    """
    阶段八：同步 Point-in-Time 基本面数据（三大报表 + 财务指标）

    【并发设计（与阶段四一致）】
       单只股票约 60 次 HTTP 请求（按报告期分批），全市场约 35 万次请求，
       数小时量级。DuckDB 连接非线程安全，因此**并发只用于取数**，
       结果回收到主线程后由单一连接串行落库。

    【写入顺序】
       利润表 / 资产负债表 / 现金流量表 → 由前两者派生 financial_indicators。
       派生失败不影响报表入库（指标可事后重算，报表才是原始事实）。

    Args:
        database (Database): 数据库连接管理器
        ts_code_list (list, optional): 指定同步的证券代码；为空时同步全市场
        max_workers (int, optional): 取数并发线程数，默认 8

    Returns:
        bool: 是否同步成功（全部失败时返回 False）
    """
    _logger.info("=" * 60)
    _logger.info(
        "阶段八：同步 Point-in-Time 基本面（并发 %d）", max_workers
    )
    _logger.info("=" * 60)

    securities = SecurityRepository(database).find_all()
    if securities.empty:
        _logger.error("证券列表为空，请先执行: sync_market_data.py securities")
        return False

    if ts_code_list:
        targets = list(ts_code_list)
        _logger.info("限定同步 %d 只指定标的", len(targets))
    else:
        targets = securities["ts_code"].tolist()
        _logger.info("全市场 %d 只，逐只抓取三大报表（耗时以小时计）", len(targets))

    income_repository = IncomeStatementRepository(database)
    balance_repository = BalanceSheetRepository(database)
    cashflow_repository = CashflowStatementRepository(database)
    indicator_repository = FinancialIndicatorRepository(database)

    service = FundamentalService()

    def fetch_one(ts_code):
        """工作线程任务：只做取数与归一化，不触碰数据库"""
        try:
            return ts_code, (
                service.fetch_income_statement(ts_code),
                service.fetch_balance_sheet(ts_code),
                service.fetch_cashflow_statement(ts_code),
            )
        except Exception as error:
            _logger.debug("并发取数异常 [%s]: %s", ts_code, error)
            return ts_code, (pd.DataFrame(), pd.DataFrame(), pd.DataFrame())

    success_count = 0
    failed_count = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for index, (ts_code, frames) in enumerate(executor.map(fetch_one, targets)):
            income, balance, cashflow = frames

            if income.empty:
                failed_count += 1
            else:
                income_repository.upsert(income)
                if balance.empty:
                    _logger.warning("[%s] 资产负债表缺失，财务指标暂按 NaN 写入", ts_code)
                else:
                    balance_repository.upsert(balance)
                if not cashflow.empty:
                    cashflow_repository.upsert(cashflow)
                indicators = build_financial_indicators(income, balance)
                if not indicators.empty:
                    indicator_repository.upsert(indicators)
                success_count += 1

            # 全市场耗时以小时计，无进度反馈难以判断是否卡死
            if (index + 1) % 20 == 0:
                _logger.info(
                    "进度 %d/%d | 成功 %d 只 | 失败 %d 只",
                    index + 1, len(targets), success_count, failed_count,
                )

    _logger.info(
        "基本面同步完成: 成功 %d 只，失败 %d 只", success_count, failed_count
    )
    if success_count == 0:
        _logger.error("全部 %d 只标的基本面抓取失败", len(targets))
        return False
    return True


# 公告翻页与逐股之间的固定等待秒数（限流；禁止高并发压测）
_ANNOUNCEMENT_SLEEP_SECONDS = 0.8


def _filter_a_share_universe(frame, database):
    """全市场模式下剔除非 A 股标的（债券/基金/ETF/B 股等以股票代码形态混入的公告）"""
    if frame is None or frame.empty:
        return frame
    conn = database.get_connection()
    rows = conn.execute("SELECT ts_code FROM reference.securities").fetchall()
    universe = {row[0] for row in rows}
    filtered = frame[frame["ts_code"].isin(universe)]
    dropped = len(frame) - len(filtered)
    if dropped:
        _logger.info("剔除不在 A 股股票池内的公告 %d 条", dropped)
    return filtered


def _write_announcements(repository, frame, existing_keys):
    """
    公告落库：补齐抓取元数据 -> 跨 announcement_id 二次去重 -> 只插入

    Returns:
        int: 实际提交写入的行数
    """
    if frame is None or frame.empty:
        return 0

    frame = frame.copy()
    # DuckDB DATE 列统一收口为 datetime.date（避免 dtype 漂移导致类型不匹配）
    frame["announcement_date"] = pd.to_datetime(frame["announcement_date"]).dt.date
    frame["crawl_time"] = datetime.now()
    frame["content_hash"] = None  # 预留：PDF 正文哈希（第二阶段能力）

    keep_mask = [
        (ts_code, day, title) not in existing_keys
        for ts_code, day, title in zip(
            frame["ts_code"], frame["announcement_date"], frame["title"]
        )
    ]
    duplicate_count = len(frame) - sum(keep_mask)
    if duplicate_count:
        _logger.info("跨 announcement_id 的同内容公告去重 %d 条", duplicate_count)
        frame = frame[keep_mask]
        if frame.empty:
            return 0

    written = repository.insert(frame)
    for ts_code, day, title in zip(
        frame["ts_code"], frame["announcement_date"], frame["title"]
    ):
        existing_keys.add((ts_code, day, title))
    return written


def sync_announcements(database, start_date=None, end_date=None, ts_codes=None,
                       client=None):
    """
    阶段九：同步巨潮公告索引（corporate.announcements；不参与一键全跑）

    【增量策略（需求 §49 / §51）】
      - 未指定 --start-date 时取表内水位 MAX(announcement_date) 作为起点；
      - 表为空时拒绝执行，必须显式给出起始日期（如 2015-01-01），
        禁止默认从远古日期无限回溯；
      - 全市场模式按日期区间翻页抓取（适合日常增量），--ts-code 指定时
        逐只抓取（每只都走 orgId 精确查询）。

    Args:
        client: 公告客户端（默认 CninfoClient；测试注入离线桩）

    Returns:
        bool: 是否成功
    """
    _logger.info("=" * 60)
    _logger.info("阶段九：同步巨潮公告索引")
    _logger.info("=" * 60)

    repository = AnnouncementRepository(database)
    if client is None:
        client = CninfoClient(sleep_seconds=_ANNOUNCEMENT_SLEEP_SECONDS)

    end = end_date or datetime.now().strftime("%Y-%m-%d")
    if start_date:
        start = start_date
    else:
        watermark = repository.latest_date()
        if watermark is None:
            _logger.error(
                "公告表为空：首次同步必须用 --start-date 指定起始日期"
                "（如 2015-01-01），禁止默认无限回溯"
            )
            return False
        start = watermark.strftime("%Y-%m-%d")

    if start > end:
        _logger.error("起始日期 %s 晚于结束日期 %s", start, end)
        return False

    existing_keys = repository.existing_keys(start, end)
    _logger.info(
        "同步区间 %s ~ %s | 区间内已有 %d 条（将做二次去重）", start, end, len(existing_keys)
    )

    written_total = 0
    if ts_codes:
        # 逐只模式：走 orgId 精确查询，适合定点补齐
        targets = [str(code).strip() for code in ts_codes if str(code).strip()]
        for index, ts_code in enumerate(targets):
            frame = client.fetch_announcements(ts_code, start, end, max_pages=1000)
            written = _write_announcements(repository, frame, existing_keys)
            written_total += written
            _logger.info(
                "进度 %d/%d | %s 新增 %d 条", index + 1, len(targets), ts_code, written
            )
            time.sleep(_ANNOUNCEMENT_SLEEP_SECONDS)
    else:
        # 全市场模式：按日期区间翻页抓取（不传 stock 参数）
        frame = client.fetch_announcements(None, start, end, max_pages=10000)
        frame = _filter_a_share_universe(frame, database)
        written_total = _write_announcements(repository, frame, existing_keys)

    _logger.info("公告同步完成: 新增 %d 条", written_total)
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

    # 全局安装浏览器 UA 补丁（规避东财 WAF 反爬阻断），仅在入口显式调用一次
    install_browser_user_agent()

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
            if not sync_valuation_history(database, args.period, max_workers=args.workers):
                _logger.error("历史估值序列同步失败")
                sys.exit(1)
            return

        # 阶段五：同步指数成分（耗时极短，默认参与一键全跑）
        if command in (None, "indexes"):
            if not sync_index_membership(database):
                _logger.warning("指数成分同步失败（不影响其他阶段数据）")
            if command == "indexes":
                return

        # 阶段六：同步行业估值（默认不参与一键全跑：数据源可能不可用且需指定日期）
        if command == "industries":
            stat_date = args.stat_date if args.stat_date else datetime.now().strftime("%Y-%m-%d")
            if not sync_industry_valuation(database, stat_date, args.classification):
                sys.exit(1)
            return

        # 阶段七：同步证券生命周期（参与一键全跑：仅 6 次请求，且是历史研究的前提）
        if command in (None, "lifecycle"):
            if not sync_lifecycle(database):
                _logger.error("证券生命周期同步失败")
                sys.exit(1)
            if command == "lifecycle":
                return

        # 阶段八：同步基本面（默认不参与一键全跑：逐只抓取，耗时以小时计）
        if command == "fundamentals":
            ts_codes = None
            if args.ts_code:
                ts_codes = [
                    item.strip() for item in args.ts_code.split(",") if item.strip()
                ]
            if not sync_fundamentals(database, ts_codes, max_workers=args.workers):
                _logger.error("基本面同步失败")
                sys.exit(1)
            return

        # 阶段九：同步公告索引（默认不参与一键全跑：区间翻页抓取，需显式触发）
        if command == "announcements":
            ts_codes = None
            if args.ts_code:
                ts_codes = [
                    item.strip() for item in args.ts_code.split(",") if item.strip()
                ]
            if not sync_announcements(database, args.start_date, args.end_date, ts_codes):
                _logger.error("公告索引同步失败")
                sys.exit(1)
            return

    _logger.info("=" * 60)
    _logger.info("同步任务完成")
    _logger.info("=" * 60)


if __name__ == "__main__":
    main()
