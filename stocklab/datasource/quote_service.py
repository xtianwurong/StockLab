#!/usr/bin/env python3
"""
==============================================================================
StockLab - 单股行情服务 (stocklab.datasource.quote_service)
==============================================================================

【模块职责】
  本模块是单股维度的行情服务门面（对应 MarketService 的入库取数维度），
  负责 A 股单只股票行情的统一获取与数据清洗，完全独立于绘图逻辑：
    1. 统一数据契约：对外输出严格标准化格式的 pandas.DataFrame 与实时行情数据对象，
       屏蔽各数据源底层的通信协议与字段差异。
    2. 多数据源三通道容错：集成 AkShare（东方财富）、BaoStock（证券宝）以及 腾讯财经（直连 HTTP）
       三大异构通道，支持网络抖动重试及故障自动无缝降级。
    3. 异构数据对齐：针对估值指标（PE-TTM）接口只有日频的特性，提供统一的月末降采样与年月对齐算法。
    4. 独立复用性：不依赖 matplotlib，可作为独立的行情抓取库被外部其它分析脚本直接 import。

【模块结构（本文件为单股维度的对外服务门面）】
  - data_contract.py        : 统一数据契约（列名常量 + StockRealtimeQuote），此处 re-export
  - _sources/base.py        : 数据源抽象基类 StockDataSource（类似 C++ 抽象类）
  - _sources/akshare_source.py  : 东方财富主通道实现（基于 akshare）
  - _sources/baostock_source.py : 证券宝备用通道实现（基于 baostock，含 Socket 会话生命周期管理）
  - _sources/tencent_source.py  : 腾讯财经直连通道实现（基于 qt.gtimg.cn，极速实时行情与备用日 K）
  本文件保留取数参数类与三级降级调度编排（StockQuoteService），并 re-export 数据契约符号，
  保证对外公共接口与拆分前完全一致。

  注：浏览器 UA 运行时补丁（规避东财 WAF 反爬阻断）位于
      stocklab.common.http_client.install_browser_user_agent，由应用入口显式调用，
      本模块导入时不再产生任何全局副作用。

【依赖清单】
  - 标准库：logging（分级日志）
  - 第三方库：
      * pandas (>=2.0)     : 数据表格清洗、类型转换、时序重采样
  - 包内：stocklab.datasource.data_contract、stocklab.datasource._sources
==============================================================================
"""

import logging

import pandas as pd

from stocklab.datasource._sources import (
    AkShareDataSource,
    BaoStockDataSource,
    SinaDataSource,
    TdxDataSource,
    TencentDataSource,
)
from stocklab.datasource.data_contract import (
    CLOSE_PRICE_COLUMN,
    PE_TTM_COLUMN,
    TRADE_DATE_COLUMN,
    StockRealtimeQuote,
)

# 初始化模块级私有 Logger
_logger = logging.getLogger(__name__)

# ============================================================================
# 最小对外暴露清单 (__all__)
# ============================================================================
# Python 约定：只有列入 __all__ 的符号才被视作公共 API。
# 当外部使用 `from stocklab.datasource.quote_service import *` 时，仅有以下 6 个符号会被导入，
# 模块内的导入符号均保持接口的简洁与稳定性。
__all__ = [
    "TRADE_DATE_COLUMN",
    "CLOSE_PRICE_COLUMN",
    "PE_TTM_COLUMN",
    "StockRealtimeQuote",
    "StockDataFetchParams",
    "StockQuoteService",
]


# ============================================================================
# 取数参数类 (Fetch Parameters)
# ============================================================================
class StockDataFetchParams:
    """
    股票数据获取参数封装类

    【设计原则：参数对象模式 (Parameter Object Pattern)】
      将股票代码、复权方式、重试控制等零散入参收拢为单个对象。
      在后续接口升级新增控制项时，只需扩充本类属性，无需修改方法签名，保持 API 稳定性。
    """

    def __init__(self, stock_code="000001.SZ"):
        """
        初始化取数参数

        Args:
            stock_code (str): 股票代码（标准 A 股格式：深市后缀 .SZ，沪市后缀 .SH，如 "600519.SH"）
        """
        # 目标股票代码
        self.stock_code = stock_code
        # 复权类型：
        #   "qfq" -> 前复权（保持当前价格真实，历史价格向下折算，最常用）
        #   "hfq" -> 后复权（保持期初价格真实，历史价格向上累积，适合长线超额收益计算）
        #   ""    -> 不复权（原始除权除息价格）
        # 说明：在长牛分红大户（如茅台）的极端前复权数据中，早年折算价格可能为负，此时可改用 "hfq"
        self.adjust_type = "qfq"
        # 主数据源请求失败时的最大重试次数
        self.retry_count = 3
        # 每次重试等待间隔时间（秒）
        self.retry_interval_seconds = 3

    def set_stock_code(self, stock_code):
        """设置股票代码"""
        self.stock_code = stock_code

    def set_adjust_type(self, adjust_type):
        """设置复权方式 (qfq / hfq / 空字符串)"""
        self.adjust_type = adjust_type


# ============================================================================
# 取数编排调度器 (Fetch Orchestrator & Strategy Manager)
# ============================================================================
class StockQuoteService:
    """
    股票市场数据服务类 (Market Data Service)

    【职责划分】
      统筹调度五个异构数据源通道：
        1. 价格通道五级降级：主源 AkShare -> BaoStock -> 腾讯财经（近 3 年日线重采样）
           -> 新浪（日线重采样，约 4 年）-> 通达信（不复权月线，最后兜底）。
        2. PE 估值通道独立降级：AkShare（百度股市通）-> 降级 BaoStock（日线重采样）。
        3. 公司简称查询优化：优先直连腾讯（最轻最快，<100ms 且无风控）-> 降级 AkShare -> 降级 BaoStock
           （新浪 / 通达信不加入简称主链，避免行为变化）。
        4. 实时盘口行情三级降级：腾讯 -> 新浪 -> 通达信。
        5. 状态追踪：记录价格生效源标识 `used_source_name`，供上层日志展示。
    """

    def __init__(self):
        # 实例化五个异构数据源（TdxDataSource 构造不建立连接，取数时才短连接）
        self._akshare_source = AkShareDataSource()
        self._baostock_source = BaoStockDataSource()
        self._tencent_source = TencentDataSource()
        self._sina_source = SinaDataSource()
        self._tdx_source = TdxDataSource()
        # 记录本次价格查询实际命中并生效的数据源名称
        self.used_source_name = ""

    def fetch_monthly_close_prices(self, fetch_params):
        """
        统一获取月线收盘价（三级主备自动切换）

        Args:
            fetch_params (StockDataFetchParams): 取数参数配置

        Returns:
            pd.DataFrame: 包含 trade_date 与 close_price；全挂时返回空表
        """
        stock_code = fetch_params.stock_code
        adjust_type = fetch_params.adjust_type

        # 1. 优先尝试主数据源 AkShare (全量 30 年历史)
        price_data = self._akshare_source.fetch_monthly_close_prices(
            stock_code,
            adjust_type,
            fetch_params.retry_count,
            fetch_params.retry_interval_seconds,
        )
        if not price_data.empty:
            self.used_source_name = self._akshare_source.SOURCE_NAME
            return price_data

        _logger.info(
            "主数据源 %s 不可用，切换备用数据源 1: %s",
            self._akshare_source.SOURCE_NAME,
            self._baostock_source.SOURCE_NAME,
        )

        # 2. 自动降级尝试备用数据源 1: BaoStock (全量 30 年历史)
        price_data = self._baostock_source.fetch_monthly_close_prices(
            stock_code, adjust_type
        )
        if not price_data.empty:
            self.used_source_name = self._baostock_source.SOURCE_NAME
            return price_data

        _logger.info(
            "备用数据源 1 %s 不可用，切换备用数据源 2: %s",
            self._baostock_source.SOURCE_NAME,
            self._tencent_source.SOURCE_NAME,
        )

        # 3. 自动降级尝试备用数据源 2: 腾讯财经 (近 3~4 年日线降采样保底)
        price_data = self._tencent_source.fetch_monthly_close_prices(
            stock_code, adjust_type
        )
        if not price_data.empty:
            self.used_source_name = self._tencent_source.SOURCE_NAME
            return price_data

        _logger.info(
            "备用数据源 2 %s 不可用，切换备用数据源 3: %s",
            self._tencent_source.SOURCE_NAME,
            self._sina_source.SOURCE_NAME,
        )

        # 4. 自动降级尝试备用数据源 3: 新浪财经 (约 4 年日线降采样，独立上游)
        price_data = self._sina_source.fetch_monthly_close_prices(
            stock_code, adjust_type
        )
        if not price_data.empty:
            self.used_source_name = self._sina_source.SOURCE_NAME
            return price_data

        _logger.info(
            "备用数据源 3 %s 不可用，切换备用数据源 4: %s",
            self._sina_source.SOURCE_NAME,
            self._tdx_source.SOURCE_NAME,
        )

        # 5. 最后兜底: 通达信公开行情服务器 (仅不复权月线；无复权因子能力)
        price_data = self._tdx_source.fetch_monthly_close_prices(
            stock_code, adjust_type
        )
        if not price_data.empty:
            self.used_source_name = self._tdx_source.SOURCE_NAME
            return price_data

        _logger.error(
            "五个数据源均获取失败。建议：检查 VPN/代理软件；稍后重试；或检查网络连接"
        )
        return pd.DataFrame()

    def fetch_monthly_price_and_pe(self, fetch_params):
        """
        统一获取「价格 + PE-TTM」综合数据表（核心推荐入口）

        【调度逻辑】
          1. 调度价格三级降级流程，获取月线价格数据；
          2. 若价格完全取不到，直接终止流程返回空表；
          3. 若价格成功获取，并发起估值获取（估值独立进行主备降级）；
          4. 最终通过 `_merge_price_and_pe` 按“年月”维度完成时序匹配对齐。

        Args:
            fetch_params (StockDataFetchParams): 取数参数配置

        Returns:
            pd.DataFrame: 包含 trade_date, close_price, pe_ttm 三列的标准表
        """
        price_data = self.fetch_monthly_close_prices(fetch_params)
        if price_data.empty:
            return pd.DataFrame()

        # 独立尝试获取估值数据
        pe_data = self._fetch_pe_ttm_with_fallback(fetch_params.stock_code)
        # 将价格序列与估值序列按年月对齐合并
        return self._merge_price_and_pe(price_data, pe_data)

    def fetch_stock_name(self, stock_code):
        """
        查询股票公司简称（三级自动切换）

        【优先级考量】
          1. 优先尝试腾讯直连（单次 HTTP GET，延迟通常 < 100ms，无需登录握手，不占东财风控配额）
          2. 失败降级尝试 AkShare
          3. 再失败降级尝试 BaoStock

        Args:
            stock_code (str): 股票代码，如 "000001.SZ"

        Returns:
            str: 公司中文简称；均失败返回空字符串
        """
        # 1. 优先腾讯
        stock_name = self._tencent_source.fetch_stock_name(stock_code)
        if stock_name:
            return stock_name

        # 2. 备选 AkShare
        stock_name = self._akshare_source.fetch_stock_name(stock_code)
        if stock_name:
            return stock_name

        # 3. 兜底 BaoStock
        stock_name = self._baostock_source.fetch_stock_name(stock_code)
        if stock_name:
            return stock_name

        _logger.info("未查询到 %s 的公司名称，标题将只显示股票代码", stock_code)
        return ""

    def fetch_realtime_quote(self, stock_code):
        """
        统一获取单只股票的实时行情快照（三级降级：腾讯 -> 新浪 -> 通达信）

        【设计说明】
          1. 首选腾讯财经极速通道，单次请求即可获取最新成交价、昨收、今开、
             涨跌幅、盘口五档、成交量/额、换手率、动态 PE/PB 及市值；
          2. 腾讯失败时降级新浪（需 HTTPS + Referer，只提供价格/量额核心字段，
             换手率与 PE/PB/市值保持默认值）；
          3. 再失败降级通达信（TCP 直连，同样只提供核心字段）；
          4. 全部失败返回 None（保持既有行为，不返回错误默认数据）。

        Args:
            stock_code (str): 标准股票代码，如 "000001.SZ", "600519.SH"

        Returns:
            StockRealtimeQuote | None: 填充后的实时行情对象；获取失败返回 None
        """
        quote = self._tencent_source.fetch_realtime_quote(stock_code)
        if quote is not None:
            return quote

        _logger.info(
            "腾讯实时行情不可用，降级尝试 %s: %s",
            self._sina_source.SOURCE_NAME,
            stock_code,
        )
        quote = self._sina_source.fetch_realtime_quote(stock_code)
        if quote is not None:
            return quote

        _logger.info(
            "新浪实时行情不可用，降级尝试 %s: %s",
            self._tdx_source.SOURCE_NAME,
            stock_code,
        )
        quote = self._tdx_source.fetch_realtime_quote(stock_code)
        if quote is not None:
            return quote

        _logger.warning("未能获取 %s 的实时行情", stock_code)
        return None

    def _fetch_pe_ttm_with_fallback(self, stock_code):
        """内部调度：估值数据的主备通道尝试"""
        pe_data = self._akshare_source.fetch_monthly_pe_ttm(stock_code)
        if not pe_data.empty:
            return pe_data

        pe_data = self._baostock_source.fetch_monthly_pe_ttm(stock_code)
        if not pe_data.empty:
            return pe_data

        _logger.info("未获取到 %s 的 PE-TTM，将只绘制价格曲线", stock_code)
        return pd.DataFrame()

    def _merge_price_and_pe(self, price_data, pe_data):
        """
        核心时序对齐：按「年-月」自然周期合并价格序列与 PE 序列

        【为何不能直接基于具体日期 (trade_date) 合并？】
          - 价格来源于月线历史（由交易所定义的该月最后一个实际交易日）。
          - PE 来源于日线采样（也是该月最后一个交易日）。
          但在历史特定月份（如跨节假日停牌、两家数据源的节假日日历维护存在 1~2 天微小差异）时，
          具体日期可能无法严格相等。如果强行用精确日期做关联，会导致对应月份的 PE 变成 NaN。
          因此，本算法将双方的交易日统一转换为 `pd.Period(freq="M")`（如 "2026-08"），
          以月份作为唯一 Join Key 进行左连接 (left join)，保证价格与估值精确挂钩。

        Args:
            price_data (pd.DataFrame): 价格基准表
            pe_data (pd.DataFrame): 估值数据表

        Returns:
            pd.DataFrame: 包含 trade_date, close_price, pe_ttm 的综合表
        """
        merged = price_data.copy()

        # 若无估值数据，补齐整列 NaN，维持列结构一致性，便于绘图层统一判断
        if pe_data is None or pe_data.empty:
            merged[PE_TTM_COLUMN] = float("nan")
            return merged

        month_key = "_month_key"
        # 生成 年-月 临时对齐键
        merged[month_key] = merged[TRADE_DATE_COLUMN].dt.to_period("M")
        pe_frame = pe_data.copy()
        pe_frame[month_key] = pd.to_datetime(pe_frame[TRADE_DATE_COLUMN]).dt.to_period("M")

        # 基于月份执行左连接
        merged = pd.merge(
            merged, pe_frame[[month_key, PE_TTM_COLUMN]], on=month_key, how="left"
        )
        # 清除临时对齐辅助列
        merged = merged.drop(columns=[month_key])
        return merged
