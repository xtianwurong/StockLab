#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金调仓分析接口 (app.web.fund_analysis_api)
==============================================================================

【模块职责】
  基金调仓分析页面的接口：
    - handle_fund_analysis_page()    页面渲染
    - handle_fund_list()             基金列表（支持类型筛选）
    - handle_fund_info()             基金基本信息
    - handle_fund_nav()              基金净值历史
    - handle_sw_indices()            申万行业指数列表
    - handle_industry_mapping()      行业映射表
    - handle_sector_indices()        板块指数日线
    - handle_capital_flow()          板块资金流
    - handle_fund_allocation()       基金调仓分析（核心）
    - handle_allocation_history()    历史分析结果

【核心约束】
  - 调仓分析必须走 FundAnalysisFacade（纯本地读）
  - 所有查询参数必须校验（code 必填、日期格式、window_days 范围）
  - 分析结果按置信度/信号强度排序返回
  - 资金流佐证作为可选增强，不可用时降级

【应答约定】
  200 + 业务 JSON / 400 参数非法 / 500 通用文案
"""

import logging
from datetime import date
from flask import jsonify, render_template, request

from app.web import store
from app.web.webcommon import parse_int_arg, parse_date_arg
from stocklab.facade import FundAnalysisFacade

_logger = logging.getLogger("StockLab.Web.FundAnalysisApi")

__all__ = [
    "handle_fund_analysis_page",
    "handle_fund_list",
    "handle_fund_info",
    "handle_fund_nav",
    "handle_sw_indices",
    "handle_industry_mapping",
    "handle_sector_indices",
    "handle_capital_flow",
    "handle_fund_allocation",
    "handle_allocation_history",
]

# 参数约束
_MIN_WINDOW_DAYS = 10
_MAX_WINDOW_DAYS = 250
_VALID_LEVELS = (1, 2)
_DEFAULT_WINDOW_DAYS = 60
_DEFAULT_LEVEL = 1


def handle_fund_analysis_page():
    """GET /fund-analysis —— 页面"""
    return render_template("fund_analysis.html")


def handle_fund_list():
    """GET /api/fund/list?type=股票型 —— 基金列表"""
    fund_type = request.args.get("type")
    status = request.args.get("status", "active")

    try:
        facade = _facade()
        df = facade.get_fund_list(fund_type=fund_type)
        if status:
            df = df[df["status"] == status] if "status" in df.columns else df

        return jsonify({
            "items": df.to_dict(orient="records") if not df.empty else [],
            "count": len(df),
        })
    except Exception:
        _logger.exception("基金列表查询失败")
        return jsonify({"error": "基金列表加载失败，请稍后重试"}), 500


def handle_fund_info():
    """GET /api/fund/info?code=000001.OF —— 基金基本信息"""
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    try:
        facade = _facade()
        df = facade.get_fund_info(code)
        if df.empty:
            return jsonify({"error": f"未找到基金: {code}"}), 404

        return jsonify(df.to_dict(orient="records")[0])
    except Exception:
        _logger.exception("基金信息查询失败 [%s]", code)
        return jsonify({"error": "基金信息加载失败，请稍后重试"}), 500


def handle_fund_nav():
    """GET /api/fund/nav?code=000001.OF&start=2025-01-01&end=2026-09-30 —— 基金净值历史"""
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    start, err = parse_date_arg("start", request.args.get("start"))
    if err:
        return jsonify({"error": err}), 400

    end, err = parse_date_arg("end", request.args.get("end"))
    if err:
        return jsonify({"error": err}), 400

    try:
        facade = _facade()
        df = facade.get_fund_nav(code, start, end)
        return jsonify({
            "code": code,
            "items": df.to_dict(orient="records") if not df.empty else [],
            "count": len(df),
        })
    except Exception:
        _logger.exception("基金净值查询失败 [%s]", code)
        return jsonify({"error": "基金净值加载失败，请稍后重试"}), 500


def handle_sw_indices():
    """GET /api/sw/indices?level=1 —— 申万行业指数列表"""
    level, err = parse_int_arg("level", request.args.get("level"), None, minimum=1, maximum=2)
    if err:
        return jsonify({"error": err}), 400

    try:
        facade = _facade()
        df = facade.get_sw_indices(level=level)
        return jsonify({
            "items": df.to_dict(orient="records") if not df.empty else [],
            "count": len(df),
        })
    except Exception:
        _logger.exception("申万指数列表查询失败")
        return jsonify({"error": "申万指数加载失败"}), 500


def handle_industry_mapping():
    """GET /api/sw/mapping —— 申万行业映射表（全量）"""
    try:
        facade = _facade()
        df = facade.get_industry_mapping()
        return jsonify({
            "items": df.to_dict(orient="records") if not df.empty else [],
            "count": len(df),
        })
    except Exception:
        _logger.exception("行业映射查询失败")
        return jsonify({"error": "行业映射加载失败"}), 500


def handle_sector_indices():
    """GET /api/sector/indices?codes=801010.SI,801020.SI&start=2025-01-01 —— 板块指数日线"""
    codes = (request.args.get("codes") or "").strip()
    if not codes:
        return jsonify({"error": "参数 [codes] 非法：必填，逗号分隔"}), 400

    code_list = [c.strip().upper() for c in codes.split(",") if c.strip()]
    if not code_list:
        return jsonify({"error": "参数 [codes] 非法：至少需要一个代码"}), 400

    start, err = parse_date_arg("start", request.args.get("start"))
    if err:
        return jsonify({"error": err}), 400

    try:
        facade = _facade()
        since = start if start else None
        df = facade.get_sector_indices(code_list, since=since)
        return jsonify({
            "codes": code_list,
            "items": df.to_dict(orient="records") if not df.empty else [],
            "count": len(df),
        })
    except Exception:
        _logger.exception("板块指数查询失败")
        return jsonify({"error": "板块指数加载失败"}), 500


def handle_capital_flow():
    """GET /api/capital/flow?type=sw_level1&date=2026-09-30 —— 板块资金流"""
    sector_type = request.args.get("type", "sw_level1")
    valid_types = ("sw_level1", "sw_level2", "concept", "industry")
    if sector_type not in valid_types:
        return jsonify({"error": f"参数 [type] 非法：必须是 {', '.join(valid_types)}"}), 400

    trade_date = request.args.get("date", date.today().strftime("%Y-%m-%d"))

    try:
        facade = _facade()
        df = facade.get_capital_flow(sector_type, trade_date)
        return jsonify({
            "sector_type": sector_type,
            "date": trade_date,
            "items": df.to_dict(orient="records") if not df.empty else [],
            "count": len(df),
        })
    except Exception:
        _logger.exception("资金流查询失败 [%s]", sector_type)
        return jsonify({"error": "资金流数据加载失败"}), 500


def handle_fund_allocation():
    """GET /api/fund/allocation?code=000001.OF&date=2026-09-30&window=60&level=1 —— 核心调仓分析"""
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    analysis_date, err = parse_date_arg("date", request.args.get("date"))
    if err:
        return jsonify({"error": err}), 400
    if analysis_date is None:
        analysis_date = date.today()

    window_days, err = parse_int_arg("window", request.args.get("window"), 60, minimum=10, maximum=250)
    if err:
        return jsonify({"error": err}), 400

    level, err = parse_int_arg("level", request.args.get("level"), 1, minimum=1, maximum=2)
    if err:
        return jsonify({"error": err}), 400

    try:
        facade = _facade()
        result = facade.analyze_fund_allocation(
            fund_code=code,
            analysis_date=analysis_date,
            window_days=window_days,
            level=level,
        )

        if "error" in result:
            return jsonify(result), 400

        return jsonify(result)
    except Exception:
        _logger.exception("基金调仓分析失败 [%s]", code)
        return jsonify({"error": "调仓分析失败，请稍后重试"}), 500


def handle_allocation_history():
    """GET /api/fund/allocation/history?code=000001.OF&date=2026-09-30&window=60&level=1 —— 历史分析结果"""
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    # 与 /api/fund/allocation 同一套校验：坏日期必须 400，
    # 否则原样透传给门面只会查出空表，调用方以为「这天没有历史」其实是「日期填错了」
    analysis_date, err = parse_date_arg("date", request.args.get("date"))
    if err:
        return jsonify({"error": err}), 400
    if not analysis_date:
        return jsonify({"error": "参数 [date] 非法：必填"}), 400

    window_days, err = parse_int_arg("window", request.args.get("window"), 60, minimum=10, maximum=250)
    if err:
        return jsonify({"error": err}), 400

    level, err = parse_int_arg("level", request.args.get("level"), 1, minimum=1, maximum=2)
    if err:
        return jsonify({"error": err}), 400

    try:
        facade = _facade()
        df = facade.get_allocation_history(code, analysis_date, window_days, level)
        return jsonify({
            "code": code,
            "date": analysis_date,
            "window_days": window_days,
            "level": level,
            "items": df.to_dict(orient="records") if not df.empty else [],
        })
    except Exception:
        _logger.exception("历史分析结果查询失败 [%s]", code)
        return jsonify({"error": "历史分析加载失败"}), 500


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def _facade():
    return FundAnalysisFacade(store.facade_database())