#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金持仓与行业暴露门面 (stocklab.facade.fund_holding)
==============================================================================

【模块职责】
   FundHoldingFacade：基金持仓与真实行业暴露的**纯本地读**门面。

   - get_stock_industry_mapping()      股票行业映射表
   - get_fund_holding()                基金持仓历史
   - get_latest_holding()              最新一期持仓
   - get_industry_exposure()           真实行业暴露（按报告期）
   - get_industry_exposure_daily()     真实行业暴露日线（插值后）
   - compute_industry_exposure_from_holding()  由持仓计算行业暴露
   - compare_rbsa_vs_holding()         RBSA 反推 vs 持仓实测 双轨对比
"""


import logging
from datetime import date, timedelta
from typing import Optional, List, Dict

import numpy as np
import pandas as pd

from stocklab.domain import FUND_INDUSTRY_EXPOSURE_DAILY_COLUMNS
from stocklab.persistence.repository.fund_holding import (
    StockIndustryMappingRepository,
    FundHoldingRepository,
    FundIndustryExposureRepository,
    FundIndustryExposureDailyRepository,
)
from stocklab.persistence.repository.fund_analysis import FundNavHistoryRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "FundHoldingFacade",
]

# 默认参数
DEFAULT_LEVEL = 1


class FundHoldingFacade:
    """基金持仓与行业暴露门面（纯本地读）"""

    def __init__(self, database=None):
        self._industry_repo = StockIndustryMappingRepository(database)
        self._holding_repo = FundHoldingRepository(database)
        self._exposure_repo = FundIndustryExposureRepository(database)
        self._daily_exposure_repo = FundIndustryExposureDailyRepository(database)
        self._nav_repo = FundNavHistoryRepository(database)

    # -------------------------------------------------------------------------
    # 静态映射
    # -------------------------------------------------------------------------

    def get_stock_industry_mapping(self) -> pd.DataFrame:
        """获取全市场股票行业映射表"""
        return self._industry_repo.find_all()

    def get_sw_l1_map(self) -> Dict[str, str]:
        """返回 ts_code -> sw_l1_code 映射"""
        return self._industry_repo.get_sw_l1_map()

    def get_sw_l2_map(self) -> Dict[str, str]:
        return self._industry_repo.get_sw_l2_map()

    # -------------------------------------------------------------------------
    # 基金持仓查询
    # -------------------------------------------------------------------------

    def get_fund_holding(self, fund_code: str, report_date: date = None) -> pd.DataFrame:
        """获取基金持仓"""
        if report_date:
            return self._holding_repo.find_by_fund_and_date(fund_code, report_date)
        return self._holding_repo.find_latest_by_fund(fund_code)

    def get_fund_holding_history(self, fund_code: str) -> pd.DataFrame:
        """获取基金完整持仓历史"""
        return self._holding_repo.find_by_fund(fund_code)

    def get_latest_report_date(self, fund_code: str) -> Optional[date]:
        return self._holding_repo.get_latest_report_date(fund_code)

    def get_report_dates(self, fund_code: str) -> List[date]:
        return self._holding_repo.find_report_dates(fund_code)

    # -------------------------------------------------------------------------
    # 行业暴露（真实持仓推导）
    # -------------------------------------------------------------------------

    def get_industry_exposure(
        self,
        fund_code: str,
        report_date: date = None,
        level: int = 1
    ) -> pd.DataFrame:
        """
        获取基金在某报告期的真实行业暴露

        【Cache-Aside】表里没有就由持仓现算并回写：fund_industry_exposure 是预计算表，
        但没有常驻任务在维护它，直读空表会把「没预计算」包装成「这只基金没有行业暴露」，
        页面上就是一行空白，用户无从分辨是数据缺失还是查询错了。
        """
        if report_date is None:
            report_date = self._holding_repo.get_latest_report_date(fund_code)
            if report_date is None:
                return pd.DataFrame()

        frame = self._exposure_repo.find_by_fund_and_date(fund_code, report_date, level)
        if not frame.empty:
            return frame

        frame = self.compute_industry_exposure_from_holding(fund_code, report_date, level)
        if frame.empty:
            return frame
        try:
            self._exposure_repo.upsert(frame)
        except Exception:
            _logger.warning(
                "基金 %s 行业暴露回写失败（不影响本次返回）", fund_code, exc_info=True
            )
        return frame

    def get_industry_exposure_daily(
        self,
        fund_code: str,
        level: int = 1,
        since: date = None,
        until: date = None
    ) -> pd.DataFrame:
        """获取基金行业暴露日线（插值后）；缓存未命中时现算并回写"""
        if since is None:
            since = date.today() - timedelta(days=365)
        if until is None:
            until = date.today()

        frame = self._daily_exposure_repo.load_series(
            fund_codes=[fund_code], level=level, since=since, until=until
        )
        if not frame.empty:
            return frame

        frame = self.interpolate_exposure_to_daily(
            fund_code, level=level, start_date=since, end_date=until
        )
        if frame.empty:
            return frame
        try:
            self._daily_exposure_repo.upsert(frame)
        except Exception:
            _logger.warning(
                "基金 %s 行业暴露日线回写失败（不影响本次返回）", fund_code, exc_info=True
            )
        return frame

    def get_latest_exposure(self, fund_code: str, level: int = 1) -> pd.DataFrame:
        return self._daily_exposure_repo.find_latest(fund_code, level)

    # -------------------------------------------------------------------------
    # 核心计算：由持仓推导行业暴露
    # -------------------------------------------------------------------------

    def compute_industry_exposure_from_holding(
        self,
        fund_code: str,
        report_date: date,
        level: int = 1,
        sw_map: Dict[str, str] = None
    ) -> pd.DataFrame:
        """
        由基金持仓推导行业暴露

        Args:
            fund_code: 基金代码
            report_date: 报告期
            level: 行业层级 1/2/3
            sw_map: 可选，预加载的 ts_code -> sw_code 映射

        Returns:
            pd.DataFrame: sector_code, sector_name, weight, source='holding'
        """
        # 1. 获取持仓
        holding = self.get_fund_holding(fund_code, report_date)
        if holding.empty:
            _logger.warning("基金 %s 在 %s 无持仓数据", fund_code, report_date)
            return pd.DataFrame()

        # 2. 获取行业映射
        if sw_map is None:
            if level == 1:
                sw_map = self._industry_repo.get_sw_l1_map()
            elif level == 2:
                sw_map = self._industry_repo.get_sw_l2_map()
            else:
                _logger.warning("暂不支持 level=%d", level)
                return pd.DataFrame()

        # 3. Join 持仓 × 行业映射
        holding = holding.copy()
        holding["sector_code"] = holding["stock_code"].map(sw_map)

        # 过滤掉无映射的股票
        mapped = holding[holding["sector_code"].notna()].copy()
        unmapped = holding[holding["sector_code"].isna()]
        if not unmapped.empty:
            _logger.warning("基金 %s 有 %d 只股票无行业映射: %s",
                           fund_code, len(unmapped), unmapped["stock_code"].tolist())

        if mapped.empty:
            return pd.DataFrame()

        # 4. 按行业汇总权重
        exposure = mapped.groupby("sector_code")["weight"].sum().reset_index()
        exposure.columns = ["sector_code", "weight"]

        # 补充行业名称
        mapping_repo = self._industry_repo
        if level == 1:
            name_map = dict(zip(mapping_repo.find_all()["sw_l1_code"], mapping_repo.find_all()["sw_l1_name"]))
        elif level == 2:
            name_map = dict(zip(mapping_repo.find_all()["sw_l2_code"], mapping_repo.find_all()["sw_l2_name"]))
        else:
            name_map = {}

        exposure["sector_name"] = exposure["sector_code"].map(name_map).fillna(exposure["sector_code"])
        exposure["fund_code"] = fund_code
        exposure["report_date"] = pd.Timestamp(report_date).date()
        exposure["level"] = level
        exposure["source"] = "holding"

        # 归一化：权重归一化到 1（忽略未映射部分）
        total_weight = exposure["weight"].sum()
        if total_weight > 0:
            exposure["weight"] = exposure["weight"] / total_weight

        exposure = exposure.sort_values("weight", ascending=False).reset_index(drop=True)
        return exposure[["fund_code", "report_date", "level", "sector_code", "sector_name", "weight"]].assign(source="holding")

    def compute_all_report_dates_exposure(self, fund_code: str, level: int = 1) -> pd.DataFrame:
        """计算基金所有报告期的行业暴露"""
        report_dates = self.get_report_dates(fund_code)
        if not report_dates:
            return pd.DataFrame()

        sw_map = self._industry_repo.get_sw_l1_map() if level == 1 else self._industry_repo.get_sw_l2_map()

        results = []
        for report_date in report_dates:
            exp = self.compute_industry_exposure_from_holding(fund_code, report_date, level)
            if not exp.empty:
                results.append(exp)

        if not results:
            return pd.DataFrame()

        return pd.concat(results, ignore_index=True)

    # -------------------------------------------------------------------------
    # 插值生成日线暴露
    # -------------------------------------------------------------------------

    def interpolate_exposure_to_daily(
        self,
        fund_code: str,
        level: int = 1,
        start_date: date = None,
        end_date: date = None
    ) -> pd.DataFrame:
        """
        将报告期离散的行业暴露线性插值到每日

        Args:
            fund_code: 基金代码
            level: 行业层级
            start_date: 起始日期
            end_date: 结束日期

        Returns:
            pd.DataFrame: fund_industry_exposure_daily 格式
        """
        # 1. 获取所有报告期暴露
        exposure_df = self.compute_all_report_dates_exposure(fund_code, level)
        if exposure_df.empty:
            return pd.DataFrame()

        # 2. 构造日期范围
        if end_date is None:
            end_date = date.today()
        if start_date is None:
            start_date = exposure_df["report_date"].min()

        all_dates = pd.date_range(start_date, end_date, freq="D")
        trade_dates = [d.date() for d in all_dates]

        # 插值不得早于最早报告期：那一天之前我们并不知道组合长什么样，
        # 把 6 月底持仓往前铺满全年，图上就变成「1 月就重仓银行」的伪事实。
        first_report = pd.to_datetime(exposure_df["report_date"]).min().date()
        trade_dates = [d for d in trade_dates if d >= first_report]
        if not trade_dates:
            return pd.DataFrame()

        # 3. 按行业分别插值
        sectors = exposure_df["sector_code"].unique()
        daily_rows = []

        for sector_code in sectors:
            sector_df = exposure_df[exposure_df["sector_code"] == sector_code].sort_values("report_date")
            sector_name = sector_df["sector_name"].iloc[0]

            # 报告期点
            x = pd.to_datetime(sector_df["report_date"]).astype(np.int64) // 10**9
            y = sector_df["weight"].values

            if len(x) < 2:
                # 单点：前向填充
                interp_y = np.full(len(trade_dates), y[0])
            else:
                # 线性插值
                from scipy.interpolate import interp1d
                f = interp1d(x, y, kind="linear", bounds_error=False, fill_value=(y[0], y[-1]))
                interp_y = f(pd.to_datetime(trade_dates).astype(np.int64) // 10**9)

            for d, w in zip(trade_dates, interp_y):
                if w > 1e-6:  # 过滤极小权重
                    daily_rows.append({
                        "fund_code": sector_df["fund_code"].iloc[0],
                        "trade_date": d,
                        "level": sector_df["level"].iloc[0],
                        "sector_code": sector_code,
                        "sector_name": sector_df["sector_name"].iloc[0],
                        "weight": float(w),
                        "source": "holding_interpolated",
                    })

        if not daily_rows:
            return pd.DataFrame()

        df = pd.DataFrame(daily_rows)
        # 常量是 tuple，直接 df[tuple] 会被当成多级索引取列报 KeyError，必须转 list
        return df[list(FUND_INDUSTRY_EXPOSURE_DAILY_COLUMNS)]

    # -------------------------------------------------------------------------
    # 双轨对比：RBSA 反推 vs 持仓实测
    # -------------------------------------------------------------------------

    def compare_rbsa_vs_holding(
        self,
        fund_code: str,
        analysis_date: date,
        window_days: int = 60,
        level: int = 1
    ) -> Dict:
        """
        RBSA 反推暴露度 vs 持仓实测暴露度 双轨对比

        Returns:
            {
                "fund_code": ...,
                "analysis_date": ...,
                "rbsa_exposures": {sector: weight},
                "holding_exposures": {sector: weight},
                "diff": {sector: rbsa - holding},
                "correlation": float,  # 两套暴露度的相关性
                "top_diffs": [...],    # 差异最大的行业
                "r_squared": float,    # RBSA 拟合优度
                "coverage": float,     # 持仓映射覆盖率
            }
        """
        from stocklab.facade.fund_analysis import FundAnalysisFacade

        # 1. RBSA 反推
        analysis_facade = FundAnalysisFacade(self._exposure_repo._db)
        rbsa_result = analysis_facade.analyze_fund_allocation(
            fund_code=fund_code,
            analysis_date=analysis_date,
            window_days=60,
            level=1,
        )

        if "error" in rbsa_result:
            return {"error": rbsa_result["error"]}

        rbsa_exposures = rbsa_result.get("current_exposures", {})
        r_squared = rbsa_result.get("r_squared", 0)

        # 2. 持仓实测（取最接近分析日期的报告期）
        latest_report = self._holding_repo.get_latest_report_date(fund_code)
        holding_exp = self.get_industry_exposure(fund_code, latest_report, level=1)
        holding_exposures = dict(zip(holding_exp["sector_code"], holding_exp["weight"])) if not holding_exp.empty else {}

        # 3. 对比
        all_sectors = set(rbsa_exposures.keys()) | set(holding_exposures.keys())
        diff = {}
        for sec in all_sectors:
            diff[sec] = rbsa_exposures.get(sec, 0) - holding_exposures.get(sec, 0)

        # 相关性
        common = set(rbsa_exposures.keys()) & set(holding_exposures.keys())
        if len(common) >= 3:
            rbsa_vals = [rbsa_exposures[s] for s in common]
            hold_vals = [holding_exposures[s] for s in common]
            correlation = float(np.corrcoef(rbsa_vals, hold_vals)[0, 1])
        else:
            correlation = np.nan

        # 持仓映射覆盖率
        rbsa_sectors = set(rbsa_exposures.keys())
        holding_sectors = set(holding_exposures.keys())
        coverage = len(rbsa_sectors & holding_sectors) / len(rbsa_sectors) if rbsa_sectors else 0

        # Top diffs
        top_diffs = sorted(diff.items(), key=lambda x: abs(x[1]), reverse=True)[:10]
        top_diffs = [{"sector_code": k, "diff": v} for k, v in top_diffs]

        return {
            "fund_code": fund_code,
            "analysis_date": str(analysis_date),
            "report_date": str(latest_report) if latest_report else None,
            "rbsa_exposures": rbsa_exposures,
            "holding_exposures": holding_exposures,
            "diff": diff,
            "correlation": float(correlation) if not np.isnan(correlation) else None,
            "top_diffs": top_diffs,
            "r_squared": r_squared,
            "coverage": coverage,
        }