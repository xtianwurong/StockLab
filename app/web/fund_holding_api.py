#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金持仓与行业暴露接口 (app.web.fund_holding_api)
==============================================================================

【模块职责】
  基金持仓与行业暴露页面的接口：
    - handle_fund_holding_page()       页面渲染（复用 fund_analysis.html，通过 tab 切换）
    - handle_fund_holding()            基金持仓历史
    - handle_latest_holding()          最新一期持仓
    - handle_industry_exposure()       真实行业暴露（报告期）
    - handle_industry_exposure_daily() 真实行业暴露日线（插值）
    - handle_stock_industry_mapping()  股票行业映射表
    - handle_holding_vs_rbsa()         RBSA vs 持仓双轨对比
    - handle_fund_attribution()        Brinson 归因（配置/选择/交互三效应）

【核心约束】
  - 所有查询必须走 FundHoldingFacade（纯本地读）
  - 日期参数严格校验 YYYY-MM-DD
  - code 必填，支持 000001.OF 格式

【应答约定】
  200 + 业务 JSON / 400 参数非法 / 500 通用文案
"""

import logging
from datetime import date

import pandas as pd
from flask import jsonify, request

from app.web import store
from app.web.webcommon import parse_int_arg, parse_date_arg
from stocklab.analytics import FundAttributionEngine
from stocklab.datasource import BENCHMARK_INDEX_CODES
from stocklab.facade import FundHoldingFacade

_logger = logging.getLogger("StockLab.Web.FundHoldingApi")

__all__ = [
    "handle_fund_holding_list",
    "handle_latest_holding",
    "handle_industry_exposure",
    "handle_industry_exposure_daily",
    "handle_stock_industry_mapping",
    "handle_holding_vs_rbsa",
    "handle_fund_attribution",
]

# 参数约束
_MIN_LEVEL = 1
_MAX_LEVEL = 3
_DEFAULT_LEVEL = 1
_DEFAULT_BENCHMARK = "000300.SH"
_ATTRIBUTION_MODELS = ("brinson-fachler", "brinson-hb")


def handle_fund_holding_list():
    """GET /api/fund/holding?code=000001.OF&start=2023-01-01&end=2024-12-31 —— 基金持仓历史"""
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
        df = facade.get_fund_holding_history(code)
        if start:
            df = df[df["report_date"] >= start]
        if end:
            df = df[df["report_date"] <= end]

        return jsonify({
            "code": code,
            "items": df.to_dict(orient="records") if not df.empty else [],
            "count": len(df),
        })
    except Exception:
        _logger.exception("基金持仓历史查询失败 [%s]", code)
        return jsonify({"error": "基金持仓加载失败，请稍后重试"}), 500


def handle_latest_holding():
    """GET /api/fund/holding/latest?code=000001.OF —— 最新一期持仓"""
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    try:
        facade = _facade()
        df = facade.get_fund_holding(code)
        if df.empty:
            return jsonify({"error": f"未找到基金 {code} 的持仓数据"}), 404

        report_date = df["report_date"].iloc[0] if "report_date" in df.columns else None
        return jsonify({
            "code": code,
            "report_date": str(report_date) if report_date else None,
            "report_type": df["report_type"].iloc[0] if "report_type" in df.columns else None,
            "items": df.to_dict(orient="records"),
            "count": len(df),
        })
    except Exception:
        _logger.exception("基金最新持仓查询失败 [%s]", code)
        return jsonify({"error": "基金持仓加载失败，请稍后重试"}), 500


def handle_industry_exposure():
    """GET /api/fund/exposure?code=000001.OF&date=2024-06-30&level=1 —— 真实行业暴露（报告期）"""
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    report_date, err = parse_date_arg("date", request.args.get("date"))
    if err:
        return jsonify({"error": err}), 400

    level, err = parse_int_arg("level", request.args.get("level"), 1, minimum=1, maximum=3)
    if err:
        return jsonify({"error": err}), 400

    try:
        facade = _facade()
        if report_date:
            report_date = pd.to_datetime(report_date).date()
        df = facade.get_industry_exposure(code, report_date, level)

        return jsonify({
            "code": code,
            "report_date": str(report_date) if report_date else None,
            "level": level,
            "items": df.to_dict(orient="records") if not df.empty else [],
            "count": len(df),
        })
    except Exception:
        _logger.exception("行业暴露查询失败 [%s]", code)
        return jsonify({"error": "行业暴露加载失败，请稍后重试"}), 500


def handle_industry_exposure_daily():
    """GET /api/fund/exposure/daily?code=000001.OF&level=1&start=2024-01-01&end=2024-12-31 —— 行业暴露日线（插值）"""
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    level, err = parse_int_arg("level", request.args.get("level"), 1, minimum=1, maximum=3)
    if err:
        return jsonify({"error": err}), 400

    start, err = parse_date_arg("start", request.args.get("start"))
    if err:
        return jsonify({"error": err}), 400

    end, err = parse_date_arg("end", request.args.get("end"))
    if err:
        return jsonify({"error": err}), 400

    try:
        facade = _facade()
        since = pd.to_datetime(start).date() if start else None
        until = pd.to_datetime(end).date() if end else None
        df = facade.get_industry_exposure_daily(code, level=level, since=since, until=until)

        return jsonify({
            "code": code,
            "level": level,
            "start": start,
            "end": end,
            "items": df.to_dict(orient="records") if not df.empty else [],
            "count": len(df),
        })
    except Exception:
        _logger.exception("行业暴露日线查询失败 [%s]", code)
        return jsonify({"error": "行业暴露日线加载失败"}), 500


def handle_stock_industry_mapping():
    """GET /api/stock/industry/mapping?level=1 —— 股票行业映射表"""
    level, err = parse_int_arg("level", request.args.get("level"), None, minimum=1, maximum=3)
    if err:
        return jsonify({"error": err}), 400

    try:
        facade = _facade()
        if level == 1:
            mapping = facade.get_sw_l1_map()
            return jsonify({"mapping": mapping, "count": len(mapping)})
        elif level == 2:
            mapping = facade.get_sw_l2_map()
            return jsonify({"mapping": mapping, "count": len(mapping)})
        else:
            df = facade.get_stock_industry_mapping()
            return jsonify({
                "items": df.to_dict(orient="records") if not df.empty else [],
                "count": len(df),
            })
    except Exception:
        _logger.exception("行业映射查询失败")
        return jsonify({"error": "行业映射加载失败"}), 500


def handle_holding_vs_rbsa():
    """GET /api/fund/holding/vs_rbsa?code=000001.OF&date=2024-06-30&window=60&level=1 —— RBSA vs 持仓双轨对比"""
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    analysis_date, err = parse_date_arg("date", request.args.get("date"))
    if err:
        return jsonify({"error": err}), 400

    window_days, err = parse_int_arg("window", request.args.get("window"), 60, minimum=10, maximum=250)
    if err:
        return jsonify({"error": err}), 400

    level, err = parse_int_arg("level", request.args.get("level"), 1, minimum=1, maximum=2)
    if err:
        return jsonify({"error": err}), 400

    try:
        facade = _facade()
        analysis_dt = pd.to_datetime(analysis_date).date() if analysis_date else date.today()
        result = facade.compare_rbsa_vs_holding(code, analysis_dt, window_days, level)

        if "error" in result:
            return jsonify(result), 400

        return jsonify(result)
    except Exception:
        _logger.exception("RBSA vs 持仓对比失败 [%s]", code)
        return jsonify({"error": "双轨对比失败，请稍后重试"}), 500


def handle_fund_attribution():
    """
    GET /api/fund/attribution?code=110022.OF&date=2026-09-30&benchmark=000300.SH
                             &days=90&level=1&model=brinson-fachler
    —— Brinson 归因：超额收益拆成配置 / 选择 / 交互三部分
    """
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    trade_date, err = parse_date_arg("date", request.args.get("date"))
    if err:
        return jsonify({"error": err}), 400
    if not trade_date:
        trade_date = date.today().isoformat()

    benchmark = (request.args.get("benchmark") or _DEFAULT_BENCHMARK).strip().upper()
    plain_benchmark = benchmark.split(".")[0]
    if benchmark not in BENCHMARK_INDEX_CODES:
        matched = next((c for c in BENCHMARK_INDEX_CODES if c.startswith(plain_benchmark)), None)
        if matched is None:
            return jsonify({
                "error": "参数 [benchmark] 非法，可选：%s" % "、".join(BENCHMARK_INDEX_CODES)
            }), 400
        benchmark = matched

    days, err = parse_int_arg("days", request.args.get("days"), 90, minimum=5, maximum=730)
    if err:
        return jsonify({"error": err}), 400

    level, err = parse_int_arg("level", request.args.get("level"), 1, minimum=1, maximum=2)
    if err:
        return jsonify({"error": err}), 400

    model = (request.args.get("model") or "brinson-fachler").strip().lower()
    if model not in _ATTRIBUTION_MODELS:
        return jsonify({
            "error": "参数 [model] 非法，可选：%s" % "、".join(_ATTRIBUTION_MODELS)
        }), 400

    engine = None
    try:
        engine = FundAttributionEngine(store.facade_database())
        payload = engine.attribute_fund(
            fund_code=code,
            trade_date=trade_date,
            benchmark_code=benchmark,
            period_days=days,
            level=level,
            model=model,
        )
        if "error" in payload:
            return jsonify(payload), 400
        return jsonify(payload)
    except Exception:
        _logger.exception("基金归因分析失败 [%s]", code)
        return jsonify({"error": "归因分析失败，请稍后重试"}), 500
    finally:
        if engine is not None:
            engine.close()


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def _facade():
    return FundHoldingFacade(store.facade_database())