#!/usr/bin/env python3
"""
==============================================================================
StockLab - 交易所上市/退市日历归一化 (stocklab.normalization.exchange)
==============================================================================

【模块职责】
   把沪深北三所官网的上市与退市日历（源列名各不相同）归一化为：
     - 上市日历帧   ts_code / name / list_date
     - 退市日历帧   ts_code / name / list_date / delist_date
     - 生命周期合并帧（证券主表契约列，补全 list_date / delist_date / status）
     - 生命周期事件帧（LISTED / DELISTED）

【为何必须做这件事】
   东财名录只含当前在市证券（5572 只），退市股票不在其中；若历史研究直接
   用「当前股票池」，会把未来退市的公司从历史样本里抹掉 → 幸存者偏差。
   用交易所官方上市/退市日历补齐 list_date / delist_date 并写入生命周期事件后，
   universe(as_of) 才能还原任意历史时点真实存在的证券集合。

【列名差异处理】
   各所同义列名不一致（上市日期 / A股上市日期；公司代码 / A股代码 / 证券代码），
   统一由 pick_column 按候选顺序探测，全部缺失时抛 DataContractError 显式失败。
"""

import pandas as pd

from stocklab.domain import (
    DataContractError,
    SECURITY_COLUMNS,
    SECURITY_EVENT_COLUMNS,
    align_columns,
)
from stocklab.normalization.base import normalize_ts_code, pick_column

__all__ = [
    "normalize_listing_calendar",
    "normalize_delisting_calendar",
    "merge_lifecycle",
    "build_lifecycle_events",
]

# 当前在市证券日历的候选列名（按优先级）
_LISTING_CODE_COLUMNS = ("A股代码", "证券代码", "公司代码")
_LISTING_DATE_COLUMNS = ("A股上市日期", "上市日期")
_LISTING_NAME_COLUMNS = ("A股简称", "证券简称", "公司简称")

# 退市日历的候选列名：终止上市日与暂停上市日并列（沪市只提供暂停上市日）
_DELISTING_CODE_COLUMNS = ("证券代码", "公司代码")
_DELISTING_DATE_COLUMNS = ("终止上市日期", "暂停上市日期")
_DELISTING_NAME_COLUMNS = ("证券简称", "公司简称")

# 生命周期事件的数据源标识
_EVENT_SOURCE = "exchange_calendar"


def normalize_listing_calendar(raw_frames):
    """
    归一化当前在市证券的上市日历（多所合并）

    Args:
        raw_frames (list): 各所上市日历源表列表（列名可各不相同）

    Returns:
        pd.DataFrame: 列 ts_code / name / list_date，按 ts_code 升序

    Raises:
        DataContractError: 某个源表缺少代码列或上市日期列时抛出
    """
    rows = []
    for raw in raw_frames:
        if raw is None or raw.empty:
            continue
        codes = pick_column(raw, _LISTING_CODE_COLUMNS)
        dates = pick_column(raw, _LISTING_DATE_COLUMNS)
        names = pick_column(raw, _LISTING_NAME_COLUMNS)
        if codes is None or dates is None:
            raise DataContractError(
                "exchange.listing_calendar 源列不匹配，实际列: %s"
                % ", ".join(list(raw.columns)[:8])
            )
        for index in range(len(raw)):
            rows.append(
                {
                    "ts_code": normalize_ts_code(codes.iloc[index]),
                    "name": str(names.iloc[index]) if names is not None else "",
                    "list_date": _to_date(dates.iloc[index]),
                }
            )
    return _calendar_frame(rows, ["ts_code", "name", "list_date"], "list_date")


def normalize_delisting_calendar(raw_frames):
    """
    归一化退市日历（多所合并）

    Args:
        raw_frames (list): 各所退市日历源表列表

    Returns:
        pd.DataFrame: 列 ts_code / name / list_date / delist_date，按 ts_code 升序

    Raises:
        DataContractError: 某个源表缺少代码列或退市日期列时抛出
    """
    rows = []
    for raw in raw_frames:
        if raw is None or raw.empty:
            continue
        codes = pick_column(raw, _DELISTING_CODE_COLUMNS)
        dates = pick_column(raw, _DELISTING_DATE_COLUMNS)
        names = pick_column(raw, _DELISTING_NAME_COLUMNS)
        listed = pick_column(raw, _LISTING_DATE_COLUMNS)
        if codes is None or dates is None:
            raise DataContractError(
                "exchange.delisting_calendar 源列不匹配，实际列: %s"
                % ", ".join(list(raw.columns)[:8])
            )
        for index in range(len(raw)):
            delist_date = _to_date(dates.iloc[index])
            if delist_date is None:
                continue
            rows.append(
                {
                    "ts_code": normalize_ts_code(codes.iloc[index]),
                    "name": str(names.iloc[index]) if names is not None else "",
                    "list_date": _to_date(listed.iloc[index]) if listed is not None else None,
                    "delist_date": delist_date,
                }
            )
    return _calendar_frame(
        rows,
        ["ts_code", "name", "list_date", "delist_date"],
        "delist_date",
    )


def merge_lifecycle(securities_df, listing_df, delisting_df):
    """
    把上市/退市日历合并进证券主表契约帧（供 Repository 幂等 upsert）

    【合并规则】
       1. 既有证券：回填 list_date / delist_date（已有值优先，缺则用日历值）；
       2. 退市证券不在既有名录中的：整行补入 —— 否则退市股永远进不了股票池；
       3. 状态统一按日期推导：退市日 <= 今天 → DELISTED，
          上市日 > 今天 → PRE_LIST，其余 → LISTED。

    Args:
        securities_df (pd.DataFrame): 既有证券帧（至少含 ts_code 等主表列）
        listing_df (pd.DataFrame): 上市日历（ts_code / name / list_date）
        delisting_df (pd.DataFrame): 退市日历（ts_code / name / list_date / delist_date）

    Returns:
        pd.DataFrame: SECURITY_COLUMNS 契约列序的完整证券帧
    """
    today = pd.Timestamp.now().date()
    listing_dates = _date_map(listing_df, "list_date")
    delisting_dates = _date_map(delisting_df, "delist_date")
    # 退市日历往往自带上市日期：退市股早已不在「当前在市」日历里，
    # 不读这一路会把它们的 list_date 留成 NULL（universe 无法按上市日推导）
    delisting_list_dates = _date_map(delisting_df, "list_date")

    data = {}
    for column in SECURITY_COLUMNS:
        data[column] = (
            securities_df[column].tolist()
            if column in securities_df.columns
            else None
        )
    merged = pd.DataFrame(data)

    merged["list_date"] = [
        _coalesce(_coalesce(value, listing_dates.get(ts_code)),
                  delisting_list_dates.get(ts_code))
        for ts_code, value in zip(merged["ts_code"], merged["list_date"])
    ]
    merged["delist_date"] = [
        _coalesce(value, delisting_dates.get(ts_code))
        for ts_code, value in zip(merged["ts_code"], merged["delist_date"])
    ]

    # 退市日历中不在名录里的证券：整行补入（关键：退市股必须进入样本池）
    if not delisting_df.empty:
        known_codes = set(merged["ts_code"])
        extra_rows = []
        for _, row in delisting_df.iterrows():
            ts_code = row["ts_code"]
            if ts_code in known_codes:
                continue
            extra_rows.append(
                {
                    "ts_code": ts_code,
                    "symbol": str(ts_code).split(".")[0],
                    "name": row["name"],
                    "exchange": ts_code.split(".")[-1],
                    "market": ts_code.split(".")[-1],
                    "industry": "",
                    "area": "",
                    "list_date": row["list_date"],
                    "delist_date": row["delist_date"],
                    "status": "DELISTED",
                    "is_hs": "",
                }
            )
        if extra_rows:
            merged = pd.concat([merged, pd.DataFrame(extra_rows)], ignore_index=True)

    merged["status"] = [
        _derive_status(list_date, delist_date, today)
        for list_date, delist_date in zip(merged["list_date"], merged["delist_date"])
    ]
    return align_columns(merged, SECURITY_COLUMNS, "lifecycle.securities")


def build_lifecycle_events(listing_df, delisting_df):
    """
    由上市/退市日历构建生命周期事件帧（LISTED / DELISTED）

    Args:
        listing_df (pd.DataFrame): 上市日历（ts_code / name / list_date）
        delisting_df (pd.DataFrame): 退市日历（ts_code / name / list_date / delist_date）

    Returns:
        pd.DataFrame: SECURITY_EVENT_COLUMNS 契约列序，主键已去重
    """
    rows = []
    if not listing_df.empty:
        for _, row in listing_df.iterrows():
            if row["list_date"] is None:
                continue
            rows.append(
                {
                    "ts_code": row["ts_code"],
                    "event_date": row["list_date"],
                    "event_type": "LISTED",
                    "detail": row["name"],
                    "source": _EVENT_SOURCE,
                }
            )
    if not delisting_df.empty:
        for _, row in delisting_df.iterrows():
            if row["list_date"] is not None:
                rows.append(
                    {
                        "ts_code": row["ts_code"],
                        "event_date": row["list_date"],
                        "event_type": "LISTED",
                        "detail": row["name"],
                        "source": _EVENT_SOURCE,
                    }
                )
            if row["delist_date"] is not None:
                rows.append(
                    {
                        "ts_code": row["ts_code"],
                        "event_date": row["delist_date"],
                        "event_type": "DELISTED",
                        "detail": row["name"],
                        "source": _EVENT_SOURCE,
                    }
                )
    if not rows:
        return pd.DataFrame(columns=list(SECURITY_EVENT_COLUMNS))
    result = pd.DataFrame(rows).drop_duplicates(
        subset=["ts_code", "event_date", "event_type"]
    )
    return align_columns(
        result.reset_index(drop=True),
        SECURITY_EVENT_COLUMNS,
        "lifecycle.security_events",
    )


def _calendar_frame(rows, columns, required_date_column):
    """
    把日历行列表整理为按 ts_code 升序的帧，丢弃关键日期缺失的行

    Args:
        rows (list): 行字典列表
        columns (tuple): 目标列
        required_date_column (str): 必须有效的日期列

    Returns:
        pd.DataFrame: 清洗去重后的日历帧
    """
    if not rows:
        return pd.DataFrame(columns=list(columns))
    result = pd.DataFrame(rows)
    result = result[result[required_date_column].notna()]
    return (
        result.drop_duplicates(subset=["ts_code"], keep="last")
        .sort_values(by="ts_code")
        .reset_index(drop=True)
    )


def _date_map(frame, column):
    """
    把日历帧的 (ts_code, 日期) 建成字典，日期缺失的行不入表

    Args:
        frame (pd.DataFrame): 日历帧
        column (str): 日期列名

    Returns:
        dict: {ts_code: date}
    """
    if frame is None or frame.empty:
        return {}
    pairs = []
    for _, row in frame.iterrows():
        value = _to_date(row[column])
        if value is None:
            continue
        pairs.append((row["ts_code"], value))
    return dict(pairs)


def _coalesce(existing, incoming):
    """
    已有值优先，缺失时用日历值

    Args:
        existing: 库内已有值（可能是 None / NaN / NaT）
        incoming: 日历值

    Returns:
        datetime.date 或 None: 归一化后的日期，两者皆缺时为 None
    """
    normalized = _to_date(existing)
    if normalized is None:
        return _to_date(incoming)
    return normalized


def _to_date(value):
    """
    把任意来源的值归一为 date；None / NaN / NaT / 无法解析的值返回 None

    Args:
        value: 原始值（date、Timestamp、字符串、None、NaT 等）

    Returns:
        datetime.date 或 None
    """
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    # NaT / NaN 与自身不相等，据此识别缺失；NaT 直接比较 date 会抛 TypeError
    if value != value:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if parsed is None or parsed != parsed:
        return None
    return parsed.date()


def _derive_status(list_date, delist_date, today):
    """
    由上市日与退市日推导证券状态

    Args:
        list_date: 上市日期（可为 None / NaT）
        delist_date: 退市日期（可为 None / NaT）
        today: 判断时点

    Returns:
        str: PRE_LIST / LISTED / DELISTED
    """
    delist_date = _to_date(delist_date)
    list_date = _to_date(list_date)
    if delist_date is not None and delist_date <= today:
        return "DELISTED"
    if list_date is not None and list_date > today:
        return "PRE_LIST"
    return "LISTED"
