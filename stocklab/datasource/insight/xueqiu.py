#!/usr/bin/env python3
"""
==============================================================================
StockLab - 雪球采集器 (stocklab.datasource.insight.xueqiu)
==============================================================================

【模块职责】
   从雪球按用户 ID 抓取该用户公开发布的内容，归一化成通用记录列表。

【为什么雪球是本域最重要的一手来源】
   国内投资家里，段永平（雪球 ID「大道无形我有型」）是少数**长期、公开、
   可溯源**表达完整投资方法论的人。他的原话可以直接回链到具体一条帖子，
   这是本库里 verification 有机会升到 verified 的唯一现实途径 ——
   其他人的「语录」大多只能停留在 unverified。

【接口形态（会变，改了要改这里）】
   GET https://xueqiu.com/query/v1/symbol/search/status.json
       ?user_id=<uid>&count=<n>&page=<p>&sort=time
   返回 {"list": [{"id":..., "text":..., "created_at":<毫秒>, "target":"/uid/xxx"}]}

   登录态要求：必须先访问一次 xueqiu.com 拿到 xq_a_token cookie，否则接口
   返回空 list 或登录页。所以 credential 传的是 cookie 串，不是账号密码 ——
   采集器不做登录（原因见 base.require_credentials）。

【降级策略】
   单页失败即抛异常，不返回半截数据：宁可这一轮同步整体失败，
   也不要让「只抓到第 1 页」看起来像「这个号今天只发了 1 条」。
   pagination 是否真的取完由 collectors.py 的 fetch_all 负责。

【限速】
   2.5 秒/次。雪球对高频请求的容忍度不高，而这个账号是本域的核心资产 ——
   被封号的代价远大于多等几秒。
"""

import logging

import pandas as pd

from stocklab.datasource.insight.base import (
    InsightCollector,
    InsightParseError,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "XueqiuCollector",
]

# 时间戳单位：雪球 created_at 是毫秒
_MILLISECOND = 1000.0


class XueqiuCollector(InsightCollector):
    """雪球用户发言采集器"""

    PLATFORM = "xueqiu"
    DISPLAY_NAME = "雪球"
    REQUIRES_CREDENTIAL = True
    MIN_INTERVAL_SECONDS = 2.5
    ROBOTS_URL = "https://xueqiu.com/robots.txt"

    STATUS_API = "https://xueqiu.com/query/v1/symbol/search/status.json"
    PAGE_SIZE = 20

    def fetch(self, request):
        """
        抓取雪球用户的公开发言

        Args:
            request (CollectRequest): 采集参数，account_uid 为雪球用户 ID

        Returns:
            list[dict]: 通用记录列表

        Raises:
            InsightCredentialError: 未配置 cookie
            InsightParseError:     响应结构不认识
        """
        self.require_credentials()
        uid = str(request.account_uid or "").strip()
        if not uid:
            # 账号表里没填 uid 是配置错误，不是「这个号没内容」
            raise InsightParseError(
                "雪球账号 %s 缺少 account_uid（雪球用户 ID）"
                % (request.account_name or "?")
            )

        return self._fetch_page(request, uid, page=1)

    def _fetch_page(self, request, uid, page):
        """抓单页并解析"""
        params = {
            "user_id": uid,
            "count": self.PAGE_SIZE,
            "page": page,
            "sort": "time",
            "source": "user",
            "comment": 0,
        }
        url = "%s?%s" % (self.STATUS_API, _encode_query(params))
        self.assert_robots_allows(self.STATUS_API)
        payload = self.get_json(url, request)
        return self._parse_records(payload, request, uid)

    def _parse_records(self, payload, request, uid):
        """
        解析雪球响应

        Args:
            payload (dict): 接口返回的 JSON
            request (CollectRequest): 采集参数
            uid (str): 雪球用户 ID，用于拼 source_url

        Returns:
            list[dict]: 通用记录列表

        Raises:
            InsightParseError: 顶层结构不认识（多半是接口改版）
        """
        if not isinstance(payload, dict):
            raise InsightParseError(
                "雪球响应顶层不是对象（类型 %s），接口可能已改版"
                % type(payload).__name__
            )
        if "list" not in payload:
            # 不静默当空处理：list 消失通常意味着换了接口或被要求验证
            raise InsightParseError(
                "雪球响应缺少 list 字段，实际字段: %s"
                % ", ".join(sorted(payload.keys())[:8])
            )

        records = []
        for item in payload.get("list") or []:
            if not isinstance(item, dict):
                continue
            record = self._parse_record(item, request, uid)
            if record:
                records.append(record)
        return records

    def _parse_record(self, item, request, uid):
        """单条雪球内容 -> 通用记录；无有效正文返回 None"""
        status_id = item.get("id")
        # text 是正文；description 在部分类型（如转发）里才有，两者互补
        text = item.get("text") or item.get("description") or ""
        if not text:
            return None
        # target 形如 "/1240335488/222222222"，缺 status_id 时用它兜底
        target = str(item.get("target") or "").strip()
        if not status_id and target:
            status_id = target.rstrip("/").split("/")[-1]
        if not status_id:
            # 没有平台 ID 就只能用内容哈希做主键，会让「改一个字算新一条」，
            # 所以宁可不收 —— 见 base 模块头关于「不静默丢条」的说明
            _logger.warning(
                "雪球记录缺少 id 与 target，跳过: text=%s", str(text)[:40]
            )
            return None

        return {
            "platform_id": str(status_id),
            "platform": self.PLATFORM,
            "investor_code": request.investor_code,
            "account_name": request.account_name or None,
            "source_url": "https://xueqiu.com%s" % target if target
            else "https://xueqiu.com/u/%s" % uid,
            "published_at": _to_datetime(item.get("created_at")),
            "content": text,
            "language": "zh",
            "raw_meta": {
                "status_id": status_id,
                "target": target or None,
                "reply_count": item.get("reply_count"),
                "retweet_count": item.get("retweet_count"),
                "like_count": item.get("like_count"),
            },
        }


def _to_datetime(milliseconds):
    """
    毫秒时间戳 -> pandas.Timestamp（UTC）

    【踩坑记录：不要用 datetime.utcfromtimestamp】
       它在 Python 3.12+ 已废弃，且返回值不带时区；而本项目其他模块统一
       用 pd.to_datetime（utc=True），混用两套时间语义会埋下「差 8 小时」的坑。

    【量级判断而非硬编码除数】
       雪球给的是毫秒，但同类接口里混着秒级（1.7e9）。用 >1e11 判别，
       比无条件除 1000 更能容忍平台切换单位。

    Args:
        milliseconds (int | float | None): 时间戳

    Returns:
        pd.Timestamp | None: 无效或缺失时返回 None（不抛异常）
    """
    if milliseconds in (None, "", 0):
        return None
    try:
        value = float(milliseconds)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    seconds = value / _MILLISECOND if value > 1e11 else value
    try:
        parsed = pd.to_datetime(seconds, unit="s", utc=True)
    except (ValueError, OverflowError, TypeError, OSError):
        return None
    return None if pd.isna(parsed) else parsed


def _encode_query(params):
    """
    手工拼查询串（不引 urllib 的原因是保持与 requests 行为一致且可预测）

    Args:
        params (dict): 查询参数

    Returns:
        str: 编码后的查询串
    """
    from urllib.parse import quote

    return "&".join(
        "%s=%s" % (quote(str(key), safe=""), quote(str(value), safe=""))
        for key, value in params.items()
    )
