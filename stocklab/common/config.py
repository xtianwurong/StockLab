#!/usr/bin/env python3
"""
==============================================================================
StockLab - 统一配置管理模块 (stocklab.common.config)
==============================================================================

【模块职责】
  统一加载与解析项目核心配置文件 (config.toml)，输出强类型/结构化配置对象：
    1. 运行配置 (settings): 历史月数、输出文件名、网络超时等
    2. 主板基准配置 (benchmark): 代码、简称、基准属性、呈现样式
    3. 核心板块 ETF 监控列表 (sectors): 纯正行业代表 ETF 标的池
    4. 取数优先级配置 (data_source): 本地库与远端接口的先后策略

【为什么从 config.ini 换成 config.toml】
  旧的 configparser 方案把每个 ETF 压成一条
  「code, name, sector, color, tracking_index, fund_manager, purity_reason」
  逗号串，解析端只能靠 parts[3] / parts[4] 这类下标取值，字段一多就极易错位，
  且 INI 无法表达嵌套结构。TOML 原生支持嵌套表与数组，`tomllib` 是标准库
  （3.11+，本项目 3.14），零依赖；配置结构即代码结构，字段按名取值。
"""

import logging
import os
import tomllib
from types import SimpleNamespace

__all__ = [
    "DATA_SOURCE_PRIORITIES",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_DATA_SOURCE_PRIORITY",
    "DEFAULT_FALLBACK_PALETTE",
    "find_config_path",
    "load_config",
    "load_data_source_priority",
]

_logger = logging.getLogger(__name__)

# 核心配置文件名（单文件入口，见项目根 config.toml）
DEFAULT_CONFIG_PATH = "config.toml"

# 取数优先级合法取值（Facade 层据此校验，避免拼写错误导致策略静默失效）
DATA_SOURCE_PRIORITIES = ("local_first", "remote_first")

# 配置缺失或非法时的兜底策略：本地优先，保证行为可预期
DEFAULT_DATA_SOURCE_PRIORITY = "local_first"

# 预设高对比度调色板（保障任何行业标的在未配颜色时均有鲜明色彩）
DEFAULT_FALLBACK_PALETTE = [
    "#795548", "#8c564b", "#a65628", "#b8860b", "#708090",
    "#20b2aa", "#e41a1c", "#00bfff", "#4169e1", "#ff1493",
    "#00ced1", "#2ca02c", "#17becf", "#ff7f00", "#636363",
    "#4682b4", "#984ea3", "#377eb8", "#d2691e", "#8fbc8f"
]


def find_config_path(config_path=DEFAULT_CONFIG_PATH):
    """
    自适应查找配置文件路径：优先当前工作目录，其次项目根目录

    找不到时返回绝对路径（便于报错信息可读），由调用方决定是否接受缺失。
    """
    if os.path.exists(config_path):
        return os.path.abspath(config_path)

    # 尝试基于当前文件回溯至根目录
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    candidate = os.path.join(base_dir, config_path)
    if os.path.exists(candidate):
        return candidate

    return os.path.abspath(config_path)


def _read_config_file(config_path):
    """
    读取并解析 TOML 配置

    文件不存在时返回空 dict（全部走调用方默认值）；文件存在但语法损坏时
    **不吞异常** —— 配置写错必须显式报错，否则会静默套用默认值，
    表现为「数据不对」而不是「配置坏了」，极难排查。

    Args:
        config_path (str): 配置文件路径

    Returns:
        dict: 解析后的配置字典；文件不存在时为空 dict

    Raises:
        tomllib.TOMLDecodeError: 配置存在但语法非法
    """
    full_path = find_config_path(config_path)
    if not os.path.exists(full_path):
        _logger.debug("配置文件不存在，全部使用默认值: %s", full_path)
        return {}
    with open(full_path, "rb") as handle:
        return tomllib.load(handle)


def _as_text(value, default=""):
    """把配置值安全转成去空白的字符串（容忍 TOML 里的数字/布尔等非字符串值）"""
    if value is None:
        return default
    return str(value).strip()


def load_config(config_path=DEFAULT_CONFIG_PATH):
    """
    从 config.toml 中加载运行参数、大盘基准与各行业 ETF 监控清单

    缺项逐个落默认值，不因缺一个键就整体失败；缺 code 的板块块跳过并告警。

    Args:
        config_path (str, optional): 配置文件路径，默认 "config.toml"

    Returns:
        tuple: (sectors, default_months, default_output, http_timeout)
            - sectors (list): 板块配置列表，sectors[0] 恒为 benchmark
    """
    raw = _read_config_file(config_path)

    # 1. 运行与分析参数
    settings = raw.get("settings", {})
    default_months = int(settings.get("default_months", 120))
    default_output = _as_text(settings.get("output_html", "output/sector_etf_trend.html"))
    http_timeout = int(settings.get("http_timeout", 8))

    # 2. 大盘基准
    bench_cfg = raw.get("benchmark", {})
    bench_code = _as_text(bench_cfg.get("code", "sh000001"))
    bench_color = _as_text(bench_cfg.get("color", "#ffffff")) or "#ffffff"
    benchmark = SimpleNamespace(
        code=bench_code,
        name=_as_text(bench_cfg.get("name", "上证指数")),
        sector=_as_text(bench_cfg.get("sector", "A股主板基准")),
        color=bench_color,
        linewidth=float(bench_cfg.get("linewidth", 3.2)),
        linestyle=_as_text(bench_cfg.get("linestyle", "-")),
        is_benchmark=True,
        ticker=bench_code[2:] if len(bench_code) > 2 else bench_code,
        tracking_index=_as_text(bench_cfg.get("tracking_index", "上证综合指数")),
        fund_manager=_as_text(bench_cfg.get("fund_manager", "上海证券交易所")),
        purity_reason=_as_text(
            bench_cfg.get("purity_reason", "A 股历史最悠久、全市场覆盖最广的主板核心大盘基准")
        ),
        reliability_reason=_as_text(bench_cfg.get("reliability_reason", "主板权威总基准")),
    )

    # 3. 核心板块行业 ETF 列表
    sectors = [benchmark]
    palette_idx = 0
    for item in raw.get("sectors", []):
        item_code = _as_text(item.get("code", ""))
        if not item_code:
            _logger.warning("跳过缺少 code 的板块配置: %r", item)
            continue
        name = _as_text(item.get("name", ""))
        item_color = _as_text(item.get("color", "")) or \
            DEFAULT_FALLBACK_PALETTE[palette_idx % len(DEFAULT_FALLBACK_PALETTE)]
        palette_idx += 1
        sectors.append(SimpleNamespace(
            code=item_code,
            name=name,
            sector=_as_text(item.get("sector", "")) or name,
            color=item_color,
            tracking_index=_as_text(item.get("tracking_index", "")),
            fund_manager=_as_text(item.get("fund_manager", "")),
            purity_reason=_as_text(item.get("purity_reason", "")),
            reliability_reason="全市场核心纯正行业代表 ETF",
            linewidth=1.8,
            linestyle="-",
            is_benchmark=False,
            ticker=item_code[2:] if len(item_code) > 2 else item_code,
        ))

    return sectors, default_months, default_output, http_timeout


def load_data_source_priority(config_path=DEFAULT_CONFIG_PATH):
    """
    读取取数优先级策略 (config.toml 的 [data_source] 段)

    【为什么单独成函数】
       load_config 的返回值签名已被 app/dashboard/sector_trend.py 等调用方
       按 4 元组解包，不可改动；本函数独立读取，避免破坏既有接口。

    Args:
        config_path (str, optional): 配置文件路径，默认 "config.toml"

    Returns:
        str: "local_first"（本地库优先，缺失回退远端）
             或 "remote_first"（远端优先，失败回退本地）
             配置文件缺失、段落缺失、解析失败或取值非法时返回 DEFAULT_DATA_SOURCE_PRIORITY
    """
    try:
        raw = _read_config_file(config_path)
    except Exception as error:
        # 配置损坏不该挡住程序启动：优先级只影响取数先后，降级为默认即可
        _logger.warning(
            "读取配置失败，取数优先级使用默认值 %s: %s",
            DEFAULT_DATA_SOURCE_PRIORITY, error,
        )
        return DEFAULT_DATA_SOURCE_PRIORITY

    value = _as_text(raw.get("data_source", {}).get("priority", DEFAULT_DATA_SOURCE_PRIORITY))
    if value not in DATA_SOURCE_PRIORITIES:
        _logger.warning(
            "取数优先级取值非法 [%s]，合法值为 %s，改用默认 %s",
            value,
            "/".join(DATA_SOURCE_PRIORITIES),
            DEFAULT_DATA_SOURCE_PRIORITY,
        )
        return DEFAULT_DATA_SOURCE_PRIORITY

    return value
