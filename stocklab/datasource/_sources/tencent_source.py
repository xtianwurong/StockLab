#!/usr/bin/env python3
"""
==============================================================================
StockLab - 腾讯财经数据源通道 (stocklab.datasource._sources.tencent_source)
==============================================================================

【模块职责】
  腾讯财经直连 HTTP 通道实现（qt.gtimg.cn）：实时行情、简称与备用日线。
==============================================================================
"""

import logging

import pandas as pd

from stocklab.common.type_conversion import safe_float, safe_int
from stocklab.datasource._sources.base import StockDataSource
from stocklab.datasource.contract import (
    CLOSE_PRICE_COLUMN,
    TRADE_DATE_COLUMN,
    StockRealtimeQuote,
)
from stocklab.datasource.tencent_client import TencentMarketClient

_logger = logging.getLogger(__name__)

__all__ = [
    "TencentDataSource",
]


class TencentDataSource(StockDataSource):
    """
    数据源实现：腾讯财经（直连 HTTP 行情接口）

    【特性分析】
      - 优势：
          1. 实时行情与简称极速获取：单次 HTTP GET，延迟低于 100ms，无需登录，无东财反爬限流。
          2. 全面实时快照：提供当前价、昨收、今开、最高最低、涨跌幅、盘口五档、
             成交量/额、换手率、量比、动态 PE/PB、总市值/流通市值。
          3. 备用历史日线：提供前复权日 K 线（最多 800 根），在主备历史源故障时可降采样提供近 3 年月线保底。
    """

    SOURCE_NAME = "tencent(腾讯财经)"

    def __init__(self, client=None):
        self._client = client if client is not None else TencentMarketClient()

    def fetch_monthly_close_prices(
        self, stock_code, adjust_type, retry_count=1, retry_interval_seconds=0
    ):
        """
        从腾讯财经获取前复权日线行情，并降采样为月线收盘价（充当第 3 级备用源）

        Returns:
            pd.DataFrame: 标准格式月线数据；失败返回空表
        """
        bars = self._client.fetch_kline(
            symbol=stock_code, period="day", adjust="qfq", count=800
        )
        if not bars:
            return pd.DataFrame()

        rows = []
        for b in bars:
            if b.get("date") and b.get("close") is not None:
                rows.append([b["date"], b["close"]])

        if not rows:
            return pd.DataFrame()

        frame = pd.DataFrame(rows, columns=[TRADE_DATE_COLUMN, CLOSE_PRICE_COLUMN])
        return self._resample_daily_to_monthly(frame, CLOSE_PRICE_COLUMN)

    def fetch_stock_name(self, stock_code):
        """
        从腾讯财经快速查询股票公司中文简称（委托底层通用客户端）
        """
        return self._client.fetch_name(stock_code)

    def fetch_realtime_quote(self, stock_code):
        """
        从腾讯财经获取单只股票的实时行情快照

        【数据清洗规范】
          - 字段 3: 最新价
          - 字段 4: 昨收价, 字段 5: 今开盘价
          - 字段 31: 涨跌额, 字段 32: 涨跌幅 (%)
          - 字段 33: 最高价, 字段 34: 最低价
          - 字段 6: 成交量（科创板 688xxx 为股，普通 A 股为手，需乘以 100 转为股）
          - 字段 35: 价格/成交量/成交额（元），字段 37 为万元口径
          - 字段 38: 换手率 (%), 字段 39: 动态 PE-TTM, 字段 46: 市净率 PB
          - 字段 44: 流通市值 (亿元 -> 元), 字段 45: 总市值 (亿元 -> 元)
          - 字段 30: 时间戳 (YYYYMMDDHHMMSS)

        Returns:
            StockRealtimeQuote | None: 填充后的实时行情对象；失败返回 None
        """
        fields = self._client.fetch_quote_fields(stock_code)
        if not fields or len(fields) < 45:
            return None

        # fields[1] 为股票简称，fields[2] 为代码
        code_part = stock_code.split(".")[0]
        if len(fields) > 2 and fields[2].strip() and fields[2].strip() != code_part:
            _logger.warning(
                "%s 返回的代码与请求不匹配: %s vs %s",
                self.SOURCE_NAME,
                fields[2],
                code_part,
            )
            return None

        quote = StockRealtimeQuote(stock_code)
        quote.stock_name = fields[1].strip()
        quote.source_name = self.SOURCE_NAME
        quote.current_price = safe_float(fields[3], 0.0)
        quote.yesterday_close = safe_float(fields[4], 0.0)
        quote.today_open = safe_float(fields[5], 0.0)
        quote.highest_price = safe_float(fields[33], 0.0)
        quote.lowest_price = safe_float(fields[34], 0.0)
        quote.change_amount = safe_float(fields[31], 0.0)
        quote.change_percent = safe_float(fields[32], 0.0)

        # 成交量换算（科创板 688xxx 字段 6 本身即为股，普通 A 股字段 6 为手，乘 100 换算为股）
        raw_vol = safe_int(fields[6], 0)
        is_star = stock_code.startswith("688") or ".688" in stock_code
        quote.volume_shares = raw_vol if is_star else raw_vol * 100

        # 成交额解析：优先提取 fields[35] 中的精确元金额，备选 fields[37]（万元）
        amount_val = None
        if len(fields) > 35 and "/" in fields[35]:
            parts = fields[35].split("/")
            if len(parts) >= 3:
                amount_val = safe_float(parts[2])
        if amount_val is None and len(fields) > 37:
            wan_val = safe_float(fields[37])
            if wan_val is not None:
                amount_val = wan_val * 10000.0
        quote.amount_yuan = amount_val if amount_val is not None else 0.0

        quote.turnover_rate = safe_float(fields[38], 0.0)
        quote.pe_ttm = safe_float(fields[39])
        quote.pb_ratio = safe_float(fields[46])

        # 市值换算（单位：亿元 -> 元）
        circ_mv_yi = safe_float(fields[44])
        quote.circulating_market_value = circ_mv_yi * 100000000.0 if circ_mv_yi else 0.0
        total_mv_yi = safe_float(fields[45])
        quote.total_market_value = total_mv_yi * 100000000.0 if total_mv_yi else 0.0

        # 时间戳格式化: 20260930161500 -> 2026-09-30 16:15:00
        raw_time = fields[30].strip() if len(fields) > 30 else ""
        if len(raw_time) == 14:
            quote.quote_time = (
                f"{raw_time[0:4]}-{raw_time[4:6]}-{raw_time[6:8]} "
                f"{raw_time[8:10]}:{raw_time[10:12]}:{raw_time[12:14]}"
            )
        else:
            quote.quote_time = raw_time

        return quote
