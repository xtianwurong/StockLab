#!/usr/bin/env python3
"""
==============================================================================
StockLab - AkShare 源归一化器 (stocklab.normalization.akshare)
==============================================================================

【模块职责】
   把 akshare 各接口返回的**源列结构**（中文列名、列序随数据源变化）
   转换为领域契约 DataFrame（stocklab.domain 的 *_COLUMNS，列名与列序固定）。
   本模块是 MarketService 与库表之间的唯一映射层，只依赖 pandas 与 domain。

【为何源归一化必须独立成层】
   - 数据源改版改列名时，require_columns 立即抛 DataContractError，
     缺列不会被静默写成 NULL 污染数据库；
   - 输出一律经 align_columns 重排为契约列序，下游 Repository 用显式列名写入，
     彻底消除「列序变化导致错位写入」的风险。

【构造约定（pandas 陷阱）】
   各函数一律用「列名字典 → pd.DataFrame(dict)」一次性构造：
   字典构造会把标量广播到全部行；若先写标量列、再写 Series 列，
   先写的标量列会因索引扩展而变成全 NaN（真实踩坑，勿改回逐列赋值）。
"""

import pandas as pd

from stocklab.domain import (
    DAILY_PRICE_COLUMNS,
    DAILY_VALUATION_COLUMNS,
    INDUSTRY_VALUATION_COLUMNS,
    INDEX_MEMBERSHIP_COLUMNS,
    require_columns,
    SECURITY_COLUMNS,
    VALUATION_HISTORY_COLUMNS,
    align_columns,
)
from stocklab.normalization.base import (
    clean_date_value,
    clean_text_value,
    normalize_ts_code,
    to_date_series,
    to_numeric_column,
)

__all__ = [
    "normalize_securities",
    "normalize_daily_prices",
    "normalize_daily_valuations",
    "normalize_valuation_history",
    "normalize_industry_valuation",
    "normalize_index_membership",
    "normalize_company_profile",
]


def _exchange_suffix(ts_code):
    """
    取 ts_code 的交易所后缀（SH / SZ / BJ）

    Args:
        ts_code (str): 标准证券代码，如 "600519.SH"

    Returns:
        str: 后缀；无后缀时返回空串
    """
    return ts_code.split(".")[1] if "." in ts_code else ""


def normalize_securities(raw):
    """
    证券名录归一化：东财代码 + 名称 → reference.securities 契约列

    Args:
        raw (pd.DataFrame): 源表，必需列 code / name

    Returns:
        pd.DataFrame: SECURITY_COLUMNS 契约列序

    Raises:
        DataContractError: 源表缺少必需列时抛出
    """
    owner = "akshare.stock_info_a_code_name"
    require_columns(raw, ("code", "name"), owner)

    ts_codes = raw["code"].apply(normalize_ts_code)
    data = {
        "ts_code": ts_codes,
        "symbol": raw["code"].astype(str),
        "name": raw["name"],
        # 交易所与市场均为 ts_code 后缀（SH / SZ / BJ）
        "exchange": ts_codes.apply(_exchange_suffix),
        "market": ts_codes.apply(_exchange_suffix),
        # 本接口不提供行业 / 地区 / 上市日期，由生命周期阶段补齐
        "industry": "",
        "area": "",
        "list_date": None,
        "delist_date": None,
        "status": "LISTED",
        "is_hs": "",
    }
    return align_columns(pd.DataFrame(data), SECURITY_COLUMNS, owner)


def normalize_daily_prices(raw, ts_code):
    """
    日 K 行情归一化：东财历史行情 → market.daily_prices 契约列

    Args:
        raw (pd.DataFrame): 源表，必需列 日期/开盘/最高/最低/收盘，
                            可选列 昨收/涨跌额/涨跌幅/成交量/成交额
        ts_code (str): 证券代码（源表不含代码，由调用方传入）

    Returns:
        pd.DataFrame: DAILY_PRICE_COLUMNS 契约列序

    Raises:
        DataContractError: 缺少必需列时抛出
    """
    owner = "akshare.stock_zh_a_hist"
    require_columns(raw, ("日期", "开盘", "最高", "最低", "收盘"), owner)

    data = {
        "ts_code": ts_code,
        "trade_date": to_date_series(raw["日期"]).dt.date,
        "open": to_numeric_column(raw, "开盘"),
        "high": to_numeric_column(raw, "最高"),
        "low": to_numeric_column(raw, "最低"),
        "close": to_numeric_column(raw, "收盘"),
        "pre_close": to_numeric_column(raw, "昨收"),
        "change": to_numeric_column(raw, "涨跌额"),
        "pct_chg": to_numeric_column(raw, "涨跌幅"),
        "volume": to_numeric_column(raw, "成交量"),
        "amount": to_numeric_column(raw, "成交额"),
    }
    return align_columns(pd.DataFrame(data), DAILY_PRICE_COLUMNS, owner)


def normalize_daily_valuations(raw, trade_date):
    """
    全市场估值快照归一化：东财实时行情 → market.daily_valuations 契约列

    【必需列为何包含市值】
       市盈率 / 市净率 / 总市值 / 流通市值是本表的核心用途（市场估值温度、
       市值分布），任一列改名都意味着整批快照失效，必须显式失败而不是整列写 NULL。

    Args:
        raw (pd.DataFrame): 源表
        trade_date: 快照写入日期（datetime.date）

    Returns:
        pd.DataFrame: DAILY_VALUATION_COLUMNS 契约列序

    Raises:
        DataContractError: 缺少必需列时抛出
    """
    owner = "akshare.stock_zh_a_spot_em"
    require_columns(
        raw, ("代码", "市盈率(TTM)", "市净率", "总市值", "流通市值"), owner
    )

    data = {
        "ts_code": raw["代码"].apply(normalize_ts_code),
        "trade_date": trade_date,
        "turnover_rate": to_numeric_column(raw, "换手率"),
        # 数据源不提供的指标整列写 NULL，保留 NULL 语义（不填 0）
        "turnover_rate_f": float("nan"),
        "pe": to_numeric_column(raw, "市盈率-动态"),
        "pe_ttm": to_numeric_column(raw, "市盈率(TTM)"),
        "pb": to_numeric_column(raw, "市净率"),
        "ps": float("nan"),
        "ps_ttm": float("nan"),
        "dv_ratio": float("nan"),
        "dv_ttm": float("nan"),
        "total_share": float("nan"),
        "float_share": float("nan"),
        "free_share": float("nan"),
        "total_mv": to_numeric_column(raw, "总市值"),
        "circ_mv": to_numeric_column(raw, "流通市值"),
    }
    return align_columns(pd.DataFrame(data), DAILY_VALUATION_COLUMNS, owner)


def normalize_valuation_history(indicator_frames, ts_code):
    """
    单股历史估值序列归一化：逐指标的 (日期, 数值) 帧 → market.valuation_history 契约列

    【对齐口径】
       以 pe_ttm 的交易日为基准轴（缺失时退化用 pb），其余指标按**日期**左连接，
       不按行号对齐——各指标返回的交易日可能不完全一致，按位置赋值会错位。

    Args:
        indicator_frames (dict): {契约列名: 含 trade_date 与该指标列的 DataFrame}
        ts_code (str): 证券代码

    Returns:
        pd.DataFrame: VALUATION_HISTORY_COLUMNS 契约列序，按交易日升序；
                      指标取不到时整列为 NaN，全部指标缺失时返回空表
    """
    owner = "akshare.stock_zh_valuation_baidu"

    base_column = None
    for candidate in ("pe_ttm", "pb"):
        frame = indicator_frames.get(candidate)
        if frame is not None and not frame.empty:
            base_column = candidate
            break
    if base_column is None:
        return pd.DataFrame(columns=list(VALUATION_HISTORY_COLUMNS))

    base_frame = indicator_frames[base_column]
    result = base_frame[["trade_date", base_column]].copy()

    for column in VALUATION_HISTORY_COLUMNS:
        if column in ("ts_code", "trade_date", base_column):
            continue
        frame = indicator_frames.get(column)
        if frame is None or frame.empty:
            # 数据源不提供该指标：保留列并整列写 NULL
            result[column] = float("nan")
            continue
        paired = frame[["trade_date", column]].drop_duplicates(
            subset=["trade_date"]
        )
        result = result.merge(paired, on="trade_date", how="left")

    result["ts_code"] = ts_code
    result = align_columns(result, VALUATION_HISTORY_COLUMNS, owner)
    return result.sort_values(by="trade_date").reset_index(drop=True)


def normalize_industry_valuation(raw, stat_date, classification):
    """
    行业估值横截面归一化：巨潮行业 PE → market.industry_valuations 契约列

    Args:
        raw (pd.DataFrame): 源表
        stat_date: 统计日期（str 或 datetime.date）
        classification (str): 行业分类体系名

    Returns:
        pd.DataFrame: INDUSTRY_VALUATION_COLUMNS 契约列序

    Raises:
        DataContractError: 缺少必需列时抛出
    """
    owner = "akshare.stock_industry_pe_ratio_cninfo"
    require_columns(
        raw,
        (
            "行业编码", "行业层级", "行业名称", "公司数量", "纳入计算公司数量",
            "总市值-静态", "净利润-静态",
            "静态市盈率-加权平均", "静态市盈率-中位数", "静态市盈率-算术平均",
        ),
        owner,
    )

    data = {
        "industry_code": raw["行业编码"].astype(str),
        "stat_date": pd.to_datetime(stat_date).date(),
        "classification": classification,
        "industry_level": pd.to_numeric(
            raw["行业层级"], errors="coerce"
        ).astype("Int64"),
        "industry_name": raw["行业名称"].astype(str).str.strip(),
        "company_count": to_numeric_column(raw, "公司数量"),
        "priced_company_count": to_numeric_column(raw, "纳入计算公司数量"),
        "total_market_value": to_numeric_column(raw, "总市值-静态"),
        "net_profit": to_numeric_column(raw, "净利润-静态"),
        "pe_weighted": to_numeric_column(raw, "静态市盈率-加权平均"),
        "pe_median": to_numeric_column(raw, "静态市盈率-中位数"),
        "pe_arithmetic": to_numeric_column(raw, "静态市盈率-算术平均"),
    }
    return align_columns(pd.DataFrame(data), INDUSTRY_VALUATION_COLUMNS, owner)


def normalize_index_membership(raw):
    """
    指数成分归一化：中证官网成分表 → reference.index_memberships 契约列

    Args:
        raw (pd.DataFrame): 源表，必需列 成分券代码/指数代码/指数名称/日期

    Returns:
        pd.DataFrame: INDEX_MEMBERSHIP_COLUMNS 契约列序（已按主键去重）

    Raises:
        DataContractError: 缺少必需列时抛出
    """
    owner = "akshare.index_stock_cons_csindex"
    require_columns(raw, ("成分券代码", "指数代码", "指数名称", "日期"), owner)

    codes = raw["成分券代码"].astype(str).str.zfill(6)
    data = {
        "ts_code": codes.apply(normalize_ts_code),
        "index_code": str(raw["指数代码"].iloc[0]),
        "index_name": str(raw["指数名称"].iloc[0]),
        "effective_date": to_date_series(raw["日期"]).dt.date,
    }
    result = align_columns(pd.DataFrame(data), INDEX_MEMBERSHIP_COLUMNS, owner)
    # 同一次返回里生效日期一致，去重后写入
    return result.drop_duplicates(
        subset=["ts_code", "index_code", "effective_date"]
    ).reset_index(drop=True)


def normalize_company_profile(raw, ts_code):
    """
    公司概况归一化：巨潮概况行 → 补列帧（industry / list_date / index_membership）

    【与库表的关系】
       本帧不是库表（多出 index_membership 列），由调用方用于回填
       reference.securities 的 industry / list_date 等空列。

    Args:
        raw (pd.DataFrame): 源表（单行）
        ts_code (str): 证券代码

    Returns:
        pd.DataFrame: 单行表，含 ts_code / industry / list_date / index_membership
    """
    row = raw.iloc[0]
    data = {
        "ts_code": ts_code,
        "industry": clean_text_value(row["所属行业"]) if "所属行业" in row else "",
        "list_date": clean_date_value(row["上市日期"]) if "上市日期" in row else None,
        "index_membership": (
            clean_text_value(row["入选指数"]) if "入选指数" in row else ""
        ),
    }
    # 全标量必须包成「行列表」，直接传字典会抛
    # ValueError: If using all scalar values, you must pass an index
    return pd.DataFrame([data])
