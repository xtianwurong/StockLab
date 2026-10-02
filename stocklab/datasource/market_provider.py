#!/usr/bin/env python3
"""
==============================================================================
StockLab - 全市场数据 Provider (stocklab.datasource.market_provider)
==============================================================================

【模块职责】
   负责从外部数据源批量获取全市场 A 股数据，输出标准化 pandas DataFrame。
   与单股数据服务 (stock_data.py) 明确边界：本模块专注于全市场批量获取。

【数据源策略】
   - 股票基础信息：AkShare (stock_info_a_code_name)
   - 日 K 行情：AkShare (stock_zh_a_hist) 按日期逐只抓取
   - 估值数据：AkShare (stock_zh_a_spot_em) 实时快照

【设计原则】
   - 不破坏现有 Provider 接口
   - 批量获取优先，避免逐股票低效调用
   - 失败降级：AkShare 失败时记录日志并返回空 DataFrame
"""

import logging
import time

import akshare as ak
import pandas as pd

_logger = logging.getLogger(__name__)

__all__ = [
    "MarketDataProvider",
]


class MarketDataProvider:
    """
    全市场数据提供类

    【职责】
      1. 获取全市场股票基础信息
      2. 获取指定日期范围的日 K 行情
      3. 获取最新全市场估值快照
    """

    def __init__(self, retry_count=2, retry_interval_seconds=2):
        """
        初始化全市场数据 Provider

        Args:
            retry_count (int): 失败重试次数
            retry_interval_seconds (int): 重试间隔秒数
        """
        self._retry_count = retry_count
        self._retry_interval_seconds = retry_interval_seconds

    def fetch_securities(self):
        """
        获取全市场股票基础信息

        Returns:
            pd.DataFrame: 证券基础信息表，包含 ts_code, name, industry, area 等列
        """
        for attempt in range(1, self._retry_count + 1):
            try:
                raw = ak.stock_info_a_code_name()
                if raw is None or raw.empty:
                    _logger.warning("stock_info_a_code_name 返回空数据")
                    return pd.DataFrame()

                # 标准化列名
                result = pd.DataFrame()
                result["ts_code"] = raw["code"].apply(self._normalize_ts_code)
                result["symbol"] = raw["code"]
                result["name"] = raw["name"]
                result["exchange"] = result["ts_code"].apply(
                    lambda x: x.split(".")[1] if "." in x else ""
                )
                result["market"] = result["ts_code"].apply(
                    lambda x: "SH" if ".SH" in x else ("SZ" if ".SZ" in x else "BJ")
                )
                result["industry"] = ""
                result["area"] = ""
                result["list_date"] = None
                result["delist_date"] = None
                result["list_status"] = "L"
                result["is_hs"] = ""

                _logger.info("获取全市场股票基础信息: %d 只", len(result))
                return result
            except Exception as error:
                _logger.debug(
                    "stock_info_a_code_name 第 %d/%d 次失败: %s",
                    attempt,
                    self._retry_count,
                    error,
                )
                if attempt < self._retry_count:
                    time.sleep(self._retry_interval_seconds)

        _logger.error("获取全市场股票基础信息失败")
        return pd.DataFrame()

    def fetch_daily_prices(self, ts_code, start_date, end_date):
        """
        获取单只股票指定日期范围的日 K 行情（不复权）

        Args:
            ts_code (str): 证券代码，如 "600519.SH"
            start_date (str): 起始日期，格式 "YYYYMMDD"
            end_date (str): 结束日期，格式 "YYYYMMDD"

        Returns:
            pd.DataFrame: 日 K 行情表，包含 ts_code, trade_date, open, high, low, close 等列
        """
        symbol = ts_code.split(".")[0]

        for attempt in range(1, self._retry_count + 1):
            try:
                raw = ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="",
                )
                if raw is None or raw.empty:
                    return pd.DataFrame()

                # 标准化列名
                result = pd.DataFrame()
                result["ts_code"] = ts_code
                result["trade_date"] = pd.to_datetime(raw["日期"]).dt.date
                result["open"] = raw["开盘"].astype(float)
                result["high"] = raw["最高"].astype(float)
                result["low"] = raw["最低"].astype(float)
                result["close"] = raw["收盘"].astype(float)
                result["pre_close"] = raw["昨收"].astype(float) if "昨收" in raw.columns else None
                result["change"] = raw["涨跌额"].astype(float) if "涨跌额" in raw.columns else None
                result["pct_chg"] = raw["涨跌幅"].astype(float) if "涨跌幅" in raw.columns else None
                result["volume"] = raw["成交量"].astype(float) if "成交量" in raw.columns else None
                result["amount"] = raw["成交额"].astype(float) if "成交额" in raw.columns else None

                return result
            except Exception as error:
                _logger.debug(
                    "stock_zh_a_hist [%s] 第 %d/%d 次失败: %s",
                    ts_code,
                    attempt,
                    self._retry_count,
                    error,
                )
                if attempt < self._retry_count:
                    time.sleep(self._retry_interval_seconds)

        return pd.DataFrame()

    def fetch_realtime_valuations(self):
        """
        获取最新全市场估值快照（东方财富实时行情）

        Returns:
            pd.DataFrame: 全市场估值表，包含 ts_code, trade_date, pe, pe_ttm, pb 等列
        """
        for attempt in range(1, self._retry_count + 1):
            try:
                raw = ak.stock_zh_a_spot_em()
                if raw is None or raw.empty:
                    _logger.warning("stock_zh_a_spot_em 返回空数据")
                    return pd.DataFrame()

                # 标准化列名：必须与 market.daily_valuations 表的 16 列严格同名同序，
                # 否则 Repository 的 SELECT * 批量写入会因列数不匹配而失败。
                # 数据源不提供的字段统一留 NULL，保留 NULL 语义。
                result = pd.DataFrame()
                result["ts_code"] = raw["代码"].apply(self._normalize_ts_code)
                result["trade_date"] = pd.Timestamp.now().date()
                result["turnover_rate"] = self._numeric_column(raw, "换手率")
                result["turnover_rate_f"] = float("nan")
                result["pe"] = self._numeric_column(raw, "市盈率-动态")
                result["pe_ttm"] = self._numeric_column(raw, "市盈率(TTM)")
                result["pb"] = self._numeric_column(raw, "市净率")
                result["ps"] = float("nan")
                result["ps_ttm"] = float("nan")
                result["dv_ratio"] = float("nan")
                result["dv_ttm"] = float("nan")
                result["total_share"] = float("nan")
                result["float_share"] = float("nan")
                result["free_share"] = float("nan")
                result["total_mv"] = self._numeric_column(raw, "总市值")
                result["circ_mv"] = self._numeric_column(raw, "流通市值")

                _logger.info("获取全市场估值快照: %d 只", len(result))
                return result
            except Exception as error:
                _logger.debug(
                    "stock_zh_a_spot_em 第 %d/%d 次失败: %s",
                    attempt,
                    self._retry_count,
                    error,
                )
                if attempt < self._retry_count:
                    time.sleep(self._retry_interval_seconds)

        _logger.error("获取全市场估值快照失败")
        return pd.DataFrame()

    def fetch_valuation_history(self, ts_code, period="全部"):
        """
        获取单只股票的逐日历史估值序列（百度股市通）

        【用途】
           与 fetch_realtime_valuations() 的「全市场单日快照」互补：
           本方法返回单只股票跨年的完整序列，用于计算当前估值的历史分位。

        【数据源选择理由】
           百度股市通接口不走东财风控，且直接提供多指标历史序列，
           是当前环境下唯一稳定可用的历史估值来源。

        【为何不含股息率】
           该接口不提供股息率指标（实测 indicator="股息率" 抛 TypeError），
           股息率需由「每股股利 / 股价」自行计算，不在本方法职责内。

        【已知数据源缺口】
           实测该接口的「市销率」不可用（抛 TypeError），对应列整列写 NULL，
           与 daily_valuations 表的 ps 列现状一致。

        Args:
            ts_code (str): 证券代码，如 "600519.SH"
            period (str, optional): 历史区间，"全部" 或 "近五年" 等

        Returns:
            pd.DataFrame: 历史估值表，列为
                          ts_code / trade_date / pe_ttm / pe_static / pb / ps / pcf；
                          单个指标取不到时整列为 NaN，不省略列
        """
        # 百度接口接受 6 位纯数字代码；部分指标缺失时整列 NaN，不抛错
        indicator_map = [
            ("pe_ttm", "市盈率(TTM)"),
            ("pe_static", "市盈率(静)"),
            ("pb", "市净率"),
            ("ps", "市销率"),
            ("pcf", "市现率"),
        ]

        symbol = ts_code.split(".")[0]

        # 以 PE-TTM 的交易日为基准轴，其余指标按日期对齐后并入；
        # PE-TTM 缺失时退化用 PB，仍缺失则说明该标的完全无可用估值数据
        base_dates, base_values = self._fetch_baidu_indicator_frame(
            symbol, "市盈率(TTM)", period
        )
        if base_dates.empty:
            base_dates, _ = self._fetch_baidu_indicator_frame(symbol, "市净率", period)
        if base_dates.empty:
            _logger.warning("[%s] 未返回任何历史估值数据", ts_code)
            return pd.DataFrame()

        result = pd.DataFrame()
        result["trade_date"] = base_dates
        result["pe_ttm"] = base_values

        for column_name, indicator in indicator_map[1:]:
            _, series_values = self._fetch_baidu_indicator_frame(symbol, indicator, period)
            result[column_name] = series_values

        result["ts_code"] = ts_code
        # 列序必须与 market.valuation_history 表定义严格一致（UPSERT 按位置匹配）
        result = result[["ts_code", "trade_date"] + [item[0] for item in indicator_map]]
        result = result.sort_values(by="trade_date")

        _logger.info(
            "获取 %s 历史估值序列: %d 个交易日（%s ~ %s）",
            ts_code,
            len(result),
            result["trade_date"].iloc[0],
            result["trade_date"].iloc[-1],
        )
        return result.reset_index(drop=True)

    def _fetch_baidu_indicator_frame(self, symbol, indicator, period):
        """
        调用百度股市通接口取单个估值指标的逐日序列（日期 + 数值）

        【为何逐指标单独调用】
           各指标可用性不一致（如市销率缺失），逐个调用可让单个失败
           不影响其余指标，符合「数据源不提供的列写 NULL，绝不省略列」。

        Args:
            symbol (str): 6 位纯数字代码
            indicator (str): 百度接口的指标名
            period (str): 历史区间

        Returns:
            tuple: (date_series, value_series)；失败返回 (空 Series, 空 Series)
        """
        for attempt in range(1, self._retry_count + 1):
            try:
                raw = ak.stock_zh_valuation_baidu(
                    symbol=symbol, indicator=indicator, period=period
                )
                if raw is None or raw.empty:
                    return pd.Series(dtype="object"), pd.Series(dtype="float64")
                return raw["date"], pd.to_numeric(raw["value"], errors="coerce")
            except Exception as error:
                _logger.debug(
                    "stock_zh_valuation_baidu [%s/%s] 第 %d/%d 次失败: %s",
                    symbol,
                    indicator,
                    attempt,
                    self._retry_count,
                    error,
                )
                if attempt < self._retry_count:
                    time.sleep(self._retry_interval_seconds)

        _logger.info("指标 [%s] 在 %s 上不可用，写入 NULL", indicator, symbol)
        return pd.Series(dtype="object"), pd.Series(dtype="float64")

    def _normalize_ts_code(self, code):
        """
        将纯数字代码转换为标准 ts_code 格式

        Args:
            code (str): 纯数字代码，如 "600519"

        Returns:
            str: 标准格式，如 "600519.SH"
        """
        code = str(code).strip()
        if code.startswith(("6", "5", "90")):
            return code + ".SH"
        if code.startswith(("4", "8", "92")):
            return code + ".BJ"
        return code + ".SZ"

    def _numeric_column(self, raw, column_name):
        """
        安全提取 DataFrame 中的数值列，列不存在时返回全 NaN 列

        Args:
            raw (pd.DataFrame): 原始数据表
            column_name (str): 目标列名

        Returns:
            pd.Series: 数值列；列缺失时返回全 NaN 序列（保持列结构完整）
        """
        if column_name in raw.columns:
            return pd.to_numeric(raw[column_name], errors="coerce")
        return float("nan")
