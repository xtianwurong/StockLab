#!/usr/bin/env python3
"""
==============================================================================
StockLab - 股票市场数据服务模块 (stocklab.datasource.stock_data)
==============================================================================

【模块职责】
  本模块专职负责 A 股股票行情的统一获取与数据清洗，完全独立于绘图逻辑：
    1. 统一数据契约：对外输出严格标准化格式的 pandas.DataFrame 与实时行情数据对象，
       屏蔽各数据源底层的通信协议与字段差异。
    2. 多数据源三通道容错：集成 AkShare（东方财富）、BaoStock（证券宝）以及 腾讯财经（直连 HTTP）
       三大异构通道，支持网络抖动重试及故障自动无缝降级。
    3. 异构数据对齐：针对估值指标（PE-TTM）接口只有日频的特性，提供统一的月末降采样与年月对齐算法。
    4. 独立复用性：不依赖 matplotlib，可作为独立的行情抓取库被外部其它分析脚本直接 import。

【对外公共接口清单（由 __all__ 显式限定）】
  - 列名常量：
      * TRADE_DATE_COLUMN   : 交易日期（datetime 类型，对应各月最后一个交易日）
      * CLOSE_PRICE_COLUMN  : 收盘价（float 类型，必选字段）
      * PE_TTM_COLUMN       : 滚动市盈率 PE-TTM（float 类型，可选字段，缺失时整列为 NaN）
  - 数据模型与参数类：
      * StockRealtimeQuote  : 股票实时盘口行情数据快照对象
      * StockDataFetchParams: 历史取数参数（股票代码、复权类型、重试次数、重试间隔）
  - 核心服务类：
      * MarketDataService   : 统一数据服务入口（月线价格、月线价格+估值、公司简称查询、实时行情查询）

【内部设计与实现细节（私有保护，外部请勿直接依赖）】
  - _StockDataSource      : 数据源抽象基类（类似 C++ 抽象类，声明纯虚方法接口）
  - _AkShareDataSource    : 东方财富主通道实现（基于 akshare）
  - _BaoStockDataSource   : 证券宝备用通道实现（基于 baostock，含 Socket 会话生命周期管理）
  - _TencentDataSource    : 腾讯财经直连通道实现（基于 qt.gtimg.cn，极速实时行情与备用日 K）
  - _install_browser_user_agent: 浏览器 UA 运行时补丁（规避东财 WAF 反爬阻断）

【依赖清单】
  - 标准库：contextlib（重定向输出）、io（内存缓冲区）、logging（分级日志）、
            time（重试休眠）、datetime（时间解析）
  - 第三方库：
      * akshare (>=1.10)   : 主通道月线行情、百度股市通估值
      * baostock (>=0.8.8) : 备通道月线行情、日线估值、基础信息
      * pandas (>=2.0)     : 数据表格清洗、类型转换、时序重采样
      * requests (>=2.22)  : 底层 HTTP 通信及会话猴子补丁
==============================================================================
"""

import contextlib
import io
import logging
import time
from datetime import datetime

import akshare as ak
import baostock as bs
import pandas as pd
import requests

from stocklab.common.type_utils import safe_float, safe_int
from stocklab.datasource.tencent_client import TencentMarketClient

# 初始化模块级私有 Logger
_logger = logging.getLogger(__name__)

# ============================================================================
# 最小对外暴露清单 (__all__)
# ============================================================================
# Python 约定：只有列入 __all__ 的符号才被视作公共 API。
# 当外部使用 `from stocklab.datasource.stock_data import *` 时，仅有以下 6 个符号会被导入，
# 模块内以 '_' 开头的内部类与函数均被有效隐藏，保持接口的简洁与稳定性。
__all__ = [
    "TRADE_DATE_COLUMN",
    "CLOSE_PRICE_COLUMN",
    "PE_TTM_COLUMN",
    "StockRealtimeQuote",
    "StockDataFetchParams",
    "MarketDataService",
]

# ============================================================================
# 统一数据契约常量 (Unified Data Contract Constants)
# ============================================================================
# 所有数据源在完成抓取后，必须通过标准化转换输出只包含以下列名的 DataFrame：
#   - trade_date  : 交易日期（pd.Timestamp / datetime 类型）
#   - close_price : 月收盘价（float 类型）
#   - pe_ttm      : 月度滚动市盈率（float 类型，缺失为 NaN）
#
# 【为何使用常量而非魔法字符串？】
#   1. 编译期与运行时安全：若开发者拼错常量名（如 TRADE_DATE_COLUM），Python 会立即抛出
#      NameError，而不是在复杂的数据管道运行中途报隐蔽的 KeyError。
#   2. 单点维护与重构支持：未来若需变更字段名称，只需修改此处常量定义，业务调用层无需改动。
#   3. IDE 友好：现代 IDE 能够提供精准的代码自动补全与交叉跳转支持。
TRADE_DATE_COLUMN = "trade_date"
CLOSE_PRICE_COLUMN = "close_price"
PE_TTM_COLUMN = "pe_ttm"


# ============================================================================
# 通用数据类型安全转换辅助函数（下沉至 type_utils.py，在此保留别名兼容内部调用）
# ============================================================================
_safe_float = safe_float
_safe_int = safe_int


# ============================================================================
# 实时行情数据对象 (Real-time Quote Model)
# ============================================================================
class StockRealtimeQuote:
    """
    股票实时行情快照数据类

    【C++ 概念映射：简单实体类 / 结构体 (Plain Old Data Class)】
      字段全带默认初值，无隐藏的元类或装饰器黑魔法，属性直观、易于调试打印与序列化。
    """

    def __init__(self, stock_code=""):
        self.stock_code = stock_code              # 股票代码，如 "000001.SZ"
        self.stock_name = ""                      # 公司简称，如 "平安银行"
        self.source_name = ""                     # 数据源标识，如 "tencent(腾讯财经)"
        self.current_price = 0.0                  # 最新价格 (元)
        self.yesterday_close = 0.0                # 昨日收盘价 (元)
        self.today_open = 0.0                     # 今日开盘价 (元)
        self.highest_price = 0.0                  # 今日最高价 (元)
        self.lowest_price = 0.0                   # 今日最低价 (元)
        self.change_amount = 0.0                  # 涨跌额 (元)
        self.change_percent = 0.0                 # 涨跌幅 (%)
        self.volume_shares = 0                    # 成交量 (股，与日线历史成交量口径一致)
        self.amount_yuan = 0.0                    # 成交额 (元)
        self.turnover_rate = 0.0                  # 换手率 (%)
        self.pe_ttm = None                        # 动态市盈率 PE-TTM (可为 None)
        self.pb_ratio = None                      # 市净率 PB (可为 None)
        self.total_market_value = 0.0             # 总市值 (元)
        self.circulating_market_value = 0.0       # 流通市值 (元)
        self.quote_time = ""                      # 行情时间字符串，如 "2026-09-30 16:15:00"

    def to_dict(self):
        """转为字典格式，便于外部模块做 JSON 序列化或数据加工"""
        return {
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "source_name": self.source_name,
            "current_price": self.current_price,
            "yesterday_close": self.yesterday_close,
            "today_open": self.today_open,
            "highest_price": self.highest_price,
            "lowest_price": self.lowest_price,
            "change_amount": self.change_amount,
            "change_percent": self.change_percent,
            "volume_shares": self.volume_shares,
            "amount_yuan": self.amount_yuan,
            "turnover_rate": self.turnover_rate,
            "pe_ttm": self.pe_ttm,
            "pb_ratio": self.pb_ratio,
            "total_market_value": self.total_market_value,
            "circulating_market_value": self.circulating_market_value,
            "quote_time": self.quote_time,
        }

    def __repr__(self):
        """控制台及日志友好的字符串表述"""
        pe_str = f"{self.pe_ttm:.2f}" if self.pe_ttm is not None else "None"
        return (
            f"StockRealtimeQuote({self.stock_code} {self.stock_name} "
            f"现价:{self.current_price:.2f} 涨跌:{self.change_percent:+.2f}% "
            f"PE-TTM:{pe_str} 时间:{self.quote_time})"
        )


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
# 数据源抽象基类 (Abstract Data Source Base Class)
# ============================================================================
class _StockDataSource:
    """
    数据源抽象基类（模块内部使用）

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


# ============================================================================
# 主通道数据源实现：AkShare（东方财富底层）
# ============================================================================
class _AkShareDataSource(_StockDataSource):
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


# ============================================================================
# 备用通道数据源实现：BaoStock（证券宝）
# ============================================================================
class _BaoStockDataSource(_StockDataSource):
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


# ============================================================================
# 腾讯财经通道数据源实现：Tencent（直连 HTTP 实时行情与日线）
# ============================================================================
class _TencentDataSource(_StockDataSource):
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
        quote.current_price = _safe_float(fields[3], 0.0)
        quote.yesterday_close = _safe_float(fields[4], 0.0)
        quote.today_open = _safe_float(fields[5], 0.0)
        quote.highest_price = _safe_float(fields[33], 0.0)
        quote.lowest_price = _safe_float(fields[34], 0.0)
        quote.change_amount = _safe_float(fields[31], 0.0)
        quote.change_percent = _safe_float(fields[32], 0.0)

        # 成交量换算（科创板 688xxx 字段 6 本身即为股，普通 A 股字段 6 为手，乘 100 换算为股）
        raw_vol = _safe_int(fields[6], 0)
        is_star = stock_code.startswith("688") or ".688" in stock_code
        quote.volume_shares = raw_vol if is_star else raw_vol * 100

        # 成交额解析：优先提取 fields[35] 中的精确元金额，备选 fields[37]（万元）
        amount_val = None
        if len(fields) > 35 and "/" in fields[35]:
            parts = fields[35].split("/")
            if len(parts) >= 3:
                amount_val = _safe_float(parts[2])
        if amount_val is None and len(fields) > 37:
            wan_val = _safe_float(fields[37])
            if wan_val is not None:
                amount_val = wan_val * 10000.0
        quote.amount_yuan = amount_val if amount_val is not None else 0.0

        quote.turnover_rate = _safe_float(fields[38], 0.0)
        quote.pe_ttm = _safe_float(fields[39])
        quote.pb_ratio = _safe_float(fields[46])

        # 市值换算（单位：亿元 -> 元）
        circ_mv_yi = _safe_float(fields[44])
        quote.circulating_market_value = circ_mv_yi * 100000000.0 if circ_mv_yi else 0.0
        total_mv_yi = _safe_float(fields[45])
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


# ============================================================================
# 取数编排调度器 (Fetch Orchestrator & Strategy Manager)
# ============================================================================
class MarketDataService:
    """
    股票市场数据服务类 (Market Data Service)

    【职责划分】
      统筹调度 AkShare、BaoStock 与 腾讯财经 三大数据源通道：
        1. 价格通道三级降级：优先主源 AkShare -> 降级 BaoStock -> 降级腾讯财经（近 3 年日线重采样）。
        2. PE 估值通道独立降级：AkShare（百度股市通）-> 降级 BaoStock（日线重采样）。
        3. 公司简称查询优化：优先直连腾讯（最轻最快，<100ms 且无风控）-> 降级 AkShare -> 降级 BaoStock。
        4. 实时盘口行情直通：直连腾讯获取最新现价、涨跌幅、量比、换手率、动态 PE/PB 及市值。
        5. 状态追踪：记录价格生效源标识 `used_source_name`，供上层日志展示。
    """

    def __init__(self):
        # 实例化三大异构数据源
        self._akshare_source = _AkShareDataSource()
        self._baostock_source = _BaoStockDataSource()
        self._tencent_source = _TencentDataSource()
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

        _logger.error(
            "三个数据源均获取失败。建议：检查 VPN/代理软件；稍后重试；或检查网络连接"
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
        统一获取单只股票的实时行情快照

        【设计说明】
          利用腾讯财经极速行情通道，单次请求即可获取最新成交价、昨收、今开、
          涨跌幅、盘口五档、成交量/额、换手率、动态 PE/PB 及市值。

        Args:
            stock_code (str): 标准股票代码，如 "000001.SZ", "600519.SH"

        Returns:
            StockRealtimeQuote | None: 填充后的实时行情对象；获取失败返回 None
        """
        quote = self._tencent_source.fetch_realtime_quote(stock_code)
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


# ============================================================================
# 浏览器 User-Agent 运行时补丁 (Monkey Patch)
# ============================================================================
# 【问题根因】
#   东方财富底层服务对调用方有反爬安全策略。如果请求头中为 Python requests 库的默认
#   User-Agent（如 "python-requests/2.31.0"），东财网关会立即执行 TCP Reset / 断连
#   （客户端抛出 RemoteDisconnected / Connection reset by peer 异常）。
#
# 【C++ 类似实现映射】
#   本操作相当于在运行时 Hook 或替换虚拟函数指针表（虚表 Hook / 函数拦截器）。
#   在模块导入时，拦截 requests.Session.request 方法，为其默认补齐真实的 Mac Chrome 浏览器 UA。
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _install_browser_user_agent():
    """在运行时为 requests 全局 Session.request 挂载浏览器 User-Agent 补丁"""
    original_session_request = requests.Session.request

    def session_request_with_browser_user_agent(self, method, url, **kwargs):
        headers = kwargs.get("headers")
        if headers is None:
            headers = {}
        # 仅当调用方未显式传递 User-Agent 时才赋默认值，不覆盖显式入参
        headers.setdefault("User-Agent", _BROWSER_USER_AGENT)
        kwargs["headers"] = headers
        return original_session_request(self, method, url, **kwargs)

    # 替换函数绑定
    requests.Session.request = session_request_with_browser_user_agent


# 模块被初次加载 (import) 时自动执行挂载
_install_browser_user_agent()
