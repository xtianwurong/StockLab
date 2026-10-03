#!/usr/bin/env python3
"""
  ==============================================================================
StockLab - Web 服务装配模块 (app.web.server)
==============================================================================

【模块职责】
  创建 Flask 应用实例并以「注册表」方式挂载全部路由。
  路由注册统一使用 app.add_url_rule(...) 普通函数调用，
  不使用 @app.route 装饰器（遵守 AGENT.md 编码规范：禁用装饰器）。

【路径规划】
  /                              个股分析页面（模板渲染）
  /market                        全市场分位 Dashboard（模板渲染）
  /indices                       指数估值页面（模板渲染）
  /favicon.ico                   站点图标（消除每页 404 噪音）
  /static/*                      本地静态资源（含 vendored ECharts，离线可用）
  /api/health                    健康检查
  /api/securities?q=             证券联想
  /api/percentile?code=          个股历史估值分位（核心接口）
  /api/market/ranking?indicator= 全市场估值分位排行（Dashboard）
  /api/indices?indicator=        指数估值列表
  /api/index/detail?code=        单指数估值走势与明细
  ==============================================================================
"""

import logging
import os

from flask import Flask

from app.web import api
from app.web import market_api

_logger = logging.getLogger("StockLab.Web.Server")

__all__ = [
    "create_app",
]


def create_app(priority=None, db_path=None):
    """
    创建并配置 Flask 应用

    Args:
        priority (str, optional): 取数优先级，None 时由 facade 读 config.ini
        db_path (str, optional): 本地 DuckDB 路径，None 时用库层默认值

    Returns:
        Flask: 已注册全部路由的应用实例
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(
        __name__,
        static_folder=os.path.join(base_dir, "static"),
        template_folder=os.path.join(base_dir, "templates"),
    )

    # 供 api 层按请求取用启动期配置（不引入全局可变量）
    app.config["STOCKLAB_PRIORITY"] = priority
    app.config["STOCKLAB_DB_PATH"] = db_path

    # 页面路由（普通函数调用，非装饰器）
    app.add_url_rule("/", "index", api.handle_index, methods=["GET"])
    app.add_url_rule("/market", "market", api.handle_market_page, methods=["GET"])
    app.add_url_rule("/indices", "indices", api.handle_indices_page, methods=["GET"])
    app.add_url_rule("/favicon.ico", "favicon", api.handle_favicon, methods=["GET"])

    # 数据接口
    app.add_url_rule("/api/health", "health", api.handle_health, methods=["GET"])
    app.add_url_rule("/api/securities", "securities", api.handle_securities, methods=["GET"])
    app.add_url_rule("/api/percentile", "percentile", api.handle_percentile, methods=["GET"])
    app.add_url_rule(
        "/api/market/ranking",
        "market_ranking",
        market_api.handle_market_ranking,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/indices", "index_list", market_api.handle_index_list, methods=["GET"]
    )
    app.add_url_rule(
        "/api/index/detail",
        "index_detail",
        market_api.handle_index_detail,
        methods=["GET"],
    )

    # 错误兜底
    app.register_error_handler(404, api.handle_not_found)
    app.register_error_handler(500, api.handle_internal_error)

    _logger.info("Flask 应用已创建，路由 10 条（页面 3 + 接口 6 + 图标 1）")
    return app
