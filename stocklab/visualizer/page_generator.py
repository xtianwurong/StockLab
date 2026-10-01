#!/usr/bin/env python3
"""
==============================================================================
StockLab - 交互式网页生成器模块 (stocklab.visualizer.page_generator)
==============================================================================

【模块职责】
  负责多标的月度时序数据清洗对齐，并基于外部 HTML 模板填充渲染独立的自包含 Web 交互网页。
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List

_logger = logging.getLogger("StockLab.Visualizer.PageGenerator")


def find_template_path(template_name: str = "dashboard.html") -> str:
    """
    智能查找模板路径：
    1. 用户指定的绝对路径或直接相对路径
    2. 项目 templates/ 目录
    3. 项目根目录
    """
    if os.path.exists(template_name):
        return os.path.abspath(template_name)

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    # 候选 1: templates/ 目录下
    c1 = os.path.join(base_dir, "templates", template_name)
    if os.path.exists(c1):
        return c1

    # 候选 2: 根目录下
    c2 = os.path.join(base_dir, template_name)
    if os.path.exists(c2):
        return c2

    # 候选 3: templates/dashboard.html 兜底
    c3 = os.path.join(base_dir, "templates", "dashboard.html")
    if os.path.exists(c3):
        return c3

    return os.path.abspath(template_name)


class SectorWebPageGenerator:
    """
    负责组装数据并通过 dashboard.html 模板渲染生成纯图表交互式 HTML 页面
    """

    def __init__(self, template_name: str = "dashboard.html"):
        self.template_path = find_template_path(template_name)

    def generate_html(
        self, sectors: List[Any], raw_data: Dict[str, Dict[str, float]], output_path: str = "output/sector_etf_trend.html"
    ) -> str:
        """
        根据抓取的原始月线字典，提取全集并集月份、对齐缺失数据、填充模板并保存 HTML 文件
        """
        all_months_set = set()
        for series in raw_data.values():
            all_months_set.update(series.keys())
        months = sorted(list(all_months_set))

        if not months:
            _logger.error("没有任何月份数据，生成中止")
            return ""

        series_payload = []
        for item in sectors:
            s_map = raw_data.get(item.code, {})
            data_points = [s_map.get(m, None) for m in months]
            series_payload.append({
                "code": item.code,
                "ticker": getattr(item, "ticker", item.code[2:]),
                "name": item.name,
                "sector": item.sector,
                "color": getattr(item, "color", ""),
                "is_benchmark": item.is_benchmark,
                "linewidth": item.linewidth,
                "linestyle": item.linestyle,
                "tracking_index": getattr(item, "tracking_index", ""),
                "fund_manager": getattr(item, "fund_manager", ""),
                "purity_reason": getattr(item, "purity_reason", ""),
                "prices": data_points,
            })

        json_data = json.dumps({
            "months": months,
            "series": series_payload,
            "generated_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False)

        if not os.path.exists(self.template_path):
            _logger.error("模板文件未找到: %s", self.template_path)
            return ""

        with open(self.template_path, "r", encoding="utf-8") as f:
            template_content = f.read()

        html_content = template_content.replace("__DATA_PAYLOAD__", json_data)

        output_dir = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(output_dir, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        _logger.info("交互式 HTML 页面已成功保存至: %s", output_path)
        return output_path
