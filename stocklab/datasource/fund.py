#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金数据源 (stocklab.datasource.fund)
==============================================================================

【模块职责】
   公募基金基本信息与净值历史采集
   数据源：AKShare fund_* 系列接口
"""


import logging
from typing import Optional
from datetime import date, datetime

import pandas as pd

from stocklab.domain import (
    FUND_INFO_COLUMNS,
    FUND_NAV_HISTORY_COLUMNS,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "FUND_SOURCE_AKSHARE",
    "fetch_fund_info",
    "fetch_fund_nav_history",
    "fetch_all_fund_codes",
]

FUND_SOURCE_AKSHARE = "akshare:fund"

# 东财 f10 历史净值 API（api.fund.eastmoney.com，与 pingzhongdata 同属东财但该子域可用）
# 两个坑：必须带 Referer 否则 ErrCode=-999；pageSize 服务端强制 20
_LSJZ_URL = "https://api.fund.eastmoney.com/f10/lsjz"
_LSJZ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://fundf10.eastmoney.com/",
}
_LSJZ_PAGE_SIZE = 20
_LSJZ_MAX_PAGES = 400
_REQUEST_TIMEOUT = 20


def fetch_all_fund_codes() -> pd.DataFrame:
    """
    获取全市场基金代码列表

    Returns:
        pd.DataFrame: fund_code, fund_name, fund_type
    """
    import akshare as ak

    # 获取所有基金列表
    df = ak.fund_name_em()
    if df is None or df.empty:
        _logger.warning("获取基金列表失败")
        return pd.DataFrame(columns=["fund_code", "fund_name", "fund_type"])

    # 标准化列名
    # AKShare 返回：基金代码、基金简称、基金类型
    df = df.rename(columns={
        "基金代码": "fund_code",
        "基金简称": "fund_name",
        "基金类型": "fund_type",
    })

    # 标准化代码格式：补全后缀 .OF
    df["fund_code"] = df["fund_code"].astype(str).str.zfill(6) + ".OF"

    return df[["fund_code", "fund_name", "fund_type"]].drop_duplicates()


def fetch_fund_info(fund_code: str) -> pd.DataFrame:
    """
    获取单只基金的基本信息

    Args:
        fund_code: 基金代码，如 "000001.OF"

    Returns:
        pd.DataFrame: FUND_INFO_COLUMNS 契约列序，单行
    """
    import akshare as ak

    # 去掉后缀
    code = fund_code.replace(".OF", "").replace(".OF", "")

    try:
        df = ak.fund_individual_basic_info_xq(symbol=code)
    except Exception as e:
        _logger.warning("获取基金 [%s] 基本信息失败: %s", fund_code, e)
        return pd.DataFrame(columns=list(FUND_INFO_COLUMNS))

    if df is None or df.empty:
        _logger.warning("基金 [%s] 无基本信息", fund_code)
        return pd.DataFrame(columns=list(FUND_INFO_COLUMNS))

    # df 结构：item, value
    info = dict(zip(df["item"], df["value"]))

    from datetime import datetime
    # 雪球返回的 item 名与早期设想不同：是「成立时间」不是「成立日期」，
    # 也没有「基金简称」「基金状态」。字段名写错的后果是 establish_date 恒 None、
    # fund_short_name 恒空 —— 页面上看得到「列表是空的」，但接口是 200。
    fund_status = str(info.get("基金状态") or "")
    frame = pd.DataFrame([{
        "fund_code": fund_code,
        "fund_name": info.get("基金名称", ""),
        "fund_short_name": info.get("基金简称") or info.get("基金名称", ""),
        "fund_type": info.get("基金类型", ""),
        "manager_name": info.get("基金经理", ""),
        "company_name": info.get("基金公司", ""),
        "establish_date": _parse_date(info.get("成立时间") or info.get("成立日期") or ""),
        "benchmark": info.get("业绩比较基准", ""),
        # 雪球没有清算状态字段：无该字段时不谎称 terminated，缺省 active
        "status": "terminated" if fund_status == "清盘" else "active",
        "source": "akshare:fund_individual",
        "fetched_at": datetime.now(),
    }])

    frame = frame[list(FUND_INFO_COLUMNS)]
    return frame


def _nav_raw_from_lsjz(
    code: str,
    start_date: Optional[str] = None,
    max_pages: int = _LSJZ_MAX_PAGES,
) -> Optional["pd.DataFrame"]:
    """
    源 1：东财 f10 历史净值 API（api.fund.eastmoney.com/f10/lsjz）

    【为什么不用 akshare 的 pingzhongdata】
      akshare 的 fund_open_fund_info_em 抓的是 fund.eastmoney.com/pingzhongdata，
      本机实测该子域持续超时/截断（与 push2 同样的网络问题），而 api.fund.eastmoney.com
      0.2s 就能返回。同一家、不同子域，可用性完全不同。

    【两个坑】
      1. 必须带 Referer: https://fundf10.eastmoney.com/ ，否则 ErrCode=-999、Data 为空串；
      2. pageSize 服务端强制 20（传 49/500 都被改回 20），全历史要翻几百页。

    Returns:
        原始中文列 DataFrame；**任一页失败整源判失败**（宁可交给备源，
        也不要拿半截历史冒充完整序列 —— 半截序列会让相关性算出错值）
    """
    import requests

    rows: list = []
    for page in range(1, int(max_pages) + 1):
        try:
            response = requests.get(
                _LSJZ_URL,
                params={"fundCode": code, "pageIndex": page, "pageSize": _LSJZ_PAGE_SIZE},
                headers=_LSJZ_HEADERS,
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            _logger.warning("lsjz 基金 %s 第 %d 页失败：%s", code, page, exc)
            return None

        if payload.get("ErrCode", 0) not in (0, None):
            _logger.warning("lsjz 基金 %s ErrCode=%s %s",
                            code, payload.get("ErrCode"), payload.get("ErrMsg"))
            return None

        data = payload.get("Data") or {}
        page_rows = (data or {}).get("LSJZList") or []
        if not page_rows:
            break

        rows.extend(page_rows)

        # 结果按日期倒序：一旦翻到早于起点的行，说明该区间已取全
        oldest = min((str(r.get("FSRQ") or "") for r in page_rows), default="")
        if start_date and oldest and oldest < start_date:
            break
    else:
        _logger.warning("lsjz 基金 %s 翻满 %d 页仍未到区间起点，可能截断",
                        code, max_pages)

    if not rows:
        return None

    frame = pd.DataFrame(rows)
    frame = frame.rename(columns={
        "FSRQ": "净值日期",
        "DWJZ": "单位净值",
        "LJJZ": "累计净值",
        "JZZZL": "日增长率",
    })
    return frame[["净值日期", "单位净值", "累计净值", "日增长率"]]


def _nav_raw_from_pingzhongdata(code: str, **_ignored) -> Optional["pd.DataFrame"]:
    """源 2（备）：akshare pingzhongdata —— 单请求全历史，但该子域在本机常超时"""
    try:
        import akshare as ak
        frame = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势", period="成立来")
    except Exception as exc:
        _logger.warning("pingzhongdata 基金 %s 失败：%s", code, exc)
        return None
    if frame is None or frame.empty:
        return None
    return frame[["净值日期", "单位净值", "日增长率"]] if "净值日期" in frame.columns else None


def _normalize_nav_frame(raw, fund_code: str, source: str, start_date=None, end_date=None):
    """把任一来源的中文净值原始表收敛到 FUND_NAV_HISTORY_COLUMNS 契约（source 必填，不编来源）"""
    frame = raw.rename(columns={
        "净值日期": "nav_date",
        "单位净值": "nav",
        "累计净值": "acc_nav",
        "日增长率": "change_pct",
    })
    # 有的源没有累计净值列：契约要求该列在，缺列补 None 而不是 KeyError
    for column in ("nav_date", "nav", "acc_nav", "change_pct"):
        if column not in frame.columns:
            frame[column] = None

    frame["nav_date"] = pd.to_datetime(frame["nav_date"], errors="coerce").dt.date
    frame["nav"] = pd.to_numeric(frame["nav"], errors="coerce")
    frame["acc_nav"] = pd.to_numeric(frame["acc_nav"], errors="coerce")
    frame["change_pct"] = pd.to_numeric(
        frame["change_pct"].astype(str).str.replace("%", "", regex=False), errors="coerce"
    )

    if start_date:
        frame = frame[frame["nav_date"] >= pd.to_datetime(start_date).date()]
    if end_date:
        frame = frame[frame["nav_date"] <= pd.to_datetime(end_date).date()]

    frame = frame.dropna(subset=["nav_date", "nav"])
    frame = frame[frame["nav"] > 0]
    if frame.empty:
        return pd.DataFrame(columns=list(FUND_NAV_HISTORY_COLUMNS))

    now = datetime.now()
    frame["fund_code"] = fund_code
    frame["source"] = source
    frame["fetched_at"] = now
    frame = frame[list(FUND_NAV_HISTORY_COLUMNS)]
    return frame.sort_values("nav_date").reset_index(drop=True)


def fetch_fund_nav_history(fund_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """
    获取单只基金的净值历史（双源回退，模式同行情日 K 的三源回退）

    Args:
        fund_code: 基金代码，如 "000001.OF"
        start_date: 起始日期 YYYY-MM-DD，None 表示不限
        end_date: 结束日期 YYYY-MM-DD，None 表示今天

    Returns:
        pd.DataFrame: FUND_NAV_HISTORY_COLUMNS 契约列序；两源都失败返回空表
                      （source 列写**实际**取数来源，不冒充）
    """
    code = fund_code.replace(".OF", "")
    if end_date is None:
        end_date = date.today().strftime("%Y-%m-%d")

    sources = (
        ("eastmoney:lsjz", _nav_raw_from_lsjz),
        ("akshare:pingzhongdata", _nav_raw_from_pingzhongdata),
    )
    for source_name, fetcher in sources:
        # 单个源抛异常只应让它自己出局，不能把整次采集带崩（源之间互不信任）
        try:
            raw = fetcher(code, start_date=start_date) if source_name.endswith("lsjz") else fetcher(code)
        except Exception as exc:
            _logger.warning("基金 [%s] 净值源 %s 抛异常：%s", fund_code, source_name, exc)
            continue
        if raw is None or raw.empty:
            continue

        frame = _normalize_nav_frame(
            raw, fund_code, source=source_name, start_date=start_date, end_date=end_date
        )
        if frame.empty:
            _logger.info("基金 [%s] 源 %s 取到数据但落在区间外 [%s, %s]",
                         fund_code, source_name, start_date, end_date)
            continue

        _logger.info("基金 [%s] 净值历史采集(%s): %d 条，%s ~ %s",
                     fund_code, source_name, len(frame),
                     frame["nav_date"].iloc[0], frame["nav_date"].iloc[-1])
        return frame

    _logger.warning("基金 [%s] 净值历史两源均失败（lsjz / pingzhongdata）", fund_code)
    return pd.DataFrame(columns=list(FUND_NAV_HISTORY_COLUMNS))


def _parse_date(date_str: str):
    """解析各种日期格式"""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return date.fromisoformat(date_str) if "-" in date_str else date_str
        except Exception:
            continue
    return None