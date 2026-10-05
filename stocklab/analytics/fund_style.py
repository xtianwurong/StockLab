#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金风格分解与调仓分析 (stocklab.analytics.fund_style)
==============================================================================

【模块职责】
   基于收益率的风格分解（Returns-Based Style Analysis, RBSA）
   滚动窗口暴露度追踪、调仓信号生成、资金流佐证

【核心方法】
   1. 经典 Sharpe RBSA：基金收益率 = Σ (行业指数收益率 × 暴露度) + α + ε
      约束：暴露度 ≥ 0，Σ 暴露度 ≤ 1（允许现金仓位）
   2. 滚动窗口回归：每日/每周重算，追踪暴露度时序变化
   3. 资金流佐证：暴露度变化方向与板块资金流方向一致性校验

【输出】
   - 暴露度时序 DataFrame
   - 调仓信号：显著增减仓行业
   - 资金流佐证评分
   - 置信度分级
"""


import logging
import warnings
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, List, Dict, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import minimize, Bounds, LinearConstraint

from stocklab.analytics.valuation_percentile import ValuationPercentileAnalyzer

_logger = logging.getLogger(__name__)

__all__ = [
    "FundStyleDecomposer",
    "FundAllocationSignal",
    "DecompositionResult",
    "SignalStrength",
    "ConfidenceLevel",
    "required_history_days",
]


# ---------------------------------------------------------------------------
# 窗口口径（全项目唯一定义处）
#
# window_days 是**交易日**，而滚动推进与单期回看按自然日（沿用既有实现），
# 因此两侧都写成「window_days × 系数」的自然日跨度。
#
# 装载方（facade._load_analysis_data）与计算方（本模块）必须共用下面这个
# 公式：装载少一天，滚动序列最早那批时点就会静默判「数据不足」，日志看起来
# 像「历史数据缺失」，实际是自己没装够 —— 这个坑已经踩过一次。
# ---------------------------------------------------------------------------
ROLLING_WINDOW_DAYS_FACTOR = 3.0   # rolling_decompose 往回滚多少个 window
SINGLE_WINDOW_DAYS_FACTOR = 1.5     # 单期分解往前回看多少个 window
HISTORY_BUFFER_DAYS = 7             # 首个收益率所需的 1 天 + 余量
MIN_ALIGNMENT_OBSERVATIONS = 20      # 少于这么多对齐观测就不做回归（样本不足）


def required_history_days(window_days: int) -> int:
    """给定单期窗口（交易日），算出从分析日往回必须装载多少**自然日**的历史

    装载区间 = 滚动跨度（3×window）+ 单期回看（1.5×window）+ 余量。
    任何要跑滚动分解的调用方都应按这个数装载净值 / 指数 / 资金流。

    Args:
        window_days: 单期分解窗口（交易日）

    Returns:
        int: 自然日天数
    """
    span = (ROLLING_WINDOW_DAYS_FACTOR + SINGLE_WINDOW_DAYS_FACTOR) * window_days
    return int(span) + HISTORY_BUFFER_DAYS


class SignalStrength:
    """调仓信号强度"""
    STRONG_INCREASE = "大幅加仓"
    MODERATE_INCREASE = "适度加仓"
    SLIGHT_INCREASE = "轻微加仓"
    NEUTRAL = "持仓稳定"
    SLIGHT_DECREASE = "轻微减仓"
    MODERATE_DECREASE = "适度减仓"
    STRONG_DECREASE = "大幅减仓"


class ConfidenceLevel:
    """置信度等级"""
    HIGH = "高"
    MEDIUM = "中"
    LOW = "低"


@dataclass
class DecompositionResult:
    """单期风格分解结果"""
    fund_code: str
    trade_date: date
    window_days: int
    level: int  # 1=一级行业, 2=二级行业
    exposures: Dict[str, float]  # sector_code -> exposure
    r_squared: float
    alpha: float
    residual_vol: float
    n_sectors: int
    cash_exposure: float  # 1 - sum(exposures)


@dataclass
class FundAllocationSignal:
    """调仓信号"""
    fund_code: str
    sector_code: str
    sector_name: str
    level: int
    current_exposure: float
    exposure_change: float
    # 5/20 期对比：上一个可比时点若没算到这个行业（指数缺数据被排除），
    # 变化量无从谈起 -> None（前端显示「—」），拿 0 当过去值会造出假信号
    exposure_change_5d: Optional[float]
    exposure_change_20d: Optional[float]
    signal: str  # SignalStrength 枚举值
    # 资金流佐证三态：True 吻合 / False 有数据但不吻合 / None 数据不足无法判断
    capital_flow_corr: Optional[float]
    capital_flow_confirm: Optional[bool]
    confidence: str  # ConfidenceLevel 枚举值
    r_squared: float
    analysis_date: date
    window_days: int


class FundStyleDecomposer:
    """
    基金风格分解器

    【使用方式】
        decomposer = FundStyleDecomposer(
            fund_nav_df,          # 基金净值历史
            sector_index_dfs,     # {sector_code: index_df} 行业指数日线
            capital_flow_df,      # 资金流数据（可选）
        )
        signals = decomposer.analyze(
            fund_code="000001.OF",
            analysis_date=date(2026, 9, 30),
            window_days=60,
            level=1
        )
    """

    # 调仓阈值（环比变化）
    THRESHOLDS = {
        "strong_increase": 0.05,    # +5%
        "moderate_increase": 0.02,  # +2%
        "slight_increase": 0.005,   # +0.5%
        "slight_decrease": -0.005,  # -0.5%
        "moderate_decrease": -0.02, # -2%
        "strong_decrease": -0.05,   # -5%
    }

    def __init__(
        self,
        fund_nav_df: pd.DataFrame,
        sector_index_dfs: Dict[str, pd.DataFrame],
        capital_flow_df: Optional[pd.DataFrame] = None,
        industry_mapping_df: Optional[pd.DataFrame] = None,
    ):
        """
        Args:
            fund_nav_df: 基金净值历史，含 fund_code, nav_date, nav
            sector_index_dfs: {sector_code: DataFrame(trade_date, close)} 行业指数收盘价
            capital_flow_df: 资金流数据，含 sector_code, trade_date, net_inflow
            industry_mapping_df: 行业映射表，含 index_code, index_name, level, parent_code
        """
        self.fund_nav_df = fund_nav_df.copy()
        self.sector_index_dfs = sector_index_dfs
        self.capital_flow_df = capital_flow_df
        self.industry_mapping_df = industry_mapping_df

        # 预处理：计算基金日收益率
        self._prepare_fund_returns()
        # 预处理：计算行业指数日收益率
        self._prepare_sector_returns()
        # 预处理：资金流
        self._prepare_capital_flow()

    def _prepare_fund_returns(self):
        """计算基金日收益率"""
        df = self.fund_nav_df.copy()
        df = df.sort_values(["fund_code", "nav_date"])
        df["fund_return"] = df.groupby("fund_code")["nav"].pct_change()
        self.fund_returns = df.dropna(subset=["fund_return"])

    def _prepare_sector_returns(self):
        """计算各行业指数日收益率"""
        self.sector_returns = {}
        for code, df in self.sector_index_dfs.items():
            df = df.sort_values("trade_date").copy()
            df["sector_return"] = df["close"].pct_change()
            self.sector_returns[code] = df.dropna(subset=["sector_return"])[["trade_date", "sector_return"]]

    def _prepare_capital_flow(self):
        """预处理资金流数据"""
        if self.capital_flow_df is not None:
            df = self.capital_flow_df.copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            # 计算各板块资金流动量的标准化值（用于相关性计算）
            df["flow_zscore"] = df.groupby("sector_code")["net_inflow"].transform(
                lambda x: (x - x.rolling(20, min_periods=5).mean()) / (x.rolling(20, min_periods=5).std() + 1e-8)
            )
            self.capital_flow_df = df
        else:
            self.capital_flow_df = pd.DataFrame()

    @staticmethod
    def _skipped_result(fund_code: str, end_date: date, window_days: int) -> DecompositionResult:
        """「这个时点没算出来」的统一结果（n_sectors=0，滚动序列会跳过它）

        数据不足、含非有限值、优化失败三条路径都走这里。
        关键在于**不返回等权占位**：等权会让「没算出来」在下游长成
        「持仓稳定」的信号，等于用一个编出来的结论回答用户的调仓问题。
        """
        return DecompositionResult(
            fund_code=fund_code,
            trade_date=end_date,
            window_days=window_days,
            level=1,
            exposures={},
            r_squared=0.0,
            alpha=0.0,
            residual_vol=0.0,
            n_sectors=0,
            cash_exposure=1.0,
        )

    def _align_data(
        self,
        fund_code: str,
        sector_codes: List[str],
        end_date: date,
        window_days: int
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        对齐基金收益率与行业指数收益率

        Returns:
            (fund_returns, sector_returns_matrix)
        """
        # 转换为 pandas Timestamp 以便比较
        end_ts = pd.Timestamp(end_date)
        start_ts = end_ts - timedelta(days=int(window_days * SINGLE_WINDOW_DAYS_FACTOR))

        # 基金收益率
        fund_ret = self.fund_returns[
            (self.fund_returns["fund_code"] == fund_code) &
            (self.fund_returns["nav_date"] >= start_ts) &
            (self.fund_returns["nav_date"] <= end_ts)
        ][["nav_date", "fund_return"]].set_index("nav_date")

        # 行业指数收益率矩阵
        sector_data = {}
        for code in sector_codes:
            if code in self.sector_returns:
                sec = self.sector_returns[code][
                    (self.sector_returns[code]["trade_date"] >= start_ts) &
                    (self.sector_returns[code]["trade_date"] <= end_ts)
                ].set_index("trade_date")["sector_return"]
                sector_data[code] = sec

        sector_df = pd.DataFrame(sector_data)

        # 对齐日期（取交集）
        common_dates = fund_ret.index.intersection(sector_df.index)
        if len(common_dates) < MIN_ALIGNMENT_OBSERVATIONS:
            return pd.DataFrame(), pd.DataFrame()

        fund_aligned = fund_ret.loc[common_dates]
        sector_aligned = sector_df.loc[common_dates]

        # 丢掉任一侧有缺失的日期，只在**完整观测**上取窗口：
        # 矩阵里残留的 NaN 会让目标函数返回 NaN，SLSQP 随即报一句与真实原因
        # 无关的「Inequality constraints incompatible」，代码便退回等权权重 ——
        # 页面看着有结果，实际是假信号。宁可这个时点算不出来（如实缩短时序）。
        valid = fund_aligned["fund_return"].notna() & sector_aligned.notna().all(axis=1)
        fund_aligned = fund_aligned[valid]
        sector_aligned = sector_aligned[valid]
        if len(fund_aligned) < MIN_ALIGNMENT_OBSERVATIONS:
            return pd.DataFrame(), pd.DataFrame()

        # 取最近 window_days 个完整交易日
        fund_aligned = fund_aligned.tail(window_days)
        sector_aligned = sector_aligned.tail(window_days)

        return fund_aligned, sector_aligned

    def decompose_single(
        self,
        fund_code: str,
        end_date: date,
        window_days: int = 60,
        sector_codes: Optional[List[str]] = None,
        non_negative: bool = True,
        sum_to_one: bool = False,
    ) -> DecompositionResult:
        """
        单期风格分解（单日截面）
        """
        if sector_codes is None:
            sector_codes = list(self.sector_returns.keys())

        fund_ret, sector_ret = self._align_data(fund_code, sector_codes, end_date, window_days)

        if fund_ret.empty or len(sector_ret.columns) < 2:
            _logger.warning("基金 [%s] 于 %s 数据不足，无法分解", fund_code, end_date)
            return self._skipped_result(fund_code, end_date, window_days)

        y = fund_ret["fund_return"].values
        X = sector_ret.values
        n_sectors = X.shape[1]

        # 数值闸门：进优化器的 y / X 必须全有限。
        # pct_change 遇到 0 净值会产 inf，交集日期也可能残留缺失值；这类脏值
        # 流进 SLSQP 后只会得到一句与真实原因无关的「Inequality constraints
        # incompatible」，然后被当成「优化失败」吞掉 —— 根因完全看不出来。
        if not (np.isfinite(y).all() and np.isfinite(X).all()):
            _logger.warning(
                "基金 [%s] 于 %s 对齐后的收益含非有限值（NaN/inf），放弃该时点",
                fund_code, end_date,
            )
            return self._skipped_result(fund_code, end_date, window_days)

        # 约束条件
        if non_negative:
            bounds = Bounds(0, 1)
        else:
            bounds = Bounds(-1, 1)

        constraints = []
        if sum_to_one:
            constraints.append(LinearConstraint(np.ones(n_sectors), lb=0.95, ub=1.0))

        # 目标函数：最小化残差平方和
        def objective(w):
            residuals = y - X @ w
            return np.sum(residuals ** 2)

        # 初始猜测：等权
        w0 = np.ones(n_sectors) / n_sectors

        result = minimize(
            objective, w0,
            bounds=bounds,
            constraints=constraints,
            method="SLSQP",
            options={"maxiter": 1000, "ftol": 1e-9}
        )

        if not result.success or not np.isfinite(result.fun) \
                or not np.isfinite(result.x).all():
            # 优化失败 = 这一期没算出来，**不拿等权 w0 顶替**：
            # 等权会让「算不出来」伪装成「持仓没变」，信号层读到一片假稳定，
            # 比少一个时点更糟（滚动序列少一点，上层会明说点数不足）。
            _logger.warning(
                "基金 [%s] 于 %s 分解优化失败，跳过该时点: %s",
                fund_code, end_date, result.message,
            )
            return self._skipped_result(fund_code, end_date, window_days)

        weights = result.x
        exposures = {code: float(w) for code, w in zip(sector_ret.columns, weights)}
        total_exposure = sum(exposures.values())
        cash_exposure = max(0, 1 - total_exposure)

        # 计算统计指标
        y_pred = X @ weights
        residuals = y - y_pred
        alpha = np.mean(residuals) * 252  # 年化
        residual_vol = np.std(residuals) * np.sqrt(252)
        ss_tot = float(np.sum((y - np.mean(y)) ** 2))
        if not np.isfinite(ss_tot) or ss_tot <= 0:
            # 窗口内基金收益恒定 -> R² 数学上无定义。
            # 记 0 而不是留 NaN：NaN 会绕过上层「R²<0.5 不可信」的告警
            # （nan < 0.5 恒为 False），还会把 summary.avg_r_squared 变成 NaN
            # 直接污染 JSON。0 既不假装拟合好，也必然触发不可信告警。
            _logger.warning("基金 [%s] 于 %s 窗口内收益无波动，R² 无定义（按 0 计）",
                            fund_code, end_date)
            r_squared = 0.0
        else:
            r_squared = 1 - float(np.sum(residuals ** 2)) / ss_tot
            r_squared = float(r_squared) if np.isfinite(r_squared) else 0.0

        # 获取行业层级
        level = 1
        if self.industry_mapping_df is not None:
            levels = self.industry_mapping_df.set_index("index_code")["level"]
            level = int(levels[sector_ret.columns[0]]) if sector_ret.columns[0] in levels.index else 1

        return DecompositionResult(
            fund_code=fund_code,
            trade_date=end_date,
            window_days=window_days,
            level=level,
            exposures=exposures,
            r_squared=float(r_squared),
            alpha=float(alpha),
            residual_vol=float(residual_vol),
            n_sectors=n_sectors,
            cash_exposure=float(cash_exposure),
        )

    def rolling_decompose(
        self,
        fund_code: str,
        end_date: date,
        window_days: int = 60,
        step_days: int = 5,
        sector_codes: Optional[List[str]] = None,
    ) -> List[DecompositionResult]:
        """
        滚动窗口分解，生成暴露度时序
        """
        results = []
        current_date = end_date

        while current_date >= end_date - timedelta(
            days=int(window_days * ROLLING_WINDOW_DAYS_FACTOR)
        ):
            result = self.decompose_single(fund_code, current_date, window_days, sector_codes)
            if result.n_sectors > 0:
                results.append(result)
            current_date -= timedelta(days=step_days)

        return list(reversed(results))  # 时间正序

    def generate_signals(
        self,
        fund_code: str,
        analysis_date: date,
        window_days: int = 60,
        level: int = 1,
        sector_codes: Optional[List[str]] = None,
    ) -> List[FundAllocationSignal]:
        """
        生成调仓信号（核心入口）

        步骤：
        1. 滚动分解获取暴露度时序
        2. 计算暴露度变化（环比、5日、20日）
        3. 分级信号强度
        4. 资金流佐证
        5. 置信度评级
        """
        # 筛选对应层级的行业
        if sector_codes is None and self.industry_mapping_df is not None:
            sector_codes = self.industry_mapping_df[
                self.industry_mapping_df["level"] == level
            ]["index_code"].tolist()
        elif sector_codes is None:
            sector_codes = list(self.sector_returns.keys())

        # 滚动分解
        history = self.rolling_decompose(fund_code, analysis_date, window_days, sector_codes=sector_codes)

        if len(history) < 3:
            _logger.warning("基金 [%s] 历史分解点数不足，无法生成信号", fund_code)
            return []

        # 当前暴露度
        current = history[-1].exposures

        # 计算变化
        prev = history[-2].exposures if len(history) >= 2 else {}
        prev_5d = history[-6].exposures if len(history) >= 6 else {}
        prev_20d = history[-21].exposures if len(history) >= 21 else {}

        # 行业名称映射
        name_map = {}
        if self.industry_mapping_df is not None:
            name_map = dict(zip(
                self.industry_mapping_df["index_code"],
                self.industry_mapping_df["index_name"]
            ))

        signals = []
        for sector_code, current_exp in current.items():
            # 环比必须有可比的上一期：上一期没算到这个行业时（指数在该窗口缺
            # 数据、被 _align_data 排除），把它当 0 会凭空造出一条「大幅加仓」。
            # 宁可这一期不出这个行业的信号，也不要编一个过去值。
            if sector_code not in prev:
                _logger.warning(
                    "基金 [%s] %s 的上一期分解不含该行业，跳过其环比信号",
                    fund_code, sector_code,
                )
                continue
            change = current_exp - prev[sector_code]
            change_5d = (current_exp - prev_5d[sector_code]
                         if sector_code in prev_5d else None)
            change_20d = (current_exp - prev_20d[sector_code]
                          if sector_code in prev_20d else None)

            # 信号分级
            if change >= self.THRESHOLDS["strong_increase"]:
                signal = SignalStrength.STRONG_INCREASE
            elif change >= self.THRESHOLDS["moderate_increase"]:
                signal = SignalStrength.MODERATE_INCREASE
            elif change >= self.THRESHOLDS["slight_increase"]:
                signal = SignalStrength.SLIGHT_INCREASE
            elif change <= self.THRESHOLDS["strong_decrease"]:
                signal = SignalStrength.STRONG_DECREASE
            elif change <= self.THRESHOLDS["moderate_decrease"]:
                signal = SignalStrength.MODERATE_DECREASE
            elif change <= self.THRESHOLDS["slight_decrease"]:
                signal = SignalStrength.SLIGHT_DECREASE
            else:
                signal = SignalStrength.NEUTRAL

            # 资金流佐证：判不出来就是 None，绝不用 0.0 冒充「不相关」——
            # 0.0 是一个结论，而「只有一个交易日的资金流」根本没有结论
            capital_flow_corr = None
            capital_flow_confirm = None
            if not self.capital_flow_df.empty:
                # 计算暴露度变化与资金流的相关性
                # 统一转换为 pd.Timestamp 进行比较
                analysis_ts = pd.Timestamp(analysis_date)
                # 确保 trade_date 是 Timestamp 类型以便比较
                trade_dates_ts = pd.to_datetime(self.capital_flow_df["trade_date"])
                flow_data = self.capital_flow_df[
                    (self.capital_flow_df["sector_code"] == sector_code) &
                    (trade_dates_ts <= analysis_ts)
                ].tail(20)

                if len(flow_data) >= 10:
                    # 暴露度变化序列：只取**确实算到过该行业**的时点，
                    # 缺席的时点跳过，而不是补 0（补 0 等于伪造一段持仓历史）
                    exposures = [h.exposures[sector_code] for h in history[-20:]
                                 if sector_code in h.exposures]
                    exp_changes = np.diff(exposures) if len(exposures) > 1 else np.array([])

                    flow_values = (flow_data["flow_zscore"].values[-len(exp_changes):]
                                   if len(exp_changes) else np.array([]))

                    if len(exp_changes) == len(flow_values) and len(exp_changes) > 5:
                        corr = np.corrcoef(exp_changes, flow_values)[0, 1]
                        # 任一侧为常数时 corrcoef 给的是 nan/inf，都不能当成结论
                        if np.isfinite(corr):
                            capital_flow_corr = float(corr)
                            capital_flow_confirm = ((corr > 0.3 and change > 0)
                                                    or (corr < -0.3 and change < 0))
                else:
                    # 这个行业在窗口内资金流样本不足 —— 佐证不了，也否定不了
                    _logger.debug(
                        "行业 [%s] 资金流样本不足（%d/%d），佐证留空",
                        sector_code, len(flow_data), 10,
                    )

            # 置信度评级
            r_sq = history[-1].r_squared
            if r_sq > 0.8 and abs(change) > 0.02 and capital_flow_confirm:
                confidence = ConfidenceLevel.HIGH
            elif r_sq > 0.6 and abs(change) > 0.01:
                confidence = ConfidenceLevel.MEDIUM
            else:
                confidence = ConfidenceLevel.LOW

            signals.append(FundAllocationSignal(
                fund_code=fund_code,
                sector_code=sector_code,
                sector_name=name_map.get(sector_code, sector_code) if name_map else sector_code,
                level=level,
                current_exposure=current_exp,
                exposure_change=change,
                exposure_change_5d=change_5d,
                exposure_change_20d=change_20d,
                signal=signal,
                capital_flow_corr=capital_flow_corr,
                capital_flow_confirm=capital_flow_confirm,
                confidence=confidence,
                r_squared=history[-1].r_squared,
                analysis_date=analysis_date,
                window_days=window_days,
            ))

        # 按信号强度排序
        signal_order = {
            SignalStrength.STRONG_INCREASE: 0,
            SignalStrength.MODERATE_INCREASE: 1,
            SignalStrength.SLIGHT_INCREASE: 2,
            SignalStrength.NEUTRAL: 3,
            SignalStrength.SLIGHT_DECREASE: 4,
            SignalStrength.MODERATE_DECREASE: 5,
            SignalStrength.STRONG_DECREASE: 6,
        }
        signals.sort(key=lambda s: signal_order.get(s.signal, 9))

        return signals

    def analyze(
        self,
        fund_code: str,
        analysis_date: date,
        window_days: int = 60,
        level: int = 1,
    ) -> Dict:
        """
        完整分析入口

        Returns:
            {
                "fund_code": ...,
                "analysis_date": ...,
                "window_days": ...,
                "level": ...,
                "current_exposures": {...},
                "exposure_history": [...],
                "signals": [...],
                "summary": {...},
            }
        """
        if level == 1:
            sector_codes = self.industry_mapping_df[
                self.industry_mapping_df["level"] == 1
            ]["index_code"].tolist() if self.industry_mapping_df is not None else None
        else:
            sector_codes = self.industry_mapping_df[
                self.industry_mapping_df["level"] == 2
            ]["index_code"].tolist() if self.industry_mapping_df is not None else None

        # 当前暴露度
        current_result = self.decompose_single(fund_code, analysis_date, window_days, sector_codes=sector_codes)

        # 历史暴露度
        history = self.rolling_decompose(fund_code, analysis_date, window_days, sector_codes=sector_codes)

        # 信号
        signals = self.generate_signals(fund_code, analysis_date, window_days, level, sector_codes)

        # 汇总
        increases = [s for s in signals if s.exposure_change > 0.005]
        decreases = [s for s in signals if s.exposure_change < -0.005]

        return {
            "fund_code": fund_code,
            "analysis_date": analysis_date,
            "window_days": window_days,
            "level": level,
            "current_exposures": current_result.exposures,
            "cash_exposure": current_result.cash_exposure,
            "r_squared": current_result.r_squared,
            "exposure_history": [
                {"date": r.trade_date, "exposures": r.exposures, "r_squared": r.r_squared}
                for r in history
            ],
            "signals": [
                {
                    "sector_code": s.sector_code,
                    "sector_name": s.sector_name,
                    "level": s.level,
                    "current_exposure": s.current_exposure,
                    "exposure_change": s.exposure_change,
                    "exposure_change_5d": s.exposure_change_5d,
                    "exposure_change_20d": s.exposure_change_20d,
                    "signal": s.signal,
                    "capital_flow_corr": s.capital_flow_corr,
                    "capital_flow_confirm": s.capital_flow_confirm,
                    "confidence": s.confidence,
                }
                for s in signals
            ],
            "summary": {
                "total_increase_sectors": len(increases),
                "total_decrease_sectors": len(decreases),
                "top_increase": increases[0].sector_name if increases else None,
                "top_decrease": decreases[0].sector_name if decreases else None,
                "avg_r_squared": np.mean([r.r_squared for r in history]) if history else 0,
            }
        }


def decompose_fund_style(
    fund_nav_df: pd.DataFrame,
    sector_index_dfs: Dict[str, pd.DataFrame],
    capital_flow_df: Optional[pd.DataFrame] = None,
    industry_mapping_df: Optional[pd.DataFrame] = None,
    fund_code: str = None,
    analysis_date: date = None,
    window_days: int = 60,
    level: int = 1,
) -> Dict:
    """
    便捷函数：一键完成基金风格分解与调仓分析

    Args:
        fund_nav_df: 基金净值历史
        sector_index_dfs: 行业指数日线字典
        capital_flow_df: 资金流数据（可选）
        industry_mapping_df: 行业映射表
        fund_code: 基金代码
        analysis_date: 分析基准日
        window_days: 回看窗口
        level: 行业层级

    Returns:
        分析结果字典
    """
    decomposer = FundStyleDecomposer(
        fund_nav_df=fund_nav_df,
        sector_index_dfs=sector_index_dfs,
        capital_flow_df=capital_flow_df,
        industry_mapping_df=industry_mapping_df,
    )

    if analysis_date is None:
        analysis_date = date.today()
    if fund_code is None:
        fund_codes = fund_nav_df["fund_code"].unique()
        fund_code = fund_codes[0] if len(fund_codes) > 0 else None

    return decomposer.analyze(fund_code, analysis_date, window_days, level)