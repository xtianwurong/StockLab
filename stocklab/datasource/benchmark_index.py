#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基准指数行业数据源 (stocklab.datasource.benchmark_index)
==============================================================================

【模块职责】
   取「基准指数按申万行业拆分的权重」所需的原始数据，供 Brinson 归因使用。

【数据链路】
   中证指数公司成分券权重（akshare: index_stock_cons_weight_csindex）
        ↓  fetch_index_constituent_weights()
   成分券级权重 DataFrame(ts_code, weight, as_of_date)
        ↓  aggregate_industry_weights() + 本地 fund.stock_industry_mapping
   行业级权重 DataFrame(sector_code, weight, coverage)

【设计原则】
   - 不内置任何「拍脑袋」的行业权重常量：权重一律来自指数公司披露数据，
     行业归属一律来自本地行业映射表；缺数据就返回空并由上层提示同步。
   - 聚合函数 aggregate_industry_weights() 为**纯函数**，可在离线测试中验证。
   - 覆盖率 coverage 如实反映未映射成分券的权重占比，供归因结果降级提示。

【支持范围】
   仅中证指数公司（CSI）发布的指数可取成分权重；上证/深证系列指数
   （000001.SH、399001.SZ 等）暂无权重口径，fetch 时返回空表并告警。
"""

import logging
from datetime import date
from typing import Optional

import pandas as pd

from stocklab.domain import BENCHMARK_INDEX_LEVEL1

_logger = logging.getLogger(__name__)

__all__ = [
    "BENCHMARK_INDEX_CODES",
    "CSI_BENCHMARK_CODES",
    "CONSTITUENT_WEIGHT_SOURCE_CSINDEX",
    "fetch_index_constituent_weights",
    "aggregate_industry_weights",
]

# 可选基准指数（代码 -> 中文名），与 stocklab.domain.benchmark 同源
BENCHMARK_INDEX_CODES = BENCHMARK_INDEX_LEVEL1

CONSTITUENT_WEIGHT_SOURCE_CSINDEX = "csindex:constituent_weight"

# 中证指数公司发布、可取成分权重的基准
CSI_BENCHMARK_CODES = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000906.SH": "中证800",
    "000985.SH": "中证全指",
}


def _to_csi_symbol(benchmark_code: str) -> Optional[str]:
    """000300.SH -> 000300；非 CSI 指数返回 None"""
    code = str(benchmark_code or "").strip()
    if code in CSI_BENCHMARK_CODES:
        return code.split(".")[0]
    plain = code.split(".")[0]
    # 中证系指数代码以 000/001 开头且由 CSI 发布
    if plain in {c.split(".")[0] for c in CSI_BENCHMARK_CODES}:
        return plain
    return None


def fetch_index_constituent_weights(
    benchmark_code: str,
    as_of: Optional[date] = None,
) -> pd.DataFrame:
    """
    抓取基准指数成分券权重（中证指数公司披露）

    Args:
        benchmark_code: 基准指数代码，如 000300.SH
        as_of: 期望披露日期；当前数据源仅提供最新一期，保留参数以兼容调用方

    Returns:
        pd.DataFrame: 列 [ts_code, weight, as_of_date, source]
                      weight 为小数（0.00433 = 0.433%）；
                      无数据 / 非 CSI 指数返回空表
    """
    symbol = _to_csi_symbol(benchmark_code)
    if symbol is None:
        _logger.warning("基准指数 %s 非中证指数公司发布，暂无成分权重口径", benchmark_code)
        return pd.DataFrame(columns=["ts_code", "weight", "as_of_date", "source"])

    try:
        import akshare as ak
        raw = ak.index_stock_cons_weight_csindex(symbol=symbol)
    except Exception as exc:  # 网络/接口变更等外部风险
        _logger.warning("基准指数 %s 成分权重抓取失败：%s", benchmark_code, exc)
        return pd.DataFrame(columns=["ts_code", "weight", "as_of_date", "source"])

    if raw is None or raw.empty:
        _logger.warning("基准指数 %s 成分权重返回空", benchmark_code)
        return pd.DataFrame(columns=["ts_code", "weight", "as_of_date", "source"])

    # 中文列：成分券代码 / 权重 / 日期
    code_col = next((c for c in raw.columns if "成分券代码" in c), None)
    weight_col = next((c for c in raw.columns if c == "权重" or "权重" in c), None)
    date_col = next((c for c in raw.columns if "日期" in c), None)
    if code_col is None or weight_col is None:
        _logger.warning("基准指数 %s 权重响应缺少必需列：%s", benchmark_code, list(raw.columns))
        return pd.DataFrame(columns=["ts_code", "weight", "as_of_date", "source"])

    codes = raw[code_col].astype(str).str.strip().str.zfill(6)
    weights = pd.to_numeric(raw[weight_col], errors="coerce")
    # 中证披露单位为百分比（合计约 100），统一转小数。
    # 判断依据用「合计」而非「单票最大值」——中证1000 单票权重普遍 <1.5%，
    # 按最大值判断会漏掉换算，导致权重整体放大 100 倍。
    if weights.sum(skipna=True) > 1.5:
        weights = weights / 100.0

    as_of_text = str(raw[date_col].iloc[0])[:10] if date_col is not None else (
        as_of.isoformat() if as_of else date.today().isoformat()
    )

    frame = pd.DataFrame({
        "ts_code": _codes_to_ts_code(codes),
        "weight": weights,
        "as_of_date": as_of_text,
        "source": CONSTITUENT_WEIGHT_SOURCE_CSINDEX,
    }).dropna(subset=["weight"])

    frame = frame[frame["weight"] > 0].reset_index(drop=True)
    _logger.info("基准指数 %s 成分权重抓取完成：%d 只，披露日期 %s",
                 benchmark_code, len(frame), as_of_text)
    return frame


def _codes_to_ts_code(codes: pd.Series) -> pd.Series:
    """成分券代码 -> ts_code（与 fund_holding 走同一归一化口径，避免两处规则漂移）"""
    from stocklab.normalization import normalize_ts_code

    def _normalize(code: str) -> str:
        text = str(code or "").strip()
        if not text or not text.isdigit():
            return text
        if len(text) < 6:
            text = text.zfill(6)
        return normalize_ts_code(text)

    return codes.map(_normalize)


def aggregate_industry_weights(
    constituent_weights: pd.DataFrame,
    industry_mapping: pd.DataFrame,
    level: int = 1,
) -> pd.DataFrame:
    """
    成分券权重 -> 行业权重（纯函数，便于离线测试）

    Args:
        constituent_weights: 列 [ts_code, weight]，weight 为小数
        industry_mapping:    列 [ts_code, sw_l1_code, sw_l1_name, sw_l2_code, ...]
        level:               行业层级 1 / 2

    Returns:
        pd.DataFrame: 列 [sector_code, sector_name, weight, coverage]
                      weight 在**已映射样本内**归一化到合计 1；
                      coverage = 已映射成分券权重 / 成分券总权重（0~1）
                      映射后无样本时返回空表
    """
    if constituent_weights is None or constituent_weights.empty:
        return pd.DataFrame(columns=["sector_code", "sector_name", "weight", "coverage"])

    if level not in (1, 2):
        _logger.warning("行业层级 %s 不受支持，仅支持 1 / 2", level)
        return pd.DataFrame(columns=["sector_code", "sector_name", "weight", "coverage"])

    code_col, name_col = (f"sw_l{level}_code", f"sw_l{level}_name")

    weights = constituent_weights[["ts_code", "weight"]].copy()
    weights["weight"] = pd.to_numeric(weights["weight"], errors="coerce").fillna(0.0)

    mapping = industry_mapping
    if mapping is None or mapping.empty or code_col not in mapping.columns:
        _logger.warning("行业映射表为空或缺少 %s 列，无法聚合行业权重", code_col)
        return pd.DataFrame(columns=["sector_code", "sector_name", "weight", "coverage"])

    keep = ["ts_code", code_col] + ([name_col] if name_col in mapping.columns else [])
    merged = weights.merge(mapping[keep].drop_duplicates("ts_code"), on="ts_code", how="left")

    total_weight = merged["weight"].sum()
    mapped = merged[merged[code_col].notna() & (merged[code_col] != "")]
    mapped_weight = mapped["weight"].sum()

    if total_weight <= 0 or mapped_weight <= 0:
        _logger.warning("行业映射后无有效权重（总权重 %.6f，已映射 %.6f）",
                        total_weight, mapped_weight)
        return pd.DataFrame(columns=["sector_code", "sector_name", "weight", "coverage"])

    grouped = mapped.groupby(code_col, dropna=True)["weight"].sum()
    sector_weights = grouped / mapped_weight

    names = {}
    if name_col in mapped.columns:
        names = (
            mapped.dropna(subset=[name_col])
            .drop_duplicates(code_col)
            .set_index(code_col)[name_col]
            .to_dict()
        )

    coverage = float(mapped_weight / total_weight)
    frame = pd.DataFrame({
        "sector_code": sector_weights.index.astype(str),
        "sector_name": [names.get(code) for code in sector_weights.index],
        "weight": sector_weights.values.astype(float),
        "coverage": coverage,
    }).sort_values("weight", ascending=False).reset_index(drop=True)

    return frame
