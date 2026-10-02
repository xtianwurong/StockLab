#!/usr/bin/env python3
"""
==============================================================================
StockLab - 板块走势对比与 Web 仪表板编排主类 (stocklab.dashboard.sector_trend)
==============================================================================

【模块职责】
  板块走势分析外观编排类 (Facade)，端到端串联配置读取、长周期月线抓取与 Web 页面生成。
"""

import logging

from stocklab.common.config import load_ini_config
from stocklab.facade.market_data import MarketDataFacade
from stocklab.dashboard.page_generator import SectorWebPageGenerator

_logger = logging.getLogger("StockLab.Visualizer.SectorTrend")

__all__ = [
    "SectorTrendVisualizer",
]


class SectorTrendVisualizer:
    """
    板块走势对比与 Web 仪表板编排主类 (Facade)
    """

    def __init__(self, num_months=None, config_path="config.ini"):
        self.sectors, default_months, self.default_output, http_timeout = load_ini_config(config_path)
        self.num_months = num_months if num_months is not None else default_months
        self.facade = MarketDataFacade()
        self.generator = SectorWebPageGenerator()

    def generate(self, output_filename=None):
        """
        端到端执行：抓取数据 -> 通过模板渲染生成自包含交互式 HTML

        Args:
            output_filename (str, optional): 输出 HTML 路径，未指定时默认使用 config.ini 配置

        Returns:
            str: 成功生成的 HTML 文件路径；失败返回空字符串
        """
        target_output = output_filename if output_filename else self.default_output
        _logger.info(
            "开始生成 A 股核心板块 ETF 与主板 10 年走势网页（抓取月数: %s 个月）",
            self.num_months,
        )
        raw_data = self.facade.fetch_multi_monthly_close(
            self.sectors, num_months=self.num_months
        )
        if not raw_data:
            _logger.error("数据获取失败，无法生成网页")
            return ""

        filename = self.generator.generate_html(
            sectors=self.sectors,
            raw_data=raw_data,
            output_path=target_output,
        )
        self.facade.close()
        return filename
