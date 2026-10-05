#!/usr/bin/env python3
"""
==============================================================================
StockLab - 申万行业指数数据源 (stocklab.datasource.sw_indices)
==============================================================================

【模块职责】
   申万一/二级行业指数的行情采集与行业映射表维护。
   数据源：AKShare index_hist_sw（申万宏源官网历史行情）/ index_realtime_sw（指数列表），
           兼容旧版 sw_index_daily / sw_index_first / sw_index_second（新版本已移除）。

【为什么要做接口探测】
   akshare 每个小版本都可能重命名或删除函数（1.18.x 起 sw_index_* 系列已不存在），
   直接调用会抛 AttributeError 并让整条同步链路静默失败。这里一律
   `hasattr` 探测 + 新旧两套实现回退，接口变更时降级到可用通道并告警。
"""


from dataclasses import dataclass
from typing import Optional
import logging

import pandas as pd

from stocklab.domain import (
    SW_INDEX_DAILY_COLUMNS,
    SW_INDUSTRY_MAPPING_COLUMNS,
)
from stocklab.domain.contract import require_columns, align_columns

_logger = logging.getLogger(__name__)

__all__ = [
    "SWIndexSpec",
    "SW_INDUSTRY_LEVELS",
    "fetch_sw_index_daily",
    "fetch_sw_industry_mapping",
    "SW_INDEX_SOURCE_AKSHARE",
]

# 申万行业层级
SW_INDUSTRY_LEVELS = (1, 2)

# 采集通道标识
SW_INDEX_SOURCE_AKSHARE = "akshare:sw_index_daily"


def _fetch_sw_history(ak, symbol: str, start_date: str, end_date: str):
    """
    抓取申万行业指数历史行情（新旧接口回退）

    新版 akshare 用 `index_hist_sw(symbol, period)`（申万宏源官网，全历史），
    旧版用 `sw_index_daily(symbol, start_date, end_date)`；两者都返回中文列。
    返回原始 DataFrame；两条通道都不可用/失败时返回 None。
    """
    code = str(symbol).split(".")[0]

    if hasattr(ak, "index_hist_sw"):
        try:
            raw = ak.index_hist_sw(symbol=code, period="day")
            if raw is not None and not raw.empty:
                return raw
            _logger.warning("index_hist_sw(%s) 返回空", code)
        except Exception as exc:
            _logger.warning("index_hist_sw(%s) 失败：%s", code, exc)

    if hasattr(ak, "sw_index_daily"):        # 旧版 akshare 兼容通道
        try:
            return ak.sw_index_daily(
                symbol=symbol,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            )
        except Exception as exc:
            _logger.warning("sw_index_daily(%s) 失败：%s", symbol, exc)

    _logger.error("申万指数历史行情无可用接口（akshare 版本 %s）",
                  getattr(ak, "__version__", "unknown"))
    return None


@dataclass(frozen=True)
class SWIndexSpec:
    """单个申万行业指数的登记项"""
    symbol: str          # 如 "801010.SI"
    name: str            # 如 "农林牧渔"
    level: int           # 1 或 2
    parent_code: str = ""  # 二级行业对应的一级行业代码


def fetch_sw_index_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    采集单个申万行业指数的日线行情

    Args:
        symbol: 指数代码，如 "801010.SI"
        start_date: 起始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD

    Returns:
        pd.DataFrame: SW_INDEX_DAILY_COLUMNS 契约列序
    """
    # 延迟导入 AKShare
    import akshare as ak

    raw = _fetch_sw_history(ak, symbol, start_date, end_date)

    if raw is None or raw.empty:
        _logger.warning("申万指数 [%s] 返回空数据", symbol)
        return pd.DataFrame(columns=list(SW_INDEX_DAILY_COLUMNS))

    # 标准化列名（AKShare 返回中文列名）
    # 标准列：日期、开盘、收盘、最高、最低、成交量、成交额
    required = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]
    missing = [c for c in required if c not in raw.columns]
    if missing:
        _logger.error("申万指数 [%s] 缺列 %s，实际列: %s", symbol, missing, list(raw.columns))
        return pd.DataFrame(columns=list(SW_INDEX_DAILY_COLUMNS))

    # 归一化
    frame = raw.rename(columns={
        "日期": "trade_date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
    }).copy()

    # 类型转换
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.date
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    # 剔除无效行
    frame = frame.dropna(subset=["trade_date", "close"])
    frame = frame[frame["close"] > 0]

    # 新版接口返回全历史，按调用区间裁剪（旧接口已在远端裁剪，重复过滤无害）
    if not frame.empty:
        stamps = pd.to_datetime(frame["trade_date"])
        frame = frame[(stamps >= pd.Timestamp(start_date)) & (stamps <= pd.Timestamp(end_date))]

    if frame.empty:
        _logger.warning("申万指数 [%s] 归一化后无有效数据", symbol)
        return pd.DataFrame(columns=list(SW_INDEX_DAILY_COLUMNS))

    # 补全契约列
    from datetime import datetime
    now = datetime.now()
    frame["symbol"] = symbol
    frame["source"] = SW_INDEX_SOURCE_AKSHARE
    frame["fetched_at"] = now

    # 契约列序
    frame = frame[list(SW_INDEX_DAILY_COLUMNS)]
    frame = frame.sort_values("trade_date").reset_index(drop=True)

    _logger.info("申万指数 [%s] 采集完成: %d 条，%s ~ %s",
                 symbol, len(frame), frame["trade_date"].iloc[0], frame["trade_date"].iloc[-1])
    return frame


def _sw_index_list(ak, level: int) -> pd.DataFrame:
    """
    获取申万行业指数列表（列名统一为 index_code / index_name）

    新版 akshare：`index_realtime_sw(symbol="一级行业"|"二级行业")`
    旧版：`sw_index_first()` / `sw_index_second()`
    """
    if hasattr(ak, "index_realtime_sw"):
        try:
            raw = ak.index_realtime_sw(symbol="一级行业" if level == 1 else "二级行业")
            if raw is not None and not raw.empty and "指数代码" in raw.columns:
                return raw.rename(columns={"指数代码": "index_code", "指数名称": "index_name"})[
                    ["index_code", "index_name"]
                ]
        except Exception as exc:
            _logger.warning("index_realtime_sw(level=%d) 失败：%s", level, exc)

    legacy = "sw_index_first" if level == 1 else "sw_index_second"
    if hasattr(ak, legacy):
        try:
            raw = getattr(ak, legacy)()
            if raw is not None and not raw.empty and "index_code" in raw.columns:
                return raw[["index_code", "index_name"]]
        except Exception as exc:
            _logger.warning("%s 失败：%s", legacy, exc)

    _logger.error("申万 %d 级行业列表无可用接口", level)
    return pd.DataFrame(columns=["index_code", "index_name"])


def fetch_sw_industry_mapping() -> pd.DataFrame:
    """
    获取申万行业分类映射表（一级 + 二级）

    Returns:
        pd.DataFrame: SW_INDUSTRY_MAPPING_COLUMNS 契约列序
    """
    import akshare as ak

    frames = []
    for level in (1, 2):
        listing = _sw_index_list(ak, level)
        if listing.empty:
            continue
        listing = listing.copy()
        listing["level"] = level
        listing["parent_code"] = ""
        listing["sw_first_code"] = ""
        listing["description"] = ""
        frames.append(listing)

    if not frames:
        return pd.DataFrame(columns=list(SW_INDUSTRY_MAPPING_COLUMNS))

    result = pd.concat(frames, ignore_index=True)

    # 二级行业补父级一级代码（sw_index_second_info 的「上级行业」是名称，需回查代码）
    if hasattr(ak, "sw_index_second_info"):
        try:
            info = ak.sw_index_second_info()
            name_to_code = dict(zip(result.loc[result["level"] == 1, "index_name"],
                                    result.loc[result["level"] == 1, "index_code"]))
            info = info.rename(columns={"行业代码": "index_code", "上级行业": "parent_name"})
            parent_codes = info["parent_name"].map(name_to_code)
            mask = result["level"] == 2
            lookup = dict(zip(info["index_code"].astype(str), parent_codes))
            result.loc[mask, "parent_code"] = (
                result.loc[mask, "index_code"].astype(str).map(lookup).fillna("")
            )
            result.loc[mask, "sw_first_code"] = result.loc[mask, "parent_code"]
        except Exception as exc:
            _logger.warning("申万二级行业父级信息获取失败：%s", exc)

    result = result.reindex(columns=list(SW_INDUSTRY_MAPPING_COLUMNS))
    _logger.info("申万行业映射：%d 条（一级 %d / 二级 %d）",
                 len(result),
                 int((result["level"] == 1).sum()),
                 int((result["level"] == 2).sum()))
    return result