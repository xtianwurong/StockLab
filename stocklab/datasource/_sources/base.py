#!/usr/bin/env python3
"""
==============================================================================
StockLab - 数据源抽象基类 (stocklab.datasource._sources.base)
==============================================================================

【模块职责】
  定义所有数据源子类必须遵守的接口契约（类似 C++ 抽象基类 / 纯虚接口）。
  只依赖 pandas 与统一数据契约（data_contract），不依赖任何具体数据源 SDK。
==============================================================================
"""

import pandas as pd

from stocklab.datasource.data_contract import (
    CLOSE_PRICE_COLUMN,
    TRADE_DATE_COLUMN,
)

__all__ = [
    "StockDataSource",
]


class StockDataSource:
    """
    数据源抽象基类（_sources 包内部使用，外部请勿直接依赖）

    【C++ 概念映射：抽象基类 (Abstract Base Class / Interface)】
      通过 raise NotImplementedError() 模拟纯虚函数 (Pure Virtual Function)。
      继承本类的具体数据源子类必须实现必需的核心虚方法：
        - fetch_monthly_close_prices(): 必须实现，获取月线价格
        - fetch_stock_name(): 必须实现，查询公司简称
      可选虚方法提供默认降级实现：
        - fetch_monthly_pe_ttm(): 默认返回空 DataFrame，不支持估值的数据源无需强制实现。
        - fetch_realtime_quote(): 默认返回 None，不支持实时盘口的数据源无需强制实现。
    """

    # 数据源可读名称标识，子类必须覆盖，用于日志追踪与展示
    SOURCE_NAME = ""

    def fetch_monthly_close_prices(
        self, stock_code, adjust_type, retry_count=1, retry_interval_seconds=0
    ):
        """
        获取月度收盘价数据（纯虚方法，子类必须实现）

        Args:
            stock_code (str): 标准股票代码（如 "000001.SZ"）
            adjust_type (str): 复权类型 ("qfq", "hfq", "")
            retry_count (int): 失败尝试重试次数
            retry_interval_seconds (int): 重试间隔秒数

        Returns:
            pd.DataFrame: 包含 trade_date 与 close_price 两列的标准表；失败返回空 DataFrame
        """
        raise NotImplementedError("子类必须实现 fetch_monthly_close_prices()")

    def fetch_monthly_pe_ttm(self, stock_code):
        """
        获取月度 PE-TTM 估值数据（可选虚方法，默认返回空表）

        【平滑降级设计】
          并非所有行情源都能免费提供长周期历史估值序列。若数据源不具备此能力，
          沿用基类默认实现返回空表即可，上层调用者将自动降级为“单轴纯价格走势图”。

        Args:
            stock_code (str): 股票代码

        Returns:
            pd.DataFrame: 包含 trade_date 与 pe_ttm 两列；若不支持则返回空 DataFrame
        """
        return pd.DataFrame()

    def fetch_stock_name(self, stock_code):
        """
        获取股票对应的公司简称（纯虚方法，子类必须实现）

        Args:
            stock_code (str): 股票代码

        Returns:
            str: 中文公司简称（如 "平安银行"）；获取失败返回空字符串
        """
        raise NotImplementedError("子类必须实现 fetch_stock_name()")

    def fetch_realtime_quote(self, stock_code):
        """
        获取单只股票的实时行情快照（可选虚方法，默认返回 None）

        Args:
            stock_code (str): 股票代码

        Returns:
            StockRealtimeQuote | None: 实时行情数据对象；不支持时返回 None
        """
        return None

    def _build_standard_dataframe(self, date_series, close_price_series):
        """
        通用工具：将任意数据源抓取的日期与价格序列装配为标准 DataFrame

        【数据清洗保障】
          - 强制将日期序列解析为统一的 pd.Timestamp (datetime64[ns])。
          - 强制将价格序列转为标准 64 位浮点数 (float64)。

        Args:
            date_series (pd.Series | list): 原始交易日期
            close_price_series (pd.Series | list): 原始价格序列

        Returns:
            pd.DataFrame: 规范化两列表结构
        """
        result = pd.DataFrame()
        result[TRADE_DATE_COLUMN] = pd.to_datetime(date_series)
        result[CLOSE_PRICE_COLUMN] = close_price_series.astype(float)
        return result

    def _resample_daily_to_monthly(self, daily_data, value_column):
        """
        通用时序工具：将高频的日线数据序列降采样为月度序列（取月末值）

        【业务背景】
          各大行情接口（如 baostock 与腾讯）在直接月线频率下可能不提供 PE-TTM 字段，
          因此统一通过日线序列拉取，再在内存中执行时序重采样。

        【算法流程】
          1. 确保交易日期列解析为 datetime 索引。
          2. 使用 Pandas 的 resample("ME") 按月末周期 (Month End) 划分桶。
          3. 调用 .last() 取该月份最后一个非空交易日的数据，自动跳过节假日与停牌无交易日。
          4. 去除可能残留的空值并重置索引。

        Args:
            daily_data (pd.DataFrame): 包含 trade_date 和目标数值列的日频数据
            value_column (str): 待降采样的数值列名（如 PE_TTM_COLUMN 或 CLOSE_PRICE_COLUMN）

        Returns:
            pd.DataFrame: 降采样后的标准月频数据表
        """
        if daily_data is None or daily_data.empty:
            return pd.DataFrame()

        frame = pd.DataFrame()
        frame[TRADE_DATE_COLUMN] = pd.to_datetime(daily_data[TRADE_DATE_COLUMN])
        frame[value_column] = pd.to_numeric(daily_data[value_column], errors="coerce")

        # 将 trade_date 设为索引并执行月末重采样
        monthly = frame.set_index(TRADE_DATE_COLUMN).resample("ME").last()
        # 清理缺失值，恢复普通整数索引
        monthly = monthly.dropna(subset=[value_column]).reset_index()
        return monthly
