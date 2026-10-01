#!/usr/bin/env python3
"""
==============================================================================
StockLab - 日 K 行情 Repository (stocklab.datasource.repository.daily_price)
==============================================================================

【模块职责】
   封装 market.daily_prices 表的读写操作，对外提供简洁的数据访问接口。
   不涉及任何外部数据源，只负责 DataFrame <-> DuckDB 的映射。

【设计原则】
   - 批量写入：利用 DuckDB 对 pandas 的原生支持进行批量 UPSERT
   - 幂等性：重复写入同一批数据不会产生重复记录
   - NULL 语义保留：不将 NULL / NaN 强制转换为 0
"""

import logging

import pandas as pd

from stocklab.datasource.storage.duckdb import Database

_logger = logging.getLogger(__name__)

__all__ = [
    "DailyPriceRepository",
]


class DailyPriceRepository:
    """
    日 K 行情数据访问类

    【职责】
      1. 批量写入 / 更新日 K 行情
      2. 查询单只股票历史行情
      3. 查询某交易日全市场行情
      4. 查询某只股票最新交易日期
    """

    _TABLE_NAME = "market.daily_prices"

    def __init__(self, database=None):
        """
        初始化 Repository

        Args:
            database (Database, optional): 数据库管理器实例，默认创建新实例
        """
        self._db = database if database else Database()

    def upsert(self, prices_df):
        """
        批量写入或更新日 K 行情（幂等操作）

        使用 (ts_code, trade_date) 复合主键，重复执行不会产生重复记录。

        Args:
            prices_df (pd.DataFrame): 日 K 行情表，必须包含 ts_code 和 trade_date 列

        Returns:
            int: 实际写入的行数
        """
        if prices_df is None or prices_df.empty:
            _logger.warning("upsert 接收到空数据，跳过写入")
            return 0

        conn = self._db.get_connection()
        conn.register("_prices_tmp", prices_df)

        try:
            conn.execute(
                f"""
                INSERT INTO {self._TABLE_NAME}
                SELECT * FROM _prices_tmp
                ON CONFLICT (ts_code, trade_date) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    pre_close = excluded.pre_close,
                    change = excluded.change,
                    pct_chg = excluded.pct_chg,
                    volume = excluded.volume,
                    amount = excluded.amount
                """
            )
            row_count = len(prices_df)
            _logger.info("daily_prices 表 UPSERT 完成: %d 条记录", row_count)
            return row_count
        except Exception as error:
            _logger.error("daily_prices 表 UPSERT 失败: %s", error)
            return 0
        finally:
            conn.unregister("_prices_tmp")

    def find_by_code(self, ts_code, start_date=None, end_date=None):
        """
        按股票代码查询历史行情

        Args:
            ts_code (str): 证券代码，如 "600519.SH"
            start_date (str, optional): 起始日期，格式 "YYYY-MM-DD"
            end_date (str, optional): 结束日期，格式 "YYYY-MM-DD"

        Returns:
            pd.DataFrame: 日 K 行情表
        """
        conn = self._db.get_connection()
        sql = f"SELECT * FROM {self._TABLE_NAME} WHERE ts_code = ?"
        params = [ts_code]

        if start_date:
            sql += " AND trade_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND trade_date <= ?"
            params.append(end_date)

        sql += " ORDER BY trade_date"

        result = conn.execute(sql, params).fetchdf()
        return result

    def find_by_date(self, trade_date):
        """
        按交易日期查询全市场行情

        Args:
            trade_date (str): 交易日期，格式 "YYYY-MM-DD"

        Returns:
            pd.DataFrame: 全市场日 K 行情表
        """
        conn = self._db.get_connection()
        result = conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE trade_date = ? ORDER BY ts_code",
            [trade_date],
        ).fetchdf()
        return result

    def get_max_trade_date(self):
        """
        查询数据库中最新交易日期

        Returns:
            str: 最新交易日期，格式 "YYYY-MM-DD"；无数据时返回 None
        """
        conn = self._db.get_connection()
        result = conn.execute(f"SELECT MAX(trade_date) FROM {self._TABLE_NAME}").fetchone()
        if result and result[0]:
            return str(result[0])
        return None

    def count(self):
        """
        查询日 K 记录总数

        Returns:
            int: 记录总数
        """
        conn = self._db.get_connection()
        result = conn.execute(f"SELECT COUNT(*) FROM {self._TABLE_NAME}").fetchone()
        return int(result[0]) if result else 0
