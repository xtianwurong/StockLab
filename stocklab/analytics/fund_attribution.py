#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金 Brinson 归因分析 (stocklab.analytics.fund_attribution)
==============================================================================

【模块职责】
   基于 Brinson-Fachler / Brinson-Hood-Beebower 模型的基金归因分析

【核心公式】
   总超额收益 = 资产配置效应 + 个股选择效应 + 交互效应

   资产配置效应 = Σ (w_p - w_b) * (R_b - R_p)
   个股选择效应 = Σ w_b * (R_p - R_b)
   交互效应 = Σ (w_p - w_b) * (R_p - R_b)

   其中：
     w_p = 组合在行业 i 的权重
     w_b = 基准在行业 i 的权重
     R_p = 组合在行业 i 的收益率
     R_b = 基准在行业 i 的收益率
     R_p = 组合总收益率 = Σ w_p * R_p
     R_b = 基准总收益率 = Σ w_b * R_b

【两种模型对比】
   Brinson-Fachler (1985):
     配置效应 = Σ (w_p - w_b) * (R_b - R_p)
     选择效应 = Σ w_b * (R_p - R_b)
     交互效应 = Σ (w_p - w_b) * (R_p - R_b)

   Brinson-Hood-Beebower (1986):
     配置效应 = Σ (w_p - w_b) * (R_b - R_total)
     选择效应 = Σ w_p * (R_p - R_b)
     交互效应 = 0 (合并入选择效应)

   本模块默认使用 Brinson-Fachler (三效应分解)，更精细。

【数据要求】
   - 组合持仓权重 (按行业分组)
   - 基准持仓权重 (按行业分组)
   - 组合各行业收益率
   - 基准各行业收益率
   - 时间窗口：通常月度/季度
"""


import logging
from dataclasses import dataclass
from datetime import date
from typing import List, Dict, Literal

import numpy as np

_logger = logging.getLogger(__name__)

__all__ = [
    "BrinsonAttribution",
    "AttributionResult",
    "AttributionConfig",
]


@dataclass
class AttributionConfig:
    """归因配置"""
    model: Literal["brinson-fachler", "brinson-hb"] = "brinson-fachler"
    # 是否使用对数收益率（连续复利），默认用简单收益率
    use_log_returns: bool = False


@dataclass
class AttributionResult:
    """单期归因结果"""
    trade_date: date
    total_return: float           # 组合总收益率
    benchmark_return: float       # 基准总收益率
    excess_return: float          # 超额收益
    allocation_effect: float      # 资产配置效应
    selection_effect: float       # 个股选择效应
    interaction_effect: float     # 交互效应
    sector_details: Dict[str, Dict]  # 各行业明细


class BrinsonAttribution:
    """
    Brinson 归因分析器

    【使用方式】
        attributor = BrinsonAttribution(config=AttributionConfig(model="brinson-fachler"))
        result = attributor.attribute(
            portfolio_weights=portfolio_weights,    # {sector: weight}
            benchmark_weights=benchmark_weights,    # {sector: weight}
            portfolio_returns=portfolio_returns,    # {sector: return}
            benchmark_returns=benchmark_returns,    # {sector: return}
            trade_date=date(2026, 9, 30)
        )

    【输出】
        AttributionResult 对象，含总效应分解 + 各行业明细
    """

    def __init__(self, config: AttributionConfig = None):
        self.config = config or AttributionConfig()

    def attribute(
        self,
        portfolio_weights: Dict[str, float],
        benchmark_weights: Dict[str, float],
        portfolio_returns: Dict[str, float],
        benchmark_returns: Dict[str, float],
        trade_date: date
    ) -> "AttributionResult":
        """
        单期归因分解

        Args:
            portfolio_weights: 组合行业权重 {sector_code: weight}
            benchmark_weights: 基准行业权重 {sector_code: weight}
            portfolio_returns: 组合行业收益率 {sector_code: return}
            benchmark_returns: 基准行业收益率 {sector_code: return}
            trade_date: 归因日期

        Returns:
            AttributionResult
        """
        # 1. 统一行业集合
        all_sectors = set(portfolio_weights.keys()) | set(benchmark_weights.keys()) \
                      | set(portfolio_returns.keys()) | set(benchmark_returns.keys())
        all_sectors = sorted(all_sectors)

        # 2. 对齐数据，缺失填 0
        w_p = np.array([portfolio_weights.get(s, 0.0) for s in all_sectors])
        w_b = np.array([benchmark_weights.get(s, 0.0) for s in all_sectors])
        r_p = np.array([portfolio_returns.get(s, 0.0) for s in all_sectors])
        r_b = np.array([benchmark_returns.get(s, 0.0) for s in all_sectors])

        # 归一化权重（防止数据问题）
        if w_p.sum() > 0:
            w_p = w_p / w_p.sum()
        if w_b.sum() > 0:
            w_b = w_b / w_b.sum()

        # 3. 计算总收益率
        total_return = np.sum(w_p * r_p)
        benchmark_return = np.sum(w_b * r_b)
        excess_return = total_return - benchmark_return

        # 4. Brinson 分解
        if self.config.model == "brinson-fachler":
            # Brinson-Fachler 三效应分解
            allocation = np.sum((w_p - w_b) * (r_b - np.sum(w_b * r_b)))
            selection = np.sum(w_b * (r_p - r_b))
            interaction = np.sum((w_p - w_b) * (r_p - r_b))
        else:
            # Brinson-Hood-Beebower 两效应分解
            total_b_return = np.sum(w_b * r_b)
            allocation = np.sum((w_p - w_b) * (r_b - total_b_return))
            selection = np.sum(w_p * (r_p - r_b))
            interaction = 0.0

        # 5. 各行业明细
        sector_details = {}
        for i, sector in enumerate(all_sectors):
            wp, wb = w_p[i], w_b[i]
            rp, rb = r_p[i], r_b[i]

            if self.config.model == "brinson-fachler":
                alloc_eff = (wp - wb) * (rb - np.sum(w_b * r_b))
                select_eff = wb * (rp - rb)
                inter_eff = (wp - wb) * (rp - rb)
            else:
                total_b = np.sum(w_b * r_b)
                alloc_eff = (wp - wb) * (rb - total_b)
                select_eff = wp * (rp - rb)
                inter_eff = 0.0

            sector_details[all_sectors[i]] = {
                "portfolio_weight": float(wp),
                "benchmark_weight": float(wb),
                "portfolio_return": float(rp),
                "benchmark_return": float(rb),
                "allocation_effect": float(alloc_eff),
                "selection_effect": float(select_eff),
                "interaction_effect": float(inter_eff),
                "total_effect": float(alloc_eff + select_eff + inter_eff),
            }

        # 验证分解完整性
        total_effects = allocation + selection + interaction
        if abs(total_effects - excess_return) > 1e-8:
            _logger.warning(
                "归因分解残差 %.2e，total=%s excess=%s",
                total_effects - excess_return, total_effects, excess_return
            )

        return AttributionResult(
            trade_date=trade_date,
            total_return=float(total_return),
            benchmark_return=float(benchmark_return),
            excess_return=float(excess_return),
            allocation_effect=float(allocation),
            selection_effect=float(selection),
            interaction_effect=float(interaction),
            sector_details=sector_details,
        )

    def rolling_attribution(
        self,
        portfolio_weights_history: List[Dict[str, Dict[str, float]]],
        benchmark_weights_history: List[Dict[str, Dict[str, float]]],
        portfolio_returns_history: List[Dict[str, Dict[str, float]]],
        benchmark_returns_history: List[Dict[str, Dict[str, float]]],
        dates: List[date]
    ) -> List[AttributionResult]:
        """
        滚动窗口归因（批量处理多期）

        Args:
            各 *_history: 长度相同的列表，每元素为 {date: {sector: value}} 格式
            dates: 对应的日期列表

        Returns:
            AttributionResult 列表
        """
        results = []
        for i, date in enumerate(dates):
            if i >= len(portfolio_weights_history):
                break
            result = self.attribute(
                portfolio_weights=portfolio_weights_history[i],
                benchmark_weights=benchmark_weights_history[i],
                portfolio_returns=portfolio_returns_history[i],
                benchmark_returns=benchmark_returns_history[i],
                trade_date=date
            )
            results.append(result)
        return results
