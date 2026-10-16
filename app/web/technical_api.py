#!/usr/bin/env python3
"""
==============================================================================
StockLab - 技术面与风险接口 (app.web.technical_api)
==============================================================================

【模块职责】
  把「个股代码 + 日期区间」翻译成一条日 K 序列，交给
  stocklab.analytics.technical_risk 做纯计算，输出技术面与风险指标 JSON。

【取数路径】
  日 K 走 store.call_facade("fetch_daily_prices", ...)（本地优先，未命中回退远端）；
  证券名称走 store.load_securities() 的只读缓存；
  指标计算完全是内存操作，不打数据库、不发请求。

【应答约定】
  成功 200 + 业务 JSON；参数非法 400 + {"error": ...}；
  本地与远端都没有行情时返回 200 + 空结果 + 可操作 message（不返回 500）。
==============================================================================
"""

import datetime
import logging

from flask import jsonify, render_template, request

from app.web import store
from app.web.webcommon import json_num, parse_date_arg
from stocklab.analytics import technical_risk

_logger = logging.getLogger("StockLab.Web.TechnicalApi")

__all__ = [
    "handle_technical_page",
    "handle_technical",
]

# 默认回看自然日数：覆盖 250 日动量（约 365 个自然日）再留足余量
_DEFAULT_LOOKBACK_DAYS = 750
_MAX_LOOKBACK_DAYS = 3650
# 图表最多下发的交易日数，避免长期限时 payload 过大
_MAX_CHART_POINTS = 400


def handle_technical_page():
    """GET /technical —— 技术面与风险页面"""
    return render_template("technical.html", nav_page="technical")


def handle_technical():
    """
    GET /api/technical?code=&start_date=&end_date=&benchmark=

    返回 {code, name, priority, as_of, price, risk, technical, message}
    """
    raw_code = (request.args.get("code") or "").strip()
    if not raw_code:
        return jsonify({"error": "缺少参数 [code]，请输入证券代码或名称"}), 400

    code, resolve_error = _resolve_code(raw_code)
    if resolve_error:
        return jsonify({"error": resolve_error}), 400

    # 缺省与非法必须分开：把 start_date=乱写 当成「没传」会静默换用默认窗口，
    # 用户指定的区间被悄悄替换成另一段数据（假阴性），且没有任何提示。
    end_text, error = parse_date_arg("end_date", request.args.get("end_date"))
    if error:
        return jsonify({"error": error}), 400
    start_text, error = parse_date_arg("start_date", request.args.get("start_date"))
    if error:
        return jsonify({"error": error}), 400

    end_date = datetime.date.fromisoformat(end_text) if end_text else datetime.date.today()
    start_date = (datetime.date.fromisoformat(start_text) if start_text
                  else end_date - datetime.timedelta(days=_DEFAULT_LOOKBACK_DAYS))
    if start_date >= end_date:
        return jsonify({"error": "参数 [start_date] 必须早于 [end_date]"}), 400
    if (end_date - start_date).days > _MAX_LOOKBACK_DAYS:
        return jsonify({"error": "查询区间过长，最多 %d 个自然日" % _MAX_LOOKBACK_DAYS}), 400

    try:
        prices = store.call_facade(
            "fetch_daily_prices", code,
            start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"),
        )
    except Exception as error:
        _logger.error("取 [%s] 日 K 失败: %s", code, error)
        return jsonify({"error": "行情取数失败，请稍后重试"}), 500

    if prices is None or prices.empty:
        return jsonify({
            "code": code,
            "name": _security_name(code),
            "priority": store.current_priority(),
            "as_of": None,
            "price": None,
            "risk": None,
            "technical": None,
            "message": "本地与远端均无 %s 在所选区间的日 K 数据，请先在 [同步] 中补齐该标的行情" % code,
        })

    closes = prices.set_index("trade_date")["close"]
    closes = closes[closes.notna()].sort_index()
    if len(closes) < 2:
        return jsonify({
            "code": code,
            "name": _security_name(code),
            "priority": store.current_priority(),
            "as_of": None,
            "price": None,
            "risk": None,
            "technical": None,
            "message": "%s 在所选区间只有 %d 个有效收盘价，不足以计算技术指标" % (code, len(closes)),
        })

    benchmark = None
    benchmark_code = (request.args.get("benchmark") or "").strip().upper()
    if benchmark_code:
        benchmark = _load_benchmark(benchmark_code, start_date, end_date)

    risk = technical_risk.risk_metrics(closes, benchmark_closes=benchmark)
    technical = technical_risk.technical_snapshot(closes)
    chart = _build_chart(closes)

    return jsonify({
        "code": code,
        "name": _security_name(code),
        "priority": store.current_priority(),
        "as_of": technical.get("as_of"),
        "benchmark": benchmark_code or None,
        "price": chart,
        "risk": risk,
        "technical": technical,
        "drawdown": technical_risk.max_drawdown(closes),
        "message": "",
    })


def _resolve_code(text):
    """
    把用户输入解析为 ts_code：代码原样返回，中文名查证券表转换

    Args:
        text (str): 证券代码或名称

    Returns:
        tuple: (ts_code 或 None, 错误文案 或 None)
    """
    if _looks_like_code(text):
        return text.upper(), None

    frame = store.load_securities()
    if frame is None or frame.empty or "name" not in frame.columns:
        return None, "无法解析输入 [%s]：证券表不可用，请先同步基础数据" % text

    names = frame["name"].fillna("")
    matches = frame[names == text]
    if len(matches) == 1:
        return matches.iloc[0]["ts_code"], None
    partial = frame[names.str.contains(text, case=False, na=False, regex=False)]
    if len(partial) == 1:
        return partial.iloc[0]["ts_code"], None
    if len(partial) > 1:
        sample = "、".join(
            "%s(%s)" % (row["name"], row["ts_code"])
            for _, row in partial.head(4).iterrows()
        )
        return None, "名称 [%s] 匹配到多只标的：%s，请输入代码" % (text, sample)
    return None, "未找到证券 [%s]，请检查输入" % text


def _looks_like_code(text):
    """判断输入是否形如证券代码（ASCII 字母数字与点号）"""
    if not text or len(text) > 20:
        return False
    for char in text:
        if not char.isascii() or not (char.isalnum() or char == "."):
            return False
    return True


def _security_name(code):
    """查证券简称；查不到时回退为代码"""
    frame = store.load_securities()
    if frame is None or frame.empty or "name" not in frame.columns:
        return code
    matched = frame[frame["ts_code"] == code]
    if matched.empty:
        return code
    name = matched.iloc[0]["name"]
    return str(name) if name is not None else code


def _load_benchmark(benchmark_code, start_date, end_date):
    """尽力取基准收盘价；取不到时返回 None，Beta/Alpha 相应留空"""
    try:
        frame = store.call_facade(
            "fetch_daily_prices", benchmark_code,
            start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"),
        )
    except Exception as error:
        _logger.warning("基准 [%s] 取数失败，Beta/Alpha 留空: %s", benchmark_code, error)
        return None
    if frame is None or frame.empty or "close" not in frame.columns:
        return None
    series = frame.set_index("trade_date")["close"]
    return series[series.notna()].sort_index()


def _build_chart(closes):
    """
    组装走势图序列（收盘价 + 均线 + RSI + 回撤），只取最近若干交易日控制体积

    Args:
        closes (pd.Series): 收盘价序列

    Returns:
        dict: {"dates": [...], "close": [...], "ma5": [...], "ma20": [...],
               "ma60": [...], "rsi14": [...], "drawdown": [...]}
    """
    # 指标一律先在**整段**序列上算，最后才截取最近 N 日：
    #   - 回撤若先截断再算，滚动最高点会被重置到窗口内的局部高点，
    #     长期下跌的标的回撤会被系统性低估（图上看似只跌了一点）；
    #   - 均线/RSI 若先截断，窗口最前段缺预热，值会明显失真。
    averages = technical_risk.moving_averages(closes)
    rsi = technical_risk.relative_strength_index(closes)
    drawdown = closes / closes.cummax() - 1.0

    view = closes.tail(_MAX_CHART_POINTS)
    return {
        "dates": [str(day)[:10] for day in view.index],
        "close": [json_num(value, 4) for value in view.tolist()],
        "ma5": [json_num(value, 4)
                for value in averages["ma5"].tail(_MAX_CHART_POINTS).tolist()],
        "ma20": [json_num(value, 4)
                 for value in averages["ma20"].tail(_MAX_CHART_POINTS).tolist()],
        "ma60": [json_num(value, 4)
                 for value in averages["ma60"].tail(_MAX_CHART_POINTS).tolist()],
        "rsi14": [json_num(value, 2)
                  for value in rsi.tail(_MAX_CHART_POINTS).tolist()],
        "drawdown": [json_num(value, 4)
                     for value in drawdown.tail(_MAX_CHART_POINTS).tolist()],
    }
