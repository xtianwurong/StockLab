#!/usr/bin/env python3
"""
==============================================================================
StockLab - AkShare 数据源通道 (stocklab.datasource._sources.akshare_source)
==============================================================================

【模块职责】
  主数据源通道实现（基于开源库 akshare，底层为东方财富 / 百度股市通 Web API）。
==============================================================================
"""

import logging
import time

import akshare as ak
import pandas as pd

from stocklab.datasource._sources.base import StockDataSource
from stocklab.datasource.data_contract import (
    PE_TTM_COLUMN,
    TRADE_DATE_COLUMN,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "AkShareDataSource",
]


class AkShareDataSource(StockDataSource):
    """
    主数据源：基于开源库 akshare 实现

    【特性分析】
      - 优势：A 股覆盖最全、更新最及时、历史行情跨度长（可追溯至上市首日）。
      - 劣势：底层高度依赖东方财富 Web API，受 IP 访问频次风控与反爬策略影响，
        容易在连续高频请求下被主动断开连接 (RemoteDisconnected)。
    """

    SOURCE_NAME = "akshare(东方财富)"

    # akshare 个股信息字典接口返回的结构列名定义
    _INFO_ITEM_COLUMN = "item"       # 键名列（属性名称）
    _INFO_VALUE_COLUMN = "value"     # 键值列（属性数值）
    _STOCK_NAME_ITEM = "股票简称"    # 用于提取中文股票名的目标属性名

    def fetch_monthly_close_prices(
        self, stock_code, adjust_type, retry_count=3, retry_interval_seconds=3
    ):
        """
        从 akshare 获取月线收盘行情（内建重试机制）

        Returns:
            pd.DataFrame: 标准格式月线数据；彻底失败返回空表
        """
        symbol = self._convert_to_akshare_code(stock_code)
        raw_data = None
        last_error = None

        for attempt in range(1, retry_count + 1):
            try:
                # period 参数仅支持 "daily", "weekly", "monthly"；此处锁定月线
                raw_data = ak.stock_zh_a_hist(
                    symbol=symbol, period="monthly", adjust=adjust_type
                )
                break
            except Exception as error:
                last_error = error
                _logger.debug(
                    "%s 第 %s/%s 次失败：%s",
                    self.SOURCE_NAME,
                    attempt,
                    retry_count,
                    error,
                )
                if attempt < retry_count:
                    time.sleep(retry_interval_seconds)

        if raw_data is None:
            _logger.warning(
                "%s 连续 %s 次失败：%s",
                self.SOURCE_NAME,
                retry_count,
                type(last_error).__name__,
            )
            return pd.DataFrame()

        if raw_data.empty:
            _logger.warning("%s 未返回 %s 的数据", self.SOURCE_NAME, stock_code)
            return pd.DataFrame()

        # 防御性编程：检测上游库返回列名是否改版变动
        if "日期" not in raw_data.columns or "收盘" not in raw_data.columns:
            _logger.warning(
                "%s 接口列名有变化，当前列：%s", self.SOURCE_NAME, list(raw_data.columns)
            )
            return pd.DataFrame()

        return self._build_standard_dataframe(raw_data["日期"], raw_data["收盘"])

    def fetch_monthly_pe_ttm(self, stock_code):
        """
        从 akshare 获取月度 PE-TTM 估值指标（调用百度股市通接口）

        Returns:
            pd.DataFrame: 包含 trade_date 与 pe_ttm 的月度表；失败返回空表
        """
        symbol = self._convert_to_akshare_code(stock_code)
        try:
            # 查询百度股市通的历史日频估值序列（indicator="市盈率(TTM)"，取全部历史）
            daily_data = ak.stock_zh_valuation_baidu(
                symbol=symbol, indicator="市盈率(TTM)", period="全部"
            )
        except Exception as error:
            _logger.warning(
                "%s 获取 %s 的 PE-TTM 失败：%s", self.SOURCE_NAME, stock_code, error
            )
            return pd.DataFrame()

        if daily_data is None or daily_data.empty:
            _logger.warning("%s 未返回 %s 的 PE-TTM 数据", self.SOURCE_NAME, stock_code)
            return pd.DataFrame()

        if "date" not in daily_data.columns or "value" not in daily_data.columns:
            _logger.warning(
                "%s 估值接口列名有变化，当前列：%s",
                self.SOURCE_NAME,
                list(daily_data.columns),
            )
            return pd.DataFrame()

        daily_frame = pd.DataFrame()
        daily_frame[TRADE_DATE_COLUMN] = daily_data["date"]
        daily_frame[PE_TTM_COLUMN] = daily_data["value"]
        return self._resample_daily_to_monthly(daily_frame, PE_TTM_COLUMN)

    def fetch_stock_name(self, stock_code):
        """
        从 akshare 查询股票公司中文简称

        Returns:
            str: 公司简称；查询失败返回空字符串
        """
        symbol = self._convert_to_akshare_code(stock_code)
        try:
            stock_info = ak.stock_individual_info_em(symbol=symbol)
        except Exception as error:
            _logger.warning(
                "%s 获取 %s 的公司名称失败：%s",
                self.SOURCE_NAME,
                stock_code,
                type(error).__name__,
            )
            return ""

        if stock_info is None or stock_info.empty:
            _logger.warning("%s 未返回 %s 的公司名称", self.SOURCE_NAME, stock_code)
            return ""

        if (
            self._INFO_ITEM_COLUMN not in stock_info.columns
            or self._INFO_VALUE_COLUMN not in stock_info.columns
        ):
            _logger.warning(
                "%s 个股信息接口列名有变化，当前列：%s",
                self.SOURCE_NAME,
                list(stock_info.columns),
            )
            return ""

        # 筛选“股票简称”行
        name_rows = stock_info[
            stock_info[self._INFO_ITEM_COLUMN] == self._STOCK_NAME_ITEM
        ]
        if name_rows.empty:
            _logger.warning(
                "%s 返回数据里没有 %s 对应的公司简称", self.SOURCE_NAME, stock_code
            )
            return ""

        return str(name_rows[self._INFO_VALUE_COLUMN].iloc[0]).strip()

    def _convert_to_akshare_code(self, stock_code):
        """转换股票代码格式为 akshare 纯数字代码 (如 '000001.SZ' -> '000001')"""
        return stock_code.split(".")[0]
