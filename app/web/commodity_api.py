#!/usr/bin/env python3
"""
==============================================================================
StockLab - 大宗商品接口处理模块 (app.web.commodity_api)
==============================================================================

【模块职责】
  大宗商品页面的三个接口：
    - handle_commodities_page()  页面渲染
    - handle_commodities()       十个品种的当前价 / 涨跌 / 分位 / 七档评级
    - handle_commodity_history() 单品种完整价格序列（趋势图）

【一个不可让渡的页面契约：数据陈旧就不给评级】
  拿 2022 年的价格算「当前在近 5 年的分位」，会输出一个看起来完全正常、
  实际基于四年前数据的评级 —— 数字不会报错，只会误导，这是本页最危险的
  失败模式（动力煤郑商所自 2022 年起交易受限，正是因此被移出首版品种）。
  门面把陈旧品种的 percentile 置为 None，这里**不得**替它编一个默认值，
  前端也要显示「数据陈旧」而不是显示评级。这条约束有专项测试钉住。

【分位在 stocklab 算，七档在这里算】
  分位（0~100）是历史序列的统计，属 stocklab 的分析能力；
  七档「极度低估 ~ 极度高估」是 Web 展示层口径，由 store.percentile_level
  统一映射，与前端 common.js 的 LEVEL7 双向校验。
  门面只交 percentile，本模块负责把它翻成 level7 —— 边界清楚后，
  同一个分位可被页面、报告、CLI 复用，而文案只有一处。

【取数路径】
  经 stocklab.facade.CommodityDataFacade，连接向 store.facade_database()
  借出门面那一个 —— 不能自己 duckdb.connect()，同文件第二个写连接会抛
  "Could not set lock"。
  **本模块不 import stocklab.persistence**：取数编排属 stocklab 内部，
  接口层只做参数校验、展示文案与 JSON 组装（依赖方向见 AGENT.md）。

【应答约定】
  与 app.web.api 一致：200 + 业务 JSON / 400 参数非法 / 500 通用文案。
"""

import logging

from flask import jsonify, render_template, request

from app.web import store
from app.web.webcommon import parse_int_arg
from stocklab.facade import CommodityDataFacade

_logger = logging.getLogger("StockLab.Web.CommodityApi")

__all__ = [
    "handle_commodities_page",
    "handle_commodities",
    "handle_commodity_history",
]

# 分位回看窗口允许的范围（年）
_MIN_WINDOW_YEARS = 1
_MAX_WINDOW_YEARS = 10


def handle_commodities_page():
    """GET /commodities —— 页面"""
    return render_template("commodities.html")


def handle_commodities():
    """
    GET /api/commodities —— 全部品种的当前价、涨跌与分位

    Returns:
        Response: 200 JSON；无数据时 200 + 空 items + message（不是 500）
    """
    window_years, error = parse_int_arg(
        "years", request.args.get("years"), 5,
        minimum=_MIN_WINDOW_YEARS, maximum=_MAX_WINDOW_YEARS,
    )
    if error:
        return jsonify({"error": error}), 400

    try:
        facade = _facade()
        payload = facade.catalog(window_years=window_years)
    except Exception:  # noqa: BLE001 —— 接口层兜底，具体原因进日志
        _logger.exception("大宗商品目录查询失败")
        return jsonify({"error": "大宗商品数据加载失败，请稍后重试"}), 500

    items = [_decorate(item) for item in payload["items"]]
    has_data = any(item["close"] is not None for item in items)

    return jsonify({
        "as_of": payload["as_of"],
        "window_years": payload["window_years"],
        "stale_days": payload["stale_days"],
        "items": items,
        # 空表是「还没同步过」这种正常状态，不是错误 —— 给一句可执行的提示，
        # 而不是让页面显示一个什么都不说的空表格
        "message": "" if has_data else (
            "本地还没有大宗商品数据，请先运行 "
            "python app/scripts/sync_commodities.py sync"
        ),
    })


def handle_commodity_history():
    """
    GET /api/commodity/history?symbol=crude_oil[&years=5] —— 单品种趋势

    Returns:
        Response: 200 JSON / 400 参数非法
    """
    symbol = (request.args.get("symbol") or "").strip()
    if not symbol:
        return jsonify({"error": "参数 [symbol] 非法：必填"}), 400

    years, error = parse_int_arg(
        "years", request.args.get("years"), 5,
        minimum=_MIN_WINDOW_YEARS, maximum=_MAX_WINDOW_YEARS,
    )
    if error:
        return jsonify({"error": error}), 400

    try:
        facade = _facade()
        payload = facade.history(symbol, window_years=years)
    except ValueError:
        # 未登记的品种代号是**客户端错误**，不能 500：前端点错或手改 URL
        # 都会走到这里，500 会被监控当成服务端故障
        return jsonify({"error": "未登记的大宗商品: %s" % symbol}), 400
    except Exception:  # noqa: BLE001
        _logger.exception("大宗商品趋势查询失败 [%s]", symbol)
        return jsonify({"error": "大宗商品数据加载失败，请稍后重试"}), 500

    item = _decorate(payload)
    item["series"] = payload.get("series") or []
    if not item["series"]:
        item["message"] = "该品种本地暂无价格序列，请先运行同步"
    return jsonify(item)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _facade():
    """构造门面（连接向 store 借，避免同文件第二个写连接）"""
    return CommodityDataFacade(store.facade_database())


def _decorate(item):
    """
    补上 Web 展示层口径：七档评级

    【为什么是 None → "-" 而不是给 50%】
      分位缺失只有三种可能：本地无数据、样本不足 60 条、数据陈旧。
      三种都不能当作「中性」呈现 —— 「中性」是一个结论，而这里恰恰是
      「得不出结论」。给一个中间值等于把「不知道」伪装成「正常」。
    """
    percentile = item.get("percentile")
    return dict(item, level7=store.percentile_level(percentile))
