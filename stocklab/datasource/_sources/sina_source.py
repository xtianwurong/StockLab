#!/usr/bin/env python3
"""
==============================================================================
StockLab - 新浪财经数据源通道 (stocklab.datasource._sources.sina_source)
==============================================================================

【模块职责】
  新浪财经直连 HTTP 通道（作为行情链路第 4 级备用源）：
    - 实时行情快照（hq.sinajs.cn）
    - 公司简称（同上，单次请求同时拿到名称与价格）
    - 日线重采样月线收盘价（quotes.sina.cn getKLineData + hfq.js 复权因子）

【硬性约束（2026-10-03 实测）】
  1. hq.sinajs.cn 必须 HTTPS + `Referer: https://finance.sina.com.cn`，
     缺失 Referer 返回 403 Forbidden（实测）；
  2. 响应为 GBK 编码，必须显式按 gbk 解码；
  3. 日线 K 线可用端点是 quotes.sina.cn 的 getKLineData(scale=240)，
     money.finance.sina.com.cn 同名接口已返回 `Service not valid`（不可用）；
  4. 单次最多约 1023 根日线（≈4 年），复权因子来自
     finance.sina.com.cn/realstock/company/{symbol}/hfq.js（事件制累计因子）；
  5. 新浪历史接口「大量抓取容易封 IP」：本通道串行调用，禁止并发。

【复权口径（已用 600519 实测验证）】
   hfq 价格 = 不复权价 × f(d)
   qfq 价格 = 不复权价 × f(d) / f(latest)
   其中 f(d) 为 hfq.js 中「日期 <= d 的最近一次除权事件」的累计因子，
   f(latest) 为最新事件因子。验证：由 hfq.js 推出的 qfq 因子与新浪 qfq.js
   所列因子 33/33 完全一致，且除权日价格序列连续（无跳空）。
==============================================================================
"""

import json
import logging
import time

import pandas as pd
import requests

from stocklab.common.http_client import BROWSER_USER_AGENT
from stocklab.common.type_conversion import safe_float
from stocklab.datasource._sources.base import StockDataSource
from stocklab.datasource.data_contract import (
    CLOSE_PRICE_COLUMN,
    TRADE_DATE_COLUMN,
    StockRealtimeQuote,
)
from stocklab.normalization.base import normalize_ts_code

_logger = logging.getLogger(__name__)

__all__ = [
    "SinaDataSource",
]

# 新浪强制要求的反防盗链 Referer（缺失即 403）
_SINA_REFERER = "https://finance.sina.com.cn"

# 实时行情端点（GBK 编码）
_QUOTE_URL = "https://hq.sinajs.cn/list={symbol}"

# 日线 K 线端点（JSONP 包裹的 JSON 数组；scale=240 表示日线）
_KLINE_URL = (
    "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_{symbol}="
    "/CN_MarketDataService.getKLineData"
    "?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
)

# 后复权因子文件（`var {symbol}hfq={...}` 形式的 JS 变量）
_HFQ_FACTOR_URL = (
    "https://finance.sina.com.cn/realstock/company/{symbol}/hfq.js"
)

# 单次日线请求的最大根数（实测 1023 ≈ 4 年）
_MAX_DAILY_BARS = 1023


def _to_sina_symbol(stock_code):
    """
    标准代码 -> 新浪带市场前缀小写代码

    Args:
        stock_code (str): "600519.SH" / "600519" / "sh600519"

    Returns:
        str: "sh600519"；无法识别返回空串
    """
    raw = str(stock_code).strip()
    if not raw:
        return ""

    if raw[:2].lower() in ("sh", "sz", "bj"):
        return raw.lower()

    if "." in raw:
        code, suffix = raw.split(".")[0], raw.split(".")[1].upper()
    else:
        code = raw
        suffix = normalize_ts_code(raw).split(".")[1]

    # 严格校验：新浪只接受 6 位数字代码（避免把任意字符串拼进 URL）
    if not code.isdigit() or len(code) != 6:
        return ""

    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix, "")
    if not prefix:
        return ""
    return prefix + code


def _parse_jsonp_array(text):
    """
    从 JSONP 响应中截取 JSON 数组并解析

    Args:
        text (str): 形如 `/*...*/\\nvar _sh600519=([{...}])` 的响应体

    Returns:
        list: 解析结果；失败返回空列表
    """
    try:
        start = text.index("[")
        end = text.rindex("]")
        payload = json.loads(text[start:end + 1])
        return payload if isinstance(payload, list) else []
    except (ValueError, json.JSONDecodeError):
        return []


class SinaDataSource(StockDataSource):
    """
    数据源实现：新浪财经（直连 HTTP）

    【定位】行情链路第 4 级备用源（AkShare -> BaoStock -> Tencent -> **Sina**）
      与东财、腾讯、证券宝互为独立上游，可承担故障切换与价格交叉校验。
      不承担 PE-TTM 通道（见 quote_service，避免为“多源”硬实现估值）。
    """

    SOURCE_NAME = "sina(新浪财经)"

    def __init__(self, http_timeout=8):
        self._http_timeout = http_timeout
        self._headers = {
            "Referer": _SINA_REFERER,
            "User-Agent": BROWSER_USER_AGENT,
        }

    # ------------------------------------------------------------------
    # 网络层
    # ------------------------------------------------------------------
    def _get_text(self, url):
        """GET 并按 GBK 解码；任何失败统一返回空串（视为一次 Source Failure）"""
        try:
            response = requests.get(url, headers=self._headers, timeout=self._http_timeout)
            if response.status_code != 200:
                _logger.warning("%s 请求失败 %s: HTTP %s", self.SOURCE_NAME, url, response.status_code)
                return ""
            if not response.content:
                _logger.warning("%s 返回空响应: %s", self.SOURCE_NAME, url)
                return ""
            response.encoding = "gbk"
            return response.text
        except requests.RequestException as error:
            _logger.warning("%s 网络异常 %s: %s", self.SOURCE_NAME, url, error)
            return ""

    def _fetch_quote_fields(self, stock_code):
        """解析 hq.sinajs.cn 返回的逗号字段列表；失败返回空列表"""
        symbol = _to_sina_symbol(stock_code)
        if not symbol:
            _logger.warning("%s 无法识别股票代码: %s", self.SOURCE_NAME, stock_code)
            return []

        text = self._get_text(_QUOTE_URL.format(symbol=symbol))
        if '="' not in text:
            return []
        payload = text.split('="', 1)[1].rstrip('";\n')
        fields = payload.split(",")
        if len(fields) < 10 or not fields[0].strip():
            return []
        return fields

    # ------------------------------------------------------------------
    # StockDataSource 接口
    # ------------------------------------------------------------------
    def fetch_realtime_quote(self, stock_code):
        """
        获取单只股票的实时行情快照（链路第 2 级：Tencent 之后）

        【字段口径（2026-10 实测 34 字段布局）】
          0 名称, 1 今开, 2 昨收, 3 现价, 4 最高, 5 最低,
          6 买一价, 7 卖一价, 8 成交量(股), 9 成交额(元),
          10-19 买一~买五(量/价), 20-29 卖一~卖五(量/价),
          30 日期, 31 时间。
          该端点不返回换手率 / PE / PB / 市值，故对应字段保持默认值
          （换手率 0、PE/PB 为 None、市值 0），由上层决定是否可用；
          涨跌额与涨跌幅按 昨收 自行计算。

        Returns:
            StockRealtimeQuote | None: 失败返回 None
        """
        fields = self._fetch_quote_fields(stock_code)
        if not fields:
            return None

        price = safe_float(fields[3], 0.0)
        prev_close = safe_float(fields[2], 0.0)
        if price <= 0 or prev_close <= 0:
            _logger.warning("%s %s 价格字段非法: price=%s prev=%s",
                            self.SOURCE_NAME, stock_code, fields[3], fields[2])
            return None

        quote = StockRealtimeQuote(stock_code)
        quote.stock_name = fields[0].strip()
        quote.source_name = self.SOURCE_NAME
        quote.current_price = price
        quote.yesterday_close = prev_close
        quote.today_open = safe_float(fields[1], 0.0)
        quote.highest_price = safe_float(fields[4], 0.0)
        quote.lowest_price = safe_float(fields[5], 0.0)
        quote.change_amount = round(price - prev_close, 4)
        quote.change_percent = round((price - prev_close) / prev_close * 100.0, 4)
        quote.volume_shares = int(safe_float(fields[8], 0.0))
        quote.amount_yuan = safe_float(fields[9], 0.0)

        # 时间：30 日期 + 31 时间
        if len(fields) > 31:
            quote.quote_time = "{} {}".format(fields[30].strip(), fields[31].strip()).strip()

        return quote

    def fetch_stock_name(self, stock_code):
        """查询股票简称（直接复用实时行情响应，避免多发一次请求）"""
        fields = self._fetch_quote_fields(stock_code)
        if not fields:
            return ""
        return fields[0].strip()

    def fetch_monthly_close_prices(
        self, stock_code, adjust_type, retry_count=1, retry_interval_seconds=0
    ):
        """
        获取月线收盘价（链路第 4 级备用源）

        实现路径：日线（quotes.sina.cn，最多 1023 根 ≈ 4 年）
                   -> 复权因子（hfq.js）
                   -> 基类 _resample_daily_to_monthly() 月末重采样

        Args:
            stock_code (str): 标准股票代码，如 "600519.SH"
            adjust_type (str): "qfq" / "hfq" / ""（不复权）
            retry_count (int): 失败重试次数
            retry_interval_seconds (int): 重试间隔秒数

        Returns:
            pd.DataFrame: trade_date + close_price 两列；失败返回空表
        """
        attempts = max(int(retry_count or 1), 1)
        for attempt in range(attempts):
            frame = self._fetch_monthly_once(stock_code, adjust_type)
            if not frame.empty:
                return frame
            if attempt < attempts - 1:
                time.sleep(max(float(retry_interval_seconds or 0), 0))
        return pd.DataFrame()

    def _fetch_monthly_once(self, stock_code, adjust_type):
        """单次月线抓取（网络 + 复权 + 重采样）"""
        symbol = _to_sina_symbol(stock_code)
        if not symbol:
            return pd.DataFrame()

        text = self._get_text(_KLINE_URL.format(symbol=symbol, datalen=_MAX_DAILY_BARS))
        rows = _parse_jsonp_array(text) if text else []
        if not rows:
            return pd.DataFrame()

        days, closes = [], []
        for row in rows:
            day = str(row.get("day", "")).strip()
            close = safe_float(row.get("close"))
            if day and close is not None:
                days.append(day)
                closes.append(close)
        if not days:
            return pd.DataFrame()

        if adjust_type:
            factors = self._fetch_adjust_factors(symbol, adjust_type)
            if not factors:
                _logger.warning("%s %s 复权因子获取失败，无法提供 %s 月线",
                                self.SOURCE_NAME, stock_code, adjust_type)
                return pd.DataFrame()
            closes = self._apply_factors(days, closes, factors)

        frame = pd.DataFrame({TRADE_DATE_COLUMN: pd.to_datetime(days),
                              CLOSE_PRICE_COLUMN: closes})
        return self._resample_daily_to_monthly(frame, CLOSE_PRICE_COLUMN)

    def _fetch_adjust_factors(self, symbol, adjust_type):
        """
        读取 hfq.js 累计因子，返回 {日期: 应用因子}

        hfq: 直接使用累计因子 f(d)
        qfq: 使用 f(d) / f(latest)
        """
        if adjust_type not in ("qfq", "hfq"):
            _logger.warning("%s 不支持的复权类型: %s", self.SOURCE_NAME, adjust_type)
            return {}

        text = self._get_text(_HFQ_FACTOR_URL.format(symbol=symbol))
        if not text or "=" not in text:
            return {}
        try:
            payload = json.loads(text.split("=", 1)[1].split("\n")[0].strip().rstrip(";"))
            events = payload.get("data") or []
        except (ValueError, json.JSONDecodeError):
            _logger.warning("%s 复权因子解析失败: %s", self.SOURCE_NAME, symbol)
            return {}

        if not events:
            return {}

        # 按事件日期升序（新浪返回按倒序），保留累计因子
        pairs = []
        for event in events:
            day = str(event.get("d", "")).strip()
            factor = safe_float(event.get("f"))
            if day and factor:
                pairs.append((day, factor))
        if not pairs:
            return {}
        pairs.sort(key=lambda item: item[0])

        if adjust_type == "qfq":
            latest = pairs[-1][1]
            pairs = [(day, factor / latest) for day, factor in pairs]

        return {day: factor for day, factor in pairs}

    def _apply_factors(self, days, closes, factors):
        """
        按事件日对日线序列应用因子（新浪语义：因子自除权日起向前生效）

        Args:
            days (list): 日期字符串（可乱序）
            closes (list): 与 days 对应的不复权收盘价
            factors (dict): 事件日期 -> 因子

        Returns:
            list: 复权后价格（保留 4 位小数，顺序与 days 一致）
        """
        ordered = sorted(factors)

        # 逐个事件日扫描，得到「日期 -> 生效因子」的前向填充映射
        applied = {}
        current_factor = 1.0
        cursor = 0
        for day in sorted(set(days)):
            while cursor < len(ordered) and ordered[cursor] <= day:
                current_factor = factors[ordered[cursor]]
                cursor += 1
            applied[day] = current_factor

        return [round(price * applied[day], 4) for day, price in zip(days, closes)]
