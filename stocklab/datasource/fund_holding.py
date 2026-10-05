#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金持仓数据源 (stocklab.datasource.fund_holding)
==============================================================================

【模块职责】
   从东方财富爬取公募基金季报/半年报/年报前十大重仓股
   数据源：http://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code=xxx

【关键设计】
   - 返回值统一为 pd.DataFrame，列序严格对齐 FUND_HOLDING_COLUMNS
   - 报告期按倒序遍历，找到首个有效报告期即返回（latest）或全量聚合（history）
   - 表格按表头名解析，不依赖固定列位置，容忍东财增删列
   - 股票代码标准化为 ts_code（600000.SH / 000001.SZ / 430047.BJ）
   - 市值统一换算为「元」（东财表头为「持仓市值（万元）」）
   - 网络异常重试 + 请求间隔限流，失败返回空表而非抛出

【应答约定】
   - 抓取失败/无数据：返回空 DataFrame（列仍在），由调用方决定 404 或跳过
"""

import logging
import re
import time
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

from stocklab.domain import FUND_HOLDING_COLUMNS

_logger = logging.getLogger(__name__)

__all__ = [
    "FUND_HOLDING_SOURCE_EASTMONEY",
    "fetch_fund_holding_history",
    "fetch_latest_fund_holding",
    "fetch_fund_holding_by_report_date",
    "fetch_fund_holding",
]

FUND_HOLDING_SOURCE_EASTMONEY = "eastmoney:fund_holding"

# 东方财富基金持仓接口
_EASTMONEY_HOLDING_URL = "http://fundf10.eastmoney.com/FundArchivesDatas.aspx"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "http://fundf10.eastmoney.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_MAX_RETRIES = 3
_RETRY_INTERVAL = 1.0   # 秒
_REQUEST_TIMEOUT = 15   # 秒
_REQUEST_DELAY = 0.3    # 报告期之间的请求间隔（限流友好）
_TOP_LINE = 50          # 单页拉取条数，覆盖前十大 + 分页


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _normalize_fund_code(fund_code: str) -> str:
    """去掉 000001.OF 的交易所后缀，得到东财需要的 6 位代码"""
    code = str(fund_code or "").strip()
    return code.split(".")[0]


def _normalize_stock_code(raw: str) -> str:
    """
    东财股票代码标准化为 ts_code（委托 stocklab.normalization 的统一口径）

    600000 -> 600000.SH   000001 -> 000001.SZ   688001 -> 688001.SH
    430047 -> 430047.BJ   非纯数字（含 HK/已带后缀）原样返回
    """
    from stocklab.normalization import normalize_ts_code

    code = str(raw or "").strip()
    if not code:
        return ""
    if "." in code:            # 已是 ts_code，直接返回
        return code.upper()
    if not code.isdigit():     # 脏值/港股代码原样交还，由调用方决定丢弃
        return code
    if len(code) < 6:          # 4 位/5 位代码补零后再推断交易所
        code = code.zfill(6)
    return normalize_ts_code(code)


def _quarter_end_date(year: int, quarter: int) -> date:
    """季度末日期（3/6/9/12 月末）"""
    return date(year, quarter * 3, (31, 30, 30, 31)[quarter - 1])


def _quarter_to_report_type(quarter: int) -> str:
    """报告期 -> 类型（一/三季报=quarterly，半年报=semi_annual，年报=annual）"""
    if quarter in (1, 3):
        return "quarterly"
    if quarter == 2:
        return "semi_annual"
    return "annual"


def build_report_dates(years: int = 3, as_of: Optional[date] = None) -> List[Tuple[date, int, str]]:
    """
    构建近 N 年已披露的报告期列表

    Args:
        years: 回溯年数
        as_of: 截止日期（默认今天），用于跳过尚未披露的报告期

    Returns:
        List[(report_date, quarter, report_type)]，按时间倒序（最新在前）
    """
    today = as_of or date.today()
    results: List[Tuple[date, int, str]] = []

    for year in range(today.year - years + 1, today.year + 1):
        for quarter in (4, 3, 2, 1):
            report_date = _quarter_end_date(year, quarter)
            if report_date > today:
                continue
            results.append((report_date, quarter, _quarter_to_report_type(quarter)))

    results.sort(key=lambda item: item[0], reverse=True)
    return results


# ---------------------------------------------------------------------------
# HTML 解析
# ---------------------------------------------------------------------------

# 表头关键词 -> 内部字段名
_HEADER_ALIASES = {
    "股票代码": "stock_code",
    "证券代码": "stock_code",
    "股票名称": "stock_name",
    "证券名称": "stock_name",
    "占净值比例": "weight",
    "占基金资产净值比例": "weight",
    "持仓市值": "market_value",
    "持股数": "shares",
    "持股数量": "shares",
}

# 表头关键词 -> 定位列的正则
_HEADER_PATTERNS = [
    (re.compile(r"股票代码|证券代码"), "stock_code"),
    (re.compile(r"股票名称|证券名称"), "stock_name"),
    (re.compile(r"占净值比例|占基金资产净值比例|净值比例"), "weight"),
    (re.compile(r"持仓市值|持股.*市值"), "market_value"),
    (re.compile(r"持股数|持股数量"), "shares"),
    (re.compile(r"^\s*序号\s*$"), "rank"),
]

_POSITIONAL_DEFAULT = ("rank", "stock_code", "stock_name", "weight", "shares", "market_value")


def _find_table(soup) -> Optional[object]:
    """定位持仓表格：优先东财固定 class，兜底按表头关键词搜索"""
    for class_name in ("w782 comm tzxq", "w782 comm", "tzxq"):
        table = soup.find("table", class_=class_name)
        if table:
            return table

    for table in soup.find_all("table"):
        header = table.find("tr")
        if header and header.find(string=re.compile(r"股票代码|证券代码")):
            return table
    return None


def _map_header(cells) -> Dict[str, int]:
    """按表头文字建立 字段名 -> 列下标 映射"""
    mapping: Dict[str, int] = {}
    for idx, cell in enumerate(cells):
        text = cell.get_text(strip=True)
        for pattern, field in _HEADER_PATTERNS:
            if pattern.search(text):
                mapping.setdefault(field, idx)
                break
    return mapping


def _to_float(text: str) -> Optional[float]:
    """提取数字：'8.53%' / '1,234.56' -> 8.53 / 1234.56；失败返回 None"""
    cleaned = re.sub(r"[^\d.\-]", "", str(text or ""))
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_holding_html(html: str) -> List[dict]:
    """
    解析持仓 HTML 表格

    Returns:
        List[dict]: 每项含 stock_code / stock_name / weight / market_value / rank
                    weight 为小数（0.0523 = 5.23%），market_value 单位为元
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - 依赖在 requirements 中声明
        _logger.error("未安装 beautifulsoup4，无法解析基金持仓 HTML")
        return []

    soup = BeautifulSoup(html, "html.parser")
    table = _find_table(soup)
    if table is None:
        _logger.debug("未找到持仓表格")
        return []

    body_rows = table.find_all("tr")
    if not body_rows:
        return []

    header_cells = body_rows[0].find_all(["th", "td"])
    column_map = _map_header(header_cells)
    # 无表头可识别时退回固定位置（东财标准 6 列：序号/代码/名称/占比/股数/市值）
    if "stock_code" not in column_map:
        column_map = {name: idx for idx, name in enumerate(_POSITIONAL_DEFAULT)}

    rows: List[dict] = []
    for tr in body_rows[1:]:
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        if column_map.get("stock_code", 1) >= len(cells):
            continue

        stock_code = _normalize_stock_code(cells[column_map["stock_code"]].get_text(strip=True))
        if not stock_code:
            continue

        stock_name_idx = column_map.get("stock_name")
        stock_name = cells[stock_name_idx].get_text(strip=True) if stock_name_idx is not None and stock_name_idx < len(cells) else ""

        weight_raw = None
        weight_idx = column_map.get("weight")
        if weight_idx is not None and weight_idx < len(cells):
            weight_raw = _to_float(cells[weight_idx].get_text(strip=True))

        # 东财表头为「持仓市值（万元）」，契约要求单位为元 -> ×10000
        market_value = None
        mv_idx = column_map.get("market_value")
        if mv_idx is not None and mv_idx < len(cells):
            mv_raw = _to_float(cells[mv_idx].get_text(strip=True))
            if mv_raw is not None:
                market_value = mv_raw * 10000.0

        # 序号列缺失时（或首列即代码列）自动顺位编号
        rank = len(rows) + 1
        rank_idx = column_map.get("rank")
        if rank_idx is not None and rank_idx < len(cells) and rank_idx != column_map.get("stock_code"):
            rank_text = cells[rank_idx].get_text(strip=True)
            if rank_text.isdigit():
                rank = int(rank_text)

        rows.append({
            "stock_code": stock_code,
            "stock_name": stock_name,
            "weight": (weight_raw / 100.0) if weight_raw is not None else None,
            "market_value": market_value,
            "rank": rank,
        })

    return rows


# ---------------------------------------------------------------------------
# HTTP 抓取
# ---------------------------------------------------------------------------

def _unescape_js_string(raw: str) -> str:
    """还原 JS 字符串转义：\\" \\' \\\\ \\n \\t \\r \\/ \\uXXXX"""
    def _replace(match: "re.Match") -> str:
        esc = match.group(1)
        simple = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v"}
        if esc in simple:
            return simple[esc]
        if esc in ('"', "'", "\\", "/"):
            return esc
        if esc.startswith("u") and len(esc) == 5:
            try:
                return chr(int(esc[1:], 16))
            except ValueError:
                return ""
        return ""

    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", _replace, raw)


def _extract_apidata_content(text: str) -> Optional[str]:
    """
    从东财 `var apidata={...}` 响应中提取 content 字段

    该响应是 JS 对象字面量而非 JSON（键不带引号、字符串用引号包裹），
    因此不能直接 json.loads，需按 JS 字符串规则定位并还原转义。
    """
    match = re.search(r"content\s*:\s*\"", text)
    if not match:
        return None

    start = match.end()
    buf: List[str] = []
    idx = start
    while idx < len(text):
        char = text[idx]
        if char == "\\" and idx + 1 < len(text):
            buf.append(text[idx:idx + 2])
            idx += 2
            continue
        if char == '"':
            return _unescape_js_string("".join(buf))
        buf.append(char)
        idx += 1

    # 未闭合（响应被截断）
    return None


def _fetch_eastmoney_holding_page(
    fund_code: str,
    report_date: date,
    max_retries: int = _MAX_RETRIES,
) -> List[dict]:
    """
    抓取单只基金单个报告期的持仓明细

    Args:
        fund_code: 基金代码（000001.OF 或 000001 均可）
        report_date: 报告期
        max_retries: 最大重试次数

    Returns:
        List[dict]；网络失败或无数据返回空列表
    """
    params = {
        "type": "jjcc",
        "code": _normalize_fund_code(fund_code),
        "topline": str(_TOP_LINE),
        "year": str(report_date.year),
        "month": str(report_date.month),
    }

    for attempt in range(max_retries):
        try:
            resp = requests.get(
                _EASTMONEY_HOLDING_URL,
                params=params,
                headers=_HEADERS,
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()

            content = _extract_apidata_content(resp.text)
            if not content:
                _logger.debug("基金 %s 报告期 %s 无 content 响应", fund_code, report_date)
                return []

            rows = _parse_holding_html(content)
            if not rows:
                _logger.debug("基金 %s 报告期 %s 解析到 0 条持仓", fund_code, report_date)
            return rows

        except requests.RequestException as exc:
            if attempt < max_retries - 1:
                wait = _RETRY_INTERVAL * (attempt + 1)
                _logger.warning(
                    "抓取基金 %s 失败（第 %d/%d 次）：%s，%.1f 秒后重试",
                    fund_code, attempt + 1, max_retries, exc, wait,
                )
                time.sleep(wait)
            else:
                _logger.error("抓取基金 %s 最终失败：%s", fund_code, exc)
                return []
        except (ValueError, KeyError, TypeError) as exc:
            _logger.warning("基金 %s 响应解析失败：%s", fund_code, exc)
            return []

    return []


# ---------------------------------------------------------------------------
# 对外采集接口
# ---------------------------------------------------------------------------

def _rows_to_frame(rows: List[dict], fund_code: str) -> pd.DataFrame:
    """行记录 -> 契约列序 DataFrame（去重 + 按排名排序）"""
    if not rows:
        return pd.DataFrame(columns=list(FUND_HOLDING_COLUMNS))

    df = pd.DataFrame(rows)
    df = df.reindex(columns=FUND_HOLDING_COLUMNS)
    df = df.drop_duplicates(subset=["fund_code", "report_date", "stock_code"], keep="first")
    df = df.sort_values(["report_date", "rank"], kind="stable").reset_index(drop=True)
    return df


def _collect_period(fund_code: str, report_date: date, report_type: str) -> List[dict]:
    """抓取单个报告期并补齐契约字段"""
    rows = _fetch_eastmoney_holding_page(fund_code, report_date)
    if not rows:
        return []

    fetched_at = datetime.now()
    for row in rows:
        row.update({
            "fund_code": fund_code,
            "report_date": report_date,
            "report_type": report_type,
            "source": FUND_HOLDING_SOURCE_EASTMONEY,
            "fetched_at": fetched_at,
        })
    return rows


def fetch_fund_holding_by_report_date(
    fund_code: str,
    report_date,
    report_type: Optional[str] = None,
) -> pd.DataFrame:
    """
    抓取指定报告期的持仓

    Args:
        fund_code: 基金代码，如 000001.OF
        report_date: 报告期（str / date / Timestamp 均可）
        report_type: 缺省时按报告期月份推断

    Returns:
        pd.DataFrame: FUND_HOLDING_COLUMNS 契约列序；无数据返回空表
    """
    dt = pd.Timestamp(report_date).date()
    quarter = (dt.month - 1) // 3 + 1
    rows = _collect_period(fund_code, dt, report_type or _quarter_to_report_type(quarter))
    return _rows_to_frame(rows, fund_code)


def fetch_fund_holding_history(fund_code: str, years: int = 3) -> pd.DataFrame:
    """
    抓取近 N 年全部报告期持仓

    Args:
        fund_code: 基金代码，如 000001.OF
        years: 回溯年数（1~10）

    Returns:
        pd.DataFrame: FUND_HOLDING_COLUMNS 契约列序，跨报告期聚合
    """
    years = max(1, min(int(years), 10))
    all_rows: List[dict] = []

    periods = build_report_dates(years)
    for idx, (report_date, _quarter, report_type) in enumerate(periods):
        all_rows.extend(_collect_period(fund_code, report_date, report_type))
        if idx < len(periods) - 1 and _REQUEST_DELAY > 0:
            time.sleep(_REQUEST_DELAY)

    df = _rows_to_frame(all_rows, fund_code)
    _logger.info("基金 %s 历史持仓采集完成：%d 条记录 / %d 个报告期",
                 fund_code, len(df), len(periods))
    return df


def fetch_latest_fund_holding(fund_code: str, years: int = 3) -> pd.DataFrame:
    """
    抓取最新一期持仓（从最近报告期起倒序探测，命中即停）

    Args:
        fund_code: 基金代码，如 000001.OF
        years: 最多回溯年数

    Returns:
        pd.DataFrame: 单一报告期的持仓；全部无数据返回空表
    """
    periods = build_report_dates(years)
    for idx, (report_date, _quarter, report_type) in enumerate(periods):
        df = _rows_to_frame(_collect_period(fund_code, report_date, report_type), fund_code)
        if not df.empty:
            return df
        if idx < len(periods) - 1 and _REQUEST_DELAY > 0:
            time.sleep(_REQUEST_DELAY)

    _logger.info("基金 %s 近 %d 年无可用持仓数据", fund_code, years)
    return pd.DataFrame(columns=list(FUND_HOLDING_COLUMNS))


def fetch_fund_holding(fund_code: str, report_date: Optional[str] = None) -> pd.DataFrame:
    """兼容入口：指定报告期则抓该期，否则抓最新一期"""
    if report_date:
        return fetch_fund_holding_by_report_date(fund_code, report_date)
    return fetch_latest_fund_holding(fund_code)
