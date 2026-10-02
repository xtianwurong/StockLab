#!/usr/bin/env python3
"""
==============================================================================
StockLab - 全市场市盈率分布分析 (stocklab.analytics.valuation_distribution)
==============================================================================

【模块职责】
   对「全市场每日估值表」(market.daily_valuations) 做市盈率(PE)分布统计，
   输出统计结果实体 ValuationDistributionProfile，交给渲染层直接排版。

【为何单列一层 analytics】
   datasource 只负责「从外部取数」，persistence 只负责「往本地存数」，
   统计聚合不属于任何一侧。本模块是纯变换层：
     - 只接收 pandas DataFrame，不发网络请求、不连数据库；
     - 不 import datasource / persistence / facade，可完全独立单测。
   取数由上层（入口脚本）经 facade 完成，再把 DataFrame 传进来。

【PE 分布的关键业务事实（决定统计口径）】
   1. A 股市盈率分布严重右偏：少数高成长股 PE 可达数千倍，
      算术平均值(均值)被极端值严重拉偏，**不能作为分布中心的代表**。
      因此本模块以「中位数 + 分位数」为主，均值仅作参考并单独标注。
   2. 亏损股没有有意义的 PE：数据源通常以 "-" 占位，本模块归入「无有效 PE」；
      PE <= 0 的负值单独计数，且一律不参与分位数统计。
   3. 「市盈率-动态」与「市盈率(TTM)」是两个不同口径，统计哪一个由 pe_column
      显式指定，绝不做隐式推断。

【统计口径输出】
   - 样本构成：有效 / 无有效 / 非正 / 极端高值 的只数与占比
   - 集中趋势：最小、最大、均值、中位数、截尾均值（剔除极端高值后的均值）
   - 分位数：P5 / P10 / P25 / P50 / P75 / P90 / P95 / P99
   - 区间分布：固定业务语义的 9 档直方图（非等宽分桶，左开右闭）
   - 分交易所对比：沪 / 深 / 北各自的样本数与中位数
   - 极值标的：PE 最低 / 最高各 N 只（含证券简称与交易所）

【依赖清单】
   标准库：logging
   第三方库：pandas（仅依赖 DataFrame 切片、排序、聚合等基础操作）
==============================================================================
"""

import logging

import pandas as pd

_logger = logging.getLogger(__name__)

__all__ = [
    "PE_VALUE_COLUMN_TTM",
    "PE_VALUE_COLUMN_DYNAMIC",
    "PE_EXTREME_HIGH_THRESHOLD",
    "PE_MIN_REASONABLE_SAMPLE_COUNT",
    "PE_DISTRIBUTION_QUANTILES",
    "PE_DISTRIBUTION_BUCKET_EDGES",
    "PE_DISTRIBUTION_BUCKET_LABELS",
    "DistributionBucket",
    "PeRankEntry",
    "MarketValuationDistribution",
    "ValuationDistributionProfile",
    "ValuationDistributionAnalyzer",
]


# ============================================================================
# 统计口径常量（单点定义，避免魔法字符串散落各处）
# ============================================================================

# 可选的 PE 口径列名，与 market.daily_valuations 的列名严格一致
PE_VALUE_COLUMN_TTM = "pe_ttm"        # 市盈率 TTM，跨期可比，分布统计的默认口径
PE_VALUE_COLUMN_DYNAMIC = "pe"        # 市盈率(动态)，东财实时快照口径

# 极端高值阈值：超过此倍数视为极端样本，不参与截尾均值计算
PE_EXTREME_HIGH_THRESHOLD = 1000.0

# 代表性样本下限：全市场 A 股逾 5500 只，有效样本低于此量级时
# 「分布」结论不成立（可能只是本地快照的少量残留记录）
PE_MIN_REASONABLE_SAMPLE_COUNT = 1000

# 极值标的榜单默认长度
DEFAULT_EXTREME_ENTRY_COUNT = 10

# 分位数口径：(展示名, 分位点)。按业务关注度从两端向中心排列。
PE_DISTRIBUTION_QUANTILES = (
    ("P5", 0.05),
    ("P10", 0.10),
    ("P25", 0.25),
    ("P50", 0.50),
    ("P75", 0.75),
    ("P90", 0.90),
    ("P95", 0.95),
    ("P99", 0.99),
)

# 分桶右端边界：最后一档 (1000, +∞) 右端开放，故下界标签不闭合
PE_DISTRIBUTION_BUCKET_EDGES = (10.0, 20.0, 30.0, 50.0, 100.0, 200.0, 500.0, 1000.0)

# 分桶展示名，与 PE_DISTRIBUTION_BUCKET_EDGES 逐档对应（边数 + 1 = 标签数）
PE_DISTRIBUTION_BUCKET_LABELS = (
    "0-10", "10-20", "20-30", "30-50",
    "50-100", "100-200", "200-500", "500-1000", "1000+",
)

# 估值表与证券表约定的列名
VALUATION_TS_CODE_COLUMN = "ts_code"
VALUATION_TRADE_DATE_COLUMN = "trade_date"
VALUATION_NAME_COLUMN = "name"
VALUATION_MARKET_COLUMN = "market"


# ============================================================================
# 结果实体类（简单实体 / POD，只承载数据与最简派生计算，不含渲染逻辑）
# ============================================================================

class DistributionBucket:
    """
    市盈率分布区间桶（简单实体类 / POD）

    【区间语义】
      左开右闭，例如 "20-30" 表示 (20, 30]；最后一档 "1000+" 表示 (1000, +∞)。
    """

    def __init__(self, label, lower_bound, upper_bound, stock_count, total_count):
        """
        初始化区间桶

        Args:
            label (str): 区间展示名，如 "20-30"
            lower_bound (float): 区间下界
            upper_bound (float, optional): 区间上界；None 表示右端开放（+∞）
            stock_count (int): 落在本区间的证券只数
            total_count (int): 参与统计的有效样本总数（作为占比分母）
        """
        self.label = label
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound
        self.stock_count = stock_count
        self.total_count = total_count

    def ratio(self):
        """
        本桶占有效样本的比例

        Returns:
            float: 0 ~ 1 之间的占比；分母为 0 时返回 0.0
        """
        if not self.total_count:
            return 0.0
        return self.stock_count / self.total_count

    def to_dict(self):
        """转换为可序列化的字典"""
        return {
            "label": self.label,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "stock_count": self.stock_count,
            "total_count": self.total_count,
        }


class PeRankEntry:
    """
    市盈率极值标的条目（简单实体类 / POD）
    """

    def __init__(self, ts_code, name, market, pe_value):
        """
        初始化极值条目

        Args:
            ts_code (str): 标准证券代码，如 "600519.SH"
            name (str): 证券简称；缺失时以证券代码兜底
            market (str): 交易所（SH / SZ / BJ），可能为空
            pe_value (float): 该标的的市盈率倍数
        """
        self.ts_code = ts_code
        self.name = name
        self.market = market
        self.pe_value = pe_value

    def to_dict(self):
        """转换为可序列化的字典"""
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "market": self.market,
            "pe_value": self.pe_value,
        }


class MarketValuationDistribution:
    """
    单个交易所的市盈率分布摘要（简单实体类 / POD）
    """

    def __init__(self, market, valid_pe_count, median_pe, min_pe, max_pe):
        """
        初始化交易所分布摘要

        Args:
            market (str): 交易所代码（SH / SZ / BJ）
            valid_pe_count (int): 该交易所的有效样本只数
            median_pe (float): 该交易所的市盈率中位数
            min_pe (float): 该交易所的市盈率最小值
            max_pe (float): 该交易所的市盈率最大值
        """
        self.market = market
        self.valid_pe_count = valid_pe_count
        self.median_pe = median_pe
        self.min_pe = min_pe
        self.max_pe = max_pe

    def to_dict(self):
        """转换为可序列化的字典"""
        return {
            "market": self.market,
            "valid_pe_count": self.valid_pe_count,
            "median_pe": self.median_pe,
            "min_pe": self.min_pe,
            "max_pe": self.max_pe,
        }


class ValuationDistributionProfile:
    """
    全市场市盈率分布统计结果实体（数据载体）

    【设计说明】
      本类只负责承载统计结果与最简派生计算（占比、是否为空），
      不做任何排版渲染——排版由 ValuationDistributionReporter 负责，
      保持「计算」与「呈现」职责分离。
    """

    def __init__(self, pe_column):
        """
        初始化统计结果实体

        Args:
            pe_column (str): 本次统计采用的市盈率口径列名（pe_ttm 或 pe）
        """
        self.pe_column = pe_column

        # 统计状态说明：正常为「正常」，异常时写入可读的原因，供报告层直接展示
        self.status = "正常"
        self.trade_date = ""

        # 样本构成
        self.total_stock_count = 0
        self.valid_pe_count = 0
        self.invalid_pe_count = 0
        self.non_positive_pe_count = 0
        self.extreme_high_threshold = 0.0
        self.extreme_high_count = 0

        # 集中趋势（None 表示该指标不可用）
        self.min_pe = None
        self.max_pe = None
        self.mean_pe = None
        self.median_pe = None
        self.trimmed_mean_pe = None

        # 分位数：[(展示名, 数值), ...]，保持 PE_DISTRIBUTION_QUANTILES 的顺序
        self.quantiles = []

        # 区间分布 / 分交易所摘要 / 极值榜单
        self.buckets = []
        self.market_distributions = []
        self.lowest_pe_entries = []
        self.highest_pe_entries = []

    def is_empty(self):
        """
        本次统计是否没有得到任何有效样本

        Returns:
            bool: True 表示无有效市盈率样本
        """
        return self.valid_pe_count <= 0

    def coverage_ratio(self):
        """
        有效市盈率样本占全市场证券的比例

        Returns:
            float: 0 ~ 1 之间的覆盖率；总样本为 0 时返回 0.0
        """
        if not self.total_stock_count:
            return 0.0
        return self.valid_pe_count / self.total_stock_count

    def is_representative(self):
        """
        本次统计的样本量是否足以支撑「全市场分布」结论

        【为何需要这个判断】
           取数门面在 local_first 策略下，只要本地快照非空即判定为命中，
           即使本地只有个位数残留记录。此时统计流程本身不会失败，
           但「全市场市盈率分布」的结论是不成立的。
           把代表性判定放在库层而非调用方，可确保任何消费方都无法绕过该校验。

        Returns:
            bool: True 表示有效样本达到代表性量级
        """
        return self.valid_pe_count >= PE_MIN_REASONABLE_SAMPLE_COUNT

    def to_dict(self):
        """转换为可序列化的字典（便于导出 JSON 或落库）"""
        return {
            "pe_column": self.pe_column,
            "status": self.status,
            "trade_date": self.trade_date,
            "total_stock_count": self.total_stock_count,
            "valid_pe_count": self.valid_pe_count,
            "invalid_pe_count": self.invalid_pe_count,
            "non_positive_pe_count": self.non_positive_pe_count,
            "extreme_high_threshold": self.extreme_high_threshold,
            "extreme_high_count": self.extreme_high_count,
            "min_pe": self.min_pe,
            "max_pe": self.max_pe,
            "mean_pe": self.mean_pe,
            "median_pe": self.median_pe,
            "trimmed_mean_pe": self.trimmed_mean_pe,
            "quantiles": [{"label": item[0], "value": item[1]} for item in self.quantiles],
            "buckets": [bucket.to_dict() for bucket in self.buckets],
            "market_distributions": [item.to_dict() for item in self.market_distributions],
            "lowest_pe_entries": [entry.to_dict() for entry in self.lowest_pe_entries],
            "highest_pe_entries": [entry.to_dict() for entry in self.highest_pe_entries],
        }


# ============================================================================
# 分析器（纯统计，不触碰任何外部资源）
# ============================================================================

class ValuationDistributionAnalyzer:
    """
    全市场市盈率分布分析器

    【职责】
      1. 校验估值表结构与 PE 口径列是否齐备
      2. 把证券简称与交易所附加到估值表上（估值表本身不含身份列）
      3. 按「有效样本 / 无效样本 / 非正样本」划分样本口径
      4. 计算集中趋势、分位数、区间分布、分交易所摘要与极值榜单
    """

    def __init__(self,
                 pe_column=PE_VALUE_COLUMN_TTM,
                 extreme_high_threshold=PE_EXTREME_HIGH_THRESHOLD,
                 extreme_entry_count=DEFAULT_EXTREME_ENTRY_COUNT):
        """
        初始化市盈率分布分析器

        Args:
            pe_column (str, optional): 统计口径列名，pe_ttm 或 pe；默认 pe_ttm
            extreme_high_threshold (float, optional): 极端高值阈值（倍）；
                                                     超过者不计入截尾均值
            extreme_entry_count (int, optional): 极值榜单展示条数
        """
        self._pe_column = pe_column
        self._extreme_high_threshold = extreme_high_threshold
        self._extreme_entry_count = extreme_entry_count

    def analyze(self, valuation_df, securities_df=None):
        """
        执行市盈率分布统计

        Args:
            valuation_df (pd.DataFrame): 全市场每日估值表，
                                         需包含 ts_code 与 pe_column 指定的 PE 列
            securities_df (pd.DataFrame, optional): 全市场证券基础信息表，
                                                    需包含 ts_code / name / market；
                                                    缺失时以 ts_code 兜底名称且不分组交易所

        Returns:
            ValuationDistributionProfile: 统计结果实体；输入不可用时返回 status 非「正常」的空结果
        """
        profile = ValuationDistributionProfile(self._pe_column)
        profile.extreme_high_threshold = self._extreme_high_threshold

        if valuation_df is None or valuation_df.empty:
            profile.status = "未取到任何全市场估值快照，无法统计"
            _logger.warning("市盈率分布统计跳过：输入估值表为空")
            return profile

        if self._pe_column not in valuation_df.columns:
            profile.status = "估值表缺少 PE 列 [%s]，无法按该口径统计" % self._pe_column
            _logger.warning("市盈率分布统计跳过：估值表缺少列 [%s]", self._pe_column)
            return profile

        work_df = self._attach_identity(valuation_df, securities_df)
        profile.trade_date = self._resolve_trade_date(work_df)
        profile.total_stock_count = len(work_df)

        pe_values = pd.to_numeric(work_df[self._pe_column], errors="coerce")
        work_df = work_df.copy()
        work_df[self._pe_column] = pe_values

        profile.non_positive_pe_count = int((pe_values <= 0).sum())

        valid_df = work_df[pe_values > 0]
        profile.valid_pe_count = len(valid_df)
        profile.invalid_pe_count = (
            profile.total_stock_count - profile.valid_pe_count - profile.non_positive_pe_count
        )

        if profile.valid_pe_count <= 0:
            profile.status = "全市场无有效市盈率样本（可能整体亏损或数据源未提供该字段）"
            _logger.warning("市盈率分布统计无有效样本：总样本 %d 只", profile.total_stock_count)
            return profile

        valid_values = valid_df[self._pe_column]

        profile.min_pe = float(valid_values.min())
        profile.max_pe = float(valid_values.max())
        profile.mean_pe = float(valid_values.mean())
        profile.median_pe = float(valid_values.median())

        trimmed_values = valid_values[valid_values <= self._extreme_high_threshold]
        profile.extreme_high_count = profile.valid_pe_count - len(trimmed_values)
        if len(trimmed_values) > 0:
            profile.trimmed_mean_pe = float(trimmed_values.mean())

        profile.quantiles = self._compute_quantiles(valid_values)
        profile.buckets = self._compute_buckets(valid_values, profile.valid_pe_count)
        profile.market_distributions = self._compute_market_distributions(valid_df)
        profile.lowest_pe_entries = self._compute_rank_entries(valid_df, True)
        profile.highest_pe_entries = self._compute_rank_entries(valid_df, False)

        _logger.info(
            "市盈率分布统计完成：口径 %s，数据日期 %s，总样本 %d，有效样本 %d",
            self._pe_column,
            profile.trade_date,
            profile.total_stock_count,
            profile.valid_pe_count,
        )
        return profile

    def _attach_identity(self, valuation_df, securities_df):
        """
        把证券简称与交易所附加到估值表上

        【为何需要】
          market.daily_valuations 只存估值数值，证券简称与交易所都在
          reference.securities 里，两者仅靠 ts_code 关联。

        Args:
            valuation_df (pd.DataFrame): 原始估值表
            securities_df (pd.DataFrame, optional): 证券基础信息表

        Returns:
            pd.DataFrame: 附加了 name 与 market 列的估值表副本
        """
        work_df = valuation_df.copy()

        # 先剔除估值表内可能已存在的同名列，避免合并后出现 _x / _y 后缀
        for column in (VALUATION_NAME_COLUMN, VALUATION_MARKET_COLUMN):
            if column in work_df.columns:
                work_df = work_df.drop(columns=[column])

        if securities_df is None or securities_df.empty:
            work_df[VALUATION_NAME_COLUMN] = work_df[VALUATION_TS_CODE_COLUMN]
            work_df[VALUATION_MARKET_COLUMN] = ""
            return work_df

        if VALUATION_TS_CODE_COLUMN not in securities_df.columns:
            _logger.warning("证券基础信息表缺少 [%s] 列，无法附加简称与交易所", VALUATION_TS_CODE_COLUMN)
            work_df[VALUATION_NAME_COLUMN] = work_df[VALUATION_TS_CODE_COLUMN]
            work_df[VALUATION_MARKET_COLUMN] = ""
            return work_df

        identity_df = pd.DataFrame()
        identity_df[VALUATION_TS_CODE_COLUMN] = securities_df[VALUATION_TS_CODE_COLUMN]
        if VALUATION_NAME_COLUMN in securities_df.columns:
            identity_df[VALUATION_NAME_COLUMN] = securities_df[VALUATION_NAME_COLUMN]
        if VALUATION_MARKET_COLUMN in securities_df.columns:
            identity_df[VALUATION_MARKET_COLUMN] = securities_df[VALUATION_MARKET_COLUMN]
        identity_df = identity_df.drop_duplicates(subset=[VALUATION_TS_CODE_COLUMN])

        merged_df = pd.merge(work_df, identity_df, on=VALUATION_TS_CODE_COLUMN, how="left")

        # 合并失败的证券以自身代码作为名称兜底，保证报告里不会出现空名称
        missing_name = merged_df[VALUATION_NAME_COLUMN].isna()
        merged_df.loc[missing_name, VALUATION_NAME_COLUMN] = merged_df.loc[
            missing_name, VALUATION_TS_CODE_COLUMN
        ]
        return merged_df

    def _resolve_trade_date(self, work_df):
        """
        解析估值快照的数据日期

        【为何取最大值而非入参】
          本方法不接收请求日期，而是从实际取到的数据里读取最大交易日，
          保证报告展示的日期与真实样本一致（远端回退时请求日期并不生效）。

        Args:
            work_df (pd.DataFrame): 已附加身份列的估值表

        Returns:
            str: YYYY-MM-DD 形式的日期；无法解析时返回空字符串
        """
        if VALUATION_TRADE_DATE_COLUMN not in work_df.columns:
            return ""

        date_series = work_df[VALUATION_TRADE_DATE_COLUMN].dropna()
        if date_series.empty:
            return ""

        latest_value = date_series.max()
        if hasattr(latest_value, "strftime"):
            return latest_value.strftime("%Y-%m-%d")
        return str(latest_value)

    def _compute_quantiles(self, valid_values):
        """
        计算各分位点的市盈率

        【插值说明】
          pandas 默认采用线性插值，此处沿用该行为以保持可复现。

        Args:
            valid_values (pd.Series): 有效市盈率序列（仅含 PE > 0）

        Returns:
            list: [(展示名, 分位数值), ...]，顺序同 PE_DISTRIBUTION_QUANTILES
        """
        results = []
        for item in PE_DISTRIBUTION_QUANTILES:
            label = item[0]
            level = item[1]
            results.append((label, float(valid_values.quantile(level))))
        return results

    def _compute_buckets(self, valid_values, total_count):
        """
        按固定业务语义分桶，生成区间分布直方图

        【为何不用等宽分桶】
          PE 的业务关注区间高度非线性：0~50 区间密集、50 以上迅速稀疏。
          固定边界让每个桶都有明确的投资含义，等宽分桶会让 0~30 全挤在一根柱子里。

        【为何用标签数而非边界数控制循环】
          分桶数 = 边界数 + 1，最后一档右端开放（如 "1000+" 表示 (1000, +∞)），
          没有对应的上界值，因此以 PE_DISTRIBUTION_BUCKET_LABELS 为循环基准，
          避免漏掉末尾的开放区间。

        Args:
            valid_values (pd.Series): 有效市盈率序列（仅含 PE > 0）
            total_count (int): 有效样本总数（占比分母）

        Returns:
            list: DistributionBucket 列表，顺序同 PE_DISTRIBUTION_BUCKET_LABELS
        """
        results = []
        bucket_count = len(PE_DISTRIBUTION_BUCKET_LABELS)

        for index in range(bucket_count):
            if index == 0:
                lower_bound = 0.0
            else:
                lower_bound = PE_DISTRIBUTION_BUCKET_EDGES[index - 1]

            if index == bucket_count - 1:
                # 最后一档右端开放：(last_edge, +∞)
                upper_bound = None
                bucket_mask = valid_values > lower_bound
            else:
                upper_bound = PE_DISTRIBUTION_BUCKET_EDGES[index]
                bucket_mask = (valid_values > lower_bound) & (valid_values <= upper_bound)

            results.append(
                DistributionBucket(
                    PE_DISTRIBUTION_BUCKET_LABELS[index],
                    lower_bound,
                    upper_bound,
                    int(bucket_mask.sum()),
                    total_count,
                )
            )
        return results

    def _compute_market_distributions(self, valid_df):
        """
        分交易所计算样本数与中位数

        Args:
            valid_df (pd.DataFrame): 已过滤为有效样本的估值表

        Returns:
            list: MarketValuationDistribution 列表，按交易所代码升序
        """
        results = []
        if VALUATION_MARKET_COLUMN not in valid_df.columns:
            return results

        market_values = valid_df[VALUATION_MARKET_COLUMN].dropna().unique()
        market_list = []
        for item in market_values:
            market_code = str(item).strip()
            # 交易所信息缺失（未提供证券基础信息）时无法分组，
            # 空串会渲染成一行无归属的数据，宁可整体不展示
            if market_code:
                market_list.append(market_code)
        market_list.sort()

        for market in market_list:
            subset_df = valid_df[valid_df[VALUATION_MARKET_COLUMN].astype(str) == market]
            subset_values = subset_df[self._pe_column]
            if subset_values.empty:
                continue
            results.append(
                MarketValuationDistribution(
                    market,
                    len(subset_values),
                    float(subset_values.median()),
                    float(subset_values.min()),
                    float(subset_values.max()),
                )
            )
        return results

    def _compute_rank_entries(self, valid_df, ascending):
        """
        生成 PE 极值榜单

        Args:
            valid_df (pd.DataFrame): 已过滤为有效样本的估值表
            ascending (bool): True 取 PE 最低 N 只；False 取 PE 最高 N 只

        Returns:
            list: PeRankEntry 列表，按 PE 升序或降序排列
        """
        ordered_df = valid_df.sort_values(by=self._pe_column, ascending=ascending)

        results = []
        for index, row in ordered_df.iterrows():
            if len(results) >= self._extreme_entry_count:
                break
            results.append(self._build_rank_entry(row))
        return results

    def _build_rank_entry(self, row):
        """
        从一行估值数据构造极值条目

        Args:
            row (pd.Series): 估值表中的一行

        Returns:
            PeRankEntry: 极值标的条目
        """
        ts_code = str(row[VALUATION_TS_CODE_COLUMN])
        name_value = row[VALUATION_NAME_COLUMN]
        if name_value is None or name_value != name_value:  # NaN 自身不等于自身
            name = ts_code
        else:
            name = str(name_value)

        if VALUATION_MARKET_COLUMN in row:
            market_value = row[VALUATION_MARKET_COLUMN]
        else:
            market_value = ""
        if market_value is None or market_value != market_value:
            market = ""
        else:
            market = str(market_value)

        return PeRankEntry(ts_code, name, market, float(row[self._pe_column]))
