#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基准指数数据门面 (stocklab.facade.benchmark_data)
==============================================================================

【模块职责】
   BenchmarkDataFacade：Brinson 归因所需「基准指数行业权重」的统一入口。

   - get_industry_weights()   读取行业权重快照（缓存优先，缺失时回源抓取并落库）
   - refresh_industry_weights() 强制回源刷新快照（同步脚本使用）
   - get_benchmark_return()   基准指数区间收益率（本地日线优先）

【为何缓存优先】
   成分券权重来自中证指数公司（季频披露），抓取需访问外网；Web 层调用
   频率高，若每次实时抓取会让接口随外网抖动超时。快照落库后本门面读本地，
   仅在缓存缺失或显式 refresh 时回源，兼顾「不编数」与「接口稳定」。

【不编数原则】
   缓存缺失且回源失败时返回**空快照**并带 reason，由上层把原因透给用户，
   绝不用估算权重兜底。
"""

import logging
from datetime import date
from typing import Dict, Optional, Tuple

import pandas as pd

from stocklab.datasource.benchmark_index import (
    BENCHMARK_INDEX_CODES,
    CSI_BENCHMARK_CODES,
    aggregate_industry_weights,
    fetch_index_constituent_weights,
)
from stocklab.persistence.repository.benchmark import BenchmarkIndustryWeightRepository
from stocklab.persistence.repository.fund_holding import StockIndustryMappingRepository
from stocklab.persistence.repository.fund_analysis import SWIndexDailyRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "BenchmarkDataFacade",
    "BENCHMARK_INDEX_CODES",
]

_SOURCE = "csindex"
_SNAPSHOT_STALE_DAYS = 45   # 权重为季频披露，超过 45 天仍可用，仅作提示


class BenchmarkDataFacade:
    """基准指数行业权重门面"""

    def __init__(self, database=None):
        self._weight_repo = BenchmarkIndustryWeightRepository(database)
        self._mapping_repo = StockIndustryMappingRepository(database)
        self._sw_index_repo = SWIndexDailyRepository(database)

    # -------------------------------------------------------------------------
    # 行业权重
    # -------------------------------------------------------------------------

    def list_benchmarks(self) -> Dict[str, str]:
        """可选基准指数（代码 -> 中文名）"""
        return dict(BENCHMARK_INDEX_CODES)

    def get_industry_weights(
        self,
        benchmark_code: str,
        level: int = 1,
        refresh: bool = False,
    ) -> Tuple[pd.DataFrame, dict]:
        """
        获取基准指数行业权重快照

        Args:
            benchmark_code: 指数代码，如 000300.SH
            level: 行业层级 1 / 2
            refresh: True 时忽略缓存强制回源

        Returns:
            (DataFrame, meta)
            DataFrame 列 [sector_code, sector_name, weight, coverage]，weight 合计 1
            meta 含 as_of_date / coverage / sector_count / source / reason / stale
                  缓存与回源均失败时 DataFrame 为空且 meta["reason"] 说明原因
        """
        code = str(benchmark_code or "").strip().upper()
        meta: dict = {"benchmark_code": code, "level": level}

        if not refresh:
            cached = self._weight_repo.find_weights(code, level)
            cached_meta = self._weight_repo.find_meta(code, level)
            if not cached.empty and cached_meta:
                meta.update(cached_meta)
                meta["cached"] = True
                meta["reason"] = None
                meta["stale"] = self._is_stale(cached_meta.get("as_of_date"))
                return self._to_weight_frame(cached), meta

        refreshed = self.refresh_industry_weights(code, level)
        if refreshed["frame"].empty:
            meta.update(refreshed["meta"])
            return refreshed["frame"], meta
        return refreshed["frame"], refreshed["meta"]

    def refresh_industry_weights(self, benchmark_code: str, level: int = 1) -> dict:
        """
        回源抓取成分券权重并按本地行业映射聚合落库

        Returns:
            {"frame": DataFrame, "meta": dict}；失败时 frame 为空、meta["reason"] 说明
        """
        code = str(benchmark_code or "").strip().upper()
        failure = {"frame": pd.DataFrame(columns=["sector_code", "sector_name", "weight", "coverage"]),
                   "meta": {"benchmark_code": code, "level": level, "cached": False}}

        constituents = fetch_index_constituent_weights(code)
        if constituents.empty:
            failure["meta"]["reason"] = (
                "该基准暂无成分权重口径（仅中证指数公司发布的指数支持）"
                if code not in CSI_BENCHMARK_CODES
                else "成分权重抓取失败，请稍后重试"
            )
            _logger.warning("基准 %s 权重回源失败：%s", code, failure["meta"]["reason"])
            return failure

        mapping = self._mapping_repo.find_all()
        if mapping.empty:
            failure["meta"]["reason"] = "本地股票行业映射为空，请先运行行业映射同步"
            _logger.warning("本地行业映射为空，无法聚合基准 %s 行业权重", code)
            return failure

        aggregated = aggregate_industry_weights(constituents, mapping, level=level)
        if aggregated.empty:
            failure["meta"]["reason"] = "行业映射后无有效权重，无法生成权重快照"
            _logger.warning("基准 %s 行业聚合结果为空", code)
            return failure

        as_of = pd.Timestamp(constituents["as_of_date"].iloc[0]).date()
        coverage = float(aggregated["coverage"].iloc[0])

        snapshot = aggregated.copy()
        snapshot.insert(0, "benchmark_code", code)
        snapshot.insert(1, "level", int(level))
        snapshot["as_of_date"] = as_of
        snapshot["source"] = _SOURCE
        snapshot["fetched_at"] = pd.Timestamp.now().to_pydatetime()

        from stocklab.domain import BENCHMARK_INDUSTRY_WEIGHT_COLUMNS
        snapshot = snapshot.reindex(columns=list(BENCHMARK_INDUSTRY_WEIGHT_COLUMNS))
        self._weight_repo.replace(snapshot, code, int(level))

        meta = {
            "benchmark_code": code,
            "level": int(level),
            "as_of_date": str(as_of),
            "coverage": coverage,
            "sector_count": int(len(aggregated)),
            "source": _SOURCE,
            "cached": False,
            "reason": None,
            "stale": False,
        }
        _logger.info("基准 %s 行业权重快照已更新：%d 个行业，覆盖率 %.1f%%，披露日 %s",
                     code, len(aggregated), coverage * 100, as_of)
        return {"frame": self._to_weight_frame(aggregated), "meta": meta}

    # -------------------------------------------------------------------------
    # 基准收益率
    # -------------------------------------------------------------------------

    def get_benchmark_return(
        self,
        start_date,
        end_date,
        index_code: str = "000300.SH",
    ) -> Optional[float]:
        """
        基准指数区间收益率（本地日线，缺数据返回 None 不编数）
        """
        if start_date is None or end_date is None:
            return None
        try:
            series = self._sw_index_repo.load_series(
                symbols=[index_code],
                since=pd.Timestamp(start_date).date(),
            )
        except Exception as exc:
            _logger.warning("基准 %s 日线读取失败：%s", index_code, exc)
            return None

        if series is None or series.empty or "trade_date" not in series.columns:
            return None

        frame = series.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        end_ts = pd.Timestamp(end_date)
        frame = frame[(frame["trade_date"] >= pd.Timestamp(start_date)) & (frame["trade_date"] <= end_ts)]
        frame = frame.sort_values("trade_date")
        if len(frame) < 2:
            return None

        first = float(frame["close"].iloc[0])
        last = float(frame["close"].iloc[-1])
        if first <= 0:
            return None
        return last / first - 1.0

    # -------------------------------------------------------------------------
    # 内部
    # -------------------------------------------------------------------------

    @staticmethod
    def _to_weight_frame(frame: pd.DataFrame) -> pd.DataFrame:
        """统一权重帧输出口径：只保留展示列，weight 降序"""
        cols = ["sector_code", "sector_name", "weight", "coverage"]
        if frame is None or frame.empty:
            return pd.DataFrame(columns=cols)
        out = frame.reindex(columns=cols).copy()
        out["weight"] = pd.to_numeric(out["weight"], errors="coerce").fillna(0.0)
        return out.sort_values("weight", ascending=False).reset_index(drop=True)

    @staticmethod
    def _is_stale(as_of_date: Optional[str]) -> bool:
        """快照是否明显过期（超过 45 天，仅作提示不阻断）"""
        if not as_of_date:
            return True
        try:
            return (date.today() - pd.Timestamp(as_of_date).date()).days > _SNAPSHOT_STALE_DAYS
        except (ValueError, TypeError):
            return True
