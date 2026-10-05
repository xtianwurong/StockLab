#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金调仓分析门面 (stocklab.facade.fund_analysis)
==============================================================================

【模块职责】
   FundAnalysisFacade：基金调仓分析页的**唯一取数入口**。
   纯本地读 —— 语料由同步脚本落库，门面不发任何远端请求。

   - get_sw_indices()         申万一/二级行业指数列表
   - get_industry_mapping()   行业分类映射
   - get_fund_list()          基金列表（可按类型筛选）
   - get_fund_info()          基金基本信息
   - get_fund_nav()           基金净值历史
   - get_sector_indices()     板块指数日线
   - get_capital_flow()       板块资金流
   - analyze_fund_allocation() 基金调仓分析（核心）
   - get_allocation_history() 历史分析结果
"""


import logging
from datetime import date, timedelta
from typing import Optional, List, Dict

import pandas as pd

from stocklab.analytics.fund_style import (
    required_history_days,
    FundStyleDecomposer,
    decompose_fund_style,
    FundAllocationSignal,
    SignalStrength,
    ConfidenceLevel,
)
from stocklab.domain import CAPITAL_FLOW_DAILY_COLUMNS
from stocklab.persistence.repository.fund_analysis import (
    SWIndexDailyRepository,
    SWIndustryMappingRepository,
    FundInfoRepository,
    FundNavHistoryRepository,
    CapitalFlowDailyRepository,
    FundAllocationAnalysisRepository,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "FundAnalysisFacade",
    "SignalStrength",
    "ConfidenceLevel",
    "FundAllocationSignal",
]

# 默认参数
DEFAULT_WINDOW_DAYS = 60
DEFAULT_LEVEL = 1
MIN_R_SQUARED = 0.5
MAX_SECTORS_LEVEL1 = 31
MAX_SECTORS_LEVEL2 = 120


class FundAnalysisFacade:
    """基金调仓分析门面（纯本地读）"""

    def __init__(self, database=None):
        self._sw_index_repo = SWIndexDailyRepository(database)
        self._mapping_repo = SWIndustryMappingRepository(database)
        self._fund_info_repo = FundInfoRepository(database)
        self._fund_nav_repo = FundNavHistoryRepository(database)
        self._capital_flow_repo = CapitalFlowDailyRepository(database)
        self._alloc_repo = FundAllocationAnalysisRepository(database)

    # -------------------------------------------------------------------------
    # 基础数据查询
    # -------------------------------------------------------------------------

    def get_sw_indices(self, level: Optional[int] = None) -> pd.DataFrame:
        """获取申万行业指数列表"""
        if level:
            return self._mapping_repo.find_by_level(level)
        return self._mapping_repo.find_all()

    def get_industry_mapping(self) -> pd.DataFrame:
        """获取完整行业映射表"""
        return self._mapping_repo.find_all()

    def get_fund_list(self, fund_type: str = None, status: str = "active") -> pd.DataFrame:
        """获取基金列表"""
        if fund_type:
            return self._fund_info_repo.find_by_type(fund_type)
        return self._fund_info_repo.find_by_code("")  # 空字符串返回全部

    def get_fund_info(self, fund_code: str) -> pd.DataFrame:
        return self._fund_info_repo.find_by_code(fund_code)

    def get_fund_nav(self, fund_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """获取基金净值历史"""
        since = None
        if start_date:
            since = pd.to_datetime(start_date).date()
        return self._fund_nav_repo.load_series([fund_code], since=since)

    def get_fund_nav_latest(self, fund_codes: List[str]) -> pd.DataFrame:
        return self._fund_nav_repo.latest_nav(fund_codes)

    def get_sector_indices(self, sector_codes: List[str], since: date = None) -> pd.DataFrame:
        return self._sw_index_repo.load_series(sector_codes, since)

    def get_capital_flow(self, sector_type: str, trade_date: str = None) -> pd.DataFrame:
        """
        板块资金流

        Args:
            trade_date: None = **库里的最新一天**（不是今天）。资金流是按天快照，
                按今天查在周末/假日/未同步时必然为空，表里有数据也显示没有。
        """
        if trade_date is None:
            trade_date = self.latest_capital_flow_date(sector_type)
            if trade_date is None:
                return pd.DataFrame(columns=list(CAPITAL_FLOW_DAILY_COLUMNS))
        return self._capital_flow_repo.load_by_type_and_date(sector_type, trade_date)

    def latest_capital_flow_date(self, sector_type: str):
        """该类型在库里的最新交易日；无数据返回 None"""
        return self._capital_flow_repo.latest_trade_date(sector_type)

    def get_capital_flow_series(self, sector_codes: List[str], since: date = None) -> pd.DataFrame:
        return self._capital_flow_repo.load_series(sector_codes, since)

    # -------------------------------------------------------------------------
    # 核心分析
    # -------------------------------------------------------------------------

    def _load_analysis_data(
        self,
        fund_code: str,
        analysis_date: date,
        window_days: int,
        level: int
    ) -> Dict:
        """加载分析所需的所有数据

        装载区间必须覆盖**整段滚动序列**（滚动往回 3×window，单期再回看
        1.5×window），而不是拍脑袋的 2.5×window：少装一天，滚动序列最早那批
        时点就会静默判「数据不足」，日志像历史缺失，其实是自己没装够。
        口径统一由 analytics.fund_style.required_history_days 给出。
        """
        since = analysis_date - timedelta(days=required_history_days(window_days))

        # 1. 行业映射
        mapping = self.get_industry_mapping()
        if level:
            sector_codes = mapping[mapping["level"] == level]["index_code"].tolist()
        else:
            sector_codes = mapping["index_code"].tolist()

        # 2. 板块指数日线
        sector_indices = {}
        for code in sector_codes:
            df = self._sw_index_repo.load_series([code], since=since)
            if not df.empty:
                sector_indices[code] = df[["trade_date", "close"]].copy()

        # 3. 基金净值
        fund_nav = self.get_fund_nav(fund_code, start_date=since.strftime("%Y-%m-%d"))

        # 4. 资金流（可选）
        capital_flow = None
        try:
            capital_flow = self._capital_flow_repo.load_series(
                sector_codes, since=since
            )
        except Exception as e:
            _logger.warning("资金流数据加载失败: %s", e)

        # 5. 本层级的行业映射（分析时用它筛 sector_codes 与回填行业名）
        mapping_df = mapping[mapping["level"] == level].copy()

        return {
            "fund_nav": fund_nav,
            "sector_indices": sector_indices,
            "capital_flow": capital_flow,
            "mapping": mapping_df,
            "sector_codes": sector_codes,
        }

    def analyze_fund_allocation(
        self,
        fund_code: str,
        analysis_date: Optional[date] = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
        level: int = DEFAULT_LEVEL,
    ) -> Dict:
        """
        基金调仓分析核心入口

        Args:
            fund_code: 基金代码，如 "000001.OF"
            analysis_date: 分析基准日，默认今天
            window_days: 回看窗口（交易日），默认 60
            level: 行业层级，1=一级行业，2=二级行业

        Returns:
            分析结果字典（含当前暴露度、历史时序、调仓信号、摘要）
        """
        if analysis_date is None:
            analysis_date = date.today()
        elif isinstance(analysis_date, str):
            analysis_date = pd.to_datetime(analysis_date).date()

        _logger.info("开始分析基金 [%s] 调仓：%s，窗口 %d 天，%d 级行业",
                     fund_code, analysis_date, window_days, level)

        # 加载数据
        data = self._load_analysis_data(fund_code, analysis_date, window_days, level)

        if data["fund_nav"].empty:
            return {
                "fund_code": fund_code,
                "error": "基金净值数据为空",
                "analysis_date": analysis_date,
            }

        # 执行分解
        result = decompose_fund_style(
            fund_nav_df=data["fund_nav"],
            sector_index_dfs=data["sector_indices"],
            capital_flow_df=data["capital_flow"],
            industry_mapping_df=data["mapping"],
            fund_code=fund_code,
            analysis_date=analysis_date,
            window_days=window_days,
            level=level,
        )

        # 保存分析结果到缓存表
        self._cache_analysis_result(fund_code, analysis_date, window_days, level, result)

        return result

    def _cache_analysis_result(
        self,
        fund_code: str,
        analysis_date: date,
        window_days: int,
        level: int,
        result: Dict
    ):
        """将分析结果写入缓存表"""
        try:
            records = []
            for signal in result.get("signals", []):
                records.append({
                    "fund_code": fund_code,
                    "analysis_date": analysis_date,
                    "window_days": window_days,
                    "level": level,
                    "sector_code": signal["sector_code"],
                    "sector_name": signal["sector_name"],
                    "exposure": signal["current_exposure"],
                    "exposure_change": signal["exposure_change"],
                    "exposure_change_5d": signal["exposure_change_5d"],
                    "exposure_change_20d": signal["exposure_change_20d"],
                    "r_squared": result.get("r_squared", 0),
                    "capital_flow_corr": signal["capital_flow_corr"],
                    "confidence": signal["confidence"],
                    "source": "FundAnalysisFacade",
                    "computed_at": pd.Timestamp.now(),
                })

            if records:
                df = pd.DataFrame(records)
                from stocklab.persistence.repository.fund_analysis import FundAllocationAnalysisRepository
                repo = FundAllocationAnalysisRepository(self._alloc_repo._db)
                repo.upsert(df)
        except Exception as e:
            _logger.warning("缓存分析结果失败: %s", e)

    def get_allocation_history(
        self,
        fund_code: str,
        analysis_date: str,
        window_days: int = DEFAULT_WINDOW_DAYS,
        level: int = DEFAULT_LEVEL,
    ) -> pd.DataFrame:
        """获取历史分析结果"""
        return self._alloc_repo.find_by_fund_and_date(
            fund_code, analysis_date, window_days, level
        )

    def get_latest_analysis(self, fund_code: str, window_days: int = DEFAULT_WINDOW_DAYS, level: int = DEFAULT_LEVEL) -> pd.DataFrame:
        return self._alloc_repo.latest_analysis(fund_code, window_days, level)