#!/usr/bin/env python3
"""
==============================================================================
StockLab - 腾讯直连行情与时序客户端 (stocklab.datasource.tencent_client)
==============================================================================

【架构设计定位】
  本模块专职作为通用的市场行情接入客户端 (Unified Market Data Client)，
  遵循 C++ 抽象设计原则，彻底抹平资产标的类型差异：
    - 不区分个股 (Stock)、行业/主题 ETF (Sector ETF)、宽基 ETF 还是大盘指数 (Index)。
    - 统一通过标准标的代码规范 (normalize_symbol) 接入。
    - 统一提供全要素 K 线 (OHLCV)、月末收盘价序列 (Monthly Close) 以及多标的并发抓取契约。
"""

import logging
from concurrent.futures import ThreadPoolExecutor

import requests

from stocklab.common.type_conversion import safe_float

_logger = logging.getLogger("StockLab.MarketClient")

__all__ = [
    "TencentMarketClient",
    "normalize_symbol",
]


def normalize_symbol(symbol):
    """
    通用标的代码标准化转换函数

    兼容输入：
      - "000001.SZ" -> "sz000001"
      - "600519.SH" -> "sh600519"
      - "sh000001"   -> "sh000001"
      - "512480"     -> "sh512480" (根据 A 股代码前缀智能推导市场)

    Args:
        symbol: 任意标准或简写代码 (str)

    Returns:
        str: 腾讯直连标准小写带市场前缀代码 (如 'sh600519', 'sz000001')
    """
    raw = str(symbol).strip().lower()
    if "." in raw:
        parts = raw.split(".")
        code, mkt = parts[0], parts[1]
        return mkt + code

    if raw.startswith(("sh", "sz", "bj")):
        return raw

    # 纯数字智能推导
    if raw.startswith(("6", "5", "90")):
        return "sh" + raw
    if raw.startswith(("4", "8", "92")):
        return "bj" + raw
    return "sz" + raw


class TencentMarketClient:
    """
    腾讯直连市场行情客户端 (Tencent Market Client)
    直连腾讯行情与 K 线网络通道，对外提供全要素 K 线、月线及实时盘口接口，统一无差别支持股票、ETF 与指数。
    """

    _KLINE_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    _QUOTE_ENDPOINT = "http://qt.gtimg.cn/q"

    def __init__(self, http_timeout=8, max_workers=8):
        self.http_timeout = http_timeout
        self.max_workers = max_workers
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "http://finance.qq.com",
        }

    def fetch_kline(self, symbol, period="month", adjust="qfq", count=120):
        """
        获取单标的完整 OHLCV 历史 K 线序列

        Args:
            symbol (str): 标的代码 (股票/ETF/指数均可)
            period (str): 周期 ('day', 'week', 'month')
            adjust (str): 复权方式 ('qfq', 'hfq', '')
            count (int): 获取条数 (默认 120 根)

        Returns:
            list: [{'date': 'YYYY-MM-DD', 'open': float, 'close': float,
                   'high': float, 'low': float, 'volume': float}, ...]
        """
        norm_sym = normalize_symbol(symbol)
        param_str = f"{norm_sym},{period},,,{count},{adjust}"
        url = f"{self._KLINE_ENDPOINT}?param={param_str}"

        try:
            res = requests.get(url, headers=self._headers, timeout=self.http_timeout)
            data = res.json().get("data", {}).get(norm_sym, {})
            # 兼容前复权与不复权返回字段
            kline = (
                data.get(f"{adjust}{period}", [])
                or data.get(period, [])
                or []
            )

            bars = []
            for item in kline:
                if len(item) >= 3:
                    bars.append({
                        "date": str(item[0]).strip(),
                        "open": safe_float(item[1]),
                        "close": safe_float(item[2]),
                        "high": safe_float(item[3]) if len(item) > 3 else None,
                        "low": safe_float(item[4]) if len(item) > 4 else None,
                        "volume": safe_float(item[5]) if len(item) > 5 else None,
                    })
            return bars
        except Exception as error:
            _logger.warning("通用客户端抓取 [%s] K线失败: %s", symbol, error)
            return []

    def fetch_monthly_close(self, symbol, num_months=120):
        """
        快捷获取单标的长周期月末收盘价字典 (不区分股票/ETF/指数)

        Args:
            symbol (str): 标的代码 (如 'sh600519', 'sz000001', 'sh512480', 'sh000001')
            num_months (int): 获取月份数

        Returns:
            dict: { 'YYYY-MM': close_price }
        """
        bars = self.fetch_kline(symbol=symbol, period="month", adjust="qfq", count=num_months)
        month_map = {}
        for bar in bars:
            d = bar["date"]
            c = bar["close"]
            if d and c is not None:
                month_map[d[:7]] = c
        return month_map

    def fetch_multi_monthly_close(self, targets, num_months=120):
        """
        高并发批量抓取多资产标的池的月线收盘价 (股票、ETF、指数混合池均可)

        Args:
            targets (list): 标的列表，支持纯字符串代码列表，或具有 `.code` 属性的实体对象列表
            num_months (int): 获取月份数

        Returns:
            dict: { code: { 'YYYY-MM': close_price } }
        """
        raw_data = {}
        _logger.info("开始并发抓取 %d 个资产标的最近 %d 个月月线数据...", len(targets), num_months)

        def worker(item):
            code_str = getattr(item, "code", item) if not isinstance(item, str) else item
            series = self.fetch_monthly_close(code_str, num_months=num_months)
            return code_str, series

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for code, series in executor.map(worker, targets):
                if series:
                    raw_data[code] = series
                else:
                    _logger.warning("标的代码 [%s] 未获取到有效月线数据", code)

        return raw_data

    def fetch_quote_fields(self, symbol):
        """
        获取腾讯直连盘口原始字段列表 (~ 分隔)

        Args:
            symbol (str): 标的代码

        Returns:
            list: 分割后的原始字段字符串列表；失败返回空列表
        """
        norm_sym = normalize_symbol(symbol)
        url = f"{self._QUOTE_ENDPOINT}={norm_sym}"
        try:
            res = requests.get(url, headers=self._headers, timeout=self.http_timeout)
            res.encoding = "gbk"
            content = res.text.strip()
            data_start = content.find('"')
            data_end = content.rfind('"')
            if data_start == -1 or data_end <= data_start:
                return []
            return content[data_start + 1 : data_end].split("~")
        except Exception as error:
            _logger.warning("查询标的 [%s] 行情失败: %s", symbol, error)
            return []

    def fetch_name(self, symbol):
        """
        通用查询任意标的的中文名称 (股票简称 / ETF简称 / 指数名称)

        Args:
            symbol (str): 标的代码

        Returns:
            str: 中文简称
        """
        fields = self.fetch_quote_fields(symbol)
        return fields[1].strip() if len(fields) > 1 else ""
