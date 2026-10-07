#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基本面数据接口处理模块 (app.web.fundamental_api)
==============================================================================

【模块职责】
  个股分析页的基本面数据接口：
    - handle_fundamental_income()        利润表
    - handle_fundamental_balance()       资产负债表
    - handle_fundamental_cashflow()      现金流量表
    - handle_fundamental_indicators()    财务指标（质量/成长）
    - handle_fundamental_latest()        最新可见报告期

【一个不可让渡的页面契约：Point-in-Time 安全】
  所有查询必须带 as_of_date（历史时点），门面在 available_date <= as_of_date
  上过滤。若前端只传 code 不传 as_of_date，默认取全部历史（用于首次加载
  展示最新几期），但任何涉及「历史回测/因子计算」的场景必须显式传 as_of。

【取数路径】
  经 stocklab.facade.FundamentalDataFacade 取数，连接向 store.facade_database()
  借出门面那一个 —— 不能自己 duckdb.connect()，同文件第二个写连接会抛
  "Could not set lock"。
  **本模块不 import stocklab.persistence**：取数编排属 stocklab 内部，
  接口层只做参数校验、展示文案与 JSON 组装（依赖方向见 AGENT.md）。

【应答约定】
  与 app.web.api 一致：200 + 业务 JSON / 400 参数非法 / 500 通用文案。
"""

import logging
import re

from flask import jsonify, request

from app.web import store
from stocklab.facade import FundamentalDataFacade

_logger = logging.getLogger("StockLab.Web.FundamentalApi")

__all__ = [
    "handle_fundamental_income",
    "handle_fundamental_balance",
    "handle_fundamental_cashflow",
    "handle_fundamental_indicators",
    "handle_fundamental_latest",
]

# 允许的 as_of_date 范围（历史回测只需到 1990 年）
_MIN_AS_OF_DATE = "1990-01-01"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _facade():
    """构造门面（连接向 store 借，避免同文件第二个写连接）"""
    return FundamentalDataFacade(store.facade_database())


def _parse_as_of():
    """
    解析 as_of_date 参数

    Returns:
        tuple: (as_of_date_str_or_None, error_response_or_None)
    """
    as_of = request.args.get("as_of")
    if as_of is None or as_of == "":
        return None, None
    # 简单的日期格式校验
    if not _DATE_RE.match(as_of):
        return None, (jsonify({"error": "参数 [as_of] 格式非法，需 YYYY-MM-DD"}), 400)
    if as_of < _MIN_AS_OF_DATE:
        return None, (jsonify({"error": f"参数 [as_of] 不能早于 {_MIN_AS_OF_DATE}"}), 400)
    return as_of, None


def handle_fundamental_income():
    """
    GET /api/fundamental/income?code=600519.SH[&as_of=2024-03-31] —— 利润表

    Returns:
        Response: 200 JSON / 400 参数非法 / 500 通用错误
    """
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    as_of, error = _parse_as_of()
    if error:
        return error

    try:
        facade = _facade()
        df = facade.income_statement(code, as_of)
    except Exception:  # noqa: BLE001
        _logger.exception("利润表查询失败 [%s]", code)
        return jsonify({"error": "利润表数据加载失败，请稍后重试"}), 500

    return jsonify({
        "code": code,
        "as_of": as_of,
        "items": df.to_dict(orient="records") if not df.empty else [],
    })


def handle_fundamental_balance():
    """
    GET /api/fundamental/balance?code=600519.SH[&as_of=2024-03-31] —— 资产负债表

    Returns:
        Response: 200 JSON / 400 参数非法 / 500 通用错误
    """
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    as_of, error = _parse_as_of()
    if error:
        return error

    try:
        facade = _facade()
        df = facade.balance_sheet(code, as_of)
    except Exception:  # noqa: BLE001
        _logger.exception("资产负债表查询失败 [%s]", code)
        return jsonify({"error": "资产负债表数据加载失败，请稍后重试"}), 500

    return jsonify({
        "code": code,
        "as_of": as_of,
        "items": df.to_dict(orient="records") if not df.empty else [],
    })


def handle_fundamental_cashflow():
    """
    GET /api/fundamental/cashflow?code=600519.SH[&as_of=2024-03-31] —— 现金流量表

    Returns:
        Response: 200 JSON / 400 参数非法 / 500 通用错误
    """
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    as_of, error = _parse_as_of()
    if error:
        return error

    try:
        facade = _facade()
        df = facade.cashflow_statement(code, as_of)
    except Exception:  # noqa: BLE001
        _logger.exception("现金流量表查询失败 [%s]", code)
        return jsonify({"error": "现金流量表数据加载失败，请稍后重试"}), 500

    return jsonify({
        "code": code,
        "as_of": as_of,
        "items": df.to_dict(orient="records") if not df.empty else [],
    })


def handle_fundamental_indicators():
    """
    GET /api/fundamental/indicators?code=600519.SH[&as_of=2024-03-31] —— 财务指标

    Returns:
        Response: 200 JSON / 400 参数非法 / 500 通用错误
    """
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    as_of, error = _parse_as_of()
    if error:
        return error

    try:
        facade = _facade()
        df = facade.financial_indicators(code, as_of)
    except Exception:  # noqa: BLE001
        _logger.exception("财务指标查询失败 [%s]", code)
        return jsonify({"error": "财务指标数据加载失败，请稍后重试"}), 500

    return jsonify({
        "code": code,
        "as_of": as_of,
        "items": df.to_dict(orient="records") if not df.empty else [],
    })


def handle_fundamental_latest():
    """
    GET /api/fundamental/latest?code=600519.SH —— 最新可见报告期

    Returns:
        Response: 200 JSON / 400 参数非法 / 500 通用错误
    """
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "参数 [code] 非法：必填"}), 400

    try:
        facade = _facade()
        latest = facade.latest_as_of(code)
    except Exception:  # noqa: BLE001
        _logger.exception("最新报告期查询失败 [%s]", code)
        return jsonify({"error": "基本面数据加载失败，请稍后重试"}), 500

    return jsonify({
        "code": code,
        "latest": latest,
    })