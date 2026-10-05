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
from datetime import date, timedelta
from typing import Optional, List, Dict, Literal

import numpy as np
import pandas as pd

_logger = logging.getLogger(__name__)

__all__ = [
    "BrinsonAttribution",
    "AttributionResult",
    "AttributionConfig",
    "FundAttributionEngine",
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


class FundAttributionEngine:
    """
    基金归因分析引擎（高层封装）

    【数据链路 —— 全部来自真实数据，缺数据即报错，不用估算值兜底】
      组合权重     fund.fund_holding（最新一期报告）× 本地行业映射
      基准权重     中证指数成分券权重 × 本地行业映射（reference.benchmark_industry_weights 快照）
      组合行业收益  持仓个股在归因区间的实际涨跌幅（本地行情优先，缺失回源并写回）
      基准行业收益  申万行业指数区间涨跌幅（sw.index_daily）

    【覆盖率口径】
      个股行情或行业指数缺失的行业会被剔除，并对两侧权重重新归一化；
      结果里用 coverage.portfolio / coverage.benchmark 如实披露被剔除的权重占比，
      绝不把「缺数据」悄悄记成 0 收益。

    【用法】
        engine = FundAttributionEngine(database)
        payload = engine.attribute_fund("110022.OF", date(2026, 9, 30))
        if "error" in payload: ...   # 由 Web 层转 400
    """

    def __init__(self, database=None, allow_remote: bool = True):
        from stocklab.facade.benchmark_data import BenchmarkDataFacade
        from stocklab.facade.fund_holding import FundHoldingFacade

        self._database = database
        self._holding_facade = FundHoldingFacade(database)
        self._benchmark_facade = BenchmarkDataFacade(database)
        self._allow_remote = allow_remote
        self._market_facade = None
        self._price_cache: Dict[str, Optional[float]] = {}
        self.attributor = BrinsonAttribution()

    # -------------------------------------------------------------------------
    # 对外主入口
    # -------------------------------------------------------------------------

    def attribute_fund(
        self,
        fund_code: str,
        trade_date,
        benchmark_code: str = "000300.SH",
        period_days: int = 90,
        level: int = 1,
        model: str = "brinson-fachler",
    ) -> Dict:
        """
        单只基金 Brinson 归因

        Args:
            fund_code: 基金代码 000001.OF
            trade_date: 归因终点日期
            benchmark_code: 基准指数代码
            period_days: 归因区间长度（天），区间为 [trade_date - period_days, trade_date]
            level: 行业层级 1 / 2
            model: brinson-fachler（三效应）或 brinson-hb（两效应）

        Returns:
            dict：成功返回归因明细；失败返回 {"error": ...}（由 Web 层转 400）
        """
        if model not in ("brinson-fachler", "brinson-hb"):
            return {"error": "参数 [model] 非法：仅支持 brinson-fachler / brinson-hb"}
        if level not in (1, 2):
            return {"error": "参数 [level] 非法：仅支持 1 / 2"}

        trade_dt = pd.Timestamp(trade_date).date()
        window_start = trade_dt - timedelta(days=int(period_days))
        warnings: List[str] = []

        # 1. 组合持仓与行业权重 -------------------------------------------------
        holding, report_date, hold_warning = self._latest_holding(fund_code, trade_dt)
        if holding is None or holding.empty:
            return {"error": "基金持仓数据为空，无法归因（请先同步基金持仓）"}
        if hold_warning:
            warnings.append(hold_warning)

        sector_of, sector_names = self._sector_maps(level)
        holding = holding.copy()
        holding["sector_code"] = holding["stock_code"].map(sector_of)
        mapped = holding[holding["sector_code"].notna()].copy()
        if mapped.empty:
            return {"error": "持仓个股均无行业映射，无法归因（请先同步股票行业映射）"}

        unmapped_weight = pd.to_numeric(
            holding.loc[holding["sector_code"].isna(), "weight"], errors="coerce"
        ).fillna(0.0).sum()
        if unmapped_weight > 1e-6:
            warnings.append(
                "持仓中 %.1f%% 权重的个股无行业映射，已剔除并对其余行业重新归一化"
                % (unmapped_weight * 100)
            )

        sector_totals = mapped.groupby("sector_code")["weight"].sum()
        total_mapped = float(sector_totals.sum())
        if total_mapped <= 0:
            return {"error": "持仓行业权重合计为 0，无法归因"}
        portfolio_weights_all = (sector_totals / total_mapped).to_dict()

        # 2. 基准行业权重 -------------------------------------------------------
        bench_frame, bench_meta = self._benchmark_facade.get_industry_weights(
            benchmark_code, level=level
        )
        if bench_frame.empty:
            reason = bench_meta.get("reason") or "基准行业权重快照缺失"
            return {"error": "基准权重不可用：%s" % reason}
        benchmark_weights_all = {
            str(row.sector_code): float(row.weight)
            for row in bench_frame.itertuples()
        }

        # 3. 组合行业收益（个股涨跌幅加权） ---------------------------------------
        portfolio_returns, warn_p = self._portfolio_sector_returns(
            mapped, window_start, window_end=trade_dt, sector_names=sector_names
        )
        warnings.extend(warn_p)

        # 4. 基准行业收益（申万行业指数） -----------------------------------------
        candidate_sectors = sorted(set(portfolio_weights_all) | set(benchmark_weights_all))
        benchmark_returns, warn_b = self._benchmark_sector_returns(
            candidate_sectors, window_start, trade_dt
        )
        warnings.extend(warn_b)

        # 5. 覆盖筛选：纳入标准分两侧 -----------------------------------------
        #   基准侧：必须有行业指数收益，否则算不出该行业的基准收益；
        #   组合侧：只有「真有持仓」的行业才要求个股行情齐全，零持仓行业本来就不需要个股行情。
        #
        # 绝不能取「两侧都有行情的交集」——基金空配的行业恰好在组合侧没有个股行情，
        # 会被整批剔除，于是配置效应只剩超配方向的贡献，把「低配了什么」从结论里删掉，
        # 等于让归因自证「没有做错仓位」。这类剔除必须只看数据有无，不看持仓有无。
        covered = []
        for sector in candidate_sectors:
            if sector not in benchmark_returns:
                continue
            if portfolio_weights_all.get(sector, 0.0) > 0 and sector not in portfolio_returns:
                continue
            covered.append(sector)
        if not covered:
            return {"error": "归因区间内缺少行情数据（个股或行业指数），无法计算归因"}

        portfolio_coverage = float(sum(portfolio_weights_all.get(s, 0.0) for s in covered))
        benchmark_coverage = float(sum(benchmark_weights_all.get(s, 0.0) for s in covered))
        if portfolio_coverage < 0.5 or benchmark_coverage < 0.5:
            warnings.append(
                "覆盖率偏低：组合 %.1f%% / 基准 %.1f%% 的权重进入归因，结论仅供参考"
                % (portfolio_coverage * 100, benchmark_coverage * 100)
            )
        excluded = [s for s in candidate_sectors if s not in covered]
        if excluded:
            warnings.append(
                "以下 %d 个行业因缺少行情数据被剔除：%s"
                % (len(excluded), "、".join(excluded[:8]) + ("…" if len(excluded) > 8 else ""))
            )

        portfolio_weights = {s: portfolio_weights_all.get(s, 0.0) for s in covered}
        benchmark_weights = {s: benchmark_weights_all.get(s, 0.0) for s in covered}
        benchmark_returns = {s: benchmark_returns[s] for s in covered}
        # 零持仓行业的组合收益没有观测值（基金不在这个行业里），令其等于基准收益：
        # 这样选择与交互项（都含 r_p - r_b 或 w_p 因子）自然为 0，
        # 只有配置项 (0 - w_b) * (r_b - R_b) 留下——低配的贡献被如实记在配置效应上。
        # 若错填 0，会凭空造出 -w_b * R_b 的假选择效应，把「没买」说成「买错了」。
        portfolio_returns = {
            s: portfolio_returns.get(s, benchmark_returns[s]) for s in covered
        }

        no_benchmark_weight = [
            s for s in covered
            if s in portfolio_weights_all and s not in benchmark_weights_all
        ]
        if no_benchmark_weight:
            warnings.append(
                "以下 %d 个持仓行业不在基准权重快照内，已按基准权重 0 处理：%s"
                % (
                    len(no_benchmark_weight),
                    "、".join(no_benchmark_weight[:8])
                    + ("…" if len(no_benchmark_weight) > 8 else ""),
                )
            )

        # 6. Brinson 分解 -------------------------------------------------------
        self.attributor = BrinsonAttribution(config=AttributionConfig(model=model))
        result = self.attributor.attribute(
            portfolio_weights=portfolio_weights,
            benchmark_weights=benchmark_weights,
            portfolio_returns=portfolio_returns,
            benchmark_returns=benchmark_returns,
            trade_date=trade_dt,
        )

        # 7. 组装返回 -----------------------------------------------------------
        sector_details = {}
        for sector, detail in result.sector_details.items():
            payload = dict(detail)
            payload["sector_code"] = sector
            payload["sector_name"] = sector_names.get(sector)
            sector_details[sector] = payload

        return {
            "fund_code": fund_code,
            "benchmark_code": benchmark_code,
            "trade_date": str(trade_dt),
            "period": {
                "start": str(window_start),
                "end": str(trade_dt),
                "days": int(period_days),
            },
            "level": int(level),
            "model": model,
            "report_date": str(report_date) if report_date else None,
            "total_return": result.total_return,
            "benchmark_return": result.benchmark_return,
            "excess_return": result.excess_return,
            "allocation_effect": result.allocation_effect,
            "selection_effect": result.selection_effect,
            "interaction_effect": result.interaction_effect,
            "coverage": {
                "portfolio": round(portfolio_coverage, 6),
                "benchmark": round(benchmark_coverage, 6),
            },
            "benchmark_meta": bench_meta,
            "sector_count": len(covered),
            "sector_details": sector_details,
            "warnings": warnings,
        }

    # -------------------------------------------------------------------------
    # 数据准备
    # -------------------------------------------------------------------------

    def _latest_holding(self, fund_code: str, as_of: date):
        """
        取归因日期之前（含当日）的最新一期持仓

        Returns:
            (DataFrame, report_date, warning)；无任何持仓返回 (None, None, None)
            归因日期早于最早报告期时退用最新报告期并附 PII 风险提示
        """
        history = self._holding_facade.get_fund_holding_history(fund_code)
        if history is None or history.empty:
            return None, None, None

        frame = history.copy()
        frame["_report"] = pd.to_datetime(frame["report_date"])
        eligible = sorted({ts.date() for ts in frame["_report"]})
        eligible_before = [d for d in eligible if d <= as_of]

        warning = None
        if eligible_before:
            report_date = eligible_before[-1]
            if report_date < max(eligible):
                warning = "使用 %s 报告期持仓（晚于该期的报告期尚未披露）" % report_date
        else:
            report_date = eligible[-1]
            warning = (
                "归因日期 %s 早于最早持仓报告期 %s，借用该报告期持仓（非严格 Point-in-Time）"
                % (as_of, report_date)
            )

        subset = frame[frame["_report"].dt.date == report_date].drop(columns=["_report"])
        return subset, report_date, warning

    def _sector_maps(self, level: int) -> tuple:
        """行业映射：返回 (ts_code -> sector_code, sector_code -> sector_name)"""
        mapping = self._holding_facade.get_stock_industry_mapping()
        if mapping is None or mapping.empty:
            return {}, {}

        code_col, name_col = "sw_l%d_code" % level, "sw_l%d_name" % level
        if code_col not in mapping.columns:
            return {}, {}

        sector_of = (
            mapping.dropna(subset=[code_col])
            .drop_duplicates("ts_code")
            .set_index("ts_code")[code_col]
            .astype(str)
            .to_dict()
        )
        sector_names = {}
        if name_col in mapping.columns:
            sector_names = (
                mapping.dropna(subset=[code_col, name_col])
                .drop_duplicates(code_col)
                .set_index(code_col)[name_col]
                .to_dict()
            )
        return sector_of, sector_names

    def _portfolio_sector_returns(
        self,
        holding: pd.DataFrame,
        window_start: date,
        window_end: date,
        sector_names: Dict[str, str],
    ) -> tuple:
        """
        组合行业收益 = 行业内个股区间涨跌幅按持仓权重加权

        Returns:
            (sector_returns: Dict[str, float], warnings: List[str])
            无行情的个股会导致其所在行业被剔除（由调用方按覆盖率披露）
        """
        codes = sorted(set(holding["stock_code"].astype(str)))
        prices, missing = self._load_stock_returns(codes, window_start, window_end)

        warnings: List[str] = []
        if missing:
            warnings.append(
                "%d/%d 只持仓个股在归因区间内无行情，其所在行业将被剔除：%s%s"
                % (len(missing), len(codes), "、".join(missing[:6]),
                   "…" if len(missing) > 6 else "")
            )

        returns: Dict[str, float] = {}
        priced_count = 0
        for sector_code, group in holding.groupby("sector_code"):
            group = group[group["stock_code"].astype(str).isin(prices)]
            if group.empty:
                continue
            weights = pd.to_numeric(group["weight"], errors="coerce").fillna(0.0)
            if weights.sum() <= 0:
                continue
            stock_codes = group["stock_code"].astype(str).tolist()
            returns[str(sector_code)] = float(
                np.average([prices[c] for c in stock_codes], weights=weights.values)
            )
            priced_count += 1

        _logger.debug("组合行业收益：%d 个行业有行情（区间 %s ~ %s）",
                      priced_count, window_start, window_end)
        return returns, warnings

    def _benchmark_sector_returns(
        self,
        sector_codes: List[str],
        window_start: date,
        window_end: date,
    ) -> tuple:
        """
        基准行业收益 = 申万行业指数区间涨跌幅（本地 sw.index_daily，缺失时回源并写回）

        Returns:
            (sector_returns: Dict[str, float], warnings: List[str])
            缺指数数据的行业不出现在返回值中，由调用方按覆盖率剔除
        """
        if not sector_codes:
            return {}, []

        from stocklab.persistence.repository.fund_analysis import SWIndexDailyRepository

        repo = SWIndexDailyRepository(self._database)
        symbols = {self._to_sw_symbol(code): code for code in sector_codes}
        try:
            series = repo.load_series(list(symbols), since=window_start)
        except Exception as exc:
            _logger.warning("申万行业指数读取失败：%s", exc)
            return {}, ["行业指数数据读取失败：%s" % exc]

        if series is None or series.empty:
            return {}, ["本地无申万行业指数日线数据，无法计算基准行业收益"]

        frame = series.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame[(frame["trade_date"] >= pd.Timestamp(window_start))
                      & (frame["trade_date"] <= pd.Timestamp(window_end))]

        returns: Dict[str, float] = {}
        for symbol, group in frame.groupby("symbol"):
            group = group.sort_values("trade_date")
            if len(group) < 2:
                continue
            first = float(group["close"].iloc[0])
            last = float(group["close"].iloc[-1])
            if first <= 0:
                continue
            sector_code = symbols.get(str(symbol))
            if sector_code:
                returns[str(sector_code)] = last / first - 1.0

        missing = [c for c in sector_codes if str(c) not in returns]
        if missing and self._allow_remote:
            # 本地只同步了快照涉及的一级行业，二级行业（或新加的行业）日线往往缺，
            # 与个股行情同一套策略：缺了才回源，抓到即写回，下次走本地
            missing = self._backfill_sw_index_returns(missing, returns, window_start, window_end, repo)

        warnings = []
        if missing:
            warnings.append(
                "%d 个行业缺少指数行情（区间 %s ~ %s）：%s%s"
                % (len(missing), window_start, window_end,
                   "、".join(missing[:6]), "…" if len(missing) > 6 else "")
            )
        return returns, warnings

    def _backfill_sw_index_returns(
        self,
        missing_codes: List[str],
        returns: Dict[str, float],
        window_start: date,
        window_end: date,
        repo,
    ) -> List[str]:
        """
        缺失行业指数的远端回源：抓取区间日线 → 写回本地 → 计算区间收益

        Returns:
            List[str]: 仍然取不到的行业代码（由调用方披露为 warning）
        """
        from stocklab.datasource.sw_indices import fetch_sw_index_daily

        start_text = window_start.isoformat()
        end_text = window_end.isoformat()
        still_missing: List[str] = []

        for code in missing_codes:
            symbol = self._to_sw_symbol(code)
            try:
                frame = fetch_sw_index_daily(symbol, start_text, end_text)
            except Exception as exc:
                _logger.debug("行业指数 %s 回源失败：%s", symbol, exc)
                still_missing.append(code)
                continue
            if frame is None or frame.empty:
                still_missing.append(code)
                continue

            try:
                repo.upsert(frame)
            except Exception as exc:
                _logger.debug("行业指数 %s 回写失败（不影响本次计算）：%s", symbol, exc)

            windowed = frame.copy()
            windowed["trade_date"] = pd.to_datetime(windowed["trade_date"])
            windowed = windowed[(windowed["trade_date"] >= pd.Timestamp(window_start))
                                & (windowed["trade_date"] <= pd.Timestamp(window_end))]
            value = self._interval_return(windowed.sort_values("trade_date"))
            if value is None:
                still_missing.append(code)
            else:
                returns[str(code)] = value
                _logger.info("行业指数 %s 回源补算区间收益 %.4f", symbol, value)

        return still_missing

    def _load_stock_returns(
        self,
        codes: List[str],
        window_start: date,
        window_end: date,
    ) -> tuple:
        """
        个股区间涨跌幅：本地日线优先，缺失时回源（allow_remote=True 且可写回）

        Returns:
            (returns: Dict[ts_code, float], missing: List[ts_code])
        """
        returns: Dict[str, float] = {}
        missing: List[str] = []
        start_text = window_start.isoformat()
        end_text = window_end.isoformat()

        local_repo = None
        if self._database is not None:
            from stocklab.persistence.repository.daily_price import DailyPriceRepository
            local_repo = DailyPriceRepository(self._database)

        for code in codes:
            code = str(code)
            if code in self._price_cache and self._price_cache[code] is not None:
                returns[code] = self._price_cache[code]
                continue

            value = None
            try:
                if local_repo is not None:
                    value = self._interval_return(
                        local_repo.find_by_code(code, start_text, end_text)
                    )
                if value is None and self._allow_remote:
                    # 门面内部为「本地优先 + 缺失回源写回」，此处只补本地缺口
                    frame = self._market().fetch_daily_prices(code, start_text, end_text)
                    value = self._interval_return(frame)
                    if value is None and frame is not None and not frame.empty:
                        # 局部命中：本地有行但凑不满 2 个点（历史残留的零星测试数据），
                        # 门面按「有行就算命中」不会回源——此时强制回源，
                        # 否则「本地残缺」会被读成「这只股票整段没行情」，行业被无谓剔除。
                        value = self._interval_return(
                            self._market().fetch_daily_prices(
                                code, start_text, end_text, force_remote=True
                            )
                        )
            except Exception as exc:
                _logger.debug("个股 %s 行情读取失败：%s", code, exc)
                value = None

            if value is None:
                missing.append(code)
            else:
                returns[code] = value
                self._price_cache[code] = value

        return returns, missing

    @staticmethod
    def _interval_return(frame: Optional[pd.DataFrame]) -> Optional[float]:
        """日线帧 -> 区间简单收益率；数据不足返回 None"""
        if frame is None or frame.empty or "close" not in frame.columns:
            return None
        close_col = "close"
        if "trade_date" in frame.columns:
            frame = frame.sort_values("trade_date")
        if len(frame) < 2:
            return None
        first = float(frame[close_col].iloc[0])
        last = float(frame[close_col].iloc[-1])
        if first <= 0:
            return None
        return last / first - 1.0

    @staticmethod
    def _to_sw_symbol(sector_code: str) -> str:
        """801010 -> 801010.SI（已是带后缀的代码则原样返回）"""
        code = str(sector_code)
        return code if ".SI" in code else f"{code}.SI"

    def _market(self):
        """懒加载行情门面（避免构造时占用数据库连接）"""
        if self._market_facade is None:
            from stocklab.facade import MarketDataFacade
            self._market_facade = MarketDataFacade(priority="local_first")
        return self._market_facade

    def close(self):
        """释放行情门面持有的数据库连接"""
        if self._market_facade is not None:
            try:
                self._market_facade.close()
            except Exception:  # pragma: no cover - 释放失败不影响主流程
                pass
            self._market_facade = None
