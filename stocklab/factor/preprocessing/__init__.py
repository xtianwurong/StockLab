#!/usr/bin/env python3
"""
==============================================================================
StockLab - 因子预处理 (stocklab.factor.preprocessing)
==============================================================================

【模块职责】
   实现 V2 需求 §7.6 的预处理，全部**可配置、可复现**：
     winsorize               缩尾（按分位截断极端值）
     rank                    排序百分位（含升降序可配）
     zscore                  截面标准化
     industry_neutralize     行业中性化（组内去均值，等价于对行业哑变量做回归取残差）
     market_cap_neutralize   市值中性化（对 log(市值) 做一元回归取残差）
     missing                 缺失值处理（保留 / 均值 / 中位数 / 定值 / 丢行）

【调用方式】
   apply_preprocessing(frame, steps)
   steps 形如：
     [{"method": "winsorize", "columns": ["pe_ttm"], "lower": 0.01, "upper": 0.01},
      {"method": "zscore", "columns": "*"}]

【设计原则】
   - 纯计算：只在传入的帧副本上就地改列，不取数、不落库；
   - 「配置错了」与「数据没有」都必须显式失败：
     未知 method、目标列缺失、分组列全空、有效样本不足 -> FactorDataError；
   - 分母为 0、样本无离散度 -> 该列置 NaN，不产生 inf、不伪造数值。
"""

import pandas as pd

from stocklab.factor.base import FactorDataError, log_positive

__all__ = [
    "apply_preprocessing",
    "PREPROCESS_METHODS",
    "MISSING_ACTIONS",
]

# 支持的预处理方法
PREPROCESS_METHODS = (
    "winsorize",
    "rank",
    "zscore",
    "industry_neutralize",
    "market_cap_neutralize",
    "missing",
)

# missing 步骤支持的缺失值处理动作
MISSING_ACTIONS = (
    "keep",    # 保留 NaN（默认）
    "mean",    # 用列均值填充
    "median",  # 用列中位数填充
    "zero",    # 填 0
    "value",   # 填 step["value"]
    "drop",    # 丢掉这些列上有缺失的行
)


def apply_preprocessing(frame, steps):
    """
    按配置对因子输入帧做预处理（返回新帧，原帧不变）

    Args:
        frame (pd.DataFrame): 因子输入帧
        steps (list): 预处理步骤列表，每项为含 "method" 的字典；None/空列表表示不处理

    Returns:
        pd.DataFrame: 处理后的帧（列集不变，除非含 drop 动作）

    Raises:
        FactorDataError: 配置非法（未知方法/动作、目标列缺失、样本不足等）
    """
    if not steps:
        return frame.copy()

    result = frame.copy()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise FactorDataError("预处理第 %d 项必须是字典，实际: %s" % (index + 1, step))
        method = step.get("method")
        if method not in PREPROCESS_METHODS:
            raise FactorDataError(
                "预处理第 %d 项的方法 %r 非法，可用: %s"
                % (index + 1, method, list(PREPROCESS_METHODS))
            )
        _HANDLERS[method](result, step)
    return result


def _resolve_columns(frame, step):
    """
    解析 step 中的 columns 目标

    Args:
        frame (pd.DataFrame): 当前帧
        step (dict): 步骤配置

    Returns:
        list: 目标列名列表

    Raises:
        FactorDataError: 目标列不在帧中
    """
    spec = step.get("columns")
    if spec is None or spec == "*":
        return [
            column
            for column in frame.columns
            if pd.api.types.is_numeric_dtype(frame[column])
        ]
    if isinstance(spec, str):
        spec = [spec]
    if not isinstance(spec, (list, tuple)):
        raise FactorDataError("columns 必须是列名、列名列表或 '*'，实际: %s" % (spec,))

    missing = [column for column in spec if column not in frame.columns]
    if missing:
        raise FactorDataError("预处理目标列 %s 不在输入帧中" % missing)
    return list(spec)


def _apply_winsorize(frame, step):
    """
    按**两端分位比例**截断极端值（与 scipy.stats.mstats.winsorize 的 limits 口径一致）

    step["lower"] / step["upper"] 表示「裁掉的上下尾比例」，如 0.01 表示
    裁掉最小 1% 与最大 1%（即截断到 1% 与 99% 分位），而不是分位点本身。
    """
    lower = float(step.get("lower", 0.01))
    upper = float(step.get("upper", 0.01))
    if lower < 0 or upper < 0 or lower + upper >= 1:
        raise FactorDataError(
            "winsorize 尾部比例非法: lower=%s upper=%s（须非负且相加小于 1）"
            % (lower, upper)
        )
    for column in _resolve_columns(frame, step):
        series = frame[column]
        low_value = series.quantile(lower)
        high_value = series.quantile(1.0 - upper)
        if pd.isna(low_value) or pd.isna(high_value):
            continue
        frame[column] = series.clip(low_value, high_value)


def _apply_rank(frame, step):
    """转成截面百分位（默认升序、pct=True，缺失保持 NaN）"""
    ascending = bool(step.get("ascending", True))
    percent = bool(step.get("pct", True))
    for column in _resolve_columns(frame, step):
        frame[column] = frame[column].rank(
            ascending=ascending, pct=percent, na_option="keep"
        )


def _apply_zscore(frame, step):
    """截面标准化：(x - 均值) / 标准差；标准差为 0 或 NaN 时整列置 NaN"""
    ddof = int(step.get("ddof", 1))
    for column in _resolve_columns(frame, step):
        series = frame[column]
        mean = series.mean()
        std = series.std(ddof=ddof)
        if pd.isna(std) or std == 0:
            frame[column] = pd.Series(float("nan"), index=series.index)
            continue
        frame[column] = (series - mean) / std


def _apply_industry_neutralize(frame, step):
    """行业中性化：组内去均值（等价于对行业哑变量回归取残差）；分组键缺失的行置 NaN"""
    by = step.get("by", "industry")
    if by not in frame.columns:
        raise FactorDataError("行业中性化需要分组列 %r，但输入帧没有它" % by)

    key_text = frame[by].fillna("").astype(str).str.strip()
    missing_key = key_text == ""
    if bool(missing_key.all()):
        raise FactorDataError(
            "分组列 %s 全部为空，无法做行业中性化（请先补齐行业字段或去掉该步骤）" % by
        )

    targets = [c for c in _resolve_columns(frame, step) if c != by]
    for column in targets:
        series = frame[column]
        group_mean = series.groupby(key_text, dropna=False).transform("mean")
        frame[column] = (series - group_mean).where(~missing_key)


def _apply_market_cap_neutralize(frame, step):
    """市值中性化：对 log(市值) 做一元回归取残差；市值或因子缺失的行结果为 NaN"""
    column = step.get("column", "total_mv")
    if column not in frame.columns:
        raise FactorDataError("市值中性化需要市值列 %r，但输入帧没有它" % column)

    raw = frame[column].astype("float64")
    scale = raw.map(log_positive) if bool(step.get("log", True)) else raw

    targets = [c for c in _resolve_columns(frame, step) if c != column]
    for target in targets:
        values = frame[target].astype("float64")
        valid = scale.notna() & values.notna()
        if int(valid.sum()) < 3:
            raise FactorDataError(
                "市值中性化因子 %s 的有效样本只有 %d 行（至少 3 行）"
                % (target, int(valid.sum()))
            )
        x = scale[valid]
        y = values[valid]
        x_mean = x.mean()
        y_mean = y.mean()
        denominator = float(((x - x_mean) ** 2).sum())
        if denominator == 0:
            raise FactorDataError(
                "市值列 %s 无离散度（全部相同），无法做市值中性化" % column
            )
        beta = float(((x - x_mean) * (y - y_mean)).sum()) / denominator
        frame[target] = values - (y_mean + beta * (scale - x_mean))


def _apply_missing(frame, step):
    """缺失值处理：keep / mean / median / zero / value / drop"""
    action = step.get("action", "keep")
    if action not in MISSING_ACTIONS:
        raise FactorDataError(
            "缺失值处理动作 %r 非法，可用: %s" % (action, list(MISSING_ACTIONS))
        )
    columns = _resolve_columns(frame, step)
    if action == "keep":
        return
    if action == "drop":
        frame.drop(index=frame.index[frame[columns].isna().any(axis=1)], inplace=True)
        return
    if action == "zero":
        for column in columns:
            frame[column] = frame[column].fillna(0)
        return
    if action == "value":
        if "value" not in step:
            raise FactorDataError("action=value 必须同时提供 value 字段")
        for column in columns:
            frame[column] = frame[column].fillna(step["value"])
        return

    # mean / median
    statistic = "mean" if action == "mean" else "median"
    for column in columns:
        frame[column] = frame[column].fillna(getattr(frame[column], statistic)())


# 方法 -> 处理函数（模块级私有，保持单一派发表，避免长 if/elif）
_HANDLERS = {
    "winsorize": _apply_winsorize,
    "rank": _apply_rank,
    "zscore": _apply_zscore,
    "industry_neutralize": _apply_industry_neutralize,
    "market_cap_neutralize": _apply_market_cap_neutralize,
    "missing": _apply_missing,
}
