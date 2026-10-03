#!/usr/bin/env python3
"""
==============================================================================
StockLab - 巨潮资讯公告 Gateway (stocklab.datasource.cninfo_client)
==============================================================================

【模块职责】
  独立的「公司披露 Source」网关，只负责巨潮资讯（CNINFO）公告索引的
  拉取、分页、orgId 解析、字段归一化与去重。**不继承 StockDataSource**
  （行情 Source 与披露 Source 领域不同），也不参与任何行情降级链路。

【接口（2026-10-03 实测通过）】
  POST http://www.cninfo.com.cn/new/hisAnnouncement/query
    - pageSize 服务端硬限 30（传更大也只返回 30）；
    - stock 必须是 "<code>,<orgId>" 形式，单传代码返回 0 条或全市场；
    - orgId 不可自拼（自拼会产生假阴性）：走
      /new/information/topSearch/query 精确匹配，或一次性拉取
      /new/data/szse_stock.json（6259 条）做模块级映射缓存；
    - column 取值：沪市 sse、其余（含北交所）szse —— 实测 bse 列查北交所返回 0；
    - 返回 announcementTime 为**毫秒时间戳，按北京时间(UTC+8)换算日期**；
    - announcementTypeName 常为 null -> category 允许 NULL；
    - adjunctUrl 为相对路径，完整 PDF = http://static.cninfo.com.cn/ + adjunctUrl；
    - 不带 stock 参数即为全市场查询（适合按日期区间做增量同步）。

【去重口径（需求 §24）】
  优先 announcement_id；同一公告可能有不同 announcementId，
  故同步侧再按 (ts_code, announcement_date, title) 做二次去重。

【合规】
  只访问公开披露接口，串行 + 固定 sleep，禁止高并发抓取。
==============================================================================
"""

import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from stocklab.common.http_client import BROWSER_USER_AGENT
from stocklab.common.type_conversion import safe_int
from stocklab.normalization.base import normalize_ts_code

_logger = logging.getLogger(__name__)

__all__ = [
    "CninfoClient",
]

# 北京时间（CNINFO 的 announcementTime 按 UTC+8 解读）
_UTC8 = timezone(timedelta(hours=8))

# 标题中的 <em> 高亮标签
_TITLE_TAG_PATTERN = re.compile(r"</?em>")


def _clean_title(raw_title):
    """去除公告标题中的 <em> 高亮标签并做 HTML 实体反转义"""
    if raw_title is None:
        return ""
    text = _TITLE_TAG_PATTERN.sub("", str(raw_title))
    return html.unescape(text).strip()


class CninfoClient:
    """
    巨潮资讯公告索引客户端（独立 Gateway，与行情链路完全隔离）

    【字段产出（announcement DataFrame，与 corporate.announcements 契约同序）】
      announcement_id / ts_code / announcement_date / publish_time / title /
      category / pdf_url / source / source_url
      （crawl_time 与 content_hash 由同步阶段补齐）
    """

    _QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    _TOP_SEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
    _STOCK_MAP_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
    _PDF_BASE = "http://static.cninfo.com.cn/"
    _PAGE_SIZE = 30

    # 模块级缓存：全市场 orgId 映射只需拉取一次（进程内常驻，不落库）
    _stock_map_cache = None
    _org_id_cache = {}

    def __init__(self, http_timeout=15, transport=None, sleep_seconds=0.8):
        """
        Args:
            http_timeout (int): 单次 HTTP 超时秒数
            transport: 传输层桩（需提供 get/post），默认 requests —— 供离线测试注入
            sleep_seconds (float): 翻页之间的固定等待秒数（限流，禁止压测）
        """
        self._http_timeout = http_timeout
        self._transport = transport if transport is not None else requests
        self._sleep_seconds = sleep_seconds

    # ------------------------------------------------------------------
    # 网络层
    # ------------------------------------------------------------------
    def _headers(self, with_referer=True):
        headers = {"User-Agent": BROWSER_USER_AGENT}
        if with_referer:
            headers["Referer"] = "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"
        return headers

    def _post_text(self, url, data):
        """POST form 表单；任何失败统一返回空串"""
        try:
            response = self._transport.post(
                url, headers=self._headers(), data=data, timeout=self._http_timeout
            )
            if response.status_code != 200:
                _logger.warning("CNINFO 请求失败 %s: HTTP %s", url, response.status_code)
                return ""
            return response.text or ""
        except requests.RequestException as error:
            _logger.warning("CNINFO 网络异常 %s: %s", url, error)
            return ""

    def _get_text(self, url):
        """GET；任何失败统一返回空串"""
        try:
            response = self._transport.get(
                url, headers=self._headers(with_referer=False), timeout=self._http_timeout
            )
            if response.status_code != 200:
                _logger.warning("CNINFO 请求失败 %s: HTTP %s", url, response.status_code)
                return ""
            return response.text or ""
        except requests.RequestException as error:
            _logger.warning("CNINFO 网络异常 %s: %s", url, error)
            return ""

    def _get_json(self, url):
        """GET 并解析 JSON；失败返回 None"""
        text = self._get_text(url)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            _logger.warning("CNINFO JSON 解析失败: %s", url)
            return None

    # ------------------------------------------------------------------
    # orgId 解析
    # ------------------------------------------------------------------
    def _load_stock_map(self):
        """
        拉取全市场 code -> orgId 映射（模块级缓存，只请求一次）

        Returns:
            dict: {"600519": "gssh0600519", ...}；失败返回空 dict
        """
        if CninfoClient._stock_map_cache is not None:
            return CninfoClient._stock_map_cache

        payload = self._get_json(self._STOCK_MAP_URL)
        rows = payload.get("stockList", []) if isinstance(payload, dict) else []
        mapping = {}
        for row in rows:
            code = str(row.get("code", "")).strip()
            org_id = str(row.get("orgId", "")).strip()
            if code and org_id:
                mapping[code] = org_id
        if mapping:
            CninfoClient._stock_map_cache = mapping
            _logger.info("CNINFO 全市场 orgId 映射加载完成: %d 条", len(mapping))
        return mapping

    def resolve_org_id(self, stock_code):
        """
        解析股票对应的 cninfo orgId（禁止自拼，必须来自接口）

        顺序：全市场映射缓存 -> topSearch 精确匹配 -> 返回空串

        Args:
            stock_code (str): 纯数字代码或 ts_code，如 "600519" / "600519.SH"

        Returns:
            str: orgId（如 "gssh0600519"）；解析失败返回空串
        """
        code = str(stock_code).split(".")[0].strip()
        if not code:
            return ""
        if code in CninfoClient._org_id_cache:
            return CninfoClient._org_id_cache[code]

        org_id = ""
        mapping = self._load_stock_map()
        if code in mapping:
            org_id = mapping[code]

        if not org_id:
            text = self._post_text(self._TOP_SEARCH_URL,
                                   {"keyWord": code, "maxNum": "10"})
            try:
                rows = json.loads(text) if text else []
            except json.JSONDecodeError:
                rows = []
            # topSearch 是模糊匹配（920000 会命中 832000），必须校验代码完全一致
            for row in rows if isinstance(rows, list) else []:
                if str(row.get("code", "")).strip() == code:
                    org_id = str(row.get("orgId", "")).strip()
                    break

        if not org_id:
            _logger.warning("CNINFO 未解析到 %s 的 orgId，该股公告查询将跳过", code)
        CninfoClient._org_id_cache[code] = org_id
        return org_id

    def clear_cache(self):
        """清空 orgId / 全市场映射缓存（测试隔离与强制刷新用）"""
        CninfoClient._stock_map_cache = None
        CninfoClient._org_id_cache = {}

    # ------------------------------------------------------------------
    # 公告查询
    # ------------------------------------------------------------------
    def _build_query_data(self, symbol, start_date, end_date, page):
        """构造 hisAnnouncement/query 的表单参数"""
        code = ""
        stock_param = ""
        if symbol:
            code = str(symbol).split(".")[0].strip()
            org_id = self.resolve_org_id(code)
            stock_param = "{},{}".format(code, org_id) if org_id else ""
            column = "sse" if code.startswith("6") else "szse"
        else:
            column = "szse"

        return {
            "pageNum": str(page),
            "pageSize": str(self._PAGE_SIZE),
            "column": column,
            "tabName": "fulltext",
            "plate": "",
            "stock": stock_param,
            "searchkey": "",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": "{}~{}".format(start_date, end_date),
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }

    def _query_page(self, symbol, start_date, end_date, page):
        """
        查询单页公告

        Returns:
            tuple: (原始公告 dict 列表, has_more 是否还有下一页)
        """
        data = self._build_query_data(symbol, start_date, end_date, page)
        if symbol and not data["stock"]:
            # 单只查询但 orgId 解析失败：直接视为无数据，避免拉回全市场造成脏数据
            _logger.warning("CNINFO %s 缺少 orgId，跳过该股公告查询", symbol)
            return [], False

        text = self._post_text(self._QUERY_URL, data)
        if not text:
            return [], False
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            _logger.warning("CNINFO 公告响应解析失败: page=%s symbol=%s", page, symbol)
            return [], False

        rows = payload.get("announcements") or []
        has_more = bool(payload.get("hasMore"))
        # 双保险：本页不满 30 条时一定没有下一页
        if len(rows) < self._PAGE_SIZE:
            has_more = False
        return rows, has_more

    def get_announcements(self, symbol, start_date, end_date, page=1):
        """
        获取单页公告索引（需求 §21 指定接口）

        Args:
            symbol (str): 股票代码（"600519.SH" / "600519"）；None/空 = 全市场
            start_date (str): 起始日期 YYYY-MM-DD
            end_date (str): 结束日期 YYYY-MM-DD
            page (int): 页号（每页上限 30 条，服务端硬限）

        Returns:
            pd.DataFrame: 公告契约列；无数据返回空表
        """
        rows, _ = self._query_page(symbol, start_date, end_date, page)
        return self._build_frame(rows)

    def fetch_announcements(self, symbol, start_date, end_date, max_pages=200):
        """
        翻页抓取区间内的全部公告并去重（同步阶段入口）

        Args:
            symbol (str): 股票代码；None/空 = 全市场按日期区间抓取
            start_date (str): 起始日期 YYYY-MM-DD
            end_date (str): 结束日期 YYYY-MM-DD
            max_pages (int): 单次抓取的页数上限（防御性熔断）

        Returns:
            pd.DataFrame: 去重后的公告契约列；无数据返回空表
        """
        collected = []
        seen_ids = set()
        seen_keys = set()
        page = 1

        while page <= max_pages:
            rows, has_more = self._query_page(symbol, start_date, end_date, page)
            if not rows:
                break

            page_count = 0
            for item in rows:
                row = self._to_row(item)
                if row is None:
                    continue
                # 去重：announcement_id 优先 + (ts_code, 日期, 标题) 兜底
                if row["announcement_id"] in seen_ids:
                    continue
                dedup_key = (row["ts_code"], row["announcement_date"], row["title"])
                if dedup_key in seen_keys:
                    continue
                seen_ids.add(row["announcement_id"])
                seen_keys.add(dedup_key)
                collected.append(row)
                page_count += 1

            if not has_more:
                break
            page += 1
            time.sleep(self._sleep_seconds)

        if not collected:
            return pd.DataFrame()
        return pd.DataFrame(collected)

    # ------------------------------------------------------------------
    # 字段归一化
    # ------------------------------------------------------------------
    def _to_row(self, item):
        """
        单条原始公告 -> 契约行字典

        Returns:
            dict | None: 关键字段缺失返回 None
        """
        sec_code = str(item.get("secCode") or "").strip()
        title = _clean_title(item.get("announcementTitle"))
        timestamp_ms = safe_int(item.get("announcementTime"))

        if not sec_code or not title or timestamp_ms is None:
            return None

        adjunct_url = str(item.get("adjunctUrl") or "").strip()
        pdf_url = adjunct_url
        if adjunct_url and not adjunct_url.startswith("http"):
            pdf_url = self._PDF_BASE + adjunct_url.lstrip("/")

        announcement_id = str(
            item.get("announcementId") or item.get("id") or ""
        ).strip()
        if not announcement_id:
            return None

        return {
            "announcement_id": announcement_id,
            "ts_code": normalize_ts_code(sec_code),
            "announcement_date": datetime.fromtimestamp(
                timestamp_ms / 1000.0, tz=_UTC8
            ).date(),
            "publish_time": timestamp_ms,
            "title": title,
            "category": item.get("announcementTypeName") or None,
            "pdf_url": pdf_url,
            "source": "cninfo",
            "source_url": self._QUERY_URL,
        }

    def _build_frame(self, rows):
        """原始公告列表 -> 契约 DataFrame（列序与 corporate.announcements 一致）"""
        records = []
        seen_ids = set()
        for item in rows or []:
            row = self._to_row(item)
            if row is None or row["announcement_id"] in seen_ids:
                continue
            seen_ids.add(row["announcement_id"])
            records.append(row)
        if not records:
            return pd.DataFrame()
        return pd.DataFrame.from_records(
            records,
            columns=[
                "announcement_id",
                "ts_code",
                "announcement_date",
                "publish_time",
                "title",
                "category",
                "pdf_url",
                "source",
                "source_url",
            ],
        )
