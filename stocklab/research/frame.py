#!/usr/bin/env python3
"""
==============================================================================
StockLab - 因子输入帧构造 (stocklab.research.frame)
==============================================================================

【模块职责】
   把本地库里分散的表按 Point-in-Time 口径拼成**一张因子输入帧**：
   每只股票一行，包含所选因子需要的全部输入列，供 stocklab.factor /
   stocklab.screener 纯计算消费。

【为什么取数放在这里】
   因子层与筛选层都必须是纯计算（V2 §2.4：分析逻辑不得访问数据库），
   因此「查什么表、怎么按 as-of 取可见数据、派生哪些输入列」全部集中在本模块。

【Point-in-Time 规则】
   - 股票池      SecurityRepository.universe(as_of)：上市 <= as_of 且未退市，
                 含已退市股票，避免幸存者偏差；
   - 估值        各证券取 trade_date <= as_of 的最近一条（各自日期允许不同）；
   - 基本面      全部走 available_date <= as_of 的横截面，读不到的期绝不补；
   - 同比/趋势   *_prior_year 取「as-of 减一年」时点的最新可见值，
                 与当期值同为当时可得信息，不构成未来信息泄漏；
   - 价格        只取 [as_of-400d, as_of] 窗口内的原始收盘价，
                 区间收益率/波动率/最大回撤在本模块派生。

【无数据源的输入列】
   ebitda、dps、dps_prior_year、dividend_years_* 当前没有接入数据源，
   构造时按 NaN 落地并记 WARNING：筛选命中这些因子时逐只报「无数据」，
   不会静默通过，也不会用近似值伪造。
"""

import datetime
import logging

import pandas as pd

from stocklab.factor import registry as factor_engine
from stocklab.factor.base import FactorDataError
from stocklab.persistence.repository import (
    BalanceSheetRepository,
    CashflowStatementRepository,
    DailyPriceRepository,
    DailyValuationRepository,
    FinancialIndicatorRepository,
    IncomeStatementRepository,
    SecurityRepository,
    ValuationHistoryRepository,
)
from stocklab.persistence.storage import Database

_logger = logging.getLogger(__name__)

__all__ = [
    "build_factor_frame",
]

# 帧必备列（除因子输入列外）：标识与行业中性化的分组键
_KEY_COLUMNS = ("ts_code", "name", "industry")

# 价格派生列 -> 唯一的数据源是 market.daily_prices
_PRICE_COLUMNS = (
    "ret_1m",
    "ret_3m",
    "ret_6m",
    "ret_12m",
    "vol_6m",
    "vol_12m",
    "max_dd_12m",
)

# 回看窗口（自然日）：动量锚点与波动率、最大回撤的统计窗口
_MOMENTUM_WINDOWS = (("ret_1m", 30), ("ret_3m", 90), ("ret_6m", 180), ("ret_12m", 365))
_VOLATILITY_WINDOWS = (("vol_6m", 180), ("vol_12m", 365))
_DRAWDOWN_WINDOW = 365
# 12M 动量锚点在 as_of-365d，再留出长假（春节等休市可超过一周）的缺口余量
_PRICE_LOOKBACK_DAYS = 400

# 上游「时点 -> 可提供列」映射：只查真正需要的表，缺数据源的列最后统一补 NaN
_SOURCE_COLUMNS = {
    "valuation_history": ("pe_ttm", "pb", "ps"),
    "daily_valuations": ("dv_ttm", "total_mv"),
    "indicators": ("roe", "roic", "gross_margin", "operating_margin",
                   "revenue_yoy", "profit_yoy", "eps_yoy"),
    "income": ("net_profit", "eps"),
    "balance": ("total_liabilities", "equity", "cash", "interest_bearing_debt"),
    "cashflow": ("operating_cashflow", "free_cashflow"),
    "prices": _PRICE_COLUMNS,
}

# 派生列的装配依赖：要 ev 就必须先取到市值与资产负债表三项
_ASSEMBLY_DEPENDENCIES = {
    "ev": ("total_mv", "interest_bearing_debt", "cash"),
}


def _required_columns(factor_names):
    """
    计算所选因子的输入列集合（含帧必备列）

    Args:
        factor_names (list): 因子名列表

    Returns:
        tuple: 必须出现在结果帧里的列（key 列在前，其余升序）

    Raises:
        FactorDataError: 因子未登记
    """
    columns = set()
    for name in factor_names:
        columns.update(factor_engine.get(name).columns)
    return _KEY_COLUMNS + tuple(sorted(columns - set(_KEY_COLUMNS)))


def _as_date(value):
    """
    把 str / datetime 规整成 date（窗口与「减一年」计算都基于它）

    Args:
        value: "YYYY-MM-DD"、date 或 datetime

    Returns:
        datetime.date
    """
    if isinstance(value, str):
        return datetime.date.fromisoformat(value[:10])
    if isinstance(value, datetime.datetime):
        return value.date()
    return value


def _pick(frame, columns, ts_column="ts_code"):
    """
    从查询结果里挑出需要的列（源表缺的列补 NaN，保持列契约稳定）

    Args:
        frame (pd.DataFrame): 源查询结果
        columns (tuple): 需要的列
        ts_column (str): 主键列名

    Returns:
        pd.DataFrame: 主键列 + 目标列（目标列可能全为 NaN）
    """
    picked = pd.DataFrame({ts_column: frame[ts_column]})
    for column in columns:
        picked[column] = frame[column] if column in frame.columns else float("nan")
    return picked


def _universe_frame(database, as_of_date, ts_codes):
    """
    构造帧的底表：as-of 股票池（含已退市股）+ 代码顺序

    Args:
        database (Database): 数据库实例
        as_of_date (datetime.date): 历史时点
        ts_codes (list, optional): 指定股票池（研究快照重跑时固化使用）

    Returns:
        pd.DataFrame: ts_code / name / industry
    """
    universe = SecurityRepository(database).universe(as_of_date)
    universe = universe[["ts_code", "name", "industry"]]

    if ts_codes is None:
        return universe.reset_index(drop=True)

    # 指定股票池：严格按给定顺序对齐（查不到的代码保留空行，由筛选器报「无数据」）
    aligned = universe.set_index("ts_code").reindex(list(ts_codes))
    return aligned.reset_index().rename(columns={"index": "ts_code"})


def _valuation_frame(database, as_of_date):
    """历史估值横截面（pe_ttm / pb / ps，各自取 <= as-of 的最近记录）"""
    history = ValuationHistoryRepository(database).cross_section_as_of(as_of_date)
    return _pick(history, _SOURCE_COLUMNS["valuation_history"])


def _snapshot_frame(database, as_of_date):
    """每日估值快照横截面（股息率 dv_ttm / 总市值 total_mv）"""
    snapshot = DailyValuationRepository(database).cross_section_as_of(as_of_date)
    return _pick(snapshot, _SOURCE_COLUMNS["daily_valuations"])


def _indicator_frame(database, as_of_date):
    """财务指标横截面（质量与成长，as-of 时点最新可见一期）"""
    indicators = FinancialIndicatorRepository(database).cross_section_as_of(as_of_date)
    return _pick(indicators, _SOURCE_COLUMNS["indicators"])


def _prior_year_frame(database, as_of_date, repository_class, columns, suffix):
    """
    取「as-of 减一年」时点的最新可见横截面，并把指定列改名加后缀

    Args:
        repository_class: 具备 cross_section_as_of 的 Repository 类
        columns (tuple): 需要的源列
        suffix (str): 改名后缀，如 "_prior_year"

    Returns:
        pd.DataFrame: ts_code + 改名后的列
    """
    prior_date = as_of_date - datetime.timedelta(days=365)
    history = repository_class(database).cross_section_as_of(prior_date)
    picked = _pick(history, columns)
    return picked.rename(columns={c: c + suffix for c in columns if c != "ts_code"})


def _balance_frame(database, as_of_date):
    """资产负债表横截面（产权比率与 EV 装配的输入）"""
    balance = BalanceSheetRepository(database).cross_section_as_of(as_of_date)
    return _pick(balance, _SOURCE_COLUMNS["balance"])


def _income_frame(database, as_of_date):
    """利润表横截面（净利润与 EPS）"""
    income = IncomeStatementRepository(database).cross_section_as_of(as_of_date)
    return _pick(income, _SOURCE_COLUMNS["income"])


def _cashflow_frame(database, as_of_date):
    """现金流量表横截面（经营现金流与自由现金流）"""
    cashflow = CashflowStatementRepository(database).cross_section_as_of(as_of_date)
    return _pick(cashflow, _SOURCE_COLUMNS["cashflow"])


def _price_inputs(database, as_of_date):
    """
    由日线收盘序列派生动量/波动率/最大回撤输入列

    【口径】
       ret_X   = as-of 收盘 / (as-of 往前 X 天内的最近一个交易日收盘) - 1，
                 回看期内没有更早收盘（上市不足）-> NaN；
       vol_X   = 回看期内日收益率的样本标准差（ddof=1，未年化），观测不足 -> NaN；
       max_dd  = 回看期内 收盘/滚动最高 - 1 的最小值（负数）。

    Args:
        database (Database): 数据库实例
        as_of_date (datetime.date): 历史时点

    Returns:
        pd.DataFrame: ts_code + _PRICE_COLUMNS；本地无日线时为空表
    """
    prices = DailyPriceRepository(database).find_window(
        as_of_date, _PRICE_LOOKBACK_DAYS
    )
    if prices.empty:
        return pd.DataFrame(columns=["ts_code"] + list(_PRICE_COLUMNS))

    prices = prices.sort_values(["ts_code", "trade_date"])
    prices["close"] = prices["close"].astype("float64")
    # 统一成时间戳，避免 date / datetime64 两种 dtype 的比较口径不一致
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    as_of = pd.Timestamp(as_of_date)

    inputs = pd.DataFrame({"ts_code": prices["ts_code"].unique()})
    inputs = inputs.set_index("ts_code")

    latest_close = (
        prices.groupby("ts_code").tail(1).set_index("ts_code")["close"]
    )

    # 动量：锚点 = 窗口截止日前最近一个交易日
    for column, days in _MOMENTUM_WINDOWS:
        cutoff = as_of - pd.Timedelta(days=days)
        anchor = (
            prices[prices["trade_date"] <= cutoff]
            .groupby("ts_code")
            .tail(1)
            .set_index("ts_code")["close"]
        )
        aligned_anchor = anchor.reindex(latest_close.index)
        inputs[column] = latest_close / aligned_anchor - 1

    # 波动率：窗口内日收益率的样本标准差
    for column, days in _VOLATILITY_WINDOWS:
        cutoff = as_of - pd.Timedelta(days=days)
        window = prices[prices["trade_date"] >= cutoff]
        previous = window.groupby("ts_code")["close"].shift(1)
        daily_return = (window["close"] - previous) / previous
        inputs[column] = daily_return.groupby(window["ts_code"]).std()

    # 最大回撤：相对滚动高点的最深跌幅
    cutoff = as_of - pd.Timedelta(days=_DRAWDOWN_WINDOW)
    window = prices[prices["trade_date"] >= cutoff]
    peak = window.groupby("ts_code")["close"].cummax()
    drawdown = window["close"] / peak - 1
    inputs["max_dd_12m"] = drawdown.groupby(window["ts_code"]).min()

    return inputs.reset_index()


def build_factor_frame(
    as_of_date, ts_codes=None, factor_names=None, extra_columns=None, database=None
):
    """
    构造因子输入帧（每只股票一行，全部按 as-of 时点可见的数据拼装）

    Args:
        as_of_date (str 或 datetime.date): 研究时点，如 "2024-06-30"
        ts_codes (list, optional): 指定股票池；默认用 as-of 股票池
        factor_names (list, optional): 因子名列表；默认全部已登记因子
        extra_columns (list, optional): 额外需要的帧列（如预处理引用的市值列）
        database (Database, optional): 数据库实例，默认新建

    Returns:
        pd.DataFrame: ts_code / name / industry + 所选因子的全部输入列

    Raises:
        FactorDataError: 因子未登记，或结果出现重复 ts_code（帧必须一行一标的）
    """
    factor_names = list(factor_names) if factor_names else factor_engine.list_factors()
    required = _required_columns(factor_names)
    if extra_columns:
        extra = [c for c in extra_columns if c not in required]
        required = required + tuple(sorted(extra))
    needed = set(required) - set(_KEY_COLUMNS)
    # 派生列的装配依赖（如 ev 需要市值与资产负债表三项）一并纳入取数需求
    for column, dependencies in _ASSEMBLY_DEPENDENCIES.items():
        if column in needed:
            needed.update(dependencies)

    database = database if database else Database()
    as_of = _as_date(as_of_date)

    frame = _universe_frame(database, as_of, ts_codes)

    if needed & set(_SOURCE_COLUMNS["valuation_history"]):
        frame = frame.merge(_valuation_frame(database, as_of), on="ts_code", how="left")
    if needed & set(_SOURCE_COLUMNS["daily_valuations"]):
        frame = frame.merge(_snapshot_frame(database, as_of), on="ts_code", how="left")
    if needed & set(_SOURCE_COLUMNS["indicators"]):
        frame = frame.merge(
            _indicator_frame(database, as_of), on="ts_code", how="left"
        )
    if needed & {"roe_prior_year"}:
        frame = frame.merge(
            _prior_year_frame(
                database, as_of, FinancialIndicatorRepository, ("roe",), "_prior_year"
            ),
            on="ts_code",
            how="left",
        )
    if needed & set(_SOURCE_COLUMNS["income"]):
        frame = frame.merge(_income_frame(database, as_of), on="ts_code", how="left")
    if needed & set(_SOURCE_COLUMNS["balance"]):
        frame = frame.merge(_balance_frame(database, as_of), on="ts_code", how="left")
    if needed & set(_SOURCE_COLUMNS["cashflow"]):
        frame = frame.merge(_cashflow_frame(database, as_of), on="ts_code", how="left")
    if needed & {"free_cashflow_prior_year"}:
        frame = frame.merge(
            _prior_year_frame(
                database,
                as_of,
                CashflowStatementRepository,
                ("free_cashflow",),
                "_prior_year",
            ),
            on="ts_code",
            how="left",
        )
    if needed & set(_PRICE_COLUMNS):
        frame = frame.merge(_price_inputs(database, as_of), on="ts_code", how="left")

    # EV = 总市值 + 有息负债 - 现金（任一缺失则为 NaN，不做近似）
    if "ev" in needed:
        frame["ev"] = (
            frame["total_mv"] + frame["interest_bearing_debt"] - frame["cash"]
        )

    # 没有数据源的列一律 NaN 落地并显式告警（筛选时逐只报「无数据」）
    missing = [column for column in required if column not in frame.columns]
    for column in missing:
        frame[column] = float("nan")
    if missing:
        _logger.warning(
            "因子输入帧 as_of=%s：以下列没有数据源，按 NaN 落地: %s",
            as_of,
            missing,
        )

    for column in required:
        if column in _KEY_COLUMNS:
            continue
        frame[column] = frame[column].astype("float64")

    frame = frame[list(required)]
    if frame["ts_code"].duplicated().any():
        raise FactorDataError(
            "因子输入帧出现重复 ts_code（%d 行 / %d 只），股票池或数据源存在重复"
            % (len(frame), frame["ts_code"].nunique())
        )

    _logger.info(
        "因子输入帧完成: as_of=%s, 股票 %d 只, 因子 %d 个, 输入列 %d 列",
        as_of,
        len(frame),
        len(factor_names),
        len(required) - len(_KEY_COLUMNS),
    )
    return frame.reset_index(drop=True)
