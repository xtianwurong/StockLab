#!/usr/bin/env python3
"""
==============================================================================
StockLab - BaoStock 数据源通道 (stocklab.datasource._sources.baostock_source)
==============================================================================

【模块职责】
  备用数据源通道实现（基于证券宝 baostock，专有 Socket 协议，与东财风控物理隔离）。
==============================================================================
"""

import contextlib
import io
import logging
from datetime import datetime

import baostock as bs
import pandas as pd

from stocklab.datasource._sources.base import StockDataSource
from stocklab.datasource.contract import (
    CLOSE_PRICE_COLUMN,
    PE_TTM_COLUMN,
    TRADE_DATE_COLUMN,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "BaoStockDataSource",
]


class BaoStockDataSource(StockDataSource):
    """
    备用数据源：基于证券宝 (baostock) 实现

    【特性分析】
      - 优势：baostock 自建独立服务器与专有 Socket 通信协议，与东财风控完全物理隔离，
        天然充当高可靠的异构历史行情备份源。
      - 机制考量：
          * 连接管理：baostock 要求在发起任何查询前后进行 login / logout。
          * 输出污染治理：baostock 内部默认向 stdout 输出 "login success!" 等日志，
            在此使用 contextlib.redirect_stdout 进行静默拦截，保持控制台整洁。
    """

    SOURCE_NAME = "baostock(证券宝)"

    # query_stock_basic 接口返回的字段元组中，第 1 个索引位置（第 2 列）为 code_name (股票简称)
    CODE_NAME_COLUMN_INDEX = 1

    def __init__(self):
        # 预设内存字符缓冲区，专门用于重定向并丢弃 baostock 的控制台打印
        self._output_buffer = io.StringIO()

    def fetch_monthly_close_prices(
        self, stock_code, adjust_type, retry_count=1, retry_interval_seconds=0
    ):
        """从 baostock 获取月线收盘行情"""
        baostock_code = self._convert_to_baostock_code(stock_code)
        baostock_adjust_flag = self._convert_to_baostock_adjust_flag(adjust_type)

        rows = self._query_baostock_kline_rows(
            baostock_code, "date,close", "m", baostock_adjust_flag
        )
        if rows is None:
            return pd.DataFrame()

        if not rows:
            _logger.warning("%s 未返回 %s 的数据", self.SOURCE_NAME, stock_code)
            return pd.DataFrame()

        frame = pd.DataFrame(rows, columns=[TRADE_DATE_COLUMN, CLOSE_PRICE_COLUMN])
        return self._build_standard_dataframe(
            frame[TRADE_DATE_COLUMN], frame[CLOSE_PRICE_COLUMN]
        )

    def fetch_monthly_pe_ttm(self, stock_code):
        """
        从 baostock 获取月度 PE-TTM 估值（走日线降采样）
        """
        baostock_code = self._convert_to_baostock_code(stock_code)

        # 查询日线 (frequency="d")，adjustflag="2" (前复权)
        rows = self._query_baostock_kline_rows(
            baostock_code, "date,peTTM", "d", "2"
        )
        if rows is None:
            return pd.DataFrame()

        if not rows:
            _logger.warning("%s 未返回 %s 的 PE-TTM 数据", self.SOURCE_NAME, stock_code)
            return pd.DataFrame()

        daily_frame = pd.DataFrame(rows, columns=[TRADE_DATE_COLUMN, PE_TTM_COLUMN])
        return self._resample_daily_to_monthly(daily_frame, PE_TTM_COLUMN)

    def fetch_stock_name(self, stock_code):
        """从 baostock 查询股票公司中文简称"""
        baostock_code = self._convert_to_baostock_code(stock_code)
        stock_name = self._query_baostock_stock_name(baostock_code)
        if not stock_name:
            _logger.warning("%s 未返回 %s 的公司名称", self.SOURCE_NAME, stock_code)
        return stock_name

    def _connect(self):
        """建立与 baostock 服务的网络会话连接"""
        login_result = None
        try:
            with contextlib.redirect_stdout(self._output_buffer):
                login_result = bs.login()
        except Exception as error:
            _logger.warning("%s 登录失败：%s", self.SOURCE_NAME, error)
            return False

        if login_result is None or login_result.error_code != "0":
            _logger.warning(
                "%s 登录失败：%s", self.SOURCE_NAME, login_result.error_msg
            )
            return False
        return True

    def _disconnect(self):
        """断开与 baostock 的会话连接（必须在 finally 代码块中强制调用）"""
        with contextlib.redirect_stdout(self._output_buffer):
            bs.logout()

    def _query_baostock_kline_rows(
        self, baostock_code, fields, frequency, adjust_flag
    ):
        """内部通用执行单次 K 线数据检索"""
        rows = []
        if not self._connect():
            return None

        try:
            query_result = bs.query_history_k_data_plus(
                code=baostock_code,
                fields=fields,
                start_date="1990-01-01",
                end_date=datetime.now().strftime("%Y-%m-%d"),
                frequency=frequency,
                adjustflag=adjust_flag,
            )
            if query_result.error_code != "0":
                _logger.warning(
                    "%s 查询失败：%s", self.SOURCE_NAME, query_result.error_msg
                )
                return None

            while query_result.next():
                row = query_result.get_row_data()
                if len(row) == 2 and row[0] and row[1]:
                    rows.append([row[0], row[1]])
        except Exception as error:
            _logger.warning("%s 查询失败：%s", self.SOURCE_NAME, error)
            return None
        finally:
            self._disconnect()

        return rows

    def _query_baostock_stock_name(self, baostock_code):
        """内部执行单只股票代码的公司名称检索"""
        if not self._connect():
            return ""

        stock_name = ""
        try:
            query_result = bs.query_stock_basic(code=baostock_code)
            if query_result.error_code != "0":
                _logger.warning(
                    "%s 公司名称查询失败：%s", self.SOURCE_NAME, query_result.error_msg
                )
                return ""

            if query_result.next():
                row = query_result.get_row_data()
                stock_name = row[self.CODE_NAME_COLUMN_INDEX]
        except Exception as error:
            _logger.warning("%s 公司名称查询失败：%s", self.SOURCE_NAME, error)
            return ""
        finally:
            self._disconnect()

        return stock_name

    def _convert_to_baostock_code(self, stock_code):
        """转换股票代码格式为 baostock 标准格式 (如 '000001.SZ' -> 'sz.000001')"""
        parts = stock_code.split(".")
        number = parts[0]
        market = parts[1].lower() if len(parts) == 2 else "sz"
        return market + "." + number

    def _convert_to_baostock_adjust_flag(self, adjust_type):
        """将标准复权标识映射为 baostock 的数字字符参数"""
        if adjust_type == "hfq":
            return "1"
        if adjust_type == "qfq":
            return "2"
        return "3"
