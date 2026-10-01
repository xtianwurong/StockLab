#!/usr/bin/env python3
"""
==============================================================================
StockLab - 板块与指数长周期月线并发抓取器 (stocklab.data.sector_fetcher)
==============================================================================

【模块职责】
  专职高效并发抓取多标的（大盘指数与各赛道行业 ETF）的月度 K 线收盘价，
  支持腾讯直连 HTTP 高吞吐前复权抓取与多线程并发汇总。
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Any
import requests

from stocklab.common.type_utils import safe_float

_logger = logging.getLogger("StockLab.DataFetcher")


class SectorDataFetcher:
    """
    负责高效并发抓取 10 年长周期月线数据
    """

    _KLINE_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

    def __init__(self, http_timeout: int = 8):
        self.http_timeout = http_timeout

    def fetch_monthly_kline(self, symbol: str, num_months: int = 120) -> Dict[str, float]:
        """
        通过直连通道获取个股/ETF/指数的前复权月 K 线

        Args:
            symbol (str): 腾讯格式代码（如 'sh512480', 'sh000001'）
            num_months (int): 获取月份数

        Returns:
            dict: { 'YYYY-MM': close_price }
        """
        url = f"{self._KLINE_ENDPOINT}?param={symbol},month,,,{num_months},qfq"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "http://finance.qq.com",
        }
        try:
            res = requests.get(url, headers=headers, timeout=self.http_timeout)
            data = res.json().get("data", {}).get(symbol, {})
            kline = data.get("qfqmonth", []) or data.get("month", [])
            month_map = {}
            for item in kline:
                if len(item) >= 3:
                    date_str = item[0].strip()  # 'YYYY-MM-DD'
                    close_val = safe_float(item[2])
                    if close_val is not None:
                        month_map[date_str[:7]] = close_val
            return month_map
        except Exception as error:
            _logger.warning("抓取 %s 月线数据失败: %s", symbol, error)
            return {}

    def fetch_all(self, sectors: List[Any], num_months: int = 120) -> Dict[str, Dict[str, float]]:
        """
        并发抓取所有标的并汇总为统一字典
        """
        raw_data = {}
        _logger.info(
            "开始并发抓取 %d 个标的的最近 %d 个月月线历史数据...", len(sectors), num_months
        )

        def worker(item):
            series = self.fetch_monthly_kline(item.code, num_months=num_months)
            return item.code, series

        with ThreadPoolExecutor(max_workers=8) as executor:
            for code, series in executor.map(worker, sectors):
                if series:
                    raw_data[code] = series
                else:
                    _logger.warning("标的代码 %s 无有效月线数据", code)

        return raw_data
