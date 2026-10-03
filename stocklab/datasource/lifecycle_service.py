#!/usr/bin/env python3
"""
==============================================================================
StockLab - 证券生命周期取数模块 (stocklab.datasource.lifecycle_service)
==============================================================================

【模块职责】
   从沪深北交易所官网拉取上市日历与退市日历，经 normalizer.exchange 归一化为
   生命周期日历帧返回。本层只取数与归一化，不落库。

【为什么用交易所官网而不是东财名录】
   东财名录只含当前在市证券，退市股票不在其中 —— 用它当历史样本池会产生
   幸存者偏差。交易所官网同时提供：
     - 上市日历（沪主板 / 沪科创板 / 深 A / 北证，含上市日期）
     - 退市日历（沪终止上市、深终止上市，含上市日期与退市日期）

【失败语义】
   任一来源失败即整批返回空表（all-or-nothing）：日历不完整时宁可不更新，
   也不能写入「部分股票有退市日、部分没有」的半成品状态。
"""

import logging
import time

import akshare as ak
import pandas as pd

from stocklab.domain import DataContractError
from stocklab.normalization.exchange import (
    normalize_delisting_calendar,
    normalize_listing_calendar,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "LifecycleService",
]


class LifecycleService:
    """
    证券生命周期取数类：交易所上市/退市日历 → 生命周期日历帧

    【方法】
      fetch_listing_calendar    -> ts_code / name / list_date（当前在市证券）
      fetch_delisting_calendar  -> ts_code / name / list_date / delist_date（退市证券）
    """

    def __init__(self, retry_count=2, retry_interval_seconds=2, interval_seconds=0.3):
        """
        初始化生命周期取数服务

        Args:
            retry_count (int, optional): 失败重试次数
            retry_interval_seconds (int, optional): 重试间隔秒数
            interval_seconds (float, optional): 两次请求之间的最小间隔（防限流）
        """
        self._retry_count = retry_count
        self._retry_interval_seconds = retry_interval_seconds
        self._interval_seconds = interval_seconds

    def fetch_listing_calendar(self):
        """
        获取当前在市证券的上市日历（沪主板 + 科创板 + 深 A + 北证）

        Returns:
            pd.DataFrame: ts_code / name / list_date；任一来源失败时为空表
        """
        sources = (
            ("上交所主板", ak.stock_info_sh_name_code, {"symbol": "主板A股"}),
            ("上交所科创板", ak.stock_info_sh_name_code, {"symbol": "科创板"}),
            ("深交所A股", ak.stock_info_sz_name_code, {"symbol": "A股列表"}),
            ("北交所", ak.stock_info_bj_name_code, {}),
        )
        raw_frames = self._fetch_sources(sources, "上市日历")
        if raw_frames is None:
            return pd.DataFrame()
        try:
            return normalize_listing_calendar(raw_frames)
        except DataContractError as error:
            _logger.error("上市日历数据契约违约，本批次不入库: %s", error)
            return pd.DataFrame()

    def fetch_delisting_calendar(self):
        """
        获取退市日历（上交所终止上市 + 深交所终止上市）

        Returns:
            pd.DataFrame: ts_code / name / list_date / delist_date；
                          任一来源失败时为空表
        """
        sources = (
            ("上交所终止上市", ak.stock_info_sh_delist, {"symbol": "全部"}),
            ("深交所终止上市", ak.stock_info_sz_delist, {"symbol": "终止上市公司"}),
        )
        raw_frames = self._fetch_sources(sources, "退市日历")
        if raw_frames is None:
            return pd.DataFrame()
        try:
            return normalize_delisting_calendar(raw_frames)
        except DataContractError as error:
            _logger.error("退市日历数据契约违约，本批次不入库: %s", error)
            return pd.DataFrame()

    def _fetch_sources(self, sources, calendar_name):
        """
        顺序抓取日历的全部来源（任一失败即整体失败）

        Args:
            sources (tuple): (来源名, akshare 接口, 参数字典) 列表
            calendar_name (str): 日历名，用于日志

        Returns:
            list: 各来源的源帧列表；任一来源失败返回 None
        """
        raw_frames = []
        for source_name, fetcher, kwargs in sources:
            frame = self._call(source_name, fetcher, kwargs)
            if frame is None:
                _logger.error("%s来源 [%s] 抓取失败，本次不更新%s",
                              calendar_name, source_name, calendar_name)
                return None
            raw_frames.append(frame)
        return raw_frames

    def _call(self, source_name, fetcher, kwargs):
        """
        带重试与限流地调用一个来源

        Args:
            source_name (str): 来源名，用于日志
            fetcher: akshare 接口函数
            kwargs (dict): 接口参数

        Returns:
            pd.DataFrame: 源帧；失败返回 None
        """
        for attempt in range(1, self._retry_count + 1):
            if attempt > 1 and self._interval_seconds:
                time.sleep(self._interval_seconds)
            try:
                frame = fetcher(**kwargs)
                if frame is None or frame.empty:
                    _logger.warning(
                        "%s [%s] 返回空数据（第 %d/%d 次）",
                        source_name, fetcher.__name__, attempt, self._retry_count,
                    )
                else:
                    return frame
            except Exception as error:
                _logger.debug(
                    "%s [%s] 第 %d/%d 次失败: %s",
                    source_name, fetcher.__name__, attempt, self._retry_count, error,
                )
            if attempt < self._retry_count:
                time.sleep(self._retry_interval_seconds)
        return None
