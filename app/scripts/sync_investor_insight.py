#!/usr/bin/env python3
"""
==============================================================================
StockLab - 投资人观点同步 CLI (app/scripts/sync_investor_insight.py)
==============================================================================

【功能用途】
   把各平台的投资人公开言论同步到 insight.investor_quotes，
   并负责载入投资人主表与平台账号映射。

【运行方式】
   # 1) 首次：载入投资人 / 账号登记表（只插入，可反复执行）
   python app/scripts/sync_investor_insight.py load-sources

   # 2) 同步人工录入的语录（不联网，秒级完成）
   python app/scripts/sync_investor_insight.py manual

   # 3) 同步某平台（需先配好登录态，见下）
   python app/scripts/sync_investor_insight.py collect --platform xueqiu --limit 50

   # 4) 同步全部启用中的平台账号
   python app/scripts/sync_investor_insight.py collect --all

   # 5) 只看会抓哪些账号、以及凭证是否就绪（不发任何请求）
   python app/scripts/sync_investor_insight.py status

【凭证配置】
   雪球需要登录态 cookie。从浏览器访问 https://xueqiu.com/ 后，
   复制 xq_a_token 等 cookie 串，设置为环境变量：
       export STOCKLAB_XUEQIU_COOKIE='xq_a_token=...; u=...'
   【为什么不支持账号密码登录】采集器里存密码意味着密钥进代码库；
   cookie 过期只需重新拷一次，而密码泄露是不可逆的。

【退出码语义】
   0  全部成功（含「本次无新增」这种正常情况）
   1  有账号采集失败（凭据/限流/改版/网络任一）
   2  配置或输入错误（登记表不存在、路径写错等）

   【为什么失败不一定退出码 1】
       一个平台的账号被限流，不该让其它平台的同步结果作废。所以退出码只回答
       「这一轮是否**有**账号失败」，具体每个账号的失败原因在日志与
       AccountFetchOutcome.describe() 里逐条列出。
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta

import pandas as pd

# 将项目根目录加入模块搜索路径（与 sync_market_data.py 一致，
# 否则直接执行脚本会 ModuleNotFoundError: No module named 'stocklab'）
sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from stocklab.common.config import find_config_path
from stocklab.common.http_client import install_browser_user_agent
from stocklab.datasource.insight import (
    AccountFetchOutcome,
    build_default_registry,
    fetch_account_quotes,
    normalize_outcome,
)
from stocklab.datasource.insight.manual import DEFAULT_SEED_PATH
from stocklab.domain import (
    INVESTOR_ACCOUNT_COLUMNS,
    INVESTOR_COLUMNS,
)
from stocklab.normalization.insight import (
    normalize_investor_accounts,
    normalize_investors,
)
from stocklab.persistence.repository.insight import (
    InvestorAccountRepository,
    InvestorQuoteRepository,
    InvestorRepository,
)
from stocklab.persistence.storage import initialize_database
from stocklab.persistence.storage.duckdb import Database

_logger = logging.getLogger(__name__)

__all__ = [
    "parse_args",
    "load_sources",
    "sync_quotes",
    "show_status",
    "main",
]

# 投资人 / 账号登记表默认路径
DEFAULT_SOURCES_PATH = os.path.join("configs", "insight_sources.json")

# 环境变量名 -> 平台标识
CREDENTIAL_ENV = {
    "xueqiu": "STOCKLAB_XUEQIU_COOKIE",
}

# 增量水位兜底：库里没有任何记录时，向前回看多少天。
# 【为什么需要兜底】第一次同步若不带 --since，会把该账号的全部历史发言
# 都抓进来；但我们通常只想要近期观点。让用户传 --since 又要求他先知道
# 该账号活跃到什么程度 —— 所以默认回看 30 天。
DEFAULT_LOOKBACK_DAYS = 30


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
            "投资人观点同步工具\n\n"
            "子命令：\n"
            "  load-sources  载入投资人主表与平台账号映射（只插入，可重复执行）\n"
            "  manual        同步人工录入的语录（不联网）\n"
            "  collect       按平台或全平台抓取言论\n"
            "  status        查看账号清单与凭证就绪状态（不发请求）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["load-sources", "manual", "collect", "status"],
        default="status",
        help="要执行的动作（缺省 = status）",
    )
    parser.add_argument(
        "--platform",
        type=str,
        default=None,
        help="collect 阶段限定的平台（xueqiu / guba / manual）；缺省 = 全部启用账号",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="collect 阶段显式要求抓全部启用账号（与不传 --platform 等价，写出来是为了让脚本调用者意图明确）",
    )
    parser.add_argument(
        "--account",
        type=str,
        default=None,
        help="collect 阶段限定的单个 account_id；缺省 = 该平台全部启用账号",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="每个账号本次最多抓多少条（默认 50）",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help=(
            "增量起点 YYYY-MM-DD，只取 published_at 晚于该日期的内容；"
            "缺省按「库内该账号最新发表时间」，库内为空时回看 %d 天"
            % DEFAULT_LOOKBACK_DAYS
        ),
    )
    parser.add_argument(
        "--sources-path",
        type=str,
        default=DEFAULT_SOURCES_PATH,
        help="投资人 / 账号登记表路径（默认 %s）" % DEFAULT_SOURCES_PATH,
    )
    parser.add_argument(
        "--manual-path",
        type=str,
        default=DEFAULT_SEED_PATH,
        help="人工录入文件路径（默认 %s）" % DEFAULT_SEED_PATH,
    )
    parser.add_argument(
        "--credential",
        type=str,
        default=None,
        help="临时指定平台登录态（覆盖环境变量；会出现在 shell 历史里，调试用）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="单请求超时秒数（默认 15）",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="数据库文件路径（默认 data/stocklab.duckdb）",
    )
    return parser.parse_args(argv)


# ----------------------------------------------------------------------------
# 阶段一：载入投资人 / 账号登记表
# ----------------------------------------------------------------------------
def load_sources(database, sources_path=DEFAULT_SOURCES_PATH):
    """
    载入投资人主表与平台账号映射

    【为什么是只插入而不是 upsert】
       登记表改了（改了名字、补了 UID、禁用了一个号），直觉上应该"更新"。
       但 investors / investor_accounts 是**事实记录**：禁用一个账号若走 UPDATE
       覆盖，历史言论就失去了「当时挂在哪个账号下」的追溯依据。所以这里的
       语义是「追加一条新事实」；要改状态请追加 is_enabled=false 的新行，
       同步侧按 account_id 最新的 enabled 状态判断。

    Args:
        database (Database): 数据库句柄
        sources_path (str): 登记表路径

    Returns:
        tuple[int, int]: (投资人**新增**行数, 账号**新增**行数)
            新增而非提交 —— 登记表是可重复执行的，只追加不覆盖，
            重跑时应当如实报 0 而不是报「写了 3 条」。

    Raises:
        FileNotFoundError: 登记表不存在
        ValueError:        JSON 结构不对
    """
    sources_path = find_config_path(sources_path)
    if not os.path.exists(sources_path):
        raise FileNotFoundError(
            "投资人登记表不存在: %s（仓库里应有一份样例 "
            "configs/insight_sources.json）" % sources_path
        )
    with open(sources_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(
            "登记表顶层应为对象（investors / accounts 两个键），实际是 %s"
            % type(payload).__name__
        )

    _logger.info("=" * 60)
    _logger.info("载入投资人登记表: %s", sources_path)
    _logger.info("=" * 60)

    investor_repo = InvestorRepository(database)
    account_repo = InvestorAccountRepository(database)

    # 记录写入前的行数：insert() 返回的是「提交行数」而非「新增行数」
    # （冲突行被静默忽略），不前后对比的话，日志会在重复执行时
    # 打出「提交 3 条」而实际一条都没进库。
    investors_before = investor_repo.count()
    investor_frame = normalize_investors(payload.get("investors"))
    investor_repo.insert(investor_frame)
    investor_new = investor_repo.count() - investors_before
    _logger.info(
        "投资人：待写入 %d 条，新增 %d 条",
        len(investor_frame), investor_new,
    )

    account_frame = normalize_investor_accounts(payload.get("accounts"))
    # 账号必须挂在一个已存在的投资人下，否则言论无处归属 —— 这里显式校验，
    # 因为登记表是手写的，写错 investor_code 的概率不低，而它不会引起任何
    # 异常，只会让账号「静静地没人用」。
    known = set(investor_frame["investor_code"]) if not investor_frame.empty else set()
    if not account_frame.empty:
        orphans = [
            code for code in account_frame["investor_code"] if code not in known
        ]
        if orphans:
            raise ValueError(
                "账号引用了登记表中不存在的投资人: %s"
                % ", ".join(sorted(set(orphans)))
            )
    accounts_before = account_repo.count()
    account_repo.insert(account_frame)
    account_new = account_repo.count() - accounts_before
    _logger.info("平台账号：待写入 %d 条，新增 %d 条", len(account_frame), account_new)

    _logger.info("库内合计：投资人 %d 人 / 账号 %d 个",
                 investor_repo.count(), account_repo.count())
    return investor_new, account_new


# ----------------------------------------------------------------------------
# 阶段二 / 三：同步言论
# ----------------------------------------------------------------------------
def resolve_since(args, quote_repo, account_frame):
    """
    推导每个账号的增量起点

    【水位按投资人取，不按账号取】
       一个人常有多个平台账号。若各账号各查各的水位，会出现「雪球侧水位老 ->
       把该投资人在雪球的全部历史重抓一遍」，而这些内容 hash 早已入库 ——
       白跑一遍请求，还可能把自己限流。见 InvestorQuoteRepository
       .latest_published_at 的说明。

    【库内为空时回看 DEFAULT_LOOKBACK_DAYS】
       第一次同步不该把某人全部历史发言都抓进来：既慢又无意义
       （价值投资的长篇论述更该走 manual 通道带溯源）。

    Args:
        args (Namespace): 命令行参数
        quote_repo (InvestorQuoteRepository): 言论表句柄
        account_frame (pd.DataFrame): 待抓账号

    Returns:
        dict[str, str]: {account_id: 增量起点 YYYY-MM-DD}
    """
    if args.since:
        return {
            str(account_id): args.since
            for account_id in account_frame["account_id"]
        }

    fallback = (
        datetime.now() - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    ).strftime("%Y-%m-%d")
    watermarks = {}
    for account in account_frame.to_dict("records"):
        latest = quote_repo.latest_published_at(account["investor_code"])
        if latest is None:
            watermarks[account["account_id"]] = fallback
        else:
            watermarks[account["account_id"]] = pd.Timestamp(
                latest
            ).strftime("%Y-%m-%d")
    return watermarks


def sync_quotes(database, args, registry=None):
    """
    采集并写入言论

    Args:
        database (Database): 数据库句柄
        args (Namespace): 命令行参数
        registry (InsightCollectorRegistry, optional): 注入的采集器注册表

    Returns:
        tuple[int, int, list[AccountFetchOutcome]]: (成功账号数, 失败账号数, 失败明细)
    """
    account_repo = InvestorAccountRepository(database)
    quote_repo = InvestorQuoteRepository(database)

    # 人工录入是个例外：它读本地文件，不绑定平台账号，
    # 所以不该走「枚举启用账号」这条路 —— 否则新人第一次跑 manual
    # 会看到「没有待抓账号」而一头雾水（登记表里本来就没有 manual 账号）。
    if args.platform == "manual":
        synthetic = [{
            "account_id": "manual:%s" % args.manual_path,
            "investor_code": None,
            "platform": "manual",
            "account_name": "人工录入",
            "account_uid": None,
        }]
        account_frame = pd.DataFrame(synthetic)
    else:
        account_frame = account_repo.find_enabled(platform=args.platform)
        if args.account:
            account_frame = account_frame[
                account_frame["account_id"] == args.account
            ]
        if account_frame.empty:
            _logger.warning(
                "没有待抓账号（platform=%s account=%s）。"
                "先跑 load-sources，或检查 is_enabled。",
                args.platform or "全部", args.account or "未指定",
            )
            return 0, 0, []

    # 增量水位只对「按账号抓取」有意义：人工录入是一次性导入文件，
    # 文件本身就是全量，再加水位过滤只会漏内容。
    watermarks = {} if args.platform == "manual" else resolve_since(
        args, quote_repo, account_frame
    )

    registry = registry or _build_registry(args)
    captured_at = pd.Timestamp.now("UTC")
    ok_count, failed, total_new = 0, [], 0

    _logger.info("=" * 60)
    _logger.info("同步投资人言论：%d 个账号 / 抓取上限 %d 条/账号",
                 len(account_frame), args.limit)
    _logger.info("=" * 60)

    for account in account_frame.to_dict("records"):
        account_id = account["account_id"]
        since = watermarks.get(account_id)
        try:
            collector = registry.get(account["platform"])
        except Exception as error:      # noqa: BLE001 - 平台未注册不应中断整轮
            _logger.error("[%s] %s", account_id, error)
            failed.append(AccountFetchOutcome(
                account_id, account.get("platform"), [], str(error), "network",
            ))
            continue

        outcome = fetch_account_quotes(
            collector, account, limit=args.limit, start_time=since,
            timeout=args.timeout,
        )
        for warning in outcome.warnings:
            _logger.warning("[%s] %s", account_id, warning)

        if not outcome.ok:
            # 分页中途失败但已有部分数据时，仍然把已有的写进去，
            # 并保留 failed 标记 —— 让退出码如实反映「这一轮有账号没抓完」
            _logger.error("[%s] %s", account_id, outcome.describe())
            if not outcome.retryable:
                _logger.info(
                    "  -> 该失败类型不建议重试（%s），请按提示处理",
                    outcome.error_kind,
                )
            failed.append(outcome)

        if not outcome.records:
            _logger.info(
            "[%s] 本次无新增内容%s",
            account_id, "（水位 %s）" % since if since else "",
        )
            if outcome.ok:
                ok_count += 1
            continue

        existing = quote_repo.existing_hashes(
            [item.get("content_hash") for item in outcome.records]
        )
        frame = normalize_outcome(
            outcome, account, captured_at=captured_at, existing_hashes=existing,
        )
        if frame.empty:
            _logger.info(
                "[%s] 抓到 %d 条，去重后 0 条（均为已知内容）",
                account_id, len(outcome.records),
            )
            if outcome.ok:
                ok_count += 1
            continue

        # insert() 返回的是**提交行数**，冲突行被 ON CONFLICT DO NOTHING
        # 静默丢弃且不计入。不前后对比行数就会在重复同步时报「新增 3 条」，
        # 而库里一条都没多 —— 用户据此以为增量同步成功了。
        before = quote_repo.count()
        quote_repo.insert(frame)
        written = quote_repo.count() - before
        total_new += written
        _logger.info(
            "[%s] 抓到 %d 条 -> 新增 %d 条（库内合计 %d）%s",
            account_id, len(outcome.records), written, quote_repo.count(),
            "（水位 %s）" % since if since else "",
        )
        if outcome.ok:
            ok_count += 1

    _logger.info("=" * 60)
    _logger.info("同步完成：成功 %d 个账号 / 失败 %d 个 / 合计新增 %d 条",
                 ok_count, len(failed), total_new)
    _logger.info("库内言论总数 %d 条，核验状态分布 %s",
                 quote_repo.count(), quote_repo.verification_summary())
    _logger.info("=" * 60)
    return ok_count, len(failed), failed


# ----------------------------------------------------------------------------
# 阶段四：状态自检
# ----------------------------------------------------------------------------
def show_status(database, sources_path=DEFAULT_SOURCES_PATH,
                manual_path=DEFAULT_SEED_PATH):
    """
    打印账号清单与凭证就绪状态（**不发任何网络请求**）

    Args:
        database (Database): 数据库句柄
        sources_path (str): 登记表路径
        manual_path (str): 人工录入文件路径

    Returns:
        dict: 状态摘要（测试断言用）
    """
    account_repo = InvestorAccountRepository(database)
    quote_repo = InvestorQuoteRepository(database)
    investor_repo = InvestorRepository(database)

    accounts = account_repo.find_enabled()
    print("=" * 62)
    print("投资人观点库状态")
    print("=" * 62)
    print("投资人 %d 人 / 平台账号 %d 个 / 言论 %d 条"
          % (investor_repo.count(), account_repo.count(), quote_repo.count()))
    summary = quote_repo.verification_summary()
    if summary:
        print("核验状态分布: %s"
              % " / ".join("%s=%d" % (k, v) for k, v in sorted(summary.items())))
    else:
        print("核验状态分布: （暂无数据）")

    if accounts.empty:
        print("\n库内没有启用中的平台账号 —— 先执行 load-sources。")
    else:
        print("\n启用中的平台账号：")
        for account in accounts.to_dict("records"):
            _print_account_row(account, sources_path)

    # 停用的账号也要列出来：上面的「平台账号 3 个」是总数，
    # 只列 1 个会让人以为数据不对；而且停用恰恰是
    # 「UID 还没确认」这种待办最常见的存放处。
    disabled = account_repo.find_all()
    if not disabled.empty:
        active_ids = set(accounts["account_id"]) if not accounts.empty else set()
        paused = [
            row for row in disabled.to_dict("records")
            if row["account_id"] not in active_ids
        ]
        if paused:
            print("\n已停用的平台账号（同步跳过，历史数据保留）：")
            for account in paused:
                print("  %-22s %-8s uid=%-12s %s"
                      % (account["account_id"],
                         account.get("platform"),
                         _uid_text(account.get("account_uid")),
                         account.get("note") or "停用中"))
                if _uid_text(account.get("account_uid")) == "（未填）":
                    print("      -> 想启用须先填 account_uid 并置 is_enabled=true，"
                          "见 %s" % sources_path)

    registry = build_default_registry()
    print("\n已注册采集器：")
    for info in registry.describe():
        flag = "就绪" if info["credential_configured"] else "缺凭证"
        print("  %-8s %-14s 限速 %.1fs  凭证: %s"
              % (info["platform"], info["display_name"],
                 info["min_interval_seconds"], flag))

    print("\n人工录入文件：%s"
          % ("存在" if os.path.exists(manual_path) else "不存在（执行 manual 前需先创建）"))
    return {
        "investor_count": investor_repo.count(),
        "account_count": account_repo.count(),
        "quote_count": quote_repo.count(),
        "enabled_accounts": [] if accounts.empty else accounts["account_id"].tolist(),
        "verification_summary": summary,
    }


# ----------------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------------
def _uid_text(value):
    """
    把 account_uid 显示成人类可读文本

    【为什么不能直接用 or "（未填）"】
       DuckDB 的 NULL 经 fetchdf() 变成 float('nan')，而
       `nan or "（未填）"` 返回的是 nan —— 因为 NaN 是 truthy。
       结果状态页会打印 `uid=nan`，看起来像配置了一个叫 "nan" 的用户号，
       而它实际是「没填」。这正是本项目一直警惕的「脏值伪装成有效值」。

    Args:
        value: 原始 uid

    Returns:
        str: uid 或 "（未填）"
    """
    if value is None:
        return "（未填）"
    try:
        if pd.isna(value):
            return "（未填）"
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text:
        return "（未填）"
    return text


def _print_account_row(account, sources_path):
    """
    打印一个启用账号及其凭证状态

    Args:
        account (dict): 账号行
        sources_path (str): 登记表路径（缺 UID 时的排查提示）
    """
    platform = account.get("platform")
    _, note = _credential_state(platform)
    uid = _uid_text(account.get("account_uid"))
    print("  %-22s %-8s uid=%-12s %s"
          % (account.get("account_id"), platform, uid, note))
    if uid == "（未填）":
        # 启用中的账号缺 UID 是硬错误：同步会每次报错
        print("      -> 采集时会报「缺少 account_uid」并跳过，"
              "请查 %s" % sources_path)


def _credential_state(platform):
    """
    查某平台的凭证是否就绪

    Args:
        platform (str): 平台标识

    Returns:
        tuple[bool, str]: (是否就绪, 人类可读说明)
    """
    env_name = CREDENTIAL_ENV.get(platform)
    if not env_name:
        return True, "无需凭证"
    if os.environ.get(env_name):
        return True, "凭证就绪 (%s)" % env_name
    return False, "缺凭证 -> export %s='...'" % env_name


def _build_registry(args):
    """
    按参数构造采集器注册表（含凭证注入）

    Args:
        args (Namespace): 命令行参数

    Returns:
        InsightCollectorRegistry
    """
    credentials = {}
    for platform, env_name in CREDENTIAL_ENV.items():
        value = os.environ.get(env_name)
        if value:
            credentials[platform] = value
    if args.credential:
        platform = str(args.platform or "").strip().lower()
        if platform:
            credentials[platform] = args.credential
    return build_default_registry(
        credentials=credentials, manual_path=args.manual_path,
    )


def main(argv=None):
    """
    主入口

    Args:
        argv (list, optional): 参数列表（测试注入用）

    Returns:
        int: 退出码（0 成功 / 1 有账号采集失败 / 2 配置或输入错误）
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)

    # 统一解析相对配置路径（CWD -> 项目根），否则从别处执行会找不到文件
    args.sources_path = find_config_path(args.sources_path)
    args.manual_path = find_config_path(args.manual_path)

    # 全局安装浏览器 UA 补丁（与其它 CLI 一致），仅在入口显式调用一次
    install_browser_user_agent()

    # 必须走 initialize_database：它每次都会检查版本并应用未生效的迁移。
    # 直接 Database(db_path) 不会迁移，在全新库上 load-sources 会立刻
    # 撞上「表 insight.investors 不存在」（与 sync_market_data.py 一致）。
    db_path = initialize_database(args.db_path)
    _logger.info("数据库路径: %s", db_path)

    with Database(db_path) as database:
        try:
            if args.command == "load-sources":
                load_sources(database, args.sources_path)
                return 0

            if args.command == "status":
                show_status(database, args.sources_path, args.manual_path)
                return 0

            if args.command == "manual":
                # 人工录入是本地文件，与「--platform」无关
                manual_args = argparse.Namespace(**vars(args))
                manual_args.platform = "manual"
                manual_args.account = None
                _, failed, _ = sync_quotes(database, manual_args)
                return 1 if failed else 0

            if args.command == "collect":
                if not args.platform and not args.all:
                    _logger.error(
                        "collect 需要 --platform <平台> 或 --all（"
                        "用 status 看清有哪些启用账号）"
                    )
                    return 2
                _, failed, _ = sync_quotes(database, args)
                return 1 if failed else 0
        except FileNotFoundError as error:
            _logger.error("%s", error)
            return 2
        except (ValueError, KeyError) as error:
            _logger.error("配置错误: %s", error)
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
