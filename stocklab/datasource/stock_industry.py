#!/usr/bin/env python3
"""
==============================================================================
StockLab - 股票行业映射数据源 (stocklab.datasource.stock_industry)
==============================================================================

【模块职责】
   从 AKShare 获取股票的申万/中信/国证/Wind 行业分类映射
   静态维表，建议月更一次
"""


import logging
from typing import Optional

import pandas as pd

from stocklab.domain import STOCK_INDUSTRY_MAPPING_COLUMNS

from .sw_indices import fetch_sw_industry_mapping

_logger = logging.getLogger(__name__)

__all__ = [
    "STOCK_INDUSTRY_SOURCE_AKSHARE",
    "fetch_stock_industry_mapping",
]

STOCK_INDUSTRY_SOURCE_AKSHARE = "akshare:stock_industry"


def fetch_stock_industry_mapping(max_workers: int = 6) -> pd.DataFrame:
    """
    获取全市场股票的申万行业分类映射（一级 + 二级）

    【数据链路】
       sw_indices.fetch_sw_industry_mapping()  -> 一/二级指数清单
       ak.sw_index_second_info()               -> 二级 -> 一级 父子关系（按名称）
       ak.index_component_sw(code)             -> 该指数的成分股
       三者按 ts_code 合并成「一股票一行」

    【为何一级/二级要合并而非各自成行】
       表主键是 ts_code（一只股票一行），若一级与二级分别落行，
       去重时后到的二级行会被整批丢弃，导致 sw_l2_* 恒为空。
       这里分别抓取成分、再按 ts_code 把二级**回填**到一级行上；
       仅出现在二级清单里的股票，用「二级 -> 一级」父子关系反推一级。

    【并发与容错】
       一级 31 个 + 二级 131 个指数 = 162 次请求，串行约需 3~5 分钟，
       这里用线程池并发（纯 I/O）压到 1 分钟内。单个指数失败只记日志并
       继续，不因一个指数挂掉而丢掉整张映射表。

    Args:
        max_workers: 并发线程数

    Returns:
        pd.DataFrame: STOCK_INDUSTRY_MAPPING_COLUMNS 契约列序
    """
    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor

    listing = fetch_sw_industry_mapping()
    if listing.empty:
        _logger.error("申万行业指数清单为空，无法采集成分股")
        return pd.DataFrame(columns=list(STOCK_INDUSTRY_MAPPING_COLUMNS))

    l1_listing = listing[listing["level"] == 1][["index_code", "index_name"]]
    l2_listing = listing[listing["level"] == 2][["index_code", "index_name"]]
    # 表里存的行业代码是**不带 .SI 后缀**的（sw_l1_code/sw_l2_code 全表如此），
    # 而清单里的 index_code 有的接口给 "801010"、有的给 "801010.SI"；
    # 这里统一剥后缀，否则回填出来的一级代码会带上 .SI，与直采路径不一致，
    # 下游按裸码 join 时那一批股票就静默丢了。
    name_to_l1 = dict(zip(l1_listing["index_name"], l1_listing["index_code"].map(_plain_code)))
    parent_names = _sw_l2_parent_names(ak)     # {二级代码: 一级名称}

    targets = [(1, _plain_code(r["index_code"]), r["index_name"])
               for _, r in l1_listing.iterrows()]
    targets += [(2, _plain_code(r["index_code"]), r["index_name"])
                for _, r in l2_listing.iterrows()]

    _logger.info("开始采集申万成分股：%d 个指数（一级 %d / 二级 %d），并发 %d",
                 len(targets), len(l1_listing), len(l2_listing),
                 max(1, int(max_workers)))

    l1_of, l2_of, failed = {}, {}, []
    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
        results = pool.map(lambda t: _fetch_components(ak, t), targets)
        for (level, code, name), rows in zip(targets, results):
            if rows is None:
                failed.append(code)
                continue
            target = l1_of if level == 1 else l2_of
            for ts_code in rows:
                target.setdefault(ts_code, (code, name))

    if failed:
        _logger.warning("%d 个指数成分股采集失败：%s%s", len(failed),
                        "、".join(failed[:6]), "…" if len(failed) > 6 else "")
    if not l1_of and not l2_of:
        _logger.warning("未获取到任何行业成分数据，返回空表")
        return pd.DataFrame(columns=list(STOCK_INDUSTRY_MAPPING_COLUMNS))

    records = []
    for ts_code in sorted(set(l1_of) | set(l2_of)):
        l1_code, l1_name = l1_of.get(ts_code, ("", ""))
        l2_code, l2_name = l2_of.get(ts_code, ("", ""))

        if not l1_code and l2_code:
            # 只在二级清单里出现：用二级所属一级名称回查一级代码。
            # name_to_l1 是 name -> code 的**标量**映射，取不到时默认值也是字符串
            # 而不是二元组 —— 早先写成 `l1_code, l1_name = ....get(x, ("",""))`
            # 会把 "801010" 拆成 6 个字符，一走到这条兜底就 ValueError 崩掉整张表。
            parent_name = parent_names.get(l2_code, "")
            resolved = name_to_l1.get(parent_name, "")
            if resolved:
                l1_code, l1_name = _plain_code(resolved), parent_name

        records.append({
            "ts_code": ts_code,
            "sw_l1_code": l1_code,
            "sw_l1_name": l1_name,
            "sw_l2_code": l2_code,
            "sw_l2_name": l2_name,
            "updated_at": pd.Timestamp.now(),
        })

    df = pd.DataFrame(records).reindex(columns=STOCK_INDUSTRY_MAPPING_COLUMNS)
    for col in df.columns:
        if col in ("ts_code", "updated_at"):
            continue
        df[col] = df[col].fillna("")

    _logger.info("股票行业映射采集完成: %d 只股票（一级覆盖 %d / 二级覆盖 %d）",
                 len(df), int((df["sw_l1_code"] != "").sum()),
                 int((df["sw_l2_code"] != "").sum()))
    return df.reset_index(drop=True)


def _plain_code(code) -> str:
    """801016.SI -> 801016（其余原样返回）"""
    text = str(code or "").strip()
    return text.split(".")[0] if "." in text else text


def _sw_l2_parent_names(ak) -> dict:
    """二级行业代码 -> 所属一级行业名称（失败返回空 dict，调用方降级为不回填）"""
    if not hasattr(ak, "sw_index_second_info"):
        return {}
    try:
        info = ak.sw_index_second_info()
    except Exception as exc:
        _logger.warning("申万二级行业父子关系获取失败：%s", exc)
        return {}
    if info is None or info.empty:
        return {}
    needed = {"行业代码", "上级行业"}
    if not needed.issubset(info.columns):
        return {}
    return dict(zip(info["行业代码"].map(_plain_code), info["上级行业"].astype(str)))


def _fetch_components(ak, target) -> list:
    """
    抓取单个指数的成分股代码

    Args:
        ak: akshare 模块
        target: (level, code, name)

    Returns:
        list[str]：规范化后的 ts_code；失败返回 None（由调用方记失败清单）
    """
    _level, code, _name = target
    if not hasattr(ak, "index_component_sw"):
        _logger.error("当前 akshare 版本缺少 index_component_sw，无法采集成分股")
        return None
    try:
        frame = ak.index_component_sw(symbol=code)
    except Exception as exc:
        _logger.debug("指数 %s 成分股采集失败：%s", code, exc)
        return None
    if frame is None or frame.empty or "证券代码" not in frame.columns:
        return None

    codes = frame["证券代码"].map(_clean_stock_code)
    return [c for c in codes.tolist() if c]


def _clean_stock_code(raw) -> str:
    """
    清洗数据源返回的股票代码并归一到 ts_code（"600000" -> "600000.SH"）

    【为何必须带交易所后缀】
       本表要和 fund_holding.stock_code、reference.securities.ts_code 关联，
       对方存的都是 ts_code；存裸 6 位码会让 join 静默匹配 0 行，
       表面「有 5220 行数据」，实际所有下游都算不出来。
    """
    from stocklab.normalization import normalize_ts_code

    code = str(raw or "").strip()
    if not code or code.lower() in ("nan", "none"):
        return ""
    if code.isdigit() and len(code) < 6:
        code = code.zfill(6)
    return normalize_ts_code(code)


def fetch_stock_industry_mapping_fallback() -> pd.DataFrame:
    """
    备用方案：使用 AKShare 的股票信息接口获取行业
    akshare.stock_individual_info_em 返回行业字段
    """
    import akshare as ak

    # 这个接口较慢，仅作备用
    _logger.warning("使用 fallback 逐只获取行业，极慢，仅用于补全")
    return pd.DataFrame(columns=list(STOCK_INDUSTRY_MAPPING_COLUMNS))