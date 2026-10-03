#!/usr/bin/env python3
"""
==============================================================================
StockLab - 分析接口处理模块 (app.web.api)
==============================================================================

【模块职责】
  把 HTTP 查询参数翻译成取数 + analyzer 计算，最终组装 JSON 应答。
  只做「参数解析 -> 调用库 -> 组装 JSON」，不含统计逻辑，也不含页面渲染。

【取数路径】
  个股详情 / 证券联想等需要「取数 + 可能回写」的操作统一经
  app.web.store.call_facade() 串行执行（进程级单例门面，见 store 模块说明）；
  纯内存统计（analyzer）放在锁外，不占用串行窗口。
  证券表与全市场聚合走 store 的只读缓存，不再占用串行锁。

【应答约定】
  接口成功返回 200 + 业务 JSON；参数非法返回 400 + {"error": ...}；
  资源不存在（未知路径）返回 404；未捕获异常返回 500 + 通用错误文案，
  细节只进日志（异常信息截断后打印，见 AGENT.md 日志规范）。
==============================================================================
"""

import logging

import pandas as pd
from flask import Response, current_app, jsonify, render_template, request

from app.web import store
from stocklab.analytics import (
    VALUATION_INDICATOR_PB,
    VALUATION_INDICATOR_PE_TTM,
    ValuationPercentileAnalyzer,
    ValuationPercentileReporter,
)
from stocklab.facade import MarketDataFacade

_logger = logging.getLogger("StockLab.Web.Api")

__all__ = [
    "handle_index",
    "handle_market_page",
    "handle_indices_page",
    "handle_industries_page",
    "handle_screener_page",
    "handle_compare_page",
    "handle_favicon",
    "handle_health",
    "handle_percentile",
    "handle_securities",
    "handle_not_found",
    "handle_internal_error",
]


# 证券联想最大返回条数
_MAX_SUGGEST_COUNT = 20

# 证券代码长度上限（防日志与远端请求被超长串拖累）
_MAX_CODE_LENGTH = 20

# 可选的历史区间口径（与 CLI 参数一致）
_PERIOD_CHOICES = ("全部", "近五年", "近十年")

# 多窗口分位的口径：标签 -> 回溯年数（None 表示不限区间）
_WINDOW_CHOICES = (
    ("全部", None),
    ("近十年", 10),
    ("近五年", 5),
    ("近三年", 3),
)

# 分位标签渲染器（复用其指标中文名映射，避免标签表出现第二份拷贝）
_reporter = ValuationPercentileReporter()


# ---------------------------------------------------------------------------
# 参数解析与校验
# ---------------------------------------------------------------------------

def _is_code_like(text):
    """
    判断输入是否「长得像代码」（ASCII 字母数字与点号，且长度合法）

    【为何限定 ASCII】
      str.isalnum() 对中文同样返回 True，若不限定字符集，「茅台」「平安」
      会被当成代码直接送去取数，名称解析永远不触发 —— 这正是初版的缺陷。

    Args:
        text (str): 待判断的输入

    Returns:
        bool: True 表示应按代码处理
    """
    if not text or len(text) > _MAX_CODE_LENGTH:
        return False
    for char in text:
        if not char.isascii():
            return False
        if not (char.isalnum() or char == "."):
            return False
    return True


def _resolve_code(text):
    """
    把用户输入解析成 ts_code：代码原样返回，名称查证券表转换

    【为何要这一步】
      输入框 placeholder 承诺可输入「600519.SH 或 贵州茅台」，但分位接口
      只接受代码；不转换就会出现「输入中文名按回车返回 400」的缺陷。

    Args:
        text (str): 用户输入的代码或名称

    Returns:
        tuple: (ts_code 或 None, 错误文案或 None)
    """
    if _is_code_like(text):
        return text, None

    frame = store.load_securities()
    if frame.empty or "name" not in frame.columns:
        return None, "无法解析输入 [%s]：证券表不可用，请先同步基础数据" % text

    names = frame["name"].fillna("")
    exact = frame[names == text]
    if len(exact) == 1:
        return exact.iloc[0]["ts_code"], None

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


def _parse_date(text, field_label):
    """
    解析并校验 YYYY-MM-DD 日期参数

    Args:
        text (str): 日期字符串
        field_label (str): 参数中文名（用于错误提示）

    Returns:
        tuple: (date 对象或 None, 错误文案或 None)
    """
    if not text:
        return None, None
    try:
        return pd.to_datetime(text).date(), None
    except (ValueError, TypeError):
        return None, "参数 [%s] 不是合法日期: %s" % (field_label, text)


def _parse_float(text, field_label):
    """
    解析并校验浮点参数

    Args:
        text (str): 数值字符串
        field_label (str): 参数中文名（用于错误提示）

    Returns:
        tuple: (float 或 None, 错误文案或 None)
    """
    if not text:
        return None, None
    try:
        return float(text), None
    except ValueError:
        return None, "参数 [%s] 不是合法数值: %s" % (field_label, text)


# ---------------------------------------------------------------------------
# 数据加工
# ---------------------------------------------------------------------------

def _slice_history(history_df, start_date, end_date):
    """
    按起止日期裁剪历史序列，供分位计算与走势图共用同一份数据

    【为何在接口层裁剪】
      analyzer 不返回裁剪后的 DataFrame，而走势图必须与分位统计使用同一
      区间；因此在本层裁剪一次，再把裁剪结果交给 analyzer（不再传 start/end），
      保证「图上看到的」与「分位算出的」永远是同一批样本。

    Args:
        history_df (pd.DataFrame): 完整历史估值序列
        start_date (datetime.date): 起始日期，None 表示不限
        end_date (datetime.date): 结束日期，None 表示不限

    Returns:
        pd.DataFrame: 裁剪并按时间升序的序列
    """
    frame = history_df.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    if start_date:
        frame = frame[frame["trade_date"] >= start_date]
    if end_date:
        frame = frame[frame["trade_date"] <= end_date]
    return frame.sort_values(by="trade_date").reset_index(drop=True)


def _to_json_number(value):
    """
    把 pandas 数值转成 JSON 安全的数值（NaN -> null），并统一轮转精度

    【为何轮转】
      analyzer 输出的分位是 19.444444444444446 这类全长浮点，612 行 × 5 指标
      直接下发会让 payload 白白变大；轮转到 4 位小数既远超展示精度，也让
      payload 收敛到可预期的大小。

    Args:
        value: 原始数值或 NaN

    Returns:
        float | None: JSON 可序列化的数值
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN 自比较不等；JSON 不接受 NaN，须转 null
        return None
    return round(number, 4)


def _round_result(data):
    """
    把单条分位结果里的数值轮转到展示精度

    Args:
        data (dict): analyzer 输出的字典

    Returns:
        dict: 轮转后的字典（原地修改并返回）
    """
    if data.get("percentile") is not None:
        data["percentile"] = round(float(data["percentile"]), 2)
    for field in ("current_value", "median_value", "min_value", "max_value"):
        if data.get(field) is not None:
            data[field] = round(float(data[field]), 4)
    return data


def _build_history_payload(frame, indicators):
    """
    组装走势图数据（日期轴 + 各指标序列）

    Args:
        frame (pd.DataFrame): 已裁剪的历史序列
        indicators (list): 需要输出的指标列名列表

    Returns:
        dict: {"dates": ["YYYY-MM-DD", ...], "series": {指标: [数值或 null]}}
    """
    dates = [item.strftime("%Y-%m-%d") for item in frame["trade_date"].tolist()]
    series = {}
    for indicator in indicators:
        if indicator not in frame.columns:
            continue
        series[indicator] = [_to_json_number(value) for value in frame[indicator].tolist()]
    return {"dates": dates, "series": series}


def _build_windows(frame, current_values):
    """
    在同一份数据上按多个回溯窗口各算一次分位，供页面做「多窗口对比」

    【为何需要】
      单一区间分位看不出趋势：某股「全部历史分位 30% 但近三年分位 85%」
      意味着它刚被市场热炒过，只看一个数会得出相反结论。多窗口并列是
      同类估值工具的通行做法。

    Args:
        frame (pd.DataFrame): 已裁剪的历史序列
        current_values (dict): 覆盖当前值的映射（可为空）

    Returns:
        list[dict]: 每项含 label / sample_rows / percentiles（指标 -> 分位）
    """
    if frame.empty:
        return []

    latest_date = frame["trade_date"].max()
    analyzer = ValuationPercentileAnalyzer()
    windows = []
    for label, years in _WINDOW_CHOICES:
        if years is None:
            subset = frame
        else:
            cutoff = pd.Timestamp(latest_date) - pd.DateOffset(years=years)
            subset = frame[frame["trade_date"] >= cutoff.date()]
        if subset.empty:
            continue

        results = analyzer.analyze(subset, current_values=current_values)
        percentiles = {}
        for item in results:
            percentiles[item.indicator] = (
                None if item.percentile is None else round(float(item.percentile), 2)
            )
        windows.append({
            "label": label,
            "sample_rows": len(subset),
            "percentiles": percentiles,
        })
    return windows


def _lookup_security(code):
    """
    读取证券基础信息（名称 / 交易所 / 市场），用于结果区展示标的的身份

    【为何只有这几个字段】
      上游数据源的 industry / area / list_date / is_hs 全表为空，
      展示恒为空的字段只会让用户以为是 bug，因此只取确有数据的列。

    Args:
        code (str): 证券代码

    Returns:
        dict: 含 name / symbol / exchange / market；查不到时全为空串
    """
    frame = store.load_securities()
    empty = {"name": "", "symbol": "", "exchange": "", "market": ""}
    if frame.empty or "ts_code" not in frame.columns:
        return empty
    hit = frame[frame["ts_code"] == code]
    if hit.empty:
        return empty
    row = hit.iloc[0]
    result = {}
    for field in empty:
        result[field] = _clean_text(row.get(field))
    return result


# ---------------------------------------------------------------------------
# 路由处理器
# ---------------------------------------------------------------------------

def handle_index():
    """
    首页：渲染个股分析页面模板

    Returns:
        str: HTML 页面
    """
    return render_template("analysis.html")


def handle_market_page():
    """
    全市场 Dashboard 页面

    Returns:
        str: HTML 页面
    """
    return render_template("market.html")


def handle_indices_page():
    """
    指数估值页面

    Returns:
        str: HTML 页面
    """
    return render_template("indices.html")


def handle_industries_page():
    """
    行业估值页面

    Returns:
        str: HTML 页面
    """
    return render_template("industries.html")


def handle_screener_page():
    """
    选股器页面（可视化规则编辑器）

    Returns:
        str: HTML 页面
    """
    return render_template("screener.html")


def handle_compare_page():
    """
    多股对比页面（2~10 只标的同屏对照）

    Returns:
        str: HTML 页面
    """
    return render_template("compare.html")


def handle_favicon():
    """
    站点图标：内联 SVG，避免浏览器默认请求 /favicon.ico 产生 404 噪音

    Returns:
        Response: image/svg+xml 响应
    """
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#2563eb"/>'
        '<path d="M6 22 L12 14 L17 18 L26 8" fill="none" stroke="#ffffff" '
        'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="26" cy="8" r="3" fill="#facc15"/>'
        "</svg>"
    )
    return Response(svg, mimetype="image/svg+xml")


def handle_health():
    """
    健康检查：确认服务存活并回显当前配置

    Returns:
        Response: JSON 状态信息
    """
    return jsonify({
        "status": "ok",
        "priority": current_app.config.get("STOCKLAB_PRIORITY") or "配置文件默认值",
        "db_path": store.current_db_path(),
        "data_as_of": store.market_data_as_of(),
    })


def handle_securities():
    """
    证券联想：按代码/名称模糊匹配，供输入框下拉提示

    【排序规则】（修复「命中哪 20 条取决于表物理顺序」的缺陷）
      代码前缀 > 代码包含 > 名称前缀 > 名称包含，同级再按代码升序，
      保证最相关的标的排在最前且结果稳定可预期。

    Query:
        q (str): 匹配关键字；空串返回空列表

    Returns:
        Response: JSON 列表 [{"code": ..., "name": ..., "market": ...}, ...]
    """
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"items": []})

    securities_df = store.load_securities()
    if securities_df is None or securities_df.empty:
        return jsonify({"items": []})

    keyword = query.lower()
    codes = securities_df["ts_code"].fillna("").str.lower()
    names = securities_df["name"].fillna("").str.lower()

    code_prefix = codes.str.startswith(keyword)
    code_hit = codes.str.contains(keyword, case=False, na=False, regex=False)
    name_prefix = names.str.startswith(keyword)
    name_hit = names.str.contains(keyword, case=False, na=False, regex=False)

    matched = securities_df[code_prefix | code_hit | name_prefix | name_hit].copy()
    if matched.empty:
        return jsonify({"items": []})

    matched["_rank"] = 3
    matched.loc[name_prefix[matched.index], "_rank"] = 2
    matched.loc[code_hit[matched.index], "_rank"] = 1
    matched.loc[code_prefix[matched.index], "_rank"] = 0
    matched = matched.sort_values(by=["_rank", "ts_code"]).head(_MAX_SUGGEST_COUNT)

    items = []
    for _, row in matched.iterrows():
        # name / market 为 NaN 时输出空串（NaN != NaN，JSON 无法序列化）
        items.append({
            "code": row["ts_code"],
            "name": _clean_text(row.get("name")),
            "market": _clean_text(row.get("market")),
        })
    return jsonify({"items": items})


def _clean_text(value):
    """
    把 DataFrame 单元格值转成干净字符串（NaN -> 空串）

    Args:
        value: 单元格原始值

    Returns:
        str: 非空字符串
    """
    if value is None:
        return ""
    if isinstance(value, float) and value != value:
        return ""
    return str(value)


def handle_percentile():
    """
    个股历史估值分位分析（核心接口）

    Query:
        code (str): 证券代码或名称，必填，如 600519.SH 或 贵州茅台
        period (str): 向远端请求的历史区间：全部 / 近五年 / 近十年（默认 全部）
        start_date (str): 计算区间起始日期 YYYY-MM-DD，可空
        end_date (str): 计算区间结束日期 YYYY-MM-DD，可空
        pe_ttm (str): 当前 PE-TTM，可空（缺省取历史序列最新一日）
        pb (str): 当前 PB，可空

    Returns:
        Response: 200 时含 results 分位列表 / history 走势 / windows 多窗口分位 /
                  security 标的信息；参数非法 400；数据缺失 200 + 空 results + message
    """
    raw_code = request.args.get("code", "").strip()
    if not raw_code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    code, resolve_error = _resolve_code(raw_code)
    if resolve_error:
        return jsonify({"error": resolve_error}), 400

    period = request.args.get("period", "全部")
    if period not in _PERIOD_CHOICES:
        return jsonify({
            "error": "参数 [period] 非法，合法值: %s" % " / ".join(_PERIOD_CHOICES)
        }), 400

    start_date, error = _parse_date(request.args.get("start_date", ""), "start_date")
    if error:
        return jsonify({"error": error}), 400
    end_date, error = _parse_date(request.args.get("end_date", ""), "end_date")
    if error:
        return jsonify({"error": error}), 400

    # 起止倒置属于参数错误，不能伪装成「区间内没有交易日」误导用户
    if start_date and end_date and start_date > end_date:
        return jsonify({"error": "参数非法：起始日期 %s 晚于结束日期 %s" % (
            start_date.isoformat(), end_date.isoformat()
        )}), 400

    pe_ttm, error = _parse_float(request.args.get("pe_ttm", ""), "pe_ttm")
    if error:
        return jsonify({"error": error}), 400
    pb, error = _parse_float(request.args.get("pb", ""), "pb")
    if error:
        return jsonify({"error": error}), 400

    current_values = {}
    if pe_ttm is not None:
        current_values[VALUATION_INDICATOR_PE_TTM] = pe_ttm
    if pb is not None:
        current_values[VALUATION_INDICATOR_PB] = pb

    # 取数（可能含远端回退与回写）串行执行
    try:
        history_df = store.call_facade("fetch_valuation_history", code, period)
        priority = store.current_priority()
    except Exception as exc:
        _logger.error("取数失败 [%s]: %s", code, str(exc)[:300])
        return jsonify({"error": "读取 %s 的估值历史失败，请稍后重试" % code}), 500

    security = _lookup_security(code)

    if history_df is None or history_df.empty:
        return jsonify({
            "code": code,
            "priority": priority,
            "security": security,
            "results": [],
            "history": None,
            "windows": [],
            "message": "未找到 %s 的历史估值数据；请检查代码，或运行数据同步后重试" % code,
        })

    frame = _slice_history(history_df, start_date, end_date)
    if frame.empty:
        return jsonify({
            "code": code,
            "priority": priority,
            "security": security,
            "results": [],
            "history": None,
            "windows": [],
            "message": "指定区间内没有交易日样本，请调整起止日期",
        })

    # 纯内存统计，放在串行锁之外执行
    analyzer = ValuationPercentileAnalyzer()
    results = analyzer.analyze(frame, current_values=current_values)

    payload_results = []
    indicators = []
    interval_text = ""
    for item in results:
        data = _round_result(item.to_dict())
        data["label"] = _reporter.indicator_label(item.indicator)
        # 七档评级（展示层口径，与 analyzer 内部的三档结论并存）
        data["level7"] = store.percentile_level(data.get("percentile"))
        payload_results.append(data)
        indicators.append(item.indicator)
        if not interval_text and item.interval_text:
            interval_text = item.interval_text

    return jsonify({
        "code": code,
        "raw_code": raw_code,
        "priority": priority,
        "security": security,
        "interval_text": interval_text,
        "sample_rows": len(frame),
        "results": payload_results,
        "history": _build_history_payload(frame, indicators),
        "windows": _build_windows(frame, current_values),
        "message": "",
    })


def handle_not_found(error):
    """
    404 兜底：接口路径返回 JSON，页面路径返回纯文本

    Args:
        error (HTTPException): 触发的 404 异常

    Returns:
        Response: 错误应答
    """
    if request.path.startswith("/api/"):
        return jsonify({"error": "接口不存在: %s" % request.path}), 404
    return "404 Not Found: %s" % request.path, 404


def handle_internal_error(error):
    """
    500 兜底：细节只进日志，对外返回通用文案

    Args:
        error (InternalServerError): 触发的 500 异常

    Returns:
        Response: JSON 错误应答
    """
    original = getattr(error, "original_exception", None) or error
    _logger.error(
        "服务端异常 [%s]: %s",
        request.path,
        str(original)[:300],
        exc_info=(type(original), original, getattr(original, "__traceback__", None)),
    )
    return jsonify({"error": "服务端内部错误，请查看服务日志"}), 500
