#!/usr/bin/env python3
"""
==============================================================================
StockLab - 全市场与指数接口处理模块 (app.web.market_api)
==============================================================================

【模块职责】
  首页 Dashboard（全市场分位排行）、指数估值页与行业估值页的数据接口。
  只做「参数解析 -> 调用 store -> 过滤排序分页 -> 组装 JSON」，
  不含统计逻辑（分位计算在 store，档位映射在 store.percentile_level）。

【数据路径】
  全市场与指数聚合走 store 的只读连接与进程内缓存，**不经过** facade 串行锁，
  因此 Dashboard 首屏查询不会阻塞个股分析请求，反之亦然。

【应答约定】
  与 app.web.api 一致：200 + 业务 JSON / 400 参数非法 / 404 路径不存在 /
  500 通用错误文案（细节只进日志）。
==============================================================================
"""

import logging
import statistics

from flask import jsonify, request

from app.web import store
from stocklab.analytics import ValuationPercentileReporter

_logger = logging.getLogger("StockLab.Web.MarketApi")

__all__ = [
    "handle_market_ranking",
    "handle_industry_valuation",
    "handle_index_list",
    "handle_index_detail",
]

# 行业层级取值（国证行业分类：1 一级 ~ 4 细分）
_INDUSTRY_LEVELS = (1, 2, 3, 4)

# 七档评级名（过滤参数合法值，与 store.percentile_level 完全一致）
_LEVEL_NAMES = (
    "极度低估", "低估", "正常偏低", "正常", "正常偏高", "高估", "极度高估", "-",
)


# 可用的估值指标（与 store 支持的列名一致）
_INDICATORS = ("pe_ttm", "pe_static", "pb", "ps", "pcf")

# 分页上限：Dashboard 一次最多回 200 行，其余靠翻页
_MAX_PAGE_SIZE = 200
_DEFAULT_PAGE_SIZE = 50

# 分位直方图分档数（0-100 每 10% 一档）
_HISTOGRAM_BUCKETS = 10

# 分位标签渲染器（复用其指标中文名映射）
_reporter = ValuationPercentileReporter()


def _parse_indicator():
    """
    读取并校验 indicator 参数

    Returns:
        tuple: (指标列名或 None, 错误文案或 None)
    """
    indicator = request.args.get("indicator", "pe_ttm").strip()
    if indicator not in _INDICATORS:
        return None, "参数 [indicator] 非法，合法值: %s" % " / ".join(_INDICATORS)
    return indicator, None


def _indicator_labels():
    """
    取全部可用指标的中文名（一次性下发给前端渲染 tab，避免前端再抄一份标签表）

    Returns:
        dict: 指标列名 -> 中文标签
    """
    return {indicator: _reporter.indicator_label(indicator) for indicator in _INDICATORS}


def _parse_paging():
    """
    读取并校验分页参数

    Returns:
        tuple: (offset, limit) 或 (None, 错误文案)
    """
    try:
        offset = int(request.args.get("offset", "0"))
        limit = int(request.args.get("limit", str(_DEFAULT_PAGE_SIZE)))
    except ValueError:
        return None, "参数 [offset]/[limit] 必须是整数"
    if offset < 0:
        return None, "参数 [offset] 不能为负数"
    if limit < 1 or limit > _MAX_PAGE_SIZE:
        return None, "参数 [limit] 取值范围 1~%d" % _MAX_PAGE_SIZE
    return (offset, limit), None


def _build_summary(rows):
    """
    汇总全市场评级分布与分位直方图，供 Dashboard 顶部指标卡使用

    Args:
        rows (list[dict]): 全市场分位结果（已按分位升序）

    Returns:
        dict: 含 total / available / level_counts / histogram / median_percentile
    """
    level_counts = {}
    available = 0
    values = []
    histogram = [0] * _HISTOGRAM_BUCKETS

    for row in rows:
        level_counts[row["level"]] = level_counts.get(row["level"], 0) + 1
        percentile = row["percentile"]
        if percentile is None:
            continue
        available += 1
        values.append(percentile)
        index = int(percentile // 10)
        if index >= _HISTOGRAM_BUCKETS:  # 分位恰为 100 时归入最后一档
            index = _HISTOGRAM_BUCKETS - 1
        histogram[index] += 1

    median = None
    if values:
        values.sort()
        mid = len(values) // 2
        if len(values) % 2:
            median = values[mid]
        else:
            median = (values[mid - 1] + values[mid]) / 2.0
        median = round(median, 2)

    return {
        "total": len(rows),
        "available": available,
        "level_counts": level_counts,
        "histogram": histogram,
        "median_percentile": median,
    }


def handle_market_ranking():
    """
    全市场估值分位排行（首页 Dashboard 主接口）

    Query:
        indicator (str): 指标列名，默认 pe_ttm
        sort (str): 排序字段，取 percentile / current_value / sample_count，默认 percentile
        order (str): asc 或 desc，默认 asc（低估在前）
        q (str): 按代码或名称过滤，可空
        market (str): 按市场过滤（SH/SZ/BJ），可空
        level (str): 按七档评级过滤（如 极度低估），可空；"-" 表示分位不可用
        codes (str): 精确指定 ts_code 列表（逗号分隔），可空；与 q 的模糊搜索互不替代
        offset (int): 分页偏移，默认 0
        limit (int): 每页条数，默认 50，上限 200

    Returns:
        Response: 200 时含 summary / items / total / offset / limit
    """
    indicator, error = _parse_indicator()
    if error:
        return jsonify({"error": error}), 400

    paging, error = _parse_paging()
    if error:
        return jsonify({"error": error}), 400
    offset, limit = paging

    sort_field = request.args.get("sort", "percentile").strip()
    if sort_field not in ("percentile", "current_value", "sample_count"):
        return jsonify({
            "error": "参数 [sort] 非法，合法值: percentile / current_value / sample_count"
        }), 400

    order = request.args.get("order", "asc").strip().lower()
    if order not in ("asc", "desc"):
        return jsonify({"error": "参数 [order] 非法，合法值: asc / desc"}), 400

    keyword = request.args.get("q", "").strip().lower()
    market = request.args.get("market", "").strip().upper()
    level = request.args.get("level", "").strip()

    # 自选股精确过滤：组合监控页按 ts_code 列表取数（q 是模糊搜索，语义不同）
    wanted_codes = None
    raw_codes = request.args.get("codes", "").strip()
    if raw_codes:
        wanted_codes = set()
        for item in raw_codes.split(","):
            code = item.strip().upper()
            if code:
                wanted_codes.add(code)

    if level and level not in _LEVEL_NAMES:
        return jsonify({
            "error": "参数 [level] 非法，合法值: %s" % " / ".join(_LEVEL_NAMES)
        }), 400

    rows = store.load_market_percentile(indicator)
    if not rows:
        return jsonify({
            "indicator": indicator,
            "indicator_label": _reporter.indicator_label(indicator),
            "summary": {"total": 0, "available": 0, "level_counts": {}, "histogram": []},
            "items": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "message": "全市场估值数据不可用，请先运行数据同步",
        })

    # 过滤（市场与关键词先做，summary 用这个口径统计，
    # 保证评级面板在按档筛选时仍显示全貌而不是塌缩成单档）
    filtered = []
    for row in rows:
        if wanted_codes is not None and row["ts_code"] not in wanted_codes:
            continue
        if market and row["market"] != market:
            continue
        if keyword:
            if keyword not in row["ts_code"].lower() and keyword not in row["name"].lower():
                continue
        filtered.append(row)

    summary = _build_summary(filtered)

    # 评级过滤只作用于列表与 total（点击七档面板钻取单一档位）
    if level:
        filtered = [row for row in filtered if row["level"] == level]

    # 排序：分位为 null 的恒排最后（升序降序都要在末尾），避免 null 抢占首屏
    reverse = order == "desc"
    usable = [item for item in filtered if item[sort_field] is not None]
    unusable = [item for item in filtered if item[sort_field] is None]
    usable.sort(key=lambda item: item[sort_field], reverse=reverse)
    filtered = usable + unusable

    page = filtered[offset:offset + limit]

    return jsonify({
        "indicator": indicator,
        "indicator_label": _reporter.indicator_label(indicator),
        "indicator_labels": _indicator_labels(),
        "summary": summary,
        "items": page,
        "total": len(filtered),
        "offset": offset,
        "limit": limit,
        "message": "",
    })


def handle_industry_valuation():
    """
    行业估值横截面：按层级列出各行业的 PE 三种口径与规模数据

    Query:
        level (str): 行业层级 1~4，默认 1（一级行业）
        stat_date (str): 统计日期 YYYY-MM-DD，缺省取本地最新一期

    Returns:
        Response: 200 时含 items / stat_date / stat_dates / summary；
                  参数非法 400；本地无数据 200 + 空 items + message
    """
    level_text = request.args.get("level", "1").strip()
    try:
        level = int(level_text)
    except ValueError:
        return jsonify({"error": "参数 [level] 必须是整数 1~4"}), 400
    if level not in _INDUSTRY_LEVELS:
        return jsonify({"error": "参数 [level] 非法，合法值: 1 / 2 / 3 / 4"}), 400

    stat_date = request.args.get("stat_date", "").strip() or None
    if stat_date and len(stat_date) > 10:
        return jsonify({"error": "参数 [stat_date] 非法，格式应为 YYYY-MM-DD"}), 400

    stat_dates = store.industry_stat_dates()
    rows, used_date = store.load_industry_valuation(level, stat_date)

    if not rows:
        message = ("本地没有行业估值数据，请先运行 "
                   "sync_market_data.py industries")
        if stat_date and stat_dates:
            message = "统计日期 %s 本地无数据，可选日期: %s" % (
                stat_date, " / ".join(stat_dates[:5]))
        return jsonify({
            "level": level,
            "stat_date": used_date,
            "stat_dates": stat_dates,
            "items": [],
            "summary": {},
            "message": message,
        })

    medians = [row["pe_median"] for row in rows if row["pe_median"] is not None]
    summary = {
        "industry_count": len(rows),
        "priced_count": sum(1 for row in rows if row["pe_median"] is not None),
        "median_pe": round(statistics.median(medians), 4) if medians else None,
        "company_count": sum(row["company_count"] for row in rows),
        "priced_company_count": sum(row["priced_company_count"] for row in rows),
    }

    return jsonify({
        "level": level,
        "stat_date": used_date,
        "stat_dates": stat_dates,
        "items": rows,
        "summary": summary,
        "message": "",
    })


def handle_index_list():
    """
    指数估值列表：本地已有成分股的全部指数，各带当前分位与七档评级

    Query:
        indicator (str): 指标列名，默认 pe_ttm

    Returns:
        Response: 200 时含 items（指数卡片数据）
    """
    indicator, error = _parse_indicator()
    if error:
        return jsonify({"error": error}), 400

    index_list = store.load_index_list()
    if not index_list:
        return jsonify({
            "indicator": indicator,
            "items": [],
            "message": "本地没有指数成分数据，请先同步 index_memberships",
        })

    items = []
    for entry in index_list:
        _, results = store.load_index_percentile(entry["index_code"], indicator)
        if not results:
            continue

        target = results[0]
        data = target.to_dict()
        if data.get("percentile") is not None:
            data["percentile"] = round(float(data["percentile"]), 2)
        for field in ("current_value", "median_value", "min_value", "max_value"):
            if data.get(field) is not None:
                data[field] = round(float(data[field]), 4)

        items.append({
            "index_code": entry["index_code"],
            "index_name": entry["index_name"],
            "member_count": entry["member_count"],
            "current_value": data.get("current_value"),
            "percentile": data.get("percentile"),
            "level": data.get("level"),
            "level7": store.percentile_level(data.get("percentile")),
            "sample_count": data.get("sample_count"),
            "median_value": data.get("median_value"),
            "min_value": data.get("min_value"),
            "max_value": data.get("max_value"),
            "interval_text": data.get("interval_text") or "",
        })

    return jsonify({
        "indicator": indicator,
        "indicator_label": _reporter.indicator_label(indicator),
        "indicator_labels": _indicator_labels(),
        "items": items,
        "message": "",
    })


def handle_index_detail():
    """
    单个指数的估值走势与分位明细（点击指数卡片展开）

    Query:
        code (str): 指数代码，必填，如 000300
        indicator (str): 指标列名，默认 pe_ttm

    Returns:
        Response: 200 时含 results / history；参数非法 400；无数据 200 + 空 results
    """
    index_code = request.args.get("code", "").strip()
    if not index_code or len(index_code) > 20:
        return jsonify({"error": "参数 [code] 非法：必填且长度不超过 20"}), 400

    indicator, error = _parse_indicator()
    if error:
        return jsonify({"error": error}), 400

    frame, results = store.load_index_percentile(index_code, indicator)
    if frame is None or not results:
        return jsonify({
            "code": index_code,
            "indicator": indicator,
            "results": [],
            "history": None,
            "message": "未找到指数 %s 的估值序列；请检查代码或同步指数成分数据" % index_code,
        })

    payload_results = []
    interval_text = ""
    for item in results:
        data = item.to_dict()
        if data.get("percentile") is not None:
            data["percentile"] = round(float(data["percentile"]), 2)
        for field in ("current_value", "median_value", "min_value", "max_value"):
            if data.get(field) is not None:
                data[field] = round(float(data[field]), 4)
        data["label"] = _reporter.indicator_label(item.indicator)
        data["level7"] = store.percentile_level(data.get("percentile"))
        payload_results.append(data)
        if not interval_text and item.interval_text:
            interval_text = item.interval_text

    dates = [value.strftime("%Y-%m-%d") for value in frame["trade_date"].tolist()]
    history = {
        "dates": dates,
        "series": {indicator: [
            None if value != value else round(float(value), 4)
            for value in frame[indicator].tolist()
        ]},
    }

    return jsonify({
        "code": index_code,
        "indicator": indicator,
        "interval_text": interval_text,
        "sample_rows": len(frame),
        "results": payload_results,
        "history": history,
        "message": "",
    })
