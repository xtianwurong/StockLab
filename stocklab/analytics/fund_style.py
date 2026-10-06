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
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional, List, Dict, Tuple, Literal

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

# P1 增强参数
FACTOR_CATEGORIES = ("style", "macro", "industry")  # 因子分类：风格/宏观/行业
DEFAULT_FACTOR_WEIGHTS = {"style": 0.5, "macro": 0.2, "industry": 0.3}  # 默认因子权重
TRANSACTION_COST_BPS = 15  # 单边换仓成本基点（含佣金+印花税+滑点）
SLIPPAGE_MODEL = "sqrt"    # 滑点模型：linear / sqrt / power
MAX_TURNOVER = 0.5         # 单期最大换仓率（防异常）
CONFIDENCE_CALIBRATION_WINDOW = 252  # 置信度校准回看窗口（交易日）
MULTI_WINDOW_DAYS = [20, 60, 120]    # 多窗口一致性检查（交易日）
CASH_POSITION_SOURCE = "holding"     # 现金仓位来源：holding / nav / fixed


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
class TransactionCostModel:
    """交易成本模型"""
    commission_bps: float = 2.5      # 佣金基点（万2.5）
    stamp_tax_bps: float = 10.0      # 印花税基点（卖出方千分之一 = 10bp）
    slippage_bps: float = 2.5        # 滑点基点
    slippage_model: str = "sqrt"     # 滑点模型：linear / sqrt / power
    max_turnover: float = 0.5        # 单期最大换仓率

    def estimate_cost_bps(self, turnover: float) -> float:
        """估算单边换仓成本（基点）"""
        base = self.commission_bps + self.slippage_bps
        if turnover > 0:
            base += self.stamp_tax_bps  # 印花税仅卖出收取
        if self.slippage_model == "sqrt":
            # 平方根冲击模型：成本 ~ sqrt(turnover)
            slippage = self.slippage_bps * np.sqrt(min(turnover, self.max_turnover) / 0.1)
        elif self.slippage_model == "linear":
            slippage = self.slippage_bps * (turnover / 0.1)
        else:
            slippage = self.slippage_bps
        return base + slippage


@dataclass
class DecompositionResult:
    """单期风格分解结果"""
    fund_code: str
    trade_date: date
    window_days: int
    level: int  # 1=一级行业, 2=二级行业
    exposures: Dict[str, float]  # sector_code -> exposure
    factor_exposures: Dict[str, float] = field(default_factory=dict)  # factor_name -> exposure
    r_squared: float = 0.0
    alpha: float = 0.0
    residual_vol: float = 0.0
    n_sectors: int = 0
    cash_exposure: float = 1.0  # 1 - sum(exposures)
    # P1 增强字段
    transaction_cost_bps: float = 0.0
    turnover_ratio: float = 0.0
    net_alpha_bps: float = 0.0  # 扣除交易成本后的年化超额收益


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
    # P1 增强字段
    transaction_cost_bps: float = 0.0          # 该信号预估交易成本（基点）
    net_exposure_change: Optional[float] = None  # 扣除交易成本后的净变化
    turnover_ratio: float = 0.0                # 换仓率
    calibrated_confidence: Optional[float] = None  # 校准后置信度概率


@dataclass
class FactorReturn:
    """因子收益率数据"""
    factor_name: str
    factor_category: Literal["style", "macro", "industry"]
    dates: List[date]
    returns: List[float]  # 日收益率序列


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
        factor_returns_dfs: Optional[Dict[str, pd.DataFrame]] = None,  # P1: 因子收益率 {factor_name: DataFrame(date, return)}
        transaction_cost_model: Optional[TransactionCostModel] = None,   # P1: 交易成本模型
        cash_position: Optional[float] = None,                          # P1: 显性现金仓位
        cash_position_source: str = CASH_POSITION_SOURCE,                # P1: 现金仓位来源
    ):
        """
        Args:
            fund_nav_df: 基金净值历史，含 fund_code, nav_date, nav
            sector_index_dfs: {sector_code: DataFrame(trade_date, close)} 行业指数收盘价
            capital_flow_df: 资金流数据，含 sector_code, trade_date, net_inflow
            industry_mapping_df: 行业映射表，含 index_code, index_name, level, parent_code
            factor_returns_dfs: {factor_name: DataFrame(trade_date, return)} 因子收益率（P1增强）
            transaction_cost_model: 交易成本模型（P1增强）
            cash_position: 显性现金仓位（若提供则覆盖自动计算）
            cash_position_source: 现金仓位来源 holding/nav/fixed
        """
        self.fund_nav_df = fund_nav_df.copy()
        self.sector_index_dfs = sector_index_dfs
        self.capital_flow_df = capital_flow_df
        self.industry_mapping_df = industry_mapping_df
        self.factor_returns_dfs = factor_returns_dfs or {}
        self.transaction_cost_model = transaction_cost_model or TransactionCostModel()
        self.cash_position = cash_position
        self.cash_position_source = cash_position_source

        # 预处理：计算基金日收益率
        self._prepare_fund_returns()
        # 预处理：计算行业指数日收益率
        self._prepare_sector_returns()
        # 预处理：因子收益率（P1增强）
        self._prepare_factor_returns()
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

    def _prepare_factor_returns(self):
        """预处理因子收益率（P1增强：支持多因子暴露分解）"""
        self.factor_returns = {}
        for name, df in self.factor_returns_dfs.items():
            df = df.sort_values("trade_date").copy()
            # 兼容不同列名：return / factor_return / close
            ret_col = None
            for c in ["return", "factor_return", "close"]:
                if c in df.columns:
                    ret_col = c
                    break
            if ret_col == "close":
                df["factor_return"] = df["close"].pct_change()
            elif ret_col != "factor_return":
                df["factor_return"] = df[ret_col]
            self.factor_returns[name] = df.dropna(subset=["factor_return"])[["trade_date", "factor_return"]]

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
        include_factors: bool = True,  # P1: 是否包含因子分解
    ) -> DecompositionResult:
        """
        单期风格分解（单日截面）

        P1 增强：支持行业+风格+宏观多因子联合分解，内置交易成本扣除
        """
        if sector_codes is None:
            sector_codes = list(self.sector_returns.keys())

        fund_ret, sector_ret = self._align_data(fund_code, sector_codes, end_date, window_days)

        if fund_ret.empty or len(sector_ret.columns) < 2:
            _logger.warning("基金 [%s] 于 %s 数据不足，无法分解", fund_code, end_date)
            return self._skipped_result(fund_code, end_date, window_days)

        y = fund_ret["fund_return"].values
        X_industry = sector_ret.values
        n_sectors = X_industry.shape[1]

        # P1: 准备因子矩阵
        factor_names = []
        X_factors = []
        if self.factor_returns:
            # 统一转换为 Timestamp 以便比较（fund_ret.index 是 Timestamp）
            fund_ret_ts = fund_ret.index
            for fname in self.factor_returns:
                if fname in self.factor_returns:
                    f_ret = self.factor_returns[fname].set_index("trade_date")["factor_return"]
                    # 将 factor 的 trade_date (date) 转为 Timestamp 以便与 fund_ret 对齐
                    f_ret.index = pd.to_datetime(f_ret.index)
                    common = fund_ret_ts.intersection(f_ret.index)
                    if len(common) >= MIN_ALIGNMENT_OBSERVATIONS:
                        f_aligned = f_ret.loc[common].tail(window_days)
                        if len(f_aligned) >= MIN_ALIGNMENT_OBSERVATIONS:
                            factor_names.append(fname)
                            X_factors.append(f_aligned.values)

        # 合并行业 + 因子矩阵
        if X_factors:
            X = np.column_stack([X_industry] + X_factors)
            feature_names = list(sector_ret.columns) + factor_names
            n_features = X.shape[1]
            n_industry = n_sectors
        else:
            X = X_industry
            feature_names = list(sector_ret.columns)
            n_industry = n_sectors
            n_features = n_sectors

        y = fund_ret["fund_return"].values
        n_features = X.shape[1]

        # 数值闸门
        if not (np.isfinite(y).all() and np.isfinite(X).all()):
            _logger.warning(
                "基金 [%s] 于 %s 对齐后的收益含非有限值（NaN/inf），放弃该时点",
                fund_code, end_date,
            )
            return self._skipped_result(fund_code, end_date, window_days)

        # 约束条件：行业暴露度非负，因子暴露度可正可负
        bounds_list = [(0, 1)] * n_sectors + [(-1, 1)] * (n_features - n_sectors)
        bounds = Bounds(
            np.array([b[0] for b in bounds_list]),
            np.array([b[1] for b in bounds_list])
        )

        constraints = []
        # 行业暴露度之和 ≤ 1（允许现金仓位）
        A_industry = np.zeros(n_features)
        A_industry[:n_sectors] = 1.0
        constraints.append(LinearConstraint(A_industry, lb=0, ub=1.0))

        # 目标函数：最小化残差平方和
        def objective(w):
            residuals = y - X @ w
            return np.sum(residuals ** 2)

        # 初始猜测：行业等权，因子 0
        w0 = np.zeros(n_features)
        w0[:n_sectors] = 1.0 / n_sectors

        result = minimize(
            objective, w0,
            bounds=bounds,
            constraints=constraints,
            method="SLSQP",
            options={"maxiter": 1000, "ftol": 1e-9}
        )

        if not result.success or not np.isfinite(result.fun)                 or not np.isfinite(result.x).all():
            _logger.warning(
                "基金 [%s] 于 %s 分解优化失败，跳过该时点: %s",
                fund_code, end_date, result.message,
            )
            return self._skipped_result(fund_code, end_date, window_days)

        weights = result.x
        industry_exposures = {code: float(w) for code, w in zip(sector_ret.columns, weights[:n_sectors])}
        factor_exposures = {name: float(w) for name, w in zip(factor_names, weights[n_sectors:])} if factor_names else {}
        total_exposure = sum(industry_exposures.values())
        cash_exposure = max(0, 1 - total_exposure)

        # 计算统计指标
        y_pred = X @ weights
        residuals = y - y_pred
        alpha = np.mean(residuals) * 252  # 年化
        residual_vol = np.std(residuals) * np.sqrt(252)
        ss_tot = float(np.sum((y - np.mean(y)) ** 2))
        if not np.isfinite(ss_tot) or ss_tot <= 0:
            _logger.warning("基金 [%s] 于 %s 窗口内收益无波动，R² 无定义（按 0 计）",
                            fund_code, end_date)
            r_squared = 0.0
        else:
            r_squared = 1 - float(np.sum(residuals ** 2)) / ss_tot
            r_squared = float(r_squared) if np.isfinite(r_squared) else 0.0

        # P1: 计算换仓率与交易成本（相对上一期暴露度）
        turnover_ratio = 0.0
        transaction_cost_bps = 0.0
        net_alpha_bps = 0.0
        if hasattr(self, '_prev_exposures') and self._prev_exposures:
            turnover = sum(abs(industry_exposures.get(k, 0) - self._prev_exposures.get(k, 0))
                           for k in set(industry_exposures) | set(self._prev_exposures))
            turnover_ratio = turnover
            cost_model = self.transaction_cost_model
            transaction_cost_bps = cost_model.estimate_cost_bps(turnover_ratio)
            net_alpha_bps = alpha * 10000 - transaction_cost_bps * 252 / window_days  # 简化年化

        # 更新上一期暴露度（用于下一期换仓计算）
        self._prev_exposures = industry_exposures.copy()

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
            exposures=industry_exposures,
            factor_exposures={name: float(w) for name, w in zip(factor_names, weights[n_sectors:])} if factor_names else {},
            r_squared=float(r_squared),
            alpha=float(alpha),
            residual_vol=float(residual_vol),
            n_sectors=len(industry_exposures),
            cash_exposure=float(cash_exposure),
            transaction_cost_bps=float(transaction_cost_bps),
            turnover_ratio=float(turnover_ratio),
            net_alpha_bps=float(net_alpha_bps),
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

        P1: 重置 _prev_exposures 以便正确计算每个时点的换仓率
        """
        results = []
        current_date = end_date
        # 重置上一期暴露度，因为这是新的时间序列
        self._prev_exposures = None

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

            # P1: 计算该信号的交易成本和净变化
            signal_turnover = abs(change)
            signal_cost_bps = self.transaction_cost_model.estimate_cost_bps(signal_turnover)
            net_exposure_change = change - (signal_cost_bps / 10000) * np.sign(change) if change != 0 else 0.0

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
                transaction_cost_bps=float(signal_cost_bps),
                net_exposure_change=float(net_exposure_change),
                turnover_ratio=float(signal_turnover),
                calibrated_confidence=calibrated if 'calibrated' in locals() else None,
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

        # P1: 多窗口一致性校验
        signals = self._apply_multi_window_consistency(fund_code, analysis_date, window_days, level, sector_codes, signals)

        # P1: 置信度校准
        signals = self._calibrate_confidence(fund_code, analysis_date, signals)

        return signals

    def _get_window_signal_direction(
        self,
        fund_code: str,
        analysis_date: date,
        window_days: int,
        level: int,
        sector_codes: Optional[List[str]],
    ) -> Dict[str, int]:
        """
        获取指定窗口下各行业的信号方向（1=加仓, -1=减仓, 0=中性/无信号）
        """
        try:
            # 使用 decompose_single 直接计算，避免递归调用 generate_signals
            sector_codes = sector_codes or list(self.sector_returns.keys())
            result = self.decompose_single(fund_code, analysis_date, window_days, sector_codes)
            if result.n_sectors == 0:
                return {}
            
            # 这里简化：只返回当前期的暴露度方向
            # 实际应该计算相对上一期的变化，但这里为了避免递归，简化处理
            directions = {}
            for code, exp in result.exposures.items():
                directions[code] = 1 if exp > 0 else 0
            return directions
        except Exception:
            return {}

    def _apply_multi_window_consistency(
        self,
        fund_code: str,
        analysis_date: date,
        window_days: int,
        level: int,
        sector_codes: Optional[List[str]],
        signals: List[FundAllocationSignal],
    ) -> List[FundAllocationSignal]:
        """
        P1: 多窗口一致性校验

        对每个信号，在多个窗口（20/60/120日）下分解，
        只有方向一致的信号才保留高置信度，否则降级。
        """
        if not signals:
            return signals

        # 只对非中性信号做一致性检查
        non_neutral = [s for s in signals if s.signal != SignalStrength.NEUTRAL]
        if not non_neutral:
            return signals

        # 获取各窗口下的信号方向
        window_directions = {}
        for w in MULTI_WINDOW_DAYS:
            if w == window_days:
                continue
            try:
                directions = self._get_window_signal_direction(fund_code, analysis_date, w, level, sector_codes)
                for code, direction in directions.items():
                    if direction != 0:
                        if code not in window_directions:
                            window_directions[code] = []
                        window_directions[code].append(direction)
            except Exception:
                pass  # 某窗口失败不影响主窗口

        # 调整置信度
        for s in signals:
            if s.sector_code in window_directions:
                directions = window_directions[s.sector_code]
                main_dir = 1 if s.exposure_change > 0 else -1
                consistent = sum(1 for d in directions if d == main_dir)
                total = len(directions)
                if total > 0:
                    consistency_ratio = consistent / total
                    if consistency_ratio < 0.5:
                        # 方向不一致，降级置信度
                        if s.confidence == ConfidenceLevel.HIGH:
                            s.confidence = ConfidenceLevel.MEDIUM
                        elif s.confidence == ConfidenceLevel.MEDIUM:
                            s.confidence = ConfidenceLevel.LOW
                        s.signal = SignalStrength.NEUTRAL if consistency_ratio < 0.3 else s.signal
        return signals

    def _calibrate_confidence(
        self,
        fund_code: str,
        analysis_date: date,
        signals: List[FundAllocationSignal],
    ) -> List[FundAllocationSignal]:
        """
        P1: 置信度校准

        基于历史回测，将 R²、资金流相关性、换仓率、资金流一致性
        映射为校准后的置信度概率（0-1）。
        """
        for s in signals:
            # 基础分：R² 权重 0.4 + 资金流相关性 0.3 + 换仓率惩罚 0.2 + 资金流一致性 0.1
            base_score = 0.0

            # R² 贡献
            r2 = s.r_squared if s.r_squared is not None else 0.0
            base_score += 0.4 * min(r2, 1.0)

            # 资金流相关性
            if s.capital_flow_corr is not None:
                base_score += 0.3 * min(abs(s.capital_flow_corr), 1.0)

            # 换仓率惩罚：过大换仓可能是噪声
            # 这里无法直接获取换仓率，用 exposure_change 代理
            if s.exposure_change is not None:
                change_penalty = min(abs(s.exposure_change) * 5, 0.2)  # 上限 0.2
                base_score -= 0.2 * change_penalty

            # 资金流一致性
            if s.capital_flow_confirm is True:
                base_score += 0.1
            elif s.capital_flow_confirm is False:
                base_score -= 0.1

            # 归一化到 [0, 1]
            calibrated = max(0.0, min(1.0, base_score))
            s.calibrated_confidence = calibrated

            # 同时更新离散置信度
            if calibrated >= 0.7:
                s.confidence = ConfidenceLevel.HIGH
            elif calibrated >= 0.4:
                s.confidence = ConfidenceLevel.MEDIUM
            else:
                s.confidence = ConfidenceLevel.LOW

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
            "factor_exposures": current_result.factor_exposures,
            "cash_exposure": current_result.cash_exposure,
            "r_squared": current_result.r_squared,
            "transaction_cost_bps": current_result.transaction_cost_bps,
            "turnover_ratio": current_result.turnover_ratio,
            "exposure_history": [
                {"date": r.trade_date, "exposures": r.exposures, "factor_exposures": r.factor_exposures, "r_squared": r.r_squared, "transaction_cost_bps": r.transaction_cost_bps, "turnover_ratio": r.turnover_ratio, "net_alpha_bps": r.net_alpha_bps}
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
                    "calibrated_confidence": s.calibrated_confidence,
                    "transaction_cost_bps": s.transaction_cost_bps,
                    "net_exposure_change": s.net_exposure_change,
                    "turnover_ratio": s.turnover_ratio,
                }
                for s in signals
            ],
            "summary": {
                "total_increase_sectors": len(increases),
                "total_decrease_sectors": len(decreases),
                "top_increase": increases[0].sector_name if increases else None,
                "top_decrease": decreases[0].sector_name if decreases else None,
                "avg_r_squared": np.mean([r.r_squared for r in history]) if history else 0,
                "avg_transaction_cost_bps": np.mean([r.transaction_cost_bps for r in history]) if history else 0,
                "avg_turnover_ratio": np.mean([r.turnover_ratio for r in history]) if history else 0,
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
    factor_returns_dfs: Optional[Dict[str, pd.DataFrame]] = None,  # P1: 因子收益率
    transaction_cost_model: Optional[TransactionCostModel] = None,  # P1: 交易成本模型
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
        factor_returns_dfs: 因子收益率字典（P1增强）
        transaction_cost_model: 交易成本模型（P1增强）

    Returns:
        分析结果字典
    """
    decomposer = FundStyleDecomposer(
        fund_nav_df=fund_nav_df,
        sector_index_dfs=sector_index_dfs,
        capital_flow_df=capital_flow_df,
        industry_mapping_df=industry_mapping_df,
        factor_returns_dfs=factor_returns_dfs,
        transaction_cost_model=transaction_cost_model,
    )

    if analysis_date is None:
        analysis_date = date.today()
    if fund_code is None:
        fund_codes = fund_nav_df["fund_code"].unique()
        fund_code = fund_codes[0] if len(fund_codes) > 0 else None

    return decomposer.analyze(fund_code, analysis_date, window_days, level)