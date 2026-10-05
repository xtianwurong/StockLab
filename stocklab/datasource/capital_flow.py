#!/usr/bin/env python3
"""
==============================================================================
StockLab - 资金流数据源 (stocklab.datasource.capital_flow)
==============================================================================

【模块职责】
   板块/行业资金流数据采集
   数据源：AKShare 板块资金流接口
"""


import logging
from typing import Dict, Literal, Optional, Tuple
from datetime import date, datetime, timedelta

import pandas as pd

from stocklab.domain import CAPITAL_FLOW_DAILY_COLUMNS

_logger = logging.getLogger(__name__)

__all__ = [
    "CAPITAL_FLOW_SOURCE_AKSHARE",
    "CAPITAL_FLOW_SOURCE_SINA_INDIVIDUAL",
    "fetch_sector_capital_flow",
    "fetch_stock_fund_flow_individual",
    "fetch_sw_capital_flow",
    "aggregate_stock_flow_to_sw",
    "fetch_sw_level1_capital_flow",
    "fetch_sw_level2_capital_flow",
    "fetch_concept_capital_flow",
]

CAPITAL_FLOW_SOURCE_AKSHARE = "akshare:capital_flow"

# 东财板块资金流的合法取值（akshare 1.x 中文标签）
_EASTMONEY_SECTOR_TYPES = {
    "industry": "行业资金流",
    "concept": "概念资金流",
    "region": "地域资金流",
}

# 新浪全市场个股资金流（即时快照）的来源标识
CAPITAL_FLOW_SOURCE_SINA_INDIVIDUAL = "sina:individual_fund_flow"

# 新浪返回的金额带单位（"39.39亿" / "696.46万" / "12345"）
_AMOUNT_UNITS = {"万亿": 1e12, "亿": 1e8, "万": 1e4, "元": 1.0}

SectorType = Literal["sw_level1", "sw_level2", "concept", "industry"]


def fetch_sector_capital_flow(
    sector_type: SectorType,
    start_date: str,
    end_date: str = None,
    top_n: int = 100
) -> pd.DataFrame:
    """
    获取板块资金流数据

    Args:
        sector_type: 板块类型
        start_date: 起始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD，默认今天
        top_n: 获取前 N 个板块

    Returns:
        pd.DataFrame: CAPITAL_FLOW_DAILY_COLUMNS 契约列序
    """
    import akshare as ak

    if end_date is None:
        end_date = date.today().strftime("%Y-%m-%d")

    # 东财口径只覆盖行业/概念/地域三类板块，**没有申万口径**。
    # akshare 1.x 的 sector_type 用中文标签；旧代码传 "sw1"/"concept"/"industry"
    # 全是非法值 → KeyError 被 except 吞成空表，接口永远回 0 行却显示 200。
    # 申万口径请走 fetch_sw_capital_flow()（新浪个股资金流按申万映射聚合）。
    if sector_type in ("sw_level1", "sw_level2"):
        _logger.warning(
            "东财板块资金流没有申万口径（sector_type=%s），请改用 fetch_sw_capital_flow()",
            sector_type,
        )
        return pd.DataFrame(columns=list(CAPITAL_FLOW_DAILY_COLUMNS))

    sub_type = _EASTMONEY_SECTOR_TYPES.get(sector_type)
    if sub_type is None:
        _logger.warning("东财板块资金流不支持的类型: %s", sector_type)
        return pd.DataFrame(columns=list(CAPITAL_FLOW_DAILY_COLUMNS))

    try:
        df = ak.stock_sector_fund_flow_rank(
            indicator="今日",
            sector_type=sub_type,
        )
    except Exception as e:
        _logger.warning("获取 %s 资金流失败: %s", sector_type, e)
        return pd.DataFrame(columns=list(CAPITAL_FLOW_DAILY_COLUMNS))

    if df is None or df.empty:
        return pd.DataFrame(columns=list(CAPITAL_FLOW_DAILY_COLUMNS))

    # AKShare 返回列：板块名称、板块代码、今日涨跌幅、主力净流入、主力净流入占比、...
    # 标准化列名
    rename_map = {
        "板块名称": "sector_name",
        "板块代码": "sector_code",
        "今日涨跌幅": "change_pct",
        "主力净流入": "main_net_inflow",
        "主力净流入占比": "main_net_inflow_rate",
        "超大单净流入": "super_net_inflow",
        "大单净流入": "large_net_inflow",
        "中单净流入": "medium_net_inflow",
        "小单净流入": "small_net_inflow",
    }

    df = df.rename(columns=rename_map)

    # 只保留需要的列
    keep_cols = ["sector_code", "sector_name"]
    for c in ["change_pct", "main_net_inflow", "main_net_inflow_rate",
              "super_net_inflow", "large_net_inflow", "medium_net_inflow", "small_net_inflow"]:
        if c in df.columns:
            keep_cols.append(c)

    df = df[keep_cols].copy()

    # 类型转换
    for col in ["change_pct", "main_net_inflow", "main_net_inflow_rate",
                "super_net_inflow", "large_net_inflow", "medium_net_inflow", "small_net_inflow"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 计算衍生字段
    if "main_net_inflow" in df.columns and "small_net_inflow" in df.columns:
        df["net_inflow"] = df["main_net_inflow"] + df.get("super_net_inflow", 0) + df.get("large_net_inflow", 0) + df.get("medium_net_inflow", 0) + df.get("small_net_inflow", 0)
    else:
        df["net_inflow"] = df.get("main_net_inflow", 0)

    # 净流入率 = 净流入 / 成交额（百分比）。源自带的「主力净流入占比」就是这个量纲，
    # 早先写的 net_inflow / 1e8 是个不成立的近似（把 1 亿当成了成交额），
    # 量纲上既不是百分比也不是小数，下游拿它做门槛判断会得到随机结论。
    if "main_net_inflow_rate" in df.columns:
        df["net_inflow_rate"] = pd.to_numeric(df["main_net_inflow_rate"], errors="coerce")
    else:
        df["net_inflow_rate"] = None

    # 注意：不要在这里 `from datetime import date` —— 函数开头 end_date 默认值
    # 已经在用模块级 date，函数体内再导入会让整函数把 date 当局部变量，
    # end_date=None 时直接 UnboundLocalError
    from datetime import datetime
    trade_date = date.today()
    now = datetime.now()

    df["sector_type"] = sector_type
    df["trade_date"] = trade_date
    df["inflow"] = df.get("main_net_inflow", 0) + df.get("super_net_inflow", 0) + df.get("large_net_inflow", 0)
    df["outflow"] = df.get("medium_net_inflow", 0) + df.get("small_net_inflow", 0)
    df["main_net_inflow"] = df.get("main_net_inflow", 0)
    df["retail_net_inflow"] = df.get("small_net_inflow", 0)
    df["source"] = CAPITAL_FLOW_SOURCE_AKSHARE
    df["fetched_at"] = now

    # 补全缺失列
    for col in CAPITAL_FLOW_DAILY_COLUMNS:
        if col not in df.columns:
            df[col] = None

    df = df[list(CAPITAL_FLOW_DAILY_COLUMNS)]

    # 限制数量
    if top_n and len(df) > top_n:
        df = df.head(top_n)

    return df


def _parse_amount(text) -> Optional[float]:
    """
    把新浪的带单位金额换成元："39.39亿" -> 3.939e9，"696.46万" -> 6.9646e6

    Returns:
        Optional[float]: 解析不了返回 None（**不返回 0** —— 0 会参与求和把板块金额做小）
    """
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        value = float(text)
        # NaN 必须判 None：NaN 参与 groupby.sum() 会把整组金额变成 NaN
        return None if value != value else value

    raw = str(text).strip().replace(",", "").replace("元", "")
    if not raw or raw in ("-", "--", "nan", "None"):
        return None

    for suffix, factor in _AMOUNT_UNITS.items():
        if suffix == "元":
            continue
        if raw.endswith(suffix):
            try:
                return float(raw[: -len(suffix)]) * factor
            except ValueError:
                return None
    try:
        return float(raw)
    except ValueError:
        return None


def fetch_stock_fund_flow_individual() -> pd.DataFrame:
    """
    新浪-全市场个股资金流（即时快照，约 5200 只 / 105 页，约 12s）

    【为什么用它】
      东财 push2 系域名在本机持续 502，板块资金流拿不到；新浪可用。
      该接口是**全市场个股**口径，配合本地申万映射可聚合出真正的申万行业资金流
      （实测 join 命中率 99.9%），比拿新浪自有的 90 个行业名去猜申万口径可靠得多。

    【口径要说清楚】
      流入 + 流出 ≈ 成交额，说明这是**全单**资金流而非主力资金流，
      因此聚合后 main_net_inflow / retail_net_inflow 一律留 None（源没有，不编）。

    Returns:
        pd.DataFrame: stock_code(6位) / stock_name / inflow / outflow / net_inflow / turnover（单位：元）
    """
    columns = ["stock_code", "stock_name", "inflow", "outflow", "net_inflow", "turnover"]
    try:
        import akshare as ak
        raw = ak.stock_fund_flow_individual(symbol="即时")
    except Exception as exc:
        _logger.warning("新浪个股资金流采集失败：%s", exc)
        return pd.DataFrame(columns=columns)

    if raw is None or raw.empty:
        return pd.DataFrame(columns=columns)

    rename = {
        "股票代码": "stock_code",
        "股票简称": "stock_name",
        "流入资金": "inflow",
        "流出资金": "outflow",
        "净额": "net_inflow",
        "成交额": "turnover",
    }
    frame = raw.rename(columns=rename)
    for column in columns:
        if column not in frame.columns:
            frame[column] = None
    frame = frame[columns].copy()

    for column in ("inflow", "outflow", "net_inflow", "turnover"):
        frame[column] = frame[column].map(_parse_amount)

    frame["stock_code"] = (frame["stock_code"].astype(str).str.strip().str.zfill(6))
    frame = frame[frame["stock_code"].str.fullmatch(r"\d{6}", na=False)]
    frame = frame.drop_duplicates(subset=["stock_code"])
    return frame.reset_index(drop=True)


def aggregate_stock_flow_to_sw(
    individual: pd.DataFrame,
    mapping: pd.DataFrame,
    level: int = 1,
    trade_date=None,
) -> Tuple[pd.DataFrame, Dict]:
    """
    把个股资金流按申万映射聚合成行业资金流

    Args:
        individual: fetch_stock_fund_flow_individual() 的结果
        mapping: fund.stock_industry_mapping（ts_code, sw_l{level}_code, sw_l{level}_name）
        level: 1 或 2
        trade_date: 快照归属交易日（由调用方给最近交易日，避免把假日数据记成今天）

    Returns:
        (frame, meta)：frame 为 CAPITAL_FLOW_DAILY_COLUMNS 契约列序；
        meta 含 stock_coverage（个股 join 覆盖率），**不覆盖的个股就是没有，不补**
    """
    empty = pd.DataFrame(columns=list(CAPITAL_FLOW_DAILY_COLUMNS))
    if individual is None or individual.empty or mapping is None or mapping.empty:
        return empty, {"sector_count": 0, "stock_matched": 0, "stock_total": 0,
                       "stock_coverage": 0.0}

    if level not in (1, 2):
        raise ValueError(f"level 必须是 1 或 2，收到 {level}")

    code_col = f"sw_l{level}_code"
    name_col = f"sw_l{level}_name"
    needed = {"ts_code", code_col, name_col}
    missing = needed - set(mapping.columns)
    if missing:
        _logger.error("行业映射缺少列 %s，无法聚合资金流", ", ".join(sorted(missing)))
        return empty, {"sector_count": 0, "stock_matched": 0, "stock_total": 0,
                       "stock_coverage": 0.0}

    cleaned = mapping[list(needed)].copy()
    cleaned["stock_code"] = cleaned["ts_code"].astype(str).str[:6]
    cleaned = cleaned[cleaned[code_col].astype(str).str.strip().ne("")]
    cleaned = cleaned.drop_duplicates(subset=["stock_code"], keep="first")

    merged = individual.merge(cleaned, on="stock_code", how="inner")
    if merged.empty:
        _logger.error("个股资金流与行业映射 join 后为 0 行（映射列=%s）", code_col)
        return empty, {"sector_count": 0, "stock_matched": 0,
                       "stock_total": int(len(individual)), "stock_coverage": 0.0}

    grouped = merged.groupby(code_col, sort=True).agg(
        sector_name=(name_col, "first"),
        inflow=("inflow", "sum"),
        outflow=("outflow", "sum"),
        net_inflow=("net_inflow", "sum"),
        turnover=("turnover", "sum"),
        stock_count=("stock_code", "nunique"),
    ).reset_index()

    if trade_date is None:
        trade_date = date.today()
    elif isinstance(trade_date, str):
        trade_date = pd.to_datetime(trade_date).date()

    frame = pd.DataFrame({
        "sector_code": grouped[code_col].astype(str),
        "sector_name": grouped["sector_name"].astype(str),
        "sector_type": f"sw_level{level}",
        "trade_date": trade_date,
        "net_inflow": grouped["net_inflow"],
        "inflow": grouped["inflow"],
        "outflow": grouped["outflow"],
        # 净流入 / 成交额（百分比）；成交额为 0 或缺失时留 None，不填 0 冒充「无流出」
        "net_inflow_rate": [
            (net / turnover * 100.0) if (turnover and turnover > 0) else None
            for net, turnover in zip(grouped["net_inflow"], grouped["turnover"])
        ],
        # 新浪是全单口径，没有主力/散户拆分 —— 源没有的字段一律 None
        "main_net_inflow": None,
        "retail_net_inflow": None,
        "source": CAPITAL_FLOW_SOURCE_SINA_INDIVIDUAL,
        "fetched_at": datetime.now(),
    })
    frame = frame[list(CAPITAL_FLOW_DAILY_COLUMNS)]

    matched = int(merged["stock_code"].nunique())
    total = int(len(individual))
    meta = {
        "sector_type": f"sw_level{level}",
        "sector_count": int(len(frame)),
        "stock_matched": matched,
        "stock_total": total,
        "stock_coverage": round(matched / total, 4) if total else 0.0,
    }
    return frame, meta


def fetch_sw_capital_flow(
    mapping: pd.DataFrame,
    level: int = 1,
    trade_date=None,
    top_n: Optional[int] = None,
) -> Tuple[pd.DataFrame, Dict]:
    """
    申万口径行业资金流：新浪个股资金流 -> 按申万映射聚合

    Returns:
        (frame, meta)：meta.stock_coverage 是个股 join 覆盖率，调用方应如实披露
    """
    individual = fetch_stock_fund_flow_individual()
    if individual.empty:
        return (pd.DataFrame(columns=list(CAPITAL_FLOW_DAILY_COLUMNS)),
                {"sector_count": 0, "stock_matched": 0, "stock_total": 0,
                 "stock_coverage": 0.0, "reason": "个股资金流采集失败"})

    frame, meta = aggregate_stock_flow_to_sw(individual, mapping, level=level,
                                             trade_date=trade_date)
    if top_n and len(frame) > top_n:
        frame = frame.reindex(frame["net_inflow"].abs().sort_values(ascending=False).index)
        frame = frame.head(int(top_n)).reset_index(drop=True)
    return frame, meta


def fetch_sw_level1_capital_flow(mapping: pd.DataFrame, trade_date=None) -> Tuple[pd.DataFrame, Dict]:
    """申万一级资金流（新浪个股聚合，返回 (frame, meta)）"""
    return fetch_sw_capital_flow(mapping, level=1, trade_date=trade_date)


def fetch_sw_level2_capital_flow(mapping: pd.DataFrame, trade_date=None) -> Tuple[pd.DataFrame, Dict]:
    """申万二级资金流（新浪个股聚合，返回 (frame, meta)）"""
    return fetch_sw_capital_flow(mapping, level=2, trade_date=trade_date)


def fetch_concept_capital_flow(start_date: str, end_date: str = None) -> pd.DataFrame:
    """概念板块资金流（东财口径；本机 push2 不可达时返回空表）"""
    return fetch_sector_capital_flow("concept", start_date, end_date)