"""
==============================================================================
StockLab - 投资人观点采集层 (stocklab.datasource.insight)
==============================================================================

【模块职责】
   从各平台采集投资人的公开言论与投资理念，产出通用记录（dict），
   交由 stocklab.normalization.insight 转成领域契约帧。

【分层】
   base.py        采集器抽象基类 + 采集礼仪（限速 / 凭证 / 失败分类 / robots）
   xueqiu.py      雪球：按用户 ID 抓公开内容（需登录态）
   guba.py        东方财富股吧：按用户号抓发帖
   manual.py      人工录入：读本地 JSON，唯一允许写非抓取内容的通道
   collectors.py  注册表 + 统一分页编排 + 归一化与去重

【与行情采集的三个根本差别，理解这三点就不会把这里的代码写错】
   1. **没有降级链**。行情是「谁先给谁说了算」；观点是「每个平台的账号都要，
      缺一个是少一个人」，所以按平台取对应采集器，找不到就报配置错误。
   2. **凭证缺失必须报错，不能降级**。返回空列表会让调用方分不清
      「这个号今天没发东西」和「cookie 没配好」。
   3. **不得产出未经抓取的内容**。唯一例外是 manual 通道，且它必须由人给全
      来源。理由见 base.py 模块头的红线说明。

【对外只暴露】
   build_default_registry / InsightCollectorRegistry /
   fetch_account_quotes / normalize_outcome / AccountFetchOutcome
"""

from stocklab.datasource.insight.base import (
    CollectRequest,
    InsightBlockedError,
    InsightCollectError,
    InsightCollector,
    InsightCredentialError,
    InsightParseError,
    make_robots_checker,
)
from stocklab.datasource.insight.collectors import (
    AccountFetchOutcome,
    InsightCollectorRegistry,
    build_default_registry,
    fetch_account_quotes,
    normalize_outcome,
)
from stocklab.datasource.insight.guba import GubaCollector
from stocklab.datasource.insight.manual import ManualInsightCollector
from stocklab.datasource.insight.xueqiu import XueqiuCollector

__all__ = [
    "CollectRequest",
    "InsightCollector",
    "InsightCollectError",
    "InsightCredentialError",
    "InsightParseError",
    "InsightBlockedError",
    "make_robots_checker",
    "XueqiuCollector",
    "GubaCollector",
    "ManualInsightCollector",
    "InsightCollectorRegistry",
    "build_default_registry",
    "AccountFetchOutcome",
    "fetch_account_quotes",
    "normalize_outcome",
]
