#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基准指数行业权重 Repository (stocklab.persistence.repository.benchmark)
==============================================================================

【模块职责】
   封装 reference.benchmark_industry_weights 快照表的读写。
   只依赖 stocklab.domain（列契约）与 storage，不涉及任何外部数据源。

【快照语义】
   同一 (benchmark_code, level) 只保留最新一期权重，刷新即整体覆盖。
"""

import logging
from typing import Optional

import pandas as pd

from stocklab.domain import BENCHMARK_INDUSTRY_WEIGHT_COLUMNS
from stocklab.persistence.repository.base import BaseRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "BenchmarkIndustryWeightRepository",
]


class BenchmarkIndustryWeightRepository(BaseRepository):
    """基准指数行业权重快照表"""

    _TABLE_NAME = "reference.benchmark_industry_weights"
    _COLUMNS = BENCHMARK_INDUSTRY_WEIGHT_COLUMNS

    def upsert(self, frame: pd.DataFrame) -> int:
        """写入权重快照（同一基准同层级的行业权重覆盖更新）"""
        return super().upsert(
            frame,
            conflict_columns=["benchmark_code", "level", "sector_code"],
            update_columns=[
                "sector_name", "weight", "as_of_date",
                "coverage", "source", "fetched_at",
            ],
        )

    def replace(self, frame: pd.DataFrame, benchmark_code: str, level: int) -> int:
        """
        整期替换某基准某层级的权重快照

        快照为「最新一期」语义，刷新前先清掉旧层级数据，
        避免行业调样后残留已不在权重内的行业。
        """
        conn = self._db.get_connection()
        conn.execute(
            f"DELETE FROM {self._TABLE_NAME} WHERE benchmark_code = ? AND level = ?",
            [benchmark_code, level],
        )
        if frame is None or frame.empty:
            return 0
        return self.upsert(frame)

    def find_weights(self, benchmark_code: str, level: int = 1) -> pd.DataFrame:
        """按权重倒序返回某基准某层级的行业权重"""
        conn = self._db.get_connection()
        return conn.execute(
            f"""
            SELECT benchmark_code, level, sector_code, sector_name,
                   weight, as_of_date, coverage, source, fetched_at
              FROM {self._TABLE_NAME}
             WHERE benchmark_code = ? AND level = ?
             ORDER BY weight DESC, sector_code
            """,
            [benchmark_code, level],
        ).fetchdf()

    def find_meta(self, benchmark_code: str, level: int = 1) -> Optional[dict]:
        """返回该基准该层级快照的元信息（披露日期 / 覆盖率），无快照返回 None"""
        conn = self._db.get_connection()
        row = conn.execute(
            f"""
            SELECT MAX(as_of_date) AS as_of_date,
                   MAX(coverage)    AS coverage,
                   MAX(source)      AS source,
                   COUNT(*)         AS sector_count
              FROM {self._TABLE_NAME}
             WHERE benchmark_code = ? AND level = ?
            """,
            [benchmark_code, level],
        ).fetchdf()
        if row.empty or row["sector_count"].iloc[0] in (0, None):
            return None
        as_of = row["as_of_date"].iloc[0]
        return {
            "as_of_date": str(pd.Timestamp(as_of).date()) if as_of is not None else None,
            "coverage": float(row["coverage"].iloc[0] or 0.0),
            "source": row["source"].iloc[0],
            "sector_count": int(row["sector_count"].iloc[0]),
        }

    def find_all_benchmarks(self) -> pd.DataFrame:
        """列出已有快照的基准代码与层级（同步脚本增量刷新用）"""
        conn = self._db.get_connection()
        return conn.execute(
            f"""
            SELECT benchmark_code, level, MAX(as_of_date) AS as_of_date, COUNT(*) AS sector_count
              FROM {self._TABLE_NAME}
             GROUP BY benchmark_code, level
             ORDER BY benchmark_code, level
            """
        ).fetchdf()
