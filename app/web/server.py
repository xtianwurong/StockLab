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
  /industries                    行业估值页面（模板渲染）
  /screener                      选股器页面（模板渲染）
  /compare                       多股对比页面（模板渲染）
  /portfolio                     组合监控页面（自选股 + 分位阈值告警，模板渲染）
  /insight                       投资人观点页面（投资理念 / 宏观判断学习，模板渲染）
  /commodities                   大宗商品页面（价格 / 趋势 / 七档评级，模板渲染）
  /favicon.ico                   站点图标（消除每页 404 噪音）
  /static/*                      本地静态资源（含 vendored ECharts，离线可用）
  /api/health                    健康检查
  /api/securities?q=             证券联想
  /api/percentile?code=          个股历史估值分位（核心接口）
  /api/market/ranking?indicator= 全市场估值分位排行（Dashboard）
  /api/industries?level=       行业估值横截面（国证行业分类 1~4 级）
  /api/indices?indicator=        指数估值列表
  /api/index/detail?code=        单指数估值走势与明细
  /api/screener/meta?as_of=      选股器元数据（因子覆盖率 / 算子 / 预置模板）
  /api/screener/run              选股器执行（POST {spec, as_of, sort, order, limit}）
  /api/compare?codes=            多股对比（2~10 只 × 5 指标分位 + 对齐后的估值走势）
  /api/market/ranking?codes=     全市场排行（codes 参数支持精确指定自选股列表）
  /api/insight/meta              投资人观点元数据（筛选枚举 / 核验状态分布）
  /api/insight/quotes?...        投资人言论多维筛选（keyword / 投资人 / 平台 /
                                 类型 / 主题 / 核验状态 / 标的 / 时间）
  /api/insight/investor?code=    单个投资人档案（生平 + 账号 + 观点时间线）
  /api/commodities?years=        大宗商品目录（当前价 / 涨跌 / 分位 / 七档）
  /api/commodity/history?symbol= 单品种价格序列与分位统计（趋势图）
  /api/fundamental/income?code=  利润表（Point-in-Time）
  /api/fundamental/balance?code= 资产负债表（Point-in-Time）
  /api/fundamental/cashflow?code= 现金流量表（Point-in-Time）
  /api/fundamental/indicators?code= 财务指标（质量/成长，Point-in-Time）
  /api/fundamental/latest?code=  最新可见报告期
  /api/fund/list?type=           基金列表（按类型筛选）
  /api/fund/info?code=           基金基本信息
  /api/fund/nav?code=            基金净值历史
  /api/fund/holding?code=        基金持仓历史
  /api/fund/holding/latest       基金最新持仓
  /api/fund/exposure?code=       真实行业暴露（报告期）
  /api/fund/exposure/daily       行业暴露日线（插值）
  /api/fund/holding/vs_rbsa      RBSA vs 持仓双轨对比
  /api/fund/attribution?code=    Brinson 归因（配置/选择/交互）
  /api/sw/indices?level=         申万行业指数列表
  /api/sw/mapping                申万行业映射表
  /api/sector/indices?codes=     板块指数日线
  /api/capital/flow?type=        板块资金流
  /api/fund/allocation?code=     基金调仓分析（核心）
  /api/fund/allocation/history   历史调仓分析结果
  /api/stock/industry/mapping    股票行业映射表
  ==============================================================================
"""

import logging
import os

from flask import Flask

from app.web import api
from app.web import commodity_api
from app.web import compare_api
from app.web import fundamental_api
from app.web import fund_analysis_api
from app.web import fund_holding_api
from app.web import insight_api
from app.web import market_api
from app.web import screener_api

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
    app.add_url_rule(
        "/industries", "industries", api.handle_industries_page, methods=["GET"]
    )
    app.add_url_rule(
        "/screener", "screener", api.handle_screener_page, methods=["GET"]
    )
    app.add_url_rule(
        "/compare", "compare", api.handle_compare_page, methods=["GET"]
    )
    app.add_url_rule(
        "/portfolio", "portfolio", api.handle_portfolio_page, methods=["GET"]
    )
    app.add_url_rule(
        "/insight", "insight", insight_api.handle_insight_page, methods=["GET"]
    )
    app.add_url_rule(
        "/commodities", "commodities",
        commodity_api.handle_commodities_page, methods=["GET"],
    )
    app.add_url_rule(
        "/fund-analysis", "fund_analysis",
        fund_analysis_api.handle_fund_analysis_page, methods=["GET"],
    )
    app.add_url_rule(
        "/fund-holding", "fund_holding",
        fund_analysis_api.handle_fund_analysis_page, methods=["GET"],
    )
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
        "/api/industries",
        "industry_valuation",
        market_api.handle_industry_valuation,
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
    app.add_url_rule(
        "/api/screener/meta",
        "screener_meta",
        screener_api.handle_screener_meta,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/screener/run",
        "screener_run",
        screener_api.handle_screener_run,
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/compare",
        "compare_api",
        compare_api.handle_compare,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/insight/meta",
        "insight_meta",
        insight_api.handle_insight_meta,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/insight/quotes",
        "insight_quotes",
        insight_api.handle_insight_quotes,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/insight/investor",
        "insight_investor",
        insight_api.handle_insight_investor,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/commodities",
        "commodities_list",
        commodity_api.handle_commodities,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/commodity/history",
        "commodity_history",
        commodity_api.handle_commodity_history,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fundamental/income",
        "fundamental_income",
        fundamental_api.handle_fundamental_income,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fundamental/balance",
        "fundamental_balance",
        fundamental_api.handle_fundamental_balance,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fundamental/cashflow",
        "fundamental_cashflow",
        fundamental_api.handle_fundamental_cashflow,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fundamental/indicators",
        "fundamental_indicators",
        fundamental_api.handle_fundamental_indicators,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fundamental/latest",
        "fundamental_latest",
        fundamental_api.handle_fundamental_latest,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fund/list",
        "fund_list",
        fund_analysis_api.handle_fund_list,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fund/info",
        "fund_info",
        fund_analysis_api.handle_fund_info,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fund/nav",
        "fund_nav",
        fund_analysis_api.handle_fund_nav,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/sw/indices",
        "sw_indices",
        fund_analysis_api.handle_sw_indices,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/sw/mapping",
        "sw_mapping",
        fund_analysis_api.handle_industry_mapping,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/sector/indices",
        "sector_indices",
        fund_analysis_api.handle_sector_indices,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/capital/flow",
        "capital_flow",
        fund_analysis_api.handle_capital_flow,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fund/allocation",
        "fund_allocation",
        fund_analysis_api.handle_fund_allocation,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fund/allocation/history",
        "fund_allocation_history",
        fund_analysis_api.handle_allocation_history,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fund/holding",
        "fund_holding_list",
        fund_holding_api.handle_fund_holding_list,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fund/holding/latest",
        "fund_holding_latest",
        fund_holding_api.handle_latest_holding,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fund/exposure",
        "fund_exposure",
        fund_holding_api.handle_industry_exposure,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fund/exposure/daily",
        "fund_exposure_daily",
        fund_holding_api.handle_industry_exposure_daily,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/stock/industry/mapping",
        "stock_industry_mapping",
        fund_holding_api.handle_stock_industry_mapping,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/fund/holding/vs_rbsa",
        "fund_holding_vs_rbsa",
        fund_holding_api.handle_holding_vs_rbsa,
        methods=["GET"],
    )

    app.add_url_rule(
        "/api/fund/attribution",
        "fund_attribution",
        fund_holding_api.handle_fund_attribution,
        methods=["GET"],
    )

    # 错误兜底
    app.register_error_handler(404, api.handle_not_found)
    app.register_error_handler(500, api.handle_internal_error)

    rules = [r.rule for r in app.url_map.iter_rules()
             if not r.rule.startswith("/static")]
    api_count = len([r for r in rules if r.startswith("/api/")])
    icon_count = len([r for r in rules if r.endswith(".ico")])
    page_count = len(rules) - api_count - icon_count
    _logger.info(
        "Flask 应用已创建，路由 %d 条（页面 %d + 接口 %d + 图标 %d）",
        len(rules), page_count, api_count, icon_count,
    )
    return app
