#!/usr/bin/env python3
"""
==============================================================================
StockLab - 采集器注册表与编排 (stocklab.datasource.insight.collectors)
==============================================================================

【模块职责】
   1. 注册表：平台标识 -> 采集器实现
   2. 编排：把「账号表的一行」变成「一批归一化后的言论」，
      含分页、增量水位、跨平台 hash 去重

【分页为什么在这里而不是各采集器内部】
   雪球按 page 翻页、股吧按 p 翻页，字段名与起点还不同（第 1 页 vs 第 0 页）。
   若让各采集器自己管分页，就会出现「某个采集器把翻页写错了但没人发现」，
   因为单独测它时只测了第一页。所以统一由本模块按「取满 limit 就停」驱动，
   采集器只负责 _fetch_page(page) —— **页码从 1 开始**，起点统一，
   各采集器自己适配自己的序号。

【增量水位：为什么用 published_at 而不是 captured_at】
   增量同步问的是「上次之后有什么新内容」，而这个「之后」只能用内容自身的
   发表时间回答。用抓取时间回答会让「上次抓过但漏掉的」永远补不上 ——
   因为它们的抓取时间也在上次之内。所以水位取 published_at；缺失时
   **不能退化成按抓取时间**，而是选择把它排在最后单独处理（见 fetch_account）。

【去重的位置】
   在归一化之后、落库之前。理由：hash 必须由归一化后的正文算出，
   否则「平台 A 的 HTML 片段」和「平台 B 的纯文本」永远不会撞上。
"""

import logging

import pandas as pd

from stocklab.domain import PLATFORMS
from stocklab.datasource.insight.base import (
    CollectRequest,
    InsightBlockedError,
    InsightCollectError,
    InsightCredentialError,
)
from stocklab.datasource.insight.guba import GubaCollector
from stocklab.datasource.insight.manual import ManualInsightCollector
from stocklab.datasource.insight.xueqiu import XueqiuCollector
from stocklab.normalization.insight import normalize_investor_quotes

_logger = logging.getLogger(__name__)

__all__ = [
    "InsightCollectorRegistry",
    "build_default_registry",
    "fetch_account_quotes",
    "AccountFetchOutcome",
]

# 平台标识 -> 采集器类。新增平台只改这一处 + PLATFORMS 元组。
_COLLECTOR_CLASSES = {
    "xueqiu": XueqiuCollector,
    "guba": GubaCollector,
    "manual": ManualInsightCollector,
}

# 分页安全上限：防止「平台忽略 page 参数、永远返回第 1 页」导致死循环
MAX_PAGES = 10


class AccountFetchOutcome:
    """
    单个账号的采集结果（成功/部分成功/失败的显式表达）

    【为什么要一个 outcome 对象而不是直接返回 DataFrame】
       采集失败必须**可区分**，否则同步脚本只能说「这个号失败了」，
       而用户真正要回答的问题是「哪个号、什么原因、要不要重试」。
       三个失败原因的处理完全不同：
         凭证缺失 -> 改配置（重试无用）
         被限流   -> 停手（再重试就是恶意抓取）
         解析失败 -> 改代码（平台改版了）
    """

    def __init__(self, account_id, platform, records, error=None,
                 error_kind="", pages=0, warnings=None):
        """
        Args:
            account_id (str): 账号 id
            platform (str): 平台标识
            records (list[dict]): 原始记录（未去重）
            error (str, optional): 失败原因文本
            error_kind (str): "" | "credential" | "blocked" | "parse" | "network"
            pages (int): 实际翻了几页
            warnings (list, optional): 非致命的告警
        """
        self.account_id = account_id
        self.platform = platform
        self.records = records
        self.error = error
        self.error_kind = error_kind
        self.pages = pages
        self.warnings = list(warnings or [])

    @property
    def ok(self):
        """是否采到数据（哪怕是空结果也算成功）"""
        return self.error is None

    @property
    def retryable(self):
        """
        是否值得重试

        只有「网络/上游瞬时故障」才重试。凭证缺失与解析失败重试一百次
        也是同样结果；而被限流时重试等于继续违规，更不能重试。
        """
        return self.error_kind == "network"

    def describe(self):
        """
        一行摘要（写进同步日志）

        Returns:
            str
        """
        state = "OK %d 条/%d 页" % (len(self.records), self.pages) if self.ok \
            else "失败[%s] %s" % (self.error_kind or "unknown", self.error)
        return "%s@%s: %s" % (self.account_id, self.platform, state)


class InsightCollectorRegistry:
    """
    采集器注册表

    【与 datasource/quote_service 的多级降级链不同】
       行情是「同一份数据问多个源，谁先给谁说了算」；
       投资人观点是「不同平台各有各的账号，必须全都要」——
       雪球有段永平、股吧有别人，缺一个不是「降级到下一个」，
       是「少一个人」。所以这里**没有降级链**，只有按平台取对应采集器；
       找不到就是配置错误，必须报错。
    """

    def __init__(self, collectors=None):
        """
        Args:
            collectors (dict, optional): 平台 -> 采集器实例；默认构造全部
        """
        # 必须用 `is not None` 而不是 truthiness：传 {} 的意图是
        # 「我要一个空注册表」（测试里用来验证错误路径），而 `if collectors`
        # 会把空 dict 当 falsy、悄悄构造出默认注册表 —— 断言就永不触发。
        self._collectors = dict(collectors) if collectors is not None \
            else build_default_registry()

    def register(self, platform, collector):
        """
        注册/替换某平台的采集器（测试注入桩用）

        Args:
            platform (str): 平台标识
            collector (InsightCollector): 采集器实例

        Raises:
            ValueError: 平台标识不在 domain.insight.PLATFORMS 里
        """
        key = str(platform or "").strip().lower()
        if key not in PLATFORMS:
            raise ValueError(
                "平台 %r 不在 PLATFORMS 契约内（%s）—— 新增平台请先改 "
                "stocklab/domain/insight.py 的 PLATFORNS"
                % (platform, ", ".join(PLATFORMS))
            )
        self._collectors[key] = collector

    def get(self, platform):
        """
        取采集器

        Args:
            platform (str): 平台标识

        Returns:
            InsightCollector

        Raises:
            InsightCollectError: 平台无对应采集器
        """
        key = str(platform or "").strip().lower()
        collector = self._collectors.get(key)
        if collector is None:
            raise InsightCollectError(
                "平台 %r 没有注册采集器（已注册: %s）"
                % (platform, ", ".join(sorted(self._collectors)))
            )
        return collector

    def platforms(self):
        """
        已注册的平台标识

        Returns:
            list[str]
        """
        return sorted(self._collectors)

    def describe(self):
        """
        全部采集器的自述（页面「数据源」面板 / 同步日志用）

        Returns:
            list[dict]
        """
        return [
            self._collectors[key].describe() for key in self.platforms()
        ]

    def set_credential(self, platform, credential):
        """
        给某平台注入登录态

        Args:
            platform (str): 平台标识
            credential (str): cookie / token 文本

        Raises:
            InsightCollectError: 平台未注册
        """
        collector = self.get(platform)
        collector._credential = credential


def build_default_registry(session=None, credentials=None,
                           sleep_fn=None, manual_path=None,
                           monotonic_fn=None):
    """
    构造默认注册表（全部平台）

    Args:
        session (requests.Session, optional): 共享会话，便于测试
        credentials (dict, optional): 平台 -> 登录态文本
        sleep_fn (callable, optional): 限速休眠函数（测试注入假函数）
        manual_path (str, optional): 人工录入文件路径
        monotonic_fn (callable, optional): 时钟函数（测试注入假函数）

    Returns:
        InsightCollectorRegistry
    """
    kwargs = {"session": session}
    if sleep_fn is not None:
        kwargs["sleep_fn"] = sleep_fn
    if monotonic_fn is not None:
        kwargs["monotonic_fn"] = monotonic_fn

    collectors = {}
    for platform, cls in _COLLECTOR_CLASSES.items():
        if cls is ManualInsightCollector:
            collectors[platform] = cls(path=manual_path, **kwargs)
        else:
            collectors[platform] = cls(**kwargs)
    for platform, credential in (credentials or {}).items():
        key = str(platform).strip().lower()
        if key in collectors and credential:
            collectors[key]._credential = credential
    return InsightCollectorRegistry(collectors)


def fetch_account_quotes(collector, account, limit=20, start_time=None,
                         timeout=15):
    """
    按账号抓取言论（含统一分页）

    Args:
        collector (InsightCollector): 采集器实例
        account (dict): investor_accounts 的一行
        limit (int): 最多取多少条
        start_time (str, optional): 增量起点（published_at 晚于该值）
        timeout (int): 单请求超时秒数

    Returns:
        AccountFetchOutcome: 含原始记录与失败原因
    """
    account_id = account.get("account_id")
    platform = account.get("platform")
    request = CollectRequest(
        account=account, limit=limit, start_time=start_time,
        timeout=timeout,
    )

    # 人工录入不分页（文件本身是全量），也不需要任何网络动作
    if getattr(collector, "PLATFORM", "") == "manual":
        try:
            records = collector.fetch(request)
            return AccountFetchOutcome(
                account_id, platform, records, pages=1,
            )
        except InsightCollectError as error:
            return AccountFetchOutcome(
                account_id, platform, [], str(error), _kind_of(error), pages=0,
            )

    if limit <= collector.PAGE_SIZE:
        try:
            records = collector._fetch_page(request, _uid_of(account), page=1)
            return AccountFetchOutcome(
                account_id, platform, records, pages=1,
                warnings=_watermark_warning(records, start_time),
            )
        except InsightCollectError as error:
            return AccountFetchOutcome(
                account_id, platform, [], str(error), _kind_of(error),
            )

    # 多页：页码统一从 1 开始（见模块头部说明）
    records, pages = [], 0
    for page in range(1, MAX_PAGES + 1):
        try:
            chunk = collector._fetch_page(request, _uid_of(account), page=page)
        except InsightCollectError as error:
            if page == 1:
                # 首页就失败：本次采集整体失败，绝不返回空列表
                return AccountFetchOutcome(
                    account_id, platform, [], str(error), _kind_of(error),
                )
            # 中途失败：返回已抓到的部分，但显式带上告警 —— 这不是「取完了」
            return AccountFetchOutcome(
                account_id, platform, records,
                error="第 %d 页失败: %s" % (page, error),
                error_kind=_kind_of(error), pages=pages,
                warnings=["分页未取完，本次入库的是前 %d 页的部分结果" % pages],
            )
        pages += 1
        records.extend(chunk)
        if len(chunk) < collector.PAGE_SIZE or len(records) >= limit:
            break

    return AccountFetchOutcome(
        account_id, platform, records[:limit], pages=pages,
        warnings=_watermark_warning(records, start_time),
    )


def normalize_outcome(outcome, account, captured_at=None,
                      existing_hashes=None):
    """
    把采集结果归一化成契约帧，并按内容哈希去重

    Args:
        outcome (AccountFetchOutcome): 采集结果
        account (dict): 账号行（提供 investor_code / account_name 兜底）
        captured_at (pd.Timestamp, optional): 抓取时间
        existing_hashes (set, optional): 库里已有的 content_hash

    Returns:
        pd.DataFrame: INVESTOR_QUOTE_COLUMNS 契约帧（已去重）
    """
    frame = normalize_investor_quotes(
        outcome.records,
        investor_code=account.get("investor_code"),
        platform=account.get("platform"),
        captured_at=captured_at or pd.Timestamp.now("UTC"),
    )
    if frame.empty or not existing_hashes:
        return frame
    mask = ~frame["content_hash"].isin(existing_hashes)
    dropped = int((~mask).sum())
    if dropped:
        _logger.info("%s: 内容哈希去重丢弃 %d 条", outcome.account_id, dropped)
    return frame.loc[mask].reset_index(drop=True)


# ----------------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------------
def _uid_of(account):
    """取账号的平台用户 ID"""
    return str(account.get("account_uid") or "").strip()


def _kind_of(error):
    """把异常映射成失败类别（决定是否值得重试）"""
    if isinstance(error, InsightCredentialError):
        return "credential"
    if isinstance(error, InsightBlockedError):
        return "blocked"
    name = type(error).__name__
    if "Parse" in name:
        return "parse"
    return "network"


def _watermark_warning(records, start_time):
    """
    检查增量水位是否真的生效

    【这个告警存在的理由】
       平台给不出发布时间（published_at 为 NULL）时，水位过滤对这条记录无效，
       于是每次同步都会把它重新抓一遍。内容哈希去重能挡住入库，
       但同步日志里「本次新增 0 条」会让人以为是不是坏了。
       所以显式告警：哪些记录没有时间、导致增量失效。
    """
    if not start_time or not records:
        return []
    missing = sum(1 for item in records if not item.get("published_at"))
    if not missing:
        return []
    return [
        "%d 条记录缺少 published_at，无法按增量水位过滤（已由内容哈希去重兜底）"
        % missing
    ]
