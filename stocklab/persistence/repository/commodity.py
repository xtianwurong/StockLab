#!/usr/bin/env python3
"""
==============================================================================
StockLab - 大宗商品价格 Repository (stocklab.persistence.repository.commodity)
==============================================================================

【模块职责】
   封装 commodity.price_history 的读写。只有一张表、一个类。

【写入语义：UPSERT，与本域其它 Repository 相反】
   本项目里只 INSERT 的表（言论、公告）都是「既成事实，改了就没法审计」；
   价格不是 —— 它是可修正的**观测值**：源站补数据、合约换月回填、
   当天抓到一行坏数，下一轮同步都必须能覆盖它。所以这里走 upsert，
   冲突列 (symbol, trade_date)，更新 close / source / fetched_at。
   fetched_at 随之刷新，于是「这条记录是哪一轮写进来的」在表里可查。

【读取为什么只给窗口，不给「全部」】
   分位的定义是「当前价格在过去 N 年中的位置」，N 是参数。把窗口下界放进
   SQL 而不是把全表拉回内存再切，是为了让「窗口是 5 年还是 10 年」这件事
   体现在查询计划里 —— 库里长了以后，全表拉回是唯一会突然变慢的路径。
"""

import pandas as pd

from stocklab.domain import COMMODITY_PRICE_COLUMNS
from stocklab.persistence.repository.base import BaseRepository

__all__ = [
    "CommodityPriceRepository",
]


class CommodityPriceRepository(BaseRepository):
    """大宗商品收盘价数据访问类"""

    _TABLE_NAME = "commodity.price_history"
    _COLUMNS = COMMODITY_PRICE_COLUMNS

    def insert(self, frame):
        """
        写入（按 symbol + trade_date 覆盖更新）

        Args:
            frame (pd.DataFrame): 契约列序或可对齐的采集结果

        Returns:
            int: 写入行数；空数据或契约违约返回 0
        """
        return self.upsert(
            frame,
            conflict_columns=["symbol", "trade_date"],
            update_columns=["close", "source", "fetched_at"],
        )

    def load_series(self, symbols=None, since=None):
        """
        读价格序列

        Args:
            symbols (list | tuple | None): 只读这些品种；None 表示全部
            since (datetime.date | None): 只读该日期（含）之后的行；None 表示不限

        Returns:
            pd.DataFrame: [symbol, trade_date, close, source, fetched_at]，
                          按 symbol、trade_date 升序；无数据时为 0 行
        """
        sql = "SELECT {} FROM {} WHERE 1=1".format(
            ", ".join(self._COLUMNS), self._TABLE_NAME
        )
        params = []
        if symbols is not None:
            symbols = list(symbols)
            if not symbols:
                return pd.DataFrame(columns=list(self._COLUMNS))
            sql += " AND symbol IN ({})".format(
                ", ".join("?" for _ in symbols)
            )
            params.extend(symbols)
        if since is not None:
            sql += " AND trade_date >= ?"
            params.append(since)
        sql += " ORDER BY symbol, trade_date"

        conn = self._db.get_connection()
        frame = conn.execute(sql, params).fetchdf()
        if frame.empty:
            return pd.DataFrame(columns=list(self._COLUMNS))
        return frame

    def latest_by_symbol(self):
        """
        每个品种的最新一行（用于「当前价 + 数据截止」）

        Returns:
            pd.DataFrame: [symbol, trade_date, close]，每个 symbol 恰好一行
        """
        conn = self._db.get_connection()
        return conn.execute(
            "SELECT symbol, MAX(trade_date) AS trade_date FROM {} "
            "GROUP BY symbol".format(self._TABLE_NAME)
        ).fetchdf()

    def as_of(self):
        """
        全库数据截止日期（页面顶栏「数据截止」用）

        Returns:
            str | None: YYYY-MM-DD；表为空时 None
        """
        conn = self._db.get_connection()
        row = conn.execute(
            "SELECT MAX(trade_date) FROM {}".format(self._TABLE_NAME)
        ).fetchone()
        value = row[0] if row else None
        return str(value) if value is not None else None

    def counts_by_symbol(self):
        """
        每个品种的样本条数（判断「分位够不够长」用，空序列无法给分位）

        Returns:
            dict[str, int]: {symbol: 条数}
        """
        conn = self._db.get_connection()
        rows = conn.execute(
            "SELECT symbol, COUNT(*) AS n FROM {} GROUP BY symbol".format(
                self._TABLE_NAME
            )
        ).fetchall()
        return {symbol: int(n) for symbol, n in rows}
