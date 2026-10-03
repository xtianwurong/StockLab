#!/usr/bin/env python3
"""
==============================================================================
StockLab - 选股器接口处理模块 (app.web.screener_api)
==============================================================================

【模块职责】
  选股器页面的两个数据接口：
    - handle_screener_meta()  因子元数据 + 算子表 + 预置模板 + 可选 as-of 时点
    - handle_screener_run()   按 spec 跑筛选，返回通过明细 + 规则漏斗 + 行业分布 + 散点

【设计要点】
  1. **只做编排，不重写引擎**：筛选逻辑全部委托 stocklab.screener.ScreenPipeline，
     因子计算委托 stocklab.factor，取数委托 stocklab.research.frame.build_factor_frame。
     本模块只负责「解析参数 -> 调库 -> 组装 JSON」。
  2. **暴露数据覆盖率**：因子库里大部分因子已登记但本地无数据源（基本面只同步了
     一只股票、日 K 尚未灌入），若不把覆盖率下发给前端，用户会对着全 NaN 的因子
     建规则却只得到 0 只通过。因此 meta 接口逐因子给出 coverage 与 availability，
     前端在规则编辑器里直接标色提示——这与项目「无数据就说无数据，绝不伪造」
     的口径一致。
  3. **因子帧按 as-of 缓存**：全市场 5572 只 × 27 因子拼装约 0.1s，
     但每次筛选都重建会让交互发涩，故按 as_of 缓存整帧（进程内）。
  4. **数据库连接复用**：因子帧走 Repository，必须用门面持有的 Database
     （store.facade_database()）；DuckDB 同一文件只允许一个写连接，
     另建连接会抛 "Could not set lock"。

【应答约定】
  与 app.web.api 一致：200 + 业务 JSON / 400 参数或 spec 非法 / 500 通用文案。
==============================================================================
"""

import logging
import threading
import time

import pandas as pd
from flask import jsonify, request

from app.web import store
from stocklab.factor import registry as factor_registry
from stocklab.research.frame import build_factor_frame
from stocklab.screener import ScreenError, ScreenPipeline

_logger = logging.getLogger("StockLab.Web.ScreenerApi")

__all__ = [
    "handle_screener_meta",
    "handle_screener_run",
]

# 因子分类的展示顺序（与 factor 包的分类包一致）
_CATEGORY_ORDER = ("value", "quality", "growth", "momentum", "dividend", "risk")

# 分类中文名
_CATEGORY_LABELS = {
    "value": "估值",
    "quality": "质量",
    "growth": "成长",
    "momentum": "动量",
    "dividend": "分红",
    "risk": "风险",
}

# 覆盖率分档：>= 全可用；>0 稀疏；=0 无数据
_COVERAGE_READY = 50.0
_COVERAGE_ANY = 0.0

# 结果表最多返回多少行（前端表格不分页，超出即截断并回传 total）
_MAX_ROWS = 500
_DEFAULT_ROWS = 200

# 散点图最多取多少个点（超过则按等间隔抽样，避免前端卡顿）
_MAX_SCATTER_POINTS = 400

# 行业分布最多返回多少条
_MAX_INDUSTRY_ROWS = 20

# 因子帧缓存：{as_of: DataFrame}
_FRAME_CACHE = {}
_FRAME_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 预置模板
# ---------------------------------------------------------------------------
# 【重要】模板分为「当前数据可用」与「需补数据源」两类，页面对后者会显式提示，
# 避免用户一点就得到 0 只通过而误以为规则写错了。
_PRESETS = (
    {
        "key": "value_low",
        "name": "价值低估",
        "note": "PE-TTM 与 PB 双低，估值两个口径都便宜",
        "spec": {
            "rules": [
                {"factor": "pe_ttm", "operator": "lt", "value": 15},
                {"factor": "pb", "operator": "lt", "value": 1.5},
            ]
        },
    },
    {
        "key": "value_deep",
        "name": "深度价值",
        "note": "更严的双低阈值，适合极端估值区",
        "spec": {
            "rules": [
                {"factor": "pe_ttm", "operator": "lt", "value": 10},
                {"factor": "pb", "operator": "lt", "value": 1},
            ]
        },
    },
    {
        "key": "value_pb_only",
        "name": "低市净率",
        "note": "只用 PB 单条件，适合重资产行业",
        "spec": {"rules": [{"factor": "pb", "operator": "lt", "value": 1}]},
    },
    {
        "key": "quality_growth",
        "name": "质量成长",
        "note": "需要基本面数据（当前本地尚未同步全市场）",
        "spec": {
            "rules": [
                {"factor": "roe", "operator": "gt", "value": 0.15},
                {"factor": "gross_margin", "operator": "gt", "value": 0.3},
            ]
        },
    },
    {
        "key": "dividend_stable",
        "name": "稳定分红",
        "note": "需要分红与波动率数据（当前本地尚未同步）",
        "spec": {
            "rules": [
                {"factor": "dividend_yield", "operator": "gt", "value": 0.03},
                {"factor": "dividend_stability", "operator": "gt", "value": 0.6},
            ]
        },
    },
    {
        "key": "momentum_trend",
        "name": "趋势动量",
        "note": "需要日 K 行情（阶段二尚未灌入）",
        "spec": {
            "rules": [
                {"factor": "momentum_12m", "operator": "gt", "value": 0.2},
                {"factor": "volatility_12m", "operator": "lt", "value": 0.35},
            ]
        },
    },
)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _num(value, digits=4):
    """
    转成 JSON 安全的数值：NaN / inf -> None

    Args:
        value: 原始数值
        digits (int): 保留小数位

    Returns:
        float | int | None: 可序列化数值
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, digits)


def _text(value):
    """
    转成 JSON 安全的字符串：None / NaN -> 空串

    Args:
        value: 单元格原始值

    Returns:
        str: 非空字符串
    """
    if value is None:
        return ""
    if isinstance(value, float) and value != value:
        return ""
    return str(value)


def _latest_as_of():
    """
    取本地最新的估值快照日期作为默认 as-of

    Returns:
        str: "YYYY-MM-DD"；本地无估值数据返回空串
    """
    dates = store.valuation_dates(limit=1)
    return dates[0] if dates else ""


def _factor_frame(as_of):
    """
    取（并缓存）指定 as-of 的因子输入帧

    Args:
        as_of (str): YYYY-MM-DD

    Returns:
        tuple: (DataFrame, 错误文案)；出错时帧为 None
    """
    with _FRAME_LOCK:
        cached = _FRAME_CACHE.get(as_of)
    if cached is not None:
        return cached, None

    try:
        frame = build_factor_frame(
            as_of, None, None, None, store.facade_database()
        )
    except Exception as exc:
        message = "构造 %s 的因子输入帧失败：%s" % (as_of, str(exc)[:200])
        _logger.error(message)
        return None, message

    if frame is None or frame.empty:
        message = "本地没有 %s 的可用数据，请先同步估值与基础数据" % as_of
        return None, message

    with _FRAME_LOCK:
        _FRAME_CACHE[as_of] = frame
    _logger.info("因子输入帧已缓存: as_of=%s 行数=%d", as_of, len(frame))
    return frame, None


def _coverage(frame, factor_name):
    """
    统计某因子在该帧上的非空覆盖率

    Args:
        frame (pd.DataFrame): 因子输入帧
        factor_name (str): 因子名

    Returns:
        float: 0~100；列不存在返回 -1.0（表示该因子当前没有输入列）
    """
    if factor_name not in frame.columns:
        return -1.0
    series = frame[factor_name]
    if len(series) == 0:
        return -1.0
    return round(float(series.notna().mean()) * 100, 1)


def _availability(coverage):
    """
    把覆盖率翻译成可用性分档

    Args:
        coverage (float): 覆盖率（%）

    Returns:
        str: ready（全市场可用）/ sparse（部分可用）/ none（完全无数据）
    """
    if coverage >= _COVERAGE_READY:
        return "ready"
    if coverage > _COVERAGE_ANY:
        return "sparse"
    return "none"


def _rule_operands(spec):
    """
    摊平 spec 里的全部叶子规则（含嵌套组内）

    Args:
        spec (dict): ScreenPipeline 的 spec

    Returns:
        list[dict]: 每个元素含 factor / operator / value
    """
    operands = []

    def walk(node):
        if not isinstance(node, dict):
            return
        if node.get("rules") is not None and "factor" not in node:
            for child in node.get("rules") or []:
                walk(child)
            return
        if node.get("factor"):
            operands.append(node)

    walk(spec)
    return operands


def _funnel(summary, spec):
    """
    统计规则漏斗：每条规则的通过数与淘汰数

    【口径说明】
      逐条规则独立判定（与引擎一致），因此各条通过数之和通常大于总通过数；
      漏斗表达的是「每条规则各自筛掉了多少」，不是逐级串联的漏斗。

    Args:
        summary (pd.DataFrame): 引擎输出的 summary 帧
        spec (dict): 筛选 spec（用于拿规则顺序与阈值文案）

    Returns:
        list[dict]: 每项含 factor / operator / threshold / passed / failed
    """
    rows = []
    for operand in _rule_operands(spec):
        factor_name = operand.get("factor")
        operator = operand.get("operator")
        threshold = operand.get("value")
        if factor_name not in summary.columns:
            rows.append({
                "factor": factor_name,
                "operator": operator,
                "threshold": _text(threshold),
                "passed": 0,
                "failed": len(summary),
                "in_frame": False,
            })
            continue

        series = summary[factor_name]
        passed_mask = _judge(series, operator, threshold)
        rows.append({
            "factor": factor_name,
            "operator": operator,
            "threshold": _text(threshold),
            "passed": int(passed_mask.sum()),
            "failed": int(len(passed_mask) - passed_mask.sum()),
            "in_frame": True,
        })
    return rows


def _judge(series, operator, threshold):
    """
    按 spec 的算子独立判定一列（与 stocklab.screener 的算子表保持同一套语义）

    【为何不复用引擎】
      引擎输出的是组合后的总判定，漏斗需要的是「单条规则各自通过多少」。
      这里只做展示用的近似判定，口径与 rules.OPERATORS 对齐：
      缺失值一律判不通过（isna 除外）。

    Args:
        series (pd.Series): 因子取值列
        operator (str): 算子键
        threshold: 阈值

    Returns:
        pd.Series[bool]: 逐行判定结果
    """
    if operator == "isna":
        return series.isna()
    if operator == "notna":
        return series.notna()

    usable = series.notna()
    if operator == "in":
        options = threshold if isinstance(threshold, (list, tuple)) else [threshold]
        judged = series.isin(list(options))
    else:
        numeric = pd.to_numeric(series, errors="coerce")
        try:
            bound = float(threshold)
        except (TypeError, ValueError):
            return pd.Series([False] * len(series), index=series.index)
        if operator == "lt":
            judged = numeric < bound
        elif operator == "le":
            judged = numeric <= bound
        elif operator == "gt":
            judged = numeric > bound
        elif operator == "ge":
            judged = numeric >= bound
        elif operator == "eq":
            judged = numeric == bound
        elif operator == "ne":
            judged = numeric != bound
        else:
            return pd.Series([False] * len(series), index=series.index)
    return judged & usable


def _industry_rows(summary):
    """
    按行业统计通过率（用于「哪些行业最符合这条规则」）

    Args:
        summary (pd.DataFrame): 引擎输出的 summary 帧

    Returns:
        list[dict]: 按通过率降序的 {industry, total, passed, rate}
    """
    if "industry" not in summary.columns or summary.empty:
        return []

    frame = summary[["industry", "passed"]].copy()
    frame["industry"] = frame["industry"].fillna("").astype(str).str.strip()
    frame.loc[frame["industry"] == "", "industry"] = "未分类"
    grouped = frame.groupby("industry")["passed"].agg(["size", "sum"])
    rows = []
    for industry, row in grouped.iterrows():
        total = int(row["size"])
        passed = int(row["sum"])
        if total < 5:  # 样本太少的行业不参与排名，避免 1/1 = 100% 的噪声
            continue
        rows.append({
            "industry": industry,
            "total": total,
            "passed": passed,
            "rate": round(passed * 100.0 / total, 1),
        })
    rows.sort(key=lambda item: (-item["rate"], -item["passed"]))
    return rows[:_MAX_INDUSTRY_ROWS]


def _scatter_points(summary, spec):
    """
    取散点图数据：横轴/纵轴取 spec 里出现频次最高且有数据的两个数值因子

    【为何不给全部因子做散点】
      因子最多 27 个，配对组合会让图不可读；规则里实际用到的因子才是用户关心的。

    Args:
        summary (pd.DataFrame): 引擎输出的 summary 帧
        spec (dict): 筛选 spec

    Returns:
        dict: {x, y, points}；可用数值因子不足 2 个时 points 为空
    """
    counter = {}
    for operand in _rule_operands(spec):
        name = operand.get("factor")
        if name:
            counter[name] = counter.get(name, 0) + 1

    numeric = []
    for name in sorted(counter, key=lambda key: (-counter[key], key)):
        if name not in summary.columns:
            continue
        series = pd.to_numeric(summary[name], errors="coerce")
        if float(series.notna().mean()) * 100 < _COVERAGE_READY:
            continue
        numeric.append(name)
        if len(numeric) == 2:
            break

    if len(numeric) < 2:
        return {"x": None, "y": None, "points": []}

    x_name, y_name = numeric
    x_series = pd.to_numeric(summary[x_name], errors="coerce")
    y_series = pd.to_numeric(summary[y_name], errors="coerce")
    usable = x_series.notna() & y_series.notna()

    index = list(summary.index[usable])
    if len(index) > _MAX_SCATTER_POINTS:
        step = len(index) / float(_MAX_SCATTER_POINTS)
        index = [index[int(position * step)] for position in range(_MAX_SCATTER_POINTS)]

    points = []
    for row_index in index:
        row = summary.loc[row_index]
        points.append([
            _num(x_series.loc[row_index]),
            _num(y_series.loc[row_index]),
            bool(row.get("passed")),
            _text(row.get("ts_code")),
            _text(row.get("name")),
        ])

    return {"x": x_name, "y": y_name, "points": points}


def _with_positive_only(spec, positive_only):
    """
    在 spec 前面补一条「PE-TTM > 0」，把亏损股整体剔除

    【为何默认剔除亏损股】
      「PE < 15」这类条件会把 PE 为负的亏损股全部放进来（负数当然小于 15），
      实测 5572 只里能筛出 600 多只，其中大半是亏损股——这不是「便宜」，
      而是「不适用」。本项目的估值分位分析早已统一按「亏损期 PE/PB ≤ 0 剔除」，
      选股器沿用同一口径，避免两个页面给出互相矛盾的结论。

    【实现方式】
      不在结果上做二次过滤（那会让「引擎判定」与「页面展示」口径分裂），
      而是直接把这条规则写进 spec 交给引擎，漏斗与条件文本都会如实体现。

    Args:
        spec (dict): 原始 spec
        positive_only (bool): 是否剔除亏损股

    Returns:
        dict: 实际执行的 spec
    """
    if not positive_only:
        return spec
    rules = spec.get("rules") or []
    for rule in rules:
        if isinstance(rule, dict) and rule.get("factor") == "pe_ttm" \
                and rule.get("operator") in ("gt", "ge") \
                and _num(rule.get("value"), 4) == 0:
            # 用户已经自己写了「PE > 0」，不再重复注入
            return spec
    merged = {"factor": "pe_ttm", "operator": "gt", "value": 0}
    result = dict(spec)
    result["rules"] = [merged] + list(rules)
    return result


def _parse_spec(payload):
    """
    从请求体里取出并校验 spec

    Args:
        payload (dict): 请求 JSON

    Returns:
        tuple: (spec 或 None, 错误文案或 None)
    """
    spec = payload.get("spec")
    if not isinstance(spec, dict):
        return None, "参数 [spec] 必须是一个对象"
    rules = spec.get("rules")
    if not isinstance(rules, list) or not rules:
        return None, "参数 [spec.rules] 必须是非空数组"
    if len(rules) > 12:
        return None, "规则条数上限为 12 条（当前 %d 条），请精简后再试" % len(rules)
    return spec, None


# ---------------------------------------------------------------------------
# 路由处理器
# ---------------------------------------------------------------------------

def handle_screener_meta():
    """
    选股器元数据：因子清单（含覆盖率）+ 算子表 + 预置模板 + 可选 as-of 时点

    Query:
        as_of (str): 可选；指定后按该时点统计覆盖率，缺省用本地最新估值日期

    Returns:
        Response: 200 时含 as_of / as_of_options / factors / categories /
                  operators / presets / universe_size / message
    """
    as_of = request.args.get("as_of", "").strip() or _latest_as_of()
    dates = store.valuation_dates(limit=60)
    if not as_of:
        return jsonify({
            "as_of": "",
            "as_of_options": [],
            "factors": [],
            "categories": [],
            "operators": [],
            "presets": [],
            "universe_size": 0,
            "message": "本地没有估值快照数据，请先运行 sync_market_data.py valuations",
        })

    frame, error = _factor_frame(as_of)
    if frame is None:
        return jsonify({
            "as_of": as_of,
            "as_of_options": dates,
            "factors": [],
            "categories": [],
            "operators": [],
            "presets": [],
            "universe_size": 0,
            "message": error,
        })

    factors = []
    for name in factor_registry.list_factors():
        item = factor_registry.get(name)
        coverage = _coverage(frame, name)
        factors.append({
            "name": name,
            "categories": list(item.categories),
            "description": item.description or "",
            "coverage": coverage,
            "availability": _availability(coverage) if coverage >= 0 else "none",
            "in_frame": coverage >= 0,
        })

    factors.sort(key=lambda entry: (-entry["coverage"], entry["name"]))

    # 预置模板：按「模板引用的因子当前是否可用」标注就绪度
    presets = []
    for preset in _PRESETS:
        readiness = "ready"
        for operand in _rule_operands(preset["spec"]):
            factor_name = operand.get("factor")
            if _availability(_coverage(frame, factor_name)) != "ready":
                readiness = "needs_data"
                break
        presets.append({
            "key": preset["key"],
            "name": preset["name"],
            "note": preset["note"],
            "spec": preset["spec"],
            "readiness": readiness,
        })

    # OPERATORS 的值是 (判定函数, 中文标签) 二元组，这里只取标签下发
    from stocklab.screener import OPERATORS
    operators = []
    for key, entry in OPERATORS.items():
        operators.append({
            "key": key,
            "label": entry[1],
            "needs_value": key not in ("isna", "notna"),
        })

    return jsonify({
        "as_of": as_of,
        "as_of_options": dates,
        "factors": factors,
        "categories": [
            {"key": key, "label": _CATEGORY_LABELS.get(key, key)}
            for key in _CATEGORY_ORDER
        ],
        "operators": operators,
        "presets": presets,
        "universe_size": len(frame),
        "message": "",
    })


def handle_screener_run():
    """
    按 spec 跑筛选并返回结果

    Body:
        spec (dict): ScreenPipeline spec，必须含非空 rules
        as_of (str): 研究时点，缺省用本地最新估值日期
        sort (str): 排序字段（ts_code/name/pe_ttm/pb 或任一在帧内的因子），缺省 ts_code
        order (str): asc / desc，缺省 asc
        limit (int): 最多返回行数，缺省 200，上限 500
        positive_only (bool): 是否剔除亏损股（PE-TTM ≤ 0），缺省 true；
                              置 false 可把亏损股一并放进结果

    Returns:
        Response: 200 时含 as_of / condition / counts / total / rows /
                  funnel / industries / scatter / spec / elapsed_ms / message；
                  参数非法 400；引擎拒绝 400；服务端异常 500
    """
    started = time.time()
    payload = request.get_json(silent=True) or {}

    spec, error = _parse_spec(payload)
    if error:
        return jsonify({"error": error}), 400

    # 剔除亏损股：默认开启（与估值分位口径一致）
    positive_only = payload.get("positive_only")
    if positive_only is None:
        positive_only = True
    spec = _with_positive_only(spec, bool(positive_only))

    as_of = str(payload.get("as_of") or "").strip() or _latest_as_of()
    if not as_of:
        return jsonify({"error": "本地没有估值快照数据，无法确定研究时点"}), 400

    sort_field = str(payload.get("sort") or "ts_code").strip()
    order = str(payload.get("order") or "asc").strip().lower()
    if order not in ("asc", "desc"):
        return jsonify({"error": "参数 [order] 非法，合法值: asc / desc"}), 400
    try:
        raw_limit = payload.get("limit")
        # 注意不能写 `payload.get("limit") or 默认值`：limit=0 会被当成缺省吞掉
        limit = int(_DEFAULT_ROWS if raw_limit is None else raw_limit)
    except (TypeError, ValueError):
        return jsonify({"error": "参数 [limit] 必须是整数"}), 400
    # 越界直接拒绝而不是静默夹取：静默夹取会让调用方拿到与请求不同的行数而不自知
    if limit < 1 or limit > _MAX_ROWS:
        return jsonify({"error": "参数 [limit] 取值范围 1~%d" % _MAX_ROWS}), 400

    frame, error = _factor_frame(as_of)
    if frame is None:
        return jsonify({"error": error}), 400

    try:
        pipeline = ScreenPipeline.from_spec(spec)
    except ScreenError as exc:
        return jsonify({"error": "筛选条件不合法：%s" % exc}), 400

    try:
        result = pipeline.run(frame)
    except ScreenError as exc:
        return jsonify({"error": "执行筛选失败：%s" % exc}), 400
    except Exception as exc:
        _logger.error("筛选执行异常: %s", str(exc)[:300])
        return jsonify({"error": "执行筛选失败，请查看服务日志"}), 500

    summary = result.summary
    total = len(summary)
    passed_count = int(summary["passed"].sum()) if "passed" in summary.columns else 0

    view = summary.copy()
    if sort_field in view.columns:
        key = pd.to_numeric(view[sort_field], errors="coerce") \
            if view[sort_field].dtype != object else view[sort_field]
        ordered = view.assign(_sort_key=key).sort_values(
            "_sort_key", ascending=(order == "asc"), na_position="last"
        )
        view = ordered.drop(columns=["_sort_key"])
    page = view.head(limit)

    rows = []
    for _, row in page.iterrows():
        record = {
            "ts_code": _text(row.get("ts_code")),
            "name": _text(row.get("name")),
            "industry": _text(row.get("industry")),
            "passed": bool(row.get("passed")),
            "failed_rules": list(row.get("failed_rules") or []),
        }
        # 把规则里用到的因子值一并带出，前端表格无需二次请求
        for operand in _rule_operands(spec):
            factor_name = operand.get("factor")
            if factor_name and factor_name not in record:
                record[factor_name] = _num(row.get(factor_name))
        rows.append(record)

    return jsonify({
        "as_of": as_of,
        "spec": spec,
        "positive_only": bool(positive_only),
        "condition": result.condition,
        "counts": result.counts,
        "total": total,
        "passed": passed_count,
        "rows": rows,
        "funnel": _funnel(summary, spec),
        "industries": _industry_rows(summary),
        "scatter": _scatter_points(summary, spec),
        "elapsed_ms": int((time.time() - started) * 1000),
        "message": "",
    })
