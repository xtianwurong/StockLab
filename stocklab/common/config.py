#!/usr/bin/env python3
"""
==============================================================================
StockLab - 统一配置管理模块 (stocklab.common.config)
==============================================================================

【模块职责】
  统一加载与解析项目核心配置文件 (config.ini)，输出强类型/结构化配置对象：
    1. 运行配置 (settings): 历史月数、输出文件名、网络超时等
    2. 主板基准配置 (benchmark): 代码、简称、基准属性、呈现样式
    3. 核心板块 ETF 监控列表 (sectors): 纯正行业代表 ETF 标的池
"""

import configparser
import os
from types import SimpleNamespace
from typing import List, Tuple

# 预设高对比度调色板（保障任何行业标的在未配颜色时均有鲜明色彩）
DEFAULT_FALLBACK_PALETTE = [
    "#795548", "#8c564b", "#a65628", "#b8860b", "#708090",
    "#20b2aa", "#e41a1c", "#00bfff", "#4169e1", "#ff1493",
    "#00ced1", "#2ca02c", "#17becf", "#ff7f00", "#636363",
    "#4682b4", "#984ea3", "#377eb8", "#d2691e", "#8fbc8f"
]


def find_config_path(config_path: str = "config.ini") -> str:
    """
    自适应查找 config.ini 路径：优先当前工作目录，其次项目根目录
    """
    if os.path.exists(config_path):
        return os.path.abspath(config_path)

    # 尝试基于当前文件回溯至根目录
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    candidate = os.path.join(base_dir, config_path)
    if os.path.exists(candidate):
        return candidate

    return os.path.abspath(config_path)


def load_ini_config(config_path: str = "config.ini") -> Tuple[List[SimpleNamespace], int, str, int]:
    """
    从 config.ini 中加载运行参数、大盘基准与各行业 ETF 监控清单

    支持 # 与 ; 注释整行，支持 ; 作为行内注释，保留十六进制颜色值如 #ffffff
    返回: (sectors_list, default_months, default_output, http_timeout)
    """
    full_path = find_config_path(config_path)

    config = configparser.ConfigParser(
        interpolation=None,
        comment_prefixes=("#", ";"),
        inline_comment_prefixes=(";",)
    )
    config.read(full_path, encoding="utf-8")

    # 1. 运行与分析参数
    settings = config["settings"] if "settings" in config else {}
    default_months = int(settings.get("default_months", 120))
    default_output = settings.get("output_html", "output/sector_etf_trend.html")
    http_timeout = int(settings.get("http_timeout", 8))

    # 2. 大盘基准
    bench_cfg = config["benchmark"] if "benchmark" in config else {}
    bench_code = bench_cfg.get("code", "sh000001").strip()
    bench_color = bench_cfg.get("color", "#ffffff").strip() or "#ffffff"
    benchmark = SimpleNamespace(
        code=bench_code,
        name=bench_cfg.get("name", "上证指数").strip(),
        sector=bench_cfg.get("sector", "A股主板基准").strip(),
        color=bench_color,
        linewidth=float(bench_cfg.get("linewidth", 3.2)),
        linestyle=bench_cfg.get("linestyle", "-").strip(),
        is_benchmark=True,
        ticker=bench_code[2:] if len(bench_code) > 2 else bench_code,
        tracking_index=bench_cfg.get("tracking_index", "上证综合指数").strip(),
        fund_manager=bench_cfg.get("fund_manager", "上海证券交易所").strip(),
        purity_reason=bench_cfg.get(
            "purity_reason", "A 股历史最悠久、全市场覆盖最广的主板核心大盘基准"
        ).strip(),
        reliability_reason=bench_cfg.get("reliability_reason", "主板权威总基准").strip(),
    )

    # 3. 核心板块行业 ETF 列表
    sectors = [benchmark]
    if "sectors" in config:
        idx = 0
        for key, val in config["sectors"].items():
            if not val:
                continue
            parts = [p.strip() for p in val.split(",")]
            if len(parts) >= 2:
                item_code = parts[0]
                item_color = (
                    parts[3]
                    if len(parts) > 3 and parts[3]
                    else DEFAULT_FALLBACK_PALETTE[idx % len(DEFAULT_FALLBACK_PALETTE)]
                )
                idx += 1
                item = SimpleNamespace(
                    code=item_code,
                    name=parts[1],
                    sector=parts[2] if len(parts) > 2 else parts[1],
                    color=item_color,
                    tracking_index=parts[4] if len(parts) > 4 else "",
                    fund_manager=parts[5] if len(parts) > 5 else "",
                    purity_reason=parts[6] if len(parts) > 6 else "",
                    reliability_reason="全市场核心纯正行业代表 ETF",
                    linewidth=1.8,
                    linestyle="-",
                    is_benchmark=False,
                    ticker=item_code[2:] if len(item_code) > 2 else item_code,
                )
                sectors.append(item)

    return sectors, default_months, default_output, http_timeout
