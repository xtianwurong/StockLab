#!/usr/bin/env python3
"""
==============================================================================================================
StockLab - 申万行业指数/基金/资金流 Repository (stocklab.persistence.repository.fund_analysis)
==============================================================================================================

【模块职责】
   封装 fund_analysis 域各表的读写
"""


from datetime import date
from typing import Optional, List

import pandas as pd

from stocklab.domain import (
    SW_INDEX_DAILY_COLUMNS,
    SW_INDUSTRY_MAPPING_COLUMNS,
    FUND_INFO_COLUMNS,
    FUND_NAV_HISTORY_COLUMNS,
    CAPITAL_FLOW_DAILY_COLUMNS,
    FUND_ALLOCATION_ANALYSIS_COLUMNS,
)
from stocklab.persistence.repository.base import BaseRepository

__all__ = [
    "SWIndexDailyRepository",
    "SWIndustryMappingRepository",
    "FundInfoRepository",
    "FundNavHistoryRepository",
    "CapitalFlowDailyRepository",
    "FundAllocationAnalysisRepository",
]


class SWIndexDailyRepository(BaseRepository):
    """申万行业指数日线数据访问"""

    _TABLE_NAME = "sw.index_daily"
    _COLUMNS = SW_INDEX_DAILY_COLUMNS

    def upsert(self, frame: pd.DataFrame) -> int:
        return super().upsert(
            frame,
            conflict_columns=["symbol", "trade_date"],
            update_columns=["open", "high", "low", "close", "volume", "amount", "source", "fetched_at"],
        )

    def load_series(self, symbols: List[str], since: Optional[date] = None) -> pd.DataFrame:
        return self._load_by_symbols_and_date(symbols, since)

    @staticmethod
    def _symbol_variants(symbols: List[str]) -> List[str]:
        """801010 与 801010.SI 两种写法都查（表里存的是带 .SI 的）

        sw.index_daily 由 sync_benchmark 写入，key 用 "801010.SI"；
        而 sw.industry_mapping.index_code 是裸码 "801010"（映射表全表不带后缀）。
        两边各按自己的约定存，谁做 join 谁就得归一 —— 这里在读路径归一。
        漏了这一步的症状极隐蔽：SQL 不报错、只是 0 行，调仓分析全程「数据不足」。
        """
        variants: List[str] = []
        for symbol in symbols:
            text = str(symbol).strip()
            if not text:
                continue
            variants.append(text)
            bare = text[:-3] if text.endswith(".SI") else text
            for candidate in (bare, f"{bare}.SI"):
                if candidate not in variants:
                    variants.append(candidate)
        return variants

    def _load_by_symbols_and_date(self, symbols: List[str],
                                  since: Optional[date] = None) -> pd.DataFrame:
        """按指数代码批量取日线；since 为起始交易日（含）

        传裸码或带 .SI 的代码都可（见 _symbol_variants）。
        """
        variants = self._symbol_variants(symbols)
        if not variants:
            return pd.DataFrame(columns=list(self._COLUMNS))
        placeholders = ",".join(["?"] * len(variants))
        sql = f"SELECT * FROM {self._TABLE_NAME} WHERE symbol IN ({placeholders})"
        params = list(variants)
        if since:
            sql += " AND trade_date >= ?"
            params.append(since)
        sql += " ORDER BY symbol, trade_date"
        conn = self._db.get_connection()
        return conn.execute(sql, params).fetchdf()

    def latest_by_symbol(self, symbols: List[str]) -> pd.DataFrame:
        return self._latest_by_symbols(symbols)

    def _latest_by_symbols(self, symbols: List[str]) -> pd.DataFrame:
        """每个指数取最新一个交易日的行情（无数据的指数不出现在结果里）"""
        if not symbols:
            return pd.DataFrame(columns=list(self._COLUMNS))
        placeholders = ",".join(["?"] * len(symbols))
        conn = self._db.get_connection()
        frame = conn.execute(
            f"""
            SELECT * EXCLUDE (rn) FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY symbol ORDER BY trade_date DESC
                ) AS rn
                  FROM {self._TABLE_NAME}
                 WHERE symbol IN ({placeholders})
            ) WHERE rn = 1
            ORDER BY symbol
            """,
            list(symbols),
        ).fetchdf()
        return frame


class SWIndustryMappingRepository(BaseRepository):
    """申万行业分类映射"""

    _TABLE_NAME = "sw.industry_mapping"
    _COLUMNS = SW_INDUSTRY_MAPPING_COLUMNS

    def upsert(self, frame: pd.DataFrame) -> int:
        return super().upsert(
            frame,
            conflict_columns=["index_code"],
            update_columns=["index_name", "level", "parent_code", "sw_first_code", "description"],
        )

    def find_all(self) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(f"SELECT * FROM {self._TABLE_NAME} ORDER BY level, index_code").fetchdf()

    def find_by_level(self, level: int) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE level = ? ORDER BY index_code", [level]
        ).fetchdf()

    def get_parent_map(self) -> dict:
        """返回 二级代码 -> 一级代码 映射"""
        df = self.find_all()
        return dict(zip(df["index_code"], df["parent_code"]))


class FundInfoRepository(BaseRepository):
    """基金基本信息"""

    _TABLE_NAME = "fund.fund_info"
    _COLUMNS = FUND_INFO_COLUMNS

    def upsert(self, frame: pd.DataFrame) -> int:
        return super().upsert(
            frame,
            conflict_columns=["fund_code"],
            update_columns=["fund_name", "fund_short_name", "fund_type", "manager_name",
                          "company_name", "establish_date", "benchmark", "status", "source", "fetched_at"],
        )

    def find_all(self) -> pd.DataFrame:
        """全量（同步脚本登记去重、状态统计都要用）"""
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} ORDER BY fund_code"
        ).fetchdf()

    def find_by_code(self, fund_code: str) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE fund_code = ?", [fund_code]
        ).fetchdf()

    def find_by_type(self, fund_type: str) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE fund_type = ?", [fund_type]
        ).fetchdf()

    def find_active(self) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE status = 'active'"
        ).fetchdf()


class FundNavHistoryRepository(BaseRepository):
    """基金净值历史"""

    _TABLE_NAME = "fund.nav_history"
    _COLUMNS = FUND_NAV_HISTORY_COLUMNS

    def upsert(self, frame: pd.DataFrame) -> int:
        return super().upsert(
            frame,
            conflict_columns=["fund_code", "nav_date"],
            update_columns=["nav", "acc_nav", "change_pct", "source", "fetched_at"],
        )

    def load_series(self, fund_codes: List[str], since: Optional[date] = None) -> pd.DataFrame:
        return self._load_by_codes_and_date(fund_codes, since)

    def _load_by_codes_and_date(self, fund_codes: List[str], since: Optional[date] = None) -> pd.DataFrame:
        if not fund_codes:
            return pd.DataFrame(columns=list(self._COLUMNS))
        placeholders = ",".join(["?"] * len(fund_codes))
        sql = f"SELECT * FROM {self._TABLE_NAME} WHERE fund_code IN ({placeholders})"
        params = list(fund_codes)
        if since:
            sql += " AND nav_date >= ?"
            params.append(since)
        sql += " ORDER BY fund_code, nav_date"
        conn = self._db.get_connection()
        return conn.execute(sql, params).fetchdf()

    def latest_nav(self, fund_codes: List[str]) -> pd.DataFrame:
        if not fund_codes:
            return pd.DataFrame(columns=["fund_code", "nav_date", "nav", "acc_nav"])
        placeholders = ",".join(["?"] * len(fund_codes))
        conn = self._db.get_connection()
        return conn.execute(
            f"""
            SELECT fund_code, nav_date, nav, acc_nav
            FROM {self._TABLE_NAME}
            WHERE fund_code IN ({placeholders})
              AND nav_date = (SELECT MAX(nav_date) FROM {self._TABLE_NAME} WHERE fund_code = fund.nav_history.fund_code)
            """, fund_codes
        ).fetchdf()


class CapitalFlowDailyRepository(BaseRepository):
    """板块资金流日线"""

    _TABLE_NAME = "capital.flow_daily"
    _COLUMNS = CAPITAL_FLOW_DAILY_COLUMNS

    def upsert(self, frame: pd.DataFrame) -> int:
        return super().upsert(
            frame,
            conflict_columns=["sector_code", "trade_date"],
            update_columns=["sector_name", "sector_type", "net_inflow", "inflow", "outflow",
                          "net_inflow_rate", "main_net_inflow", "retail_net_inflow", "source", "fetched_at"],
        )

    def latest_trade_date(self, sector_type: str):
        """
        该类型在库里的最新交易日（无数据返回 None）

        【为什么需要】资金流是**按天落库**的快照，调用方不传日期时若按「今天」查，
        周末/假日/上游没跑同步时必然查空 —— 表里明明有数据却显示「没有」。
        """
        conn = self._db.get_connection()
        row = conn.execute(
            f"SELECT MAX(trade_date) FROM {self._TABLE_NAME} WHERE sector_type = ?",
            [sector_type],
        ).fetchone()
        if not row or row[0] is None:
            return None
        value = row[0]
        if isinstance(value, str):
            return value
        if isinstance(value, date):
            return value.isoformat()
        return pd.Timestamp(value).date().isoformat()

    def load_by_type_and_date(self, sector_type: str, trade_date: str) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE sector_type = ? AND trade_date = ? ORDER BY net_inflow DESC",
            [sector_type, trade_date]
        ).fetchdf()

    def load_series(self, sector_codes: List[str], since: Optional[date] = None) -> pd.DataFrame:
        return self._load_by_codes_and_date(sector_codes, since, code_col="sector_code")

    def _load_by_codes_and_date(self, codes: List[str], since: Optional[date] = None, code_col: str = "sector_code") -> pd.DataFrame:
        if not codes:
            return pd.DataFrame(columns=list(self._COLUMNS))
        placeholders = ",".join(["?"] * len(codes))
        sql = f"SELECT * FROM {self._TABLE_NAME} WHERE {code_col} IN ({placeholders})"
        params = list(codes)
        if since:
            sql += " AND trade_date >= ?"
            params.append(since)
        sql += " ORDER BY {}, trade_date".format(code_col)
        conn = self._db.get_connection()
        return conn.execute(sql, params).fetchdf()


class FundAllocationAnalysisRepository(BaseRepository):
    """基金调仓分析结果缓存"""

    _TABLE_NAME = "fund.allocation_analysis"
    _COLUMNS = FUND_ALLOCATION_ANALYSIS_COLUMNS

    def upsert(self, frame: pd.DataFrame) -> int:
        return super().upsert(
            frame,
            conflict_columns=["fund_code", "analysis_date", "window_days", "level", "sector_code"],
            update_columns=["sector_name", "exposure", "exposure_change", "exposure_change_5d",
                          "exposure_change_20d", "r_squared", "capital_flow_corr",
                          "confidence", "source", "computed_at"],
        )

    def find_by_fund_and_date(self, fund_code: str, analysis_date: str, window_days: int, level: int) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"""
            SELECT * FROM {self._TABLE_NAME}
            WHERE fund_code = ? AND analysis_date = ? AND window_days = ? AND level = ?
            ORDER BY exposure DESC
            """, [fund_code, analysis_date, window_days, level]
        ).fetchdf()

    def latest_analysis(self, fund_code: str, window_days: int = 20, level: int = 1) -> pd.DataFrame:
        conn = self._db.get_connection()
        return conn.execute(
            f"""
            SELECT * FROM {self._TABLE_NAME}
            WHERE fund_code = ? AND window_days = ? AND level = ?
            ORDER BY analysis_date DESC
            LIMIT 1
            """, [fund_code, window_days, level]
        ).fetchdf()