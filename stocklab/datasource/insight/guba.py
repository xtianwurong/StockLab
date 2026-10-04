#!/usr/bin/env python3
"""
==============================================================================
StockLab - 东方财富股吧采集器 (stocklab.datasource.insight.guba)
==============================================================================

【模块职责】
   从东方财富股吧按用户 ID 抓取该用户的发帖，归一化成通用记录列表。

【为什么股吧默认权重低于雪球（这是产品判断，不是技术判断）】
   股吧的内容分布是：少量有价值讨论 + 大量荐股喊单、引流广告、庄托互相捧。
   抓到的「按股吧里的名人账号」尤其危险 —— 认证信息本身可被伪造，
   且荐股内容有明显的合规红线。所以：
     1. 抓到的内容一律仍是 unverified，不会因为「在股吧发过」就升级可信度；
     2. 页面按平台分组展示时，股吧来源会被明确标注，不与雪球并列呈现。

【接口形态（东财这套接口变动频繁，改了要改这里）】
   GET https://gbapi.eastmoney.com/webarticlelist/api/Article/Articlelist
       ?code=<uid>&ps=<n>&p=<页码，从1开始>&type=0&order=1
   返回 {"re":0, "data":{"list":[{"post_id","post_title","post_content",
                                  "user_nickname","post_publish_time"}]}}

   post_publish_time 是「yy-mm-dd HH:MM:SS」字符串，**不是时间戳**，
   而且这个格式在东财的另一套接口里会变成毫秒数。两种都按字符串特征判别。

【兜底解析】
   list 有时挂在 data.list，有时直接在根上。两种都试；都不在就抛解析异常
   而不是当空处理 —— 「接口改版」和「这个号没发帖」必须能区分开。
"""

import logging

import pandas as pd

from stocklab.datasource.insight.base import (
    InsightCollector,
    InsightParseError,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "GubaCollector",
]

# 文章列表接口
ARTICLE_API = "https://gbapi.eastmoney.com/webarticlelist/api/Article/Articlelist"
# 用户主页（source_url 兜底）
PROFILE_URL = "https://guba.eastmoney.com/"


class GubaCollector(InsightCollector):
    """东方财富股吧用户发帖采集器"""

    PLATFORM = "guba"
    DISPLAY_NAME = "东方财富股吧"
    REQUIRES_CREDENTIAL = False
    # 5 秒/次：东财对高频请求会直接 403，且账号本身不重要到值得冒险
    MIN_INTERVAL_SECONDS = 5.0
    ROBOTS_URL = "https://guba.eastmoney.com/robots.txt"
    # 文章列表接口（必须是类属性：_fetch_page 用 self.ARTICLE_API 引用，
    # 写成模块常量会让每一次真实抓取都 AttributeError）
    ARTICLE_API = ARTICLE_API
    # 用户主页（source_url 兜底）
    PROFILE_URL = PROFILE_URL

    PAGE_SIZE = 30

    def fetch(self, request):
        """
        抓取股吧用户的发帖

        Args:
            request (CollectRequest): 采集参数，account_uid 为股吧用户号

        Returns:
            list[dict]: 通用记录列表

        Raises:
            InsightParseError: 缺 uid 或响应结构不认识
        """
        uid = str(request.account_uid or "").strip()
        if not uid:
            raise InsightParseError(
                "股吧账号 %s 缺少 account_uid（股吧用户号）"
                % (request.account_name or "?")
            )
        self.assert_robots_allows(ARTICLE_API)
        return self._fetch_page(request, uid, page=1)

    def _fetch_page(self, request, uid, page):
        """抓单页并解析"""
        params = {
            "code": uid,
            "ps": self.PAGE_SIZE,
            "p": page,
            "type": 0,
            "order": 1,
        }
        url = self.ARTICLE_API
        self.throttle()
        self.mark_request_sent()
        response = self.get(url, request, params=params)
        try:
            payload = response.json()
        except ValueError as error:
            preview = (response.text or "")[:160].replace("\n", " ")
            raise InsightParseError(
                "股吧返回的不是 JSON（多半是反爬挑战页）: %s | 片段: %s"
                % (url, preview)
            ) from error
        return self._parse_records(payload, request, uid)

    def _parse_records(self, payload, request, uid):
        """
        解析股吧响应

        Args:
            payload (dict): 接口返回的 JSON
            request (CollectRequest): 采集参数
            uid (str): 股吧用户号

        Returns:
            list[dict]: 通用记录列表

        Raises:
            InsightParseError: 找不到文章列表（接口改版）
        """
        if not isinstance(payload, dict):
            raise InsightParseError(
                "股吧响应顶层不是对象（类型 %s）" % type(payload).__name__
            )
        items = None
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            items = data["list"]
        elif isinstance(payload.get("list"), list):
            # 兜底：list 直接挂在根上
            items = payload["list"]
        if items is None:
            raise InsightParseError(
                "股吧响应找不到文章列表，实际字段: %s"
                % ", ".join(sorted(payload.keys())[:8])
            )

        records = []
        for item in items:
            if not isinstance(item, dict):
                continue
            record = self._parse_record(item, request, uid)
            if record:
                records.append(record)
        return records

    def _parse_record(self, item, request, uid):
        """单条股吧发帖 -> 通用记录；无有效正文返回 None"""
        post_id = item.get("post_id") or item.get("id")
        if not post_id:
            _logger.warning("股吧记录缺少 post_id，跳过: %s", str(item)[:60])
            return None
        # 标题 + 正文合并：股吧的观点常在标题里，只取正文会丢掉一半信息
        title = item.get("post_title") or ""
        body = item.get("post_content") or ""
        content = ("%s\n%s" % (title, body)).strip() if title else body.strip()
        if not content:
            return None

        return {
            "platform_id": str(post_id),
            "platform": self.PLATFORM,
            "investor_code": request.investor_code,
            "account_name": item.get("user_nickname") or request.account_name or None,
            "source_url": "https://guba.eastmoney.com/news,%s,%s.html"
                          % (uid, post_id),
            "published_at": _parse_post_time(item.get("post_publish_time")),
            "content": content,
            "language": "zh",
            "raw_meta": {
                "post_id": post_id,
                "post_type": item.get("post_type"),
                "post_source_id": item.get("post_source_id"),
                "title": title or None,
            },
        }


def _parse_post_time(value):
    """
    解析股吧的发布时间

    【必须两种格式都认】
       post_publish_time 在东财的不同接口/不同时间点分别是
       "2025-09-16 13:20:31" 与 1758000000000（毫秒）。
       判别方式：长度 > 12 且含 '-' -> 字符串；否则当时间戳。

    Args:
        value (str | int | None): 原始值

    Returns:
        pd.Timestamp | None: 无法解析时返回 None
    """
    if value in (None, "", 0):
        return None
    if isinstance(value, str) and "-" in value and len(value) > 12:
        try:
            return pd.to_datetime(value, errors="raise")
        except (ValueError, TypeError):
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    seconds = number / 1000.0 if number > 1e11 else number
    try:
        parsed = pd.to_datetime(seconds, unit="s", utc=True)
    except (ValueError, OverflowError, TypeError, OSError):
        return None
    return None if pd.isna(parsed) else parsed
