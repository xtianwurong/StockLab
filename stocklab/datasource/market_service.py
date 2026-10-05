#!/usr/bin/env python3
"""
==============================================================================
StockLab - 入库取数模块 (stocklab.datasource.market_service)
==============================================================================

【模块职责】
   从外部数据源取数，并经 stocklab.normalization.akshare 归一化为
   **领域契约 DataFrame**（列名与列序固定，见 stocklab.domain）。
   归一化前是源列结构、归一化后是契约结构，Repository 写入时按契约显式列名写入，
   因此数据源改列序/改列名不会再影响落库正确性。
   7 个公开 fetch_* 方法与 7 张库表一一对应，调用方仅两个：
     - app/scripts/sync_market_data.py（入库同步编排）
     - stocklab.facade.MarketDataFacade（远端分支，Cache-Aside 回写本地库）

【粒度说明（注意：并非全为「全市场批量」）】
   - 全市场一次返回：fetch_securities / fetch_realtime_valuations
   - 单股逐只：      fetch_daily_prices / fetch_valuation_history / fetch_company_profile
   - 行业横截面：    fetch_industry_valuation
   - 单指数成分：    fetch_index_membership

【数据源策略】（直连 akshare 各接口，自带重试与限流）
   - 东方财富：证券名录、日 K、全市场估值快照
   - 百度股市通：单股历史估值序列
   - 巨潮资讯：行业估值横截面、公司概况
   - 中证官网：指数成分

【唯一的例外：日 K 三源回退】
   日 K 是本模块唯一「按单只反复请求」的接口，归因、同步、远端回写都要逐股拉取，
   东财单点不可达（反爬、网络抖动）会让整条链路静默返回空表——调用方看到的不是
   「源挂了」而是「这只股票没数据」，是最难排查的假阴性。因此 fetch_daily_prices
   在方法内部按 东财 → 腾讯 → 新浪 顺序回退，三个源最终都走同一个
   normalize_daily_prices，输出契约完全一致，调用方无感知。
   其余接口都是「一次拿全市场」的批量拉取，源不可达时整批为空、由调用方显式中止，
   不做源级回退，避免为了降级把不同源的口径混进同一张表。

【设计原则】
   - 契约归一化：源列 → normalizer → 领域契约列序，Repository 显式列名写入
   - 失败降级：取数失败记录日志并返回空 DataFrame，由调用方决定是否中止
   - 源列改版（DataContractError）不可重试：记 ERROR 并直接返回空帧，绝不带病写库
"""

import logging
import time

import akshare as ak
import pandas as pd

from stocklab.domain import DataContractError
from stocklab.normalization.akshare import (
    normalize_company_profile,
    normalize_daily_prices,
    normalize_daily_valuations,
    normalize_index_membership,
    normalize_industry_valuation,
    normalize_securities,
    normalize_valuation_history,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "MarketService",
]

# ── 日 K 三源回退（见模块头部【唯一的例外】）────────────────────────────
_KLINE_SOURCES = ("eastmoney", "tencent", "sina")

# 腾讯/新浪返回英文列名，先改名成东财列名，才能复用同一个归一化器
_KLINE_EN_TO_CN = {
    "date": "日期",
    "open": "开盘",
    "high": "最高",
    "low": "最低",
    "close": "收盘",
    "volume": "成交量",
    "amount": "成交额",
}

# 成交量口径：东财 kline 为「手」，腾讯（其接口文档明示 volume 统一为股）与新浪为「股」。
# 同一张 market.daily_prices 不能混两种单位，备用源统一 ÷100 换算为手。
_SHARES_PER_HAND = 100


def _prefixed_symbol(ts_code: str) -> str:
    """600000.SH -> sh600000（腾讯与新浪要求代码带市场前缀）"""
    code, _, market = ts_code.partition(".")
    return f"{market.lower()}{code}" if market else code


def _adapt_kline_frame(raw, source):
    """把备用源的列名与成交量口径对齐东财，供 normalize_daily_prices 统一处理"""
    if source == "eastmoney":
        return raw
    frame = raw.rename(columns=_KLINE_EN_TO_CN)
    if "成交量" in frame.columns:
        frame = frame.copy()
        frame["成交量"] = pd.to_numeric(frame["成交量"], errors="coerce") / _SHARES_PER_HAND
    return frame


class MarketService:
    """
    入库取数类：外部数据源 → 归一化后的领域契约 DataFrame

    【职责】按粒度分四组，共 7 个方法：
      1. 全市场一次返回：fetch_securities（证券名录）、fetch_realtime_valuations（估值快照）
      2. 单股逐只：      fetch_daily_prices（日K）、fetch_valuation_history（历史估值）、
                         fetch_company_profile（公司概况补列）
      3. 行业横截面：    fetch_industry_valuation（某时点全部行业估值）
      4. 单指数成分：    fetch_index_membership（某指数全部成分）
    """

    def __init__(self, retry_count=2, retry_interval_seconds=2, interval_seconds=0.2):
        """
        初始化入库取数服务

        Args:
            retry_count (int, optional): 失败重试次数
            retry_interval_seconds (int, optional): 重试间隔秒数
            interval_seconds (float, optional): 同一数据源的两次调用之间的最小间隔，
                                               用于规避巨潮等接口的高频限流
        """
        self._retry_count = retry_count
        self._retry_interval_seconds = retry_interval_seconds
        self._interval_seconds = interval_seconds

    def fetch_securities(self):
        """
        获取全市场股票基础信息

        Returns:
            pd.DataFrame: 证券基础信息表（SECURITY_COLUMNS 契约列序）；
                          失败或源列不匹配时返回空表
        """
        for attempt in range(1, self._retry_count + 1):
            try:
                raw = ak.stock_info_a_code_name()
                if raw is None or raw.empty:
                    _logger.warning("stock_info_a_code_name 返回空数据")
                    return pd.DataFrame()

                result = normalize_securities(raw)
                _logger.info("获取全市场股票基础信息: %d 只", len(result))
                return result
            except DataContractError as error:
                # 源列改版不可重试：拒绝产出缺列数据
                _logger.error("证券名录数据契约违约，本批次不入库: %s", error)
                return pd.DataFrame()
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

        按 东财 → 腾讯 → 新浪 顺序回退，任一源返回数据即停；三源同契约。

        Args:
            ts_code (str): 证券代码，如 "600519.SH"
            start_date (str): 起始日期，格式 "YYYYMMDD"
            end_date (str): 结束日期，格式 "YYYYMMDD"

        Returns:
            pd.DataFrame: 日 K 行情表（DAILY_PRICE_COLUMNS 契约列序）；
                          三源均无数据或均失败时返回空表
        """
        symbol = ts_code.split(".")[0]
        prefixed = _prefixed_symbol(ts_code)

        for source in _KLINE_SOURCES:
            frame = self._fetch_kline_from(
                source, symbol, prefixed, ts_code, start_date, end_date
            )
            if not frame.empty:
                return frame

        _logger.warning(
            "[%s] 日 K 三源均无数据 %s~%s", ts_code, start_date, end_date
        )
        return pd.DataFrame()

    def _fetch_kline_from(self, source, symbol, prefixed, ts_code, start_date, end_date):
        """
        单源日 K 拉取（带重试）；失败或源列改版返回空表，交由调用方切下一源

        与原单源实现的两处差异：
          1. DataContractError 不再是终点——列改版只代表这个源坏了，换源仍可能取到；
          2. 重试耗尽只记 WARNING，因为后面还有备用源，只有三源全挂才值得 ERROR。
        """
        for attempt in range(1, self._retry_count + 1):
            try:
                raw = self._call_kline_source(
                    source, symbol, prefixed, start_date, end_date
                )
                if raw is None or raw.empty:
                    return pd.DataFrame()

                return normalize_daily_prices(_adapt_kline_frame(raw, source), ts_code)
            except DataContractError as error:
                _logger.warning(
                    "[%s] 源 %s 日 K 列结构变更，改用下一数据源: %s",
                    ts_code, source, error,
                )
                return pd.DataFrame()
            except Exception as error:
                _logger.debug(
                    "%s [%s] 第 %d/%d 次失败: %s",
                    source, ts_code, attempt, self._retry_count, error,
                )
                if attempt < self._retry_count:
                    time.sleep(self._retry_interval_seconds)

        _logger.warning(
            "[%s] 源 %s 连续 %d 次失败，切换下一数据源",
            ts_code, source, self._retry_count,
        )
        return pd.DataFrame()

    @staticmethod
    def _call_kline_source(source, symbol, prefixed, start_date, end_date):
        """按源名调用对应 akshare 接口（三源入参不同，集中在此处分支）"""
        if source == "tencent":
            return ak.stock_zh_a_hist_tx(
                symbol=prefixed, start_date=start_date, end_date=end_date, adjust=""
            )
        if source == "sina":
            return ak.stock_zh_a_daily(
                symbol=prefixed, start_date=start_date, end_date=end_date, adjust=""
            )
        return ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="",
        )

    def fetch_realtime_valuations(self):
        """
        获取最新全市场估值快照（东方财富实时行情）

        Returns:
            pd.DataFrame: 全市场估值表（DAILY_VALUATION_COLUMNS 契约列序）；
                          失败或源列不匹配时返回空表
        """
        for attempt in range(1, self._retry_count + 1):
            try:
                raw = ak.stock_zh_a_spot_em()
                if raw is None or raw.empty:
                    _logger.warning("stock_zh_a_spot_em 返回空数据")
                    return pd.DataFrame()

                result = normalize_daily_valuations(raw, pd.Timestamp.now().date())
                _logger.info("获取全市场估值快照: %d 只", len(result))
                return result
            except DataContractError as error:
                _logger.error("估值快照数据契约违约，本批次不入库: %s", error)
                return pd.DataFrame()
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
            pd.DataFrame: 历史估值表（VALUATION_HISTORY_COLUMNS 契约列序）；
                          单个指标取不到时整列为 NaN，不省略列
        """
        # 百度接口接受 6 位纯数字代码；各指标可用性不一致，逐指标单独取，
        # 单个指标失败不影响其余指标（缺口整列写 NULL，绝不省略列）
        indicator_map = [
            ("pe_ttm", "市盈率(TTM)"),
            ("pe_static", "市盈率(静)"),
            ("pb", "市净率"),
            ("ps", "市销率"),
            ("pcf", "市现率"),
        ]

        symbol = ts_code.split(".")[0]

        indicator_frames = {}
        for column_name, indicator in indicator_map:
            dates, values = self._fetch_baidu_indicator_frame(symbol, indicator, period)
            if dates.empty:
                indicator_frames[column_name] = pd.DataFrame(
                    columns=["trade_date", column_name]
                )
            else:
                indicator_frames[column_name] = pd.DataFrame(
                    {"trade_date": dates, column_name: values}
                )

        # PE-TTM 与 PB 是「有无估值数据」的最低判据（ps / pcf 该接口本就不提供）
        if indicator_frames["pe_ttm"].empty and indicator_frames["pb"].empty:
            _logger.warning("[%s] 未返回任何历史估值数据", ts_code)
            return pd.DataFrame()

        try:
            result = normalize_valuation_history(indicator_frames, ts_code)
        except DataContractError as error:
            _logger.error("[%s] 历史估值数据契约违约: %s", ts_code, error)
            return pd.DataFrame()
        if result.empty:
            return pd.DataFrame()

        _logger.info(
            "获取 %s 历史估值序列: %d 个交易日（%s ~ %s）",
            ts_code,
            len(result),
            result["trade_date"].iloc[0],
            result["trade_date"].iloc[-1],
        )
        return result

    def fetch_industry_valuation(self, stat_date, classification="国证行业分类"):
        """
        获取某个时点的行业估值横截面（巨潮资讯）

        【用途】
           个股估值分位只能回答「相对自己历史上贵不贵」，回答不了「相对同行业贵不贵」。
           本方法提供行业级估值，用于定位价值洼地板块与同业比较。

        【为何同时取三个 PE 口径】
           加权平均 PE 反映龙头主导的估值；中位数 PE 反映「典型公司」的估值，
           抵御极端值干扰。三者差异本身即信息：加权远低于中位数，
           说明估值集中在少数权重股上，此时只看单一口径会误判洼地程度。

        【已知数据源边界】
           - 该接口**不提供市净率（PB）**，故本表无 PB 字段；
           - 历史仅可回溯至 2023 年（实测更早日期抛 ValueError）；
           - 支持任意有效日期，不限于季末。

        Args:
            stat_date (str): 统计日期，YYYY-MM-DD 或 YYYYMMDD 均可
                             （巨潮接口内部按 date[:4]+date[4:6]+date[6:] 拼接，
                              只认 YYYYMMDD，故此处统一转换）
            classification (str, optional): 行业分类体系，
                                            "国证行业分类"（293 个，4 层）
                                            或 "证监会行业分类"（120 个，2 层）

        Returns:
            pd.DataFrame: 行业估值表，含 industry_code / stat_date / classification /
                          industry_level / industry_name / company_count /
                          priced_company_count / total_market_value / net_profit /
                          pe_weighted / pe_median / pe_arithmetic
        """
        cninfo_date = pd.to_datetime(stat_date).strftime("%Y%m%d")
        raw = pd.DataFrame()
        for attempt in range(1, self._retry_count + 1):
            try:
                fetched = ak.stock_industry_pe_ratio_cninfo(
                    symbol=classification, date=cninfo_date
                )
                if fetched is None or fetched.empty:
                    _logger.warning(
                        "行业估值 [%s @ %s] 返回空数据", classification, stat_date
                    )
                    return pd.DataFrame()
                raw = fetched
                break
            except Exception as error:
                _logger.debug(
                    "stock_industry_pe_ratio_cninfo [%s @ %s] 第 %d/%d 次失败: %s",
                    classification, stat_date, attempt, self._retry_count, error,
                )
                if attempt < self._retry_count:
                    time.sleep(self._retry_interval_seconds)

        if raw.empty:
            return pd.DataFrame()

        try:
            result = normalize_industry_valuation(raw, stat_date, classification)
        except DataContractError as error:
            _logger.error(
                "行业估值 [%s @ %s] 数据契约违约，本批次不入库: %s",
                classification, stat_date, error,
            )
            return pd.DataFrame()

        _logger.info(
            "获取行业估值 [%s @ %s]: %d 个行业（%d 个一级）",
            classification, stat_date, len(result),
            int((result["industry_level"] == 1).sum()),
        )
        return result

    def fetch_index_membership(self, index_code):
        """
        获取指数成分股（中证指数官网）

        【用途】
           在行业分类数据不可用时，指数成分可作为「同业分组」的近似维度：
             - 同指数成分股构成可比样本池；
             - 低估值可做指数内横向比较，替代「行业平均估值」；
             - 全市场预筛时先在指数内缩样本。

        【为何用中证官网】
           一次请求即返回全量成分（含成分券代码、名称、交易所、生效日期），
           无需逐只拼接，且为指数编制机构官方口径。

        Args:
            index_code (str): 指数代码，如 "000300"（沪深300）

        Returns:
            pd.DataFrame: 指数成分表，列为
                          ts_code / index_code / index_name / effective_date
        """
        raw = pd.DataFrame()
        for attempt in range(1, self._retry_count + 1):
            try:
                fetched = ak.index_stock_cons_csindex(symbol=index_code)
                if fetched is None or fetched.empty:
                    _logger.warning("指数 [%s] 未返回成分数据", index_code)
                    return pd.DataFrame()
                raw = fetched
                break
            except Exception as error:
                _logger.debug(
                    "index_stock_cons_csindex [%s] 第 %d/%d 次失败: %s",
                    index_code, attempt, self._retry_count, error,
                )
                if attempt < self._retry_count:
                    time.sleep(self._retry_interval_seconds)

        if raw.empty:
            return pd.DataFrame()

        try:
            result = normalize_index_membership(raw)
        except DataContractError as error:
            _logger.error(
                "指数 [%s] 成分数据契约违约，本批次不入库: %s", index_code, error
            )
            return pd.DataFrame()

        _logger.info(
            "获取指数 [%s] %s 成分: %d 只（生效日 %s）",
            result["index_code"].iloc[0],
            result["index_name"].iloc[0],
            len(result),
            result["effective_date"].iloc[0],
        )
        return result

    def fetch_company_profile(self, ts_code):
        """
        获取个股公司概况（巨潮资讯）

        【为何用它补全证券基础信息】
           reference.securities 的 industry / area / list_date 三个字段
           建表时留空（原数据源不提供）。本接口一次请求即可补齐：
             - 所属行业  -> industry（分组做同业比较的基础）
             - 上市日期  -> list_date（上市时长 / 次新股识别）
             - 入选指数  -> index_membership（沪深300 / 上证50 等，
                            可作为「同业」的近似分组维度）
           巨潮为官方披露平台，不走东财风控，实测单只约 0.2 秒、无封禁。

        Args:
            ts_code (str): 证券代码，如 "600519.SH"

        Returns:
            pd.DataFrame: 含 ts_code / industry / list_date / index_membership
                          的单行表；取不到时返回空表
        """
        symbol = ts_code.split(".")[0]

        raw = pd.DataFrame()
        for attempt in range(1, self._retry_count + 1):
            try:
                # 巨潮对高频请求会静默限流（返回 HTTP 200 但数据为空），
                # 因此每次调用之间保持固定间隔，并对空结果也退避重试
                if attempt > 1:
                    time.sleep(self._interval_seconds)
                    time.sleep(self._retry_interval_seconds)

                fetched = ak.stock_profile_cninfo(symbol=symbol)
                if fetched is not None and not fetched.empty:
                    raw = fetched
                    break
                _logger.debug(
                    "stock_profile_cninfo [%s] 返回空数据（第 %d/%d 次）",
                    symbol, attempt, self._retry_count,
                )
            except Exception as error:
                _logger.debug(
                    "stock_profile_cninfo [%s] 第 %d/%d 次失败: %s",
                    symbol, attempt, self._retry_count, error,
                )
                if attempt < self._retry_count:
                    time.sleep(self._retry_interval_seconds)

        if raw.empty:
            _logger.warning("[%s] 公司概况不可用（巨潮限流或该标的未收录）", ts_code)
            return pd.DataFrame()

        return normalize_company_profile(raw, ts_code)

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
