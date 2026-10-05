#!/usr/bin/env python3
"""
==============================================================================================================
StockLab - 基金持仓与行业暴露 Repository (stocklab.persistence.repository.fund_holding)
==============================================================================================================

【模块职责】
   封装 fund_holding 域各表的读写
"""


from datetime import date
from typing import List, Optional

import pandas as pd

from stocklab.domain import (
    STOCK_INDUSTRY_MAPPING_COLUMNS,
    FUND_HOLDING_COLUMNS,
    FUND_INDUSTRY_EXPOSURE_COLUMNS,
    FUND_INDUSTRY_EXPOSURE_DAILY_COLUMNS,
    FUND_ATTRIBUTION_COLUMNS,
    FUND_MANAGER_TENURE_COLUMNS,
)
from stocklab.persistence.repository.base import BaseRepository

__all__ = [
    "StockIndustryMappingRepository",
    "FundHoldingRepository",
    "FundIndustryExposureRepository",
    "FundIndustryExposureDailyRepository",
    "FundAttributionRepository",
    "FundManagerTenureRepository",
]


class StockIndustryMappingRepository(BaseRepository):
    """股票行业映射表（静态维表）"""

    _TABLE_NAME = "fund.stock_industry_mapping"
    _COLUMNS = STOCK_INDUSTRY_MAPPING_COLUMNS

    def upsert(self, frame: pd.DataFrame) -> int:
        return super().upsert(
            frame,
            conflict_columns=["ts_code"],
            update_columns=[
                "sw_l1_code", "sw_l1_name", "sw_l2_code", "sw_l2_name",
                "sw_l3_code", "sw_l3_name", "citics_l1_code", "citics_l1_name",
                "citics_l2_code", "citics_l2_name", "wind_l1_code", "wind_l1_name",
                "updated_at",
            ],
        )

    def find_by_code(self, ts_code: str) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE ts_code = ?", [ts_code]
        ).fetchdf()

    def find_all(self) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} ORDER BY ts_code"
        ).fetchdf()

    def find_by_sw_l1(self, sw_l1_code: str) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE sw_l1_code = ?", [sw_l1_code]
        ).fetchdf()

    def find_by_sw_l2(self, sw_l2_code: str) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE sw_l2_code = ?", [sw_l2_code]
        ).fetchdf()

    def get_sw_l1_map(self) -> dict:
        """返回 ts_code -> sw_l1_code 映射"""
        df = self.find_all()
        return dict(zip(df["ts_code"], df["sw_l1_code"]))

    def get_sw_l2_map(self) -> dict:
        return self.get_sw_l2_map_by_level(2)

    def get_sw_l2_map_by_level(self, level: int) -> dict:
        conn = self._db.get_connection()
        df = conn.execute(
            f"SELECT ts_code, sw_l{level}_code FROM {self._TABLE_NAME} WHERE sw_l{level}_code != ''"
        ).fetchdf()
        return dict(zip(df["ts_code"], df[f"sw_l{level}_code"]))


class FundHoldingRepository(BaseRepository):
    """基金持仓表"""

    _TABLE_NAME = "fund.fund_holding"
    _COLUMNS = FUND_HOLDING_COLUMNS

    def upsert(self, frame: pd.DataFrame) -> int:
        return super().upsert(
            frame,
            conflict_columns=["fund_code", "report_date", "stock_code"],
            update_columns=["report_type", "stock_name", "weight", "market_value", "rank", "source", "fetched_at"],
        )

    def find_by_fund_and_date(self, fund_code: str, report_date: date) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE fund_code = ? AND report_date = ? ORDER BY rank",
            [fund_code, report_date]
        ).fetchdf()

    def find_latest_by_fund(self, fund_code: str) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"""
            SELECT * FROM {self._TABLE_NAME}
            WHERE fund_code = ? AND report_date = (
                SELECT MAX(report_date) FROM {self._TABLE_NAME} WHERE fund_code = ?
            )
            ORDER BY rank
            """, [fund_code, fund_code]
        ).fetchdf()

    def find_by_fund(self, fund_code: str) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE fund_code = ? ORDER BY report_date DESC, rank",
            [fund_code]
        ).fetchdf()

    def find_report_dates(self, fund_code: str) -> List[date]:
        conn = self._db.get_connection()
        rows = conn.execute(
            f"SELECT DISTINCT report_date FROM {self._TABLE_NAME} WHERE fund_code = ? ORDER BY report_date DESC",
            [fund_code]
        ).fetchall()
        return [row[0] for row in rows]

    def get_latest_report_date(self, fund_code: str) -> Optional[date]:
        conn = self._db.get_connection()
        row = conn.execute(
            f"SELECT MAX(report_date) FROM {self._TABLE_NAME} WHERE fund_code = ?", [fund_code]
        ).fetchone()
        return row[0] if row and row[0] else None


class FundIndustryExposureRepository(BaseRepository):
    """基金行业暴露表（按报告期）"""

    _TABLE_NAME = "fund.fund_industry_exposure"
    _COLUMNS = FUND_INDUSTRY_EXPOSURE_COLUMNS

    def upsert(self, frame: pd.DataFrame) -> int:
        return super().upsert(
            frame,
            conflict_columns=["fund_code", "report_date", "level", "sector_code"],
            update_columns=["sector_name", "weight", "source"],
        )

    def find_by_fund_and_date(self, fund_code: str, report_date: date, level: int = None) -> pd.DataFrame:
        conn = self._db.get_connection()
        sql = f"SELECT * FROM {self._TABLE_NAME} WHERE fund_code = ? AND report_date = ?"
        params = [fund_code, report_date]
        if level:
            sql += " AND level = ?"
            params.append(level)
        sql += " ORDER BY weight DESC"
        return conn.execute(sql, params).fetchdf()

    def find_latest_by_fund(self, fund_code: str, level: int = 1) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"""
            SELECT * FROM {self._TABLE_NAME}
            WHERE fund_code = ? AND level = ?
              AND report_date = (SELECT MAX(report_date) FROM {self._TABLE_NAME} WHERE fund_code = ? AND level = ?)
            ORDER BY weight DESC
            """, [fund_code, level, fund_code, level]
        ).fetchdf()

    def find_report_dates(self, fund_code: str) -> List[date]:
        conn = self._db.get_connection()
        rows = conn.execute(
            f"SELECT DISTINCT report_date FROM {self._TABLE_NAME} WHERE fund_code = ? ORDER BY report_date DESC",
            [fund_code]
        ).fetchall()
        return [row[0] for row in rows]


class FundIndustryExposureDailyRepository(BaseRepository):
    """基金行业暴露日线表（线性插值后的每日数据）"""

    _TABLE_NAME = "fund.fund_industry_exposure_daily"
    _COLUMNS = FUND_INDUSTRY_EXPOSURE_DAILY_COLUMNS

    def upsert(self, frame: pd.DataFrame) -> int:
        return super().upsert(
            frame,
            conflict_columns=["fund_code", "trade_date", "level", "sector_code"],
            update_columns=["sector_name", "weight", "source"],
        )

    def load_series(self, fund_codes: List[str], level: int, since: date = None, until: date = None) -> pd.DataFrame:
        conn = self._db.get_connection()
        if not fund_codes:
            return pd.DataFrame(columns=list(self._COLUMNS))

        placeholders = ",".join(["?"] * len(fund_codes))
        sql = f"SELECT * FROM {self._TABLE_NAME} WHERE fund_code IN ({placeholders}) AND level = ?"
        params = list(fund_codes) + [level]

        if since:
            sql += " AND trade_date >= ?"
            params.append(since)
        if until:
            sql += " AND trade_date <= ?"
            params.append(until)

        sql += " ORDER BY fund_code, trade_date, sector_code"
        return conn.execute(sql, params).fetchdf()

    def find_latest(self, fund_code: str, level: int) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"""
            SELECT * FROM {self._TABLE_NAME}
            WHERE fund_code = ? AND level = ?
              AND trade_date = (SELECT MAX(trade_date) FROM {self._TABLE_NAME} WHERE fund_code = ? AND level = ?)
            ORDER BY weight DESC
            """, [fund_code, level, fund_code, level]
        ).fetchdf()


class FundAttributionRepository(BaseRepository):
    """Brinson 归因结果表"""

    _TABLE_NAME = "fund.fund_attribution"
    _COLUMNS = FUND_ATTRIBUTION_COLUMNS

    def upsert(self, frame: pd.DataFrame) -> int:
        return super().upsert(
            frame,
            conflict_columns=["fund_code", "trade_date", "benchmark_code"],
            update_columns=["total_return", "benchmark_return", "excess_return",
                          "allocation_effect", "selection_effect", "interaction_effect"],
        )

    def find_by_fund_and_date(self, fund_code: str, trade_date: date, benchmark_code: str = None) -> pd.DataFrame:
        conn = self._db.get_connection()
        sql = f"SELECT * FROM {self._TABLE_NAME} WHERE fund_code = ? AND trade_date = ?"
        params = [fund_code, trade_date]
        if benchmark_code:
            sql += " AND benchmark_code = ?"
            params.append(benchmark_code)
        return conn.execute(sql, params).fetchdf()


class FundManagerTenureRepository(BaseRepository):
    """基金经理任职记录"""

    _TABLE_NAME = "fund.fund_manager_tenure"
    _COLUMNS = FUND_MANAGER_TENURE_COLUMNS

    def upsert(self, frame: pd.DataFrame) -> int:
        return super().upsert(
            frame,
            conflict_columns=["fund_code", "manager_name", "start_date"],
            update_columns=["end_date", "is_current", "aum"],
        )

    def find_by_fund(self, fund_code: str) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE fund_code = ? ORDER BY start_date DESC",
            [fund_code]
        ).fetchdf()

    def find_current_managers(self, fund_code: str) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE fund_code = ? AND is_current = TRUE",
            [fund_code]
        ).fetchdf()