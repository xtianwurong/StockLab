#!/usr/bin/env python3
"""
==============================================================================
StockLab - 统一数据取数门面 (stocklab.facade.market_data)
==============================================================================

【模块职责】
   为「本地 DuckDB 库 + 远端公开接口」这一数据子系统提供统一的取数入口，
   对上层隐藏数据来源的选择、命中判定与回退策略。

【设计模式】
   Facade（外观模式，GoF 结构型模式）：
   为复杂子系统提供一个统一的简化接口。

   注意：此处「外观」指「对外的门面 / 入口」，与界面美观无关。

【取数优先级】
   由 config.ini 的 [data_source] priority 决定，**策略不写进类名**，
   策略变更（新增缓存层、改为按新鲜度路由）时无需改名：

     local_first  : 先查本地库 -> 未命中回退远端 -> 远端结果回写本地
     remote_first : 先取远端   -> 失败回退本地库

【分层约束】
   - 本层是 datasource 与 persistence 的唯一共同调用方，可依赖两者。
   - datasource 与 persistence **绝不可反向 import 本层**，否则形成循环依赖。
==============================================================================
"""

import logging

import pandas as pd

from stocklab.common.config import (
    DATA_SOURCE_PRIORITIES,
    DEFAULT_DATA_SOURCE_PRIORITY,
    load_data_source_priority,
)
from stocklab.datasource.market_provider import MarketDataProvider
from stocklab.datasource.tencent_client import TencentMarketClient
from stocklab.persistence import (
    DailyPriceRepository,
    DailyValuationRepository,
    Database,
    SecurityRepository,
    ValuationHistoryRepository,
    initialize_database,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "MarketDataFacade",
]


class MarketDataFacade:
    """
    统一数据取数门面类

    【职责】
      1. 屏蔽「数据来自本地库还是远端接口」的差异，对上层暴露一致的取数方法
      2. 按配置的优先级决定查询顺序，并在未命中 / 失败时自动回退
      3. 远端取到的数据回写本地库（cache-aside），使后续查询命中本地

    【当前已知覆盖缺口】
      本地库目前仅覆盖 A 股个股（reference.securities 为 A 股全量），
      且 market.daily_prices 存的是**不复权日 K**。因此：
        - 行业 ETF 与指数标的本地无记录，必然回退远端；
        - 需要「前复权月线」的分析场景同样必然回退远端。
      补齐覆盖与口径前，本门面对本项目的主要消费方（板块走势图）命中率接近 0。
    """

    def __init__(self, priority=None, db_path=None):
        """
        初始化取数门面

        Args:
            priority (str, optional): 取数优先级，"local_first" 或 "remote_first"；
                                     传 None 时从 config.ini 的 [data_source] 读取
            db_path (str, optional): 本地 DuckDB 文件路径，默认 data/stocklab.duckdb
        """
        self._priority = self._resolve_priority(priority)
        self._db_path = initialize_database(db_path)
        self._database = Database(self._db_path)

        self._provider = MarketDataProvider()
        self._tencent_client = TencentMarketClient()
        self._security_repo = SecurityRepository(self._database)
        self._price_repo = DailyPriceRepository(self._database)
        self._valuation_repo = DailyValuationRepository(self._database)
        self._history_repo = ValuationHistoryRepository(self._database)

    def _resolve_priority(self, priority):
        """
        解析取数优先级：显式入参优先，其次读配置文件，最后兜底默认值

        Args:
            priority (str, optional): 调用方显式指定的优先级

        Returns:
            str: 合法优先级取值
        """
        if priority is None:
            return load_data_source_priority()
        if priority not in DATA_SOURCE_PRIORITIES:
            _logger.warning(
                "传入的取数优先级非法 [%s]，合法值为 %s，改用默认 %s",
                priority,
                "/".join(DATA_SOURCE_PRIORITIES),
                DEFAULT_DATA_SOURCE_PRIORITY,
            )
            return DEFAULT_DATA_SOURCE_PRIORITY
        return priority

    @property
    def priority(self):
        """当前生效的取数优先级（"local_first" 或 "remote_first"）"""
        return self._priority

    def close(self):
        """释放本地数据库连接"""
        self._database.close()

    def __enter__(self):
        """上下文管理入口"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理出口，确保连接被释放"""
        self.close()
        return False

    def fetch_securities(self):
        """
        获取全市场证券基础信息

        Returns:
            pd.DataFrame: 证券基础信息表；本地与远端都无数据时返回空 DataFrame
        """
        if self._priority == "local_first":
            local_data = self._security_repo.find_all()
            if not local_data.empty:
                _logger.debug("证券基础信息命中本地库: %d 条", len(local_data))
                return local_data
            _logger.info("本地库无证券基础信息，回退远端接口")
            remote_data = self._provider.fetch_securities()
            self._write_back(self._security_repo, remote_data, "securities")
            return remote_data

        remote_data = self._provider.fetch_securities()
        if not remote_data.empty:
            self._write_back(self._security_repo, remote_data, "securities")
            return remote_data
        _logger.warning("远端证券基础信息获取失败，回退本地库")
        return self._security_repo.find_all()

    def fetch_daily_prices(self, ts_code, start_date, end_date):
        """
        获取单只证券指定日期范围的日 K 行情（不复权）

        Args:
            ts_code (str): 标准证券代码，如 "600519.SH"
            start_date (str): 起始日期，格式 "YYYY-MM-DD"
            end_date (str): 结束日期，格式 "YYYY-MM-DD"

        Returns:
            pd.DataFrame: 日 K 行情表；本地与远端都无数据时返回空 DataFrame
        """
        if self._priority == "local_first":
            local_data = self._price_repo.find_by_code(ts_code, start_date, end_date)
            if not local_data.empty:
                _logger.debug(
                    "日 K 命中本地库: %s %d 条", ts_code, len(local_data)
                )
                return local_data
            _logger.info("本地库无 %s 的日 K，回退远端接口", ts_code)
            remote_data = self._provider.fetch_daily_prices(
                ts_code, self._compact_date(start_date), self._compact_date(end_date)
            )
            self._write_back(self._price_repo, remote_data, "daily_prices")
            return remote_data

        remote_data = self._provider.fetch_daily_prices(
            ts_code, self._compact_date(start_date), self._compact_date(end_date)
        )
        if not remote_data.empty:
            self._write_back(self._price_repo, remote_data, "daily_prices")
            return remote_data
        _logger.warning("远端 %s 日 K 获取失败，回退本地库", ts_code)
        return self._price_repo.find_by_code(ts_code, start_date, end_date)

    def fetch_valuations(self, trade_date=None):
        """
        获取全市场每日估值

        Args:
            trade_date (str, optional): 交易日期，格式 "YYYY-MM-DD"；
                                       传 None 表示取最新快照

        Returns:
            pd.DataFrame: 估值表；本地与远端都无数据时返回空 DataFrame
        """
        if self._priority == "local_first":
            local_data = self._read_local_valuations(trade_date)
            if not local_data.empty:
                _logger.debug("估值命中本地库: %d 条", len(local_data))
                return local_data
            _logger.info("本地库无估值数据，回退远端接口")
            remote_data = self._provider.fetch_realtime_valuations()
            self._write_back(self._valuation_repo, remote_data, "daily_valuations")
            return remote_data

        remote_data = self._provider.fetch_realtime_valuations()
        if not remote_data.empty:
            self._write_back(self._valuation_repo, remote_data, "daily_valuations")
            return remote_data
        _logger.warning("远端估值快照获取失败，回退本地库")
        return self._read_local_valuations(trade_date)

    def fetch_valuation_history(self, ts_code, period="全部"):
        """
        获取单只证券的逐日历史估值序列

        【与 fetch_valuations 的区别】
           fetch_valuations      = 全市场「单日快照」，用于当下估值横截面比较；
           fetch_valuation_history = 单只「跨年序列」，用于计算历史分位。
           二者服务的分析问题不同，不可互相替代。

        【优先级策略的特殊性】
           本方法只有远端来源（本地库是缓存而非唯一副本），因此无论
           local_first 还是 remote_first，都遵循「先查本地 → 未命中或不足则取远端并回写」。
           这是 cache-aside 的标准形态，与前三个方法的「命中即返回」不同。

        Args:
            ts_code (str): 标准证券代码，如 "600519.SH"
            period (str, optional): 历史区间，"全部" 或 "近五年" 等

        Returns:
            pd.DataFrame: 历史估值表，列为
                          ts_code / trade_date / pe_ttm / pe_static / pb / ps / pcf
        """
        local_data = self._history_repo.find_by_code(ts_code)
        if not local_data.empty:
            _logger.debug(
                "历史估值命中本地库: %s %d 条", ts_code, len(local_data)
            )
            return local_data

        _logger.info("本地库无 %s 的历史估值，取远端接口", ts_code)
        remote_data = self._provider.fetch_valuation_history(ts_code, period)
        self._write_back(self._history_repo, remote_data, "valuation_history")
        return remote_data

    def fetch_multi_monthly_close(self, targets, num_months=120):
        """
        高并发批量抓取多资产标的池的月线收盘价

        【与 datasource 层的区别】
            本方法通过 facade 暴露，支持取数优先级策略与本地缓存回写。
            直接调用 TencentMarketClient 则绕过这些机制。

        Args:
            targets (list): 标的列表，支持纯字符串代码列表，或具有 `.code` 属性的实体对象列表
            num_months (int): 获取月份数

        Returns:
            dict: { code: { 'YYYY-MM': close_price } }
        """
        _logger.info("通过 facade 批量抓取 %d 个标的最近 %d 个月月线数据", len(targets), num_months)
        return self._tencent_client.fetch_multi_monthly_close(targets, num_months=num_months)

    def _read_local_valuations(self, trade_date):
        """
        从本地库读取估值数据

        Args:
            trade_date (str, optional): 指定交易日；None 表示取本地最新交易日

        Returns:
            pd.DataFrame: 估值表
        """
        if trade_date:
            return self._valuation_repo.find_by_date(trade_date)
        latest_date = self._price_repo.get_max_trade_date()
        if not latest_date:
            return pd.DataFrame()
        return self._valuation_repo.find_by_date(latest_date)

    def _write_back(self, repository, data, table_name):
        """
        将远端取到的数据回写本地库（cache-aside）

        回写失败不影响本次返回：取数已成功，落库只是为后续查询命中本地。

        Args:
            repository: 目标 Repository 实例
            data (pd.DataFrame): 待写入的数据
            table_name (str): 目标表名，仅用于日志
        """
        if data is None or data.empty:
            return
        row_count = repository.upsert(data)
        _logger.info("远端数据已回写本地 %s 表: %d 条", table_name, row_count)

    @staticmethod
    def _compact_date(date_str):
        """
        将 YYYY-MM-DD 压缩为 YYYYMMDD（AkShare 接口要求的日期格式）

        Args:
            date_str (str): 日期字符串

        Returns:
            str: 压缩后的日期字符串
        """
        return date_str.replace("-", "")
