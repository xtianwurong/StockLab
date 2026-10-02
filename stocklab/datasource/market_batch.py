#!/usr/bin/env python3
"""
==============================================================================
StockLab - 入库取数模块 (stocklab.datasource.market_batch)
==============================================================================

【模块职责】
   从外部数据源取数，输出与本地 DuckDB 表**严格同名同序**的 pandas DataFrame，
   供 Repository 的 UPSERT 按位置匹配写入（列序即契约，改任一侧必须同步改另一侧）。
   7 个公开 fetch_* 方法与 7 张库表一一对应，调用方仅两个：
     - app/scripts/sync_market_data.py（入库同步编排）
     - stocklab.facade.MarketDataFacade（远端分支，Cache-Aside 回写本地库）

【粒度说明（注意：并非全为「全市场批量」）】
   - 全市场一次返回：fetch_securities / fetch_realtime_valuations
   - 单股逐只：      fetch_daily_prices / fetch_valuation_history / fetch_company_profile
   - 行业横截面：    fetch_industry_valuation
   - 单指数成分：    fetch_index_membership

【数据源策略】（直连 akshare 各接口，自带重试与限流，不做 _sources 三通道降级）
   - 东方财富：证券名录、日 K、全市场估值快照
   - 百度股市通：单股历史估值序列
   - 巨潮资讯：行业估值横截面、公司概况
   - 中证官网：指数成分

【设计原则】
   - 列序即契约：输出列与库表严格同名同序，UPSERT SELECT * 按位置匹配
   - 失败降级：取数失败记录日志并返回空 DataFrame，由调用方决定是否中止
"""

import logging
import time

import akshare as ak
import pandas as pd

_logger = logging.getLogger(__name__)

__all__ = [
    "MarketBatchProvider",
]


class MarketBatchProvider:
    """
    入库取数类：外部数据源 → 与库表同名同序的 DataFrame

    【职责】按粒度分四组，共 7 个方法：
      1. 全市场一次返回：fetch_securities（证券名录）、fetch_realtime_valuations（估值快照）
      2. 单股逐只：      fetch_daily_prices（日K）、fetch_valuation_history（历史估值）、
                         fetch_company_profile（公司概况补列）
      3. 行业横截面：    fetch_industry_valuation（某时点全部行业估值）
      4. 单指数成分：    fetch_index_membership（某指数全部成分）

    【命名备注】类名中的 "MarketBatch" 为历史遗留（初版仅全市场批量两个方法），
      现实际语义是「表同构入库取数」，如需更名建议 TableFetcher，由调用方统一决策。
    """

    def __init__(self, retry_count=2, retry_interval_seconds=2, interval_seconds=0.2):
        """
        初始化全市场批量 Provider

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
            stat_date (str): 统计日期 YYYY-MM-DD
            classification (str, optional): 行业分类体系，
                                            "国证行业分类"（293 个，4 层）
                                            或 "证监会行业分类"（120 个，2 层）

        Returns:
            pd.DataFrame: 行业估值表，含 industry_code / stat_date / classification /
                          industry_level / industry_name / company_count /
                          priced_company_count / total_market_value / net_profit /
                          pe_weighted / pe_median / pe_arithmetic
        """
        raw = pd.DataFrame()
        for attempt in range(1, self._retry_count + 1):
            try:
                fetched = ak.stock_industry_pe_ratio_cninfo(
                    symbol=classification, date=stat_date
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

        result = pd.DataFrame()
        result["industry_code"] = raw["行业编码"].astype(str)
        result["stat_date"] = pd.to_datetime(stat_date).date()
        result["classification"] = classification
        result["industry_level"] = pd.to_numeric(
            raw["行业层级"], errors="coerce"
        ).astype("Int64")
        result["industry_name"] = raw["行业名称"].astype(str).str.strip()
        result["company_count"] = pd.to_numeric(raw["公司数量"], errors="coerce")
        result["priced_company_count"] = pd.to_numeric(
            raw["纳入计算公司数量"], errors="coerce"
        )
        result["total_market_value"] = self._numeric_or_nan(raw["总市值-静态"])
        result["net_profit"] = self._numeric_or_nan(raw["净利润-静态"])
        result["pe_weighted"] = self._numeric_or_nan(raw["静态市盈率-加权平均"])
        result["pe_median"] = self._numeric_or_nan(raw["静态市盈率-中位数"])
        result["pe_arithmetic"] = self._numeric_or_nan(raw["静态市盈率-算术平均"])

        _logger.info(
            "获取行业估值 [%s @ %s]: %d 个行业（%d 个一级）",
            classification, stat_date, len(result),
            int((result["industry_level"] == 1).sum()),
        )
        return result

    def _numeric_or_nan(self, series):
        """
        安全转数值；列缺失或无法解析时返回全 NaN

        【为何不省略列】
           UPSERT 按位置匹配列序，省略列会导致整体写入失败。
           数据源不提供的列必须保留并写 NULL。

        Args:
            series (pd.Series): 原始列

        Returns:
            pd.Series: 数值列
        """
        if series is None:
            return float("nan")
        return pd.to_numeric(series, errors="coerce")

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

        result = pd.DataFrame()
        # _normalize_ts_code 是标量函数，对 Series 需逐元素调用
        constituent_codes = raw["成分券代码"].astype(str).str.zfill(6)
        result["ts_code"] = constituent_codes.apply(self._normalize_ts_code)
        result["index_code"] = str(raw["指数代码"].iloc[0])
        result["index_name"] = str(raw["指数名称"].iloc[0])
        result["effective_date"] = pd.to_datetime(raw["日期"]).dt.date

        # 同一次返回里生效日期一致，去重后写入
        result = result.drop_duplicates(subset=["ts_code", "index_code", "effective_date"])

        _logger.info(
            "获取指数 [%s] %s 成分: %d 只（生效日 %s）",
            result["index_code"].iloc[0],
            result["index_name"].iloc[0],
            len(result),
            result["effective_date"].iloc[0],
        )
        return result[["ts_code", "index_code", "index_name", "effective_date"]]

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

        row = raw.iloc[0]
        result = pd.DataFrame()
        result["ts_code"] = ts_code
        result["industry"] = self._clean_text_field(row, "所属行业")
        result["list_date"] = self._clean_date_field(row, "上市日期")
        result["index_membership"] = self._clean_text_field(row, "入选指数")
        return result

    def _clean_text_field(self, row, column_name):
        """
        提取并清洗概况表中的文本字段

        Args:
            row (pd.Series): 概况表的一行
            column_name (str): 目标列名

        Returns:
            str: 清洗后的文本；缺失或为占位值时返回空字符串
        """
        if column_name not in row:
            return ""
        value = row[column_name]
        # NaN 自身不等于自身，据此识别缺失
        if value is None or value != value:
            return ""
        text = str(value).strip()
        if text in ("None", "nan", "NaT", "-"):
            return ""
        return text

    def _clean_date_field(self, row, column_name):
        """
        提取并清洗概况表中的日期字段

        Args:
            row (pd.Series): 概况表的一行
            column_name (str): 目标列名

        Returns:
            datetime.date: 日期对象；缺失或无法解析时返回 None（落库即 NULL）
        """
        if column_name not in row:
            return None
        value = row[column_name]
        if value is None or value != value:
            return None
        try:
            return pd.to_datetime(str(value).strip()).date()
        except (ValueError, TypeError):
            return None

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
