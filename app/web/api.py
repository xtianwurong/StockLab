#!/usr/bin/env python3
"""
==============================================================================
StockLab - 分析接口处理模块 (app.web.api)
==============================================================================

【模块职责】
  把 HTTP 查询参数翻译成 facade 取数 + analyzer 计算，最终组装 JSON 应答。
  只做「参数解析 -> 调用库 -> 组装 JSON」，不含统计逻辑，也不含页面渲染。

【并发约束】
  DuckDB 连接非线程安全（见 AGENT.md 注意事项），且 facade 取数可能触发
  远端回写（UPSERT）。因此所有 facade 调用统一经 _FACADE_LOCK 串行执行，
  统计计算（analyzer，纯内存）放在锁外，不占用串行窗口。

【应答约定】
  接口成功返回 200 + 业务 JSON；参数非法返回 400 + {"error": ...}；
  资源不存在（未知路径）返回 404；未捕获异常返回 500 + 通用错误文案，
  细节只进日志（异常信息截断后打印，见 AGENT.md 日志规范）。
==============================================================================
"""

import logging
import threading

import pandas as pd
from flask import current_app, jsonify, render_template, request

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
    "handle_health",
    "handle_percentile",
    "handle_securities",
    "handle_not_found",
    "handle_internal_error",
]


# 全局串行锁：包住 facade 的「取数 + 可能的回写」，保证落库串行
_FACADE_LOCK = threading.Lock()

# 证券联想最大返回条数
_MAX_SUGGEST_COUNT = 20

# 证券代码长度上限（防日志与远端请求被超长串拖累）
_MAX_CODE_LENGTH = 20

# 可选的历史区间口径（与 CLI 参数一致）
_PERIOD_CHOICES = ("全部", "近五年", "近十年")

# 分位标签渲染器（复用其指标中文名映射，避免标签表出现第二份拷贝）
_reporter = ValuationPercentileReporter()


def _create_facade():
    """
    按服务启动时的配置创建取数门面

    Returns:
        MarketDataFacade: 已按 config.ini / 命令行参数配置好的门面实例
    """
    return MarketDataFacade(
        priority=current_app.config.get("STOCKLAB_PRIORITY"),
        db_path=current_app.config.get("STOCKLAB_DB_PATH"),
    )


def _is_valid_code(code):
    """
    校验证券代码字符集与长度（只拦明显非法输入，未知代码交由取数层处理）

    Args:
        code (str): 待校验的代码

    Returns:
        bool: True 表示格式可接受
    """
    if not code or len(code) > _MAX_CODE_LENGTH:
        return False
    for char in code:
        if not (char.isalnum() or char in "."):
            return False
    return True


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
    把 pandas 数值转成 JSON 安全的数值（NaN -> null）

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
    return number


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


def handle_index():
    """
    首页：渲染分析页面模板

    Returns:
        str: HTML 页面
    """
    return render_template("analysis.html")


def handle_health():
    """
    健康检查：确认服务存活并回显当前配置

    Returns:
        Response: JSON 状态信息
    """
    return jsonify({
        "status": "ok",
        "priority": current_app.config.get("STOCKLAB_PRIORITY") or "配置文件默认值",
        "db_path": current_app.config.get("STOCKLAB_DB_PATH") or "data/stocklab.duckdb",
    })


def handle_securities():
    """
    证券联想：按代码前缀或名称模糊匹配，供输入框下拉提示

    Query:
        q (str): 匹配关键字；空串返回空列表

    Returns:
        Response: JSON 列表 [{"code": ..., "name": ...}, ...]
    """
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"items": []})

    with _FACADE_LOCK:
        with _create_facade() as facade:
            securities_df = facade.fetch_securities()

    if securities_df is None or securities_df.empty:
        return jsonify({"items": []})

    code_hit = securities_df["ts_code"].str.contains(
        query, case=False, na=False, regex=False
    )
    name_hit = securities_df["name"].fillna("").str.contains(query, regex=False)
    matched = securities_df[code_hit | name_hit].head(_MAX_SUGGEST_COUNT)

    items = []
    for code, name in zip(matched["ts_code"].tolist(), matched["name"].tolist()):
        # name 为 NaN 时改输出空串（NaN != NaN，JSON 也无法序列化 NaN）
        items.append({"code": code, "name": name if name == name else ""})
    return jsonify({"items": items})


def handle_percentile():
    """
    个股历史估值分位分析（核心接口）

    Query:
        code (str): 证券代码，必填，如 600519.SH
        period (str): 向远端请求的历史区间：全部 / 近五年 / 近十年（默认 全部）
        start_date (str): 计算区间起始日期 YYYY-MM-DD，可空
        end_date (str): 计算区间结束日期 YYYY-MM-DD，可空
        pe_ttm (str): 当前 PE-TTM，可空（缺省取历史序列最新一日）
        pb (str): 当前 PB，可空

    Returns:
        Response: 200 时含 results 分位列表与 history 走势数据；
                  参数非法 400；数据缺失 200 + 空 results + message
    """
    code = request.args.get("code", "").strip()
    if not _is_valid_code(code):
        return jsonify({"error": "参数 [code] 非法：必填且仅允许字母数字与点号"}), 400

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
    with _FACADE_LOCK:
        with _create_facade() as facade:
            history_df = facade.fetch_valuation_history(code, period)
            priority = facade.priority

    if history_df is None or history_df.empty:
        return jsonify({
            "code": code,
            "priority": priority,
            "results": [],
            "history": None,
            "message": "未找到 %s 的历史估值数据；请检查代码，或运行数据同步后重试" % code,
        })

    frame = _slice_history(history_df, start_date, end_date)
    if frame.empty:
        return jsonify({
            "code": code,
            "priority": priority,
            "results": [],
            "history": None,
            "message": "指定区间内没有交易日样本，请调整起止日期",
        })

    # 纯内存统计，放在串行锁之外执行
    analyzer = ValuationPercentileAnalyzer()
    results = analyzer.analyze(frame, current_values=current_values)

    payload_results = []
    indicators = []
    interval_text = ""
    for item in results:
        data = item.to_dict()
        data["label"] = _reporter.indicator_label(item.indicator)
        payload_results.append(data)
        indicators.append(item.indicator)
        if not interval_text and item.interval_text:
            interval_text = item.interval_text

    return jsonify({
        "code": code,
        "priority": priority,
        "interval_text": interval_text,
        "sample_rows": len(frame),
        "results": payload_results,
        "history": _build_history_payload(frame, indicators),
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
