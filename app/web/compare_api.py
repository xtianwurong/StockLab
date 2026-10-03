#!/usr/bin/env python3
"""
==============================================================================
StockLab - 多股对比接口处理模块 (app.web.compare_api)
==============================================================================

【模块职责】
  多股对比页的单个接口：一次取多只标的的估值历史，算出各自的当前值与历史分位，
  并把各标的的序列按交易日对齐，供前端在同一张图上叠加对比。

【设计要点】
  1. **只做编排**：历史序列走 store.load_valuation_histories（一次 IN 查询），
     分位计算走 stocklab.analytics.ValuationPercentileAnalyzer，与个股页
     （app/web/api.py）、指数页（store.load_index_percentile）**同一个 analyzer**，
     因此三处的分位口径、亏损期剔除、七档评级天然一致，不存在「同一只票两个分位」。
  2. **序列按交易日并集对齐**：各标的的交易日不完全重合（停牌、上市时间不同），
     对齐后缺失点填 None，图上表现为断线而不是「用上一日价格假装有值」。
  3. **口径写在字段里**：返回的 current / percentile / level7 / sample_rows
     与个股页逐字同义，前端可直接复用同一套渲染函数。

【应答约定】
  与 app.web.api 一致：200 + 业务 JSON / 400 参数非法 / 500 通用文案。
==============================================================================
"""

import logging

import pandas as pd
from flask import jsonify, request

from app.web import store
from stocklab.analytics import ValuationPercentileAnalyzer, ValuationPercentileReporter

_logger = logging.getLogger("StockLab.Web.CompareApi")

__all__ = [
    "handle_compare",
]

# 一次最多对比多少只标的（走势图每只一条线，太多会糊成一片）
_MAX_CODES = 10
_MIN_CODES = 2

# 可用的估值指标（与 store 的 _INDICATORS 一致）
_INDICATORS = ("pe_ttm", "pe_static", "pb", "ps", "pcf")

# 指标中文名渲染器（复用既有映射，避免前端再抄一份标签表）
_reporter = ValuationPercentileReporter()


def _num(value, digits=4):
    """
    转成 JSON 安全的数值：NaN / inf -> None

    Args:
        value: 原始数值
        digits (int): 保留小数位

    Returns:
        float | None: 可序列化数值
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, digits)


def _parse_indicator_list():
    """
    解析 indicators 参数（可多选，默认 5 项全出）

    Returns:
        tuple: (指标列表或 None, 错误文案或 None)
    """
    raw = request.args.get("indicators", "").strip()
    if not raw:
        return list(_INDICATORS), None

    wanted = []
    for item in raw.split(","):
        name = item.strip()
        if not name:
            continue
        if name not in _INDICATORS:
            return None, "参数 [indicators] 含非法指标 %s，合法值: %s" % (
                name, " / ".join(_INDICATORS))
        if name not in wanted:
            wanted.append(name)
    if not wanted:
        return None, "参数 [indicators] 不能为空"
    if len(wanted) > len(_INDICATORS):
        return None, "参数 [indicators] 最多 %d 项" % len(_INDICATORS)
    return wanted, None


def _slice_period(frame, period):
    """
    按口径裁剪序列区间

    Args:
        frame (pd.DataFrame): 含 ts_code / trade_date / 指标列
        period (str): 全部 / 近五年 / 近十年

    Returns:
        tuple: (裁剪后的帧, 区间文案)；无有效样本返回 (None, 文案)
    """
    years = {"近五年": 5, "近十年": 10}.get(period)
    text = period or "全部"
    if frame is None or frame.empty:
        return None, text

    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    if years:
        latest = frame["trade_date"].max()
        cutoff = pd.Timestamp(latest) - pd.DateOffset(years=years)
        frame = frame[frame["trade_date"] >= cutoff.date()]
    return frame, text


def _align_history(frame, codes, indicator):
    """
    把多只标的的序列按交易日并集对齐成一张宽表（供 ECharts 多线叠加）

    【为何填充 None 而不是前向填充】
      前向填充会让停牌日画出一条水平线，把「当日无成交」误读成「价格没变」。
      缺失就断开，是更诚实的画法。

    Args:
        frame (pd.DataFrame): 长表 [ts_code, trade_date, 指标列]
        codes (list): 标的列表（保持前端给定顺序）
        indicator (str): 指标列名

    Returns:
        dict: {"dates": [...], "series": {ts_code: [...]}}
    """
    if frame is None or frame.empty:
        return {"dates": [], "series": {}}

    dates = sorted(set(frame["trade_date"].tolist()))
    index = {day: position for position, day in enumerate(dates)}

    # 先把每只标的聚成 date -> value 字典，再按并集位置填
    per_code = {}
    for code, group in frame.groupby("ts_code"):
        mapping = {}
        for _, row in group.iterrows():
            day = row["trade_date"]
            if day in index:
                mapping[index[day]] = _num(row[indicator])
        per_code[code] = mapping

    series = {}
    for code in codes:
        mapping = per_code.get(code, {})
        series[code] = [mapping.get(position) for position in range(len(dates))]
    return {"dates": [day.isoformat() for day in dates], "series": series}


def _merge_indicator_frames(codes, indicators, period):
    """
    逐指标取历史并按 (ts_code, trade_date) 拼成一张宽表

    【为何逐指标各查一次而不是一次 SELECT 五列】
      单列查询命中 DuckDB 的列存裁剪，只读用到的列；五列全取在只需要
      其中一两个指标时会多读三倍数据。store 层已有结果缓存，重复请求不花钱。

    Args:
        codes (list): 标的列表
        indicators (list): 指标列表
        period (str): 区间口径

    Returns:
        tuple: (宽表 DataFrame 或 None, 区间文案)
    """
    merged = None
    interval_text = period or "全部"
    for indicator in indicators:
        raw = store.load_valuation_histories(codes, indicator)
        if raw is None or raw.empty:
            _logger.warning("多股对比：%s 无本地历史，跳过该指标", indicator)
            continue
        sliced, interval_text = _slice_period(raw, period)
        if sliced is None or sliced.empty:
            continue
        if merged is None:
            merged = sliced
        else:
            merged = pd.merge(
                merged, sliced, on=["ts_code", "trade_date"], how="outer"
            )
    return merged, interval_text


def _stock_rows(frame, codes, indicators):
    """
    逐标的算出各指标的分位结论

    【口径一致性 —— 列名不可改】
      ValuationPercentileAnalyzer 内部按**固定列名**（pe_ttm / pe_static / pb /
      ps / pcf）从帧里挑候选指标，若把列改名成 value，它会一个候选都找不到、
      静默返回全 None。因此这里保留规范列名，把整只标的的多指标帧一次性
      送进 analyzer（它本身就会逐指标各出一个结果），与个股页 api.handle_percentile
      走的是同一段代码，样本不足时同样返回 percentile=None。

    Args:
        frame (pd.DataFrame): 宽表 [ts_code, trade_date, 各指标列]
        codes (list): 标的列表
        indicators (list): 指标列表

    Returns:
        list[dict]: 每只标的一条，含 name / sample_rows / metrics
    """
    analyzer = ValuationPercentileAnalyzer()
    securities = store.load_securities()
    names = {}
    markets = {}
    if securities is not None and not securities.empty:
        for _, row in securities.iterrows():
            names[row["ts_code"]] = row.get("name")
            markets[row["ts_code"]] = row.get("market")

    rows = []
    for code in codes:
        record = {
            "ts_code": code,
            "name": str(names.get(code) or ""),
            "market": str(markets.get(code) or ""),
            "sample_rows": 0,
            "metrics": {},
        }
        if frame is None or frame.empty:
            rows.append(record)
            continue

        subset = frame[frame["ts_code"] == code]
        present = [name for name in indicators if name in subset.columns]
        if subset.empty or not present:
            rows.append(record)
            continue

        record["sample_rows"] = len(subset)
        results = analyzer.analyze(subset[["trade_date"] + present])
        for target in results:
            indicator = target.indicator
            if indicator not in present:
                continue
            data = target.to_dict()
            record["metrics"][indicator] = {
                "label": _reporter.indicator_label(indicator),
                "current_value": _num(data.get("current_value")),
                "percentile": _num(data.get("percentile"), 2),
                "median_value": _num(data.get("median_value")),
                "min_value": _num(data.get("min_value")),
                "max_value": _num(data.get("max_value")),
                "sample_count": data.get("sample_count"),
                "interval_text": data.get("interval_text") or "",
                "level7": store.percentile_level(data.get("percentile")),
            }
        rows.append(record)
    return rows


def _palette_colors(count):
    """
    取 N 条区分度较高的分类色（用于走势图多线）

    【为何不用七档评级色】
      七档色是「有序语义色」（绿=低估 → 黄 → 红=高估），拿来区分不同标的会
      让读者误以为线与线之间有优劣关系；而且读者已经见过这套颜色，看到绿色
      会条件反射地理解成「低估」。因此这里的分类色板**刻意避开绿/黄/红这一段
      语义轴**，只用蓝、青、紫、粉等冷暖中性色，与七档色零重叠
      （tests/test_web_api.py 阶段十三对此有断言）。

    Args:
        count (int): 需要几条

    Returns:
        list[str]: 十六进制颜色列表
    """
    base = [
        "#2563eb",  # 蓝
        "#0d9488",  # 青绿偏冷
        "#9333ea",  # 紫
        "#0891b2",  # 青
        "#db2777",  # 品红
        "#4f46e5",  # 靛
        "#0369a1",  # 深蓝
        "#7e22ce",  # 深紫
        "#0f766e",  # 深青
        "#be185d",  # 深品红
    ]
    return [base[index % len(base)] for index in range(count)]


def handle_compare():
    """
    多股对比：一次给出多只标的的多指标分位与对齐后的估值走势

    Query:
        codes (str, 必填): 逗号分隔的证券代码，2~10 只，如 600519.SH,000001.SZ
        indicators (str): 逗号分隔的指标，缺省 5 项全出（pe_ttm/pe_static/pb/ps/pcf）
        period (str): 全部 / 近五年 / 近十年，缺省 全部

    Returns:
        Response: 200 时含 codes / names / indicators / indicator_labels /
                  rows / history / sample_rows / message；
                  参数非法 400；全部无数据 200 + 空 rows + message
    """
    raw_codes = request.args.get("codes", "").strip()
    if not raw_codes:
        return jsonify({"error": "参数 [codes] 非法：必填"}), 400

    codes = []
    for item in raw_codes.split(","):
        code = item.strip().upper()
        if not code:
            continue
        if code not in codes:
            codes.append(code)
    if len(codes) < _MIN_CODES:
        return jsonify({
            "error": "至少需要 %d 只标的，当前 %d 只" % (_MIN_CODES, len(codes))
        }), 400
    if len(codes) > _MAX_CODES:
        return jsonify({
            "error": "一次最多对比 %d 只标的，当前 %d 只" % (_MAX_CODES, len(codes))
        }), 400

    indicators, error = _parse_indicator_list()
    if error:
        return jsonify({"error": error}), 400

    period = request.args.get("period", "全部").strip() or "全部"
    if period not in ("全部", "近五年", "近十年"):
        return jsonify({"error": "参数 [period] 非法，合法值: 全部 / 近五年 / 近十年"}), 400

    primary = indicators[0]
    try:
        frame, interval_text = _merge_indicator_frames(codes, indicators, period)
    except Exception as exc:
        _logger.error("多股估值历史取数失败: %s", str(exc)[:300])
        return jsonify({"error": "读取估值历史失败，请查看服务日志"}), 500

    if frame is None or frame.empty:
        return jsonify({
            "codes": codes,
            "names": [],
            "indicators": indicators,
            "indicator_labels": {
                name: _reporter.indicator_label(name) for name in indicators
            },
            "rows": [],
            "history": {"dates": [], "series": {}},
            "sample_rows": 0,
            "message": "本地没有这些标的的估值历史，请先运行 "
                       "sync_market_data.py valuation-history",
        })

    rows = _stock_rows(frame, codes, indicators)
    if not rows:
        return jsonify({"error": "未取到任何标的的估值数据"}), 400

    names = []
    for row in rows:
        names.append({
            "ts_code": row["ts_code"],
            "name": row["name"],
            "market": row["market"],
            "color": _palette_colors(len(codes))[len(names)],
        })

    sample_dates = 0
    history = {"dates": [], "series": {}}
    if frame is not None and not frame.empty:
        history = _align_history(frame, codes, primary)
        sample_dates = len(history["dates"])

    return jsonify({
        "codes": codes,
        "names": names,
        "indicators": indicators,
        "indicator_labels": {
            name: _reporter.indicator_label(name) for name in indicators
        },
        "primary_indicator": primary,
        "period": period,
        "interval_text": interval_text,
        "rows": rows,
        "history": history,
        "sample_rows": sample_dates,
        "message": "",
    })
