#!/usr/bin/env python3
"""
==============================================================================
StockLab - 投资人观点 Repository (stocklab.persistence.repository.insight)
==============================================================================

【模块职责】
   封装 insight 域三张表的读写，三个类对应三份不同的访问语义：
     - InvestorRepository       投资人主表：只插入 + 按 code / name 查
     - InvestorAccountRepository 平台账号：只插入 + 「该抓哪些账号」入口
     - InvestorQuoteRepository  言论：只插入 + 增量去重 + 多维筛选

【为什么不合成一个类】
   三张表的读写模式差别很大：账号表有一次「按平台枚举待抓账号」的查询，
   言论表有按投资人/主题/平台/核验状态四种筛选和全文检索；
   硬塞进一个类会得到一个几十个方法的巨型类。保持一表一类。

【写入语义】
   三张表一律只 INSERT、不 UPDATE：
     - 投资人 / 账号是「已确认的事实」，变更就新增历史行；
     - 言论是既成事实，改原文就破坏了「什么时候抓到什么」的审计链。

【言论去重的两级】
   1. quote_id 主键冲突 -> ON CONFLICT DO NOTHING（同一平台重复抓）
   2. content_hash 跨平台同内容 -> 由调用方用 existing_hashes() 在写入前过滤。
      之所以不放库层做：同一段话被 A、B 两个平台转载时，是否算「两条记录」
      取决于业务判断（想看「谁在什么时候说过」还是要「一观点一记录」），
      这个判断留给调用方，库层只提供查重能力。
"""

import pandas as pd

from stocklab.domain import (
    INVESTOR_ACCOUNT_COLUMNS,
    INVESTOR_COLUMNS,
    INVESTOR_QUOTE_COLUMNS,
    # 本模块对外转出这两个编解码助手：洞察原始报文的读写都在 repository 层，
    # 下游（同步脚本与测试）从这里取比再绕回 domain 更贴近使用场景。
    decode_raw_meta,
    dumps_raw_meta,
)
from stocklab.persistence.repository.base import BaseRepository

__all__ = [
    "InvestorRepository",
    "InvestorAccountRepository",
    "InvestorQuoteRepository",
    # 转出自 stocklab.domain，见上方 import 处说明
    "decode_raw_meta",
    "dumps_raw_meta",
]


class InvestorRepository(BaseRepository):
    """投资人主表数据访问类（只插入）"""

    _TABLE_NAME = "insight.investors"
    _COLUMNS = INVESTOR_COLUMNS

    def insert(self, frame):
        """
        插入投资人（主键冲突忽略）

        Args:
            frame (pd.DataFrame): 投资人行，须含全部契约列

        Returns:
            int: 写入行数；空数据或契约违约返回 0
        """
        return self._insert_ignore_conflict(frame, ["investor_code"])

    def find_all(self):
        """
        全部投资人（按 code 升序）

        Returns:
            pd.DataFrame: 契约列序的投资人表
        """
        conn = self._db.get_connection()
        return conn.execute(
            "SELECT * FROM %s ORDER BY investor_code" % self._TABLE_NAME
        ).fetchdf()

    def find_by_code(self, investor_code):
        """
        按 code 查单个投资人

        Args:
            investor_code (str): 投资人代码

        Returns:
            pd.DataFrame: 0 或 1 行
        """
        conn = self._db.get_connection()
        return conn.execute(
            "SELECT * FROM %s WHERE investor_code = ?" % self._TABLE_NAME,
            [investor_code],
        ).fetchdf()

    def count(self):
        """
        投资人总数

        Returns:
            int: 行数
        """
        conn = self._db.get_connection()
        return conn.execute(
            "SELECT COUNT(*) FROM %s" % self._TABLE_NAME).fetchone()[0]


class InvestorAccountRepository(BaseRepository):
    """平台账号映射数据访问类（只插入 + 待抓账号枚举）"""

    _TABLE_NAME = "insight.investor_accounts"
    _COLUMNS = INVESTOR_ACCOUNT_COLUMNS

    def insert(self, frame):
        """
        插入平台账号（主键冲突忽略）

        Args:
            frame (pd.DataFrame): 账号行，须含全部契约列

        Returns:
            int: 写入行数
        """
        return self._insert_ignore_conflict(frame, ["account_id"])

    def find_enabled(self, platform=None):
        """
        枚举待抓账号（同步入口）

        Args:
            platform (str, optional): 平台标识；None 表示全部平台

        Returns:
            pd.DataFrame: 账号表，is_enabled 为真
        """
        conn = self._db.get_connection()
        if platform:
            return conn.execute(
                "SELECT * FROM %s WHERE is_enabled AND platform = ? "
                "ORDER BY account_id" % self._TABLE_NAME,
                [platform],
            ).fetchdf()
        return conn.execute(
            "SELECT * FROM %s WHERE is_enabled ORDER BY platform, account_id"
            % self._TABLE_NAME
        ).fetchdf()

    def find_all(self):
        """
        全部平台账号（含停用的）

        与 find_enabled 的区别：状态页要用它显示「总共有几个、
        哪几个被停了」，只查启用的会让数字对不上。

        Returns:
            pd.DataFrame: 账号表，按 platform / account_id 排序
        """
        conn = self._db.get_connection()
        return conn.execute(
            "SELECT * FROM %s ORDER BY platform, account_id" % self._TABLE_NAME
        ).fetchdf()

    def find_by_investor(self, investor_code):
        """
        查某投资人的全部平台账号

        Args:
            investor_code (str): 投资人代码

        Returns:
            pd.DataFrame: 账号表
        """
        conn = self._db.get_connection()
        return conn.execute(
            "SELECT * FROM %s WHERE investor_code = ? ORDER BY platform"
            % self._TABLE_NAME,
            [investor_code],
        ).fetchdf()

    def count(self):
        """
        账号总数

        Returns:
            int: 行数
        """
        conn = self._db.get_connection()
        return conn.execute(
            "SELECT COUNT(*) FROM %s" % self._TABLE_NAME).fetchone()[0]


class InvestorQuoteRepository(BaseRepository):
    """
    理念与言论数据访问类（只插入 + 多维筛选）

    【查询约定】
      published_at 可能为 NULL（很多 UGC 平台不给原始发布时间），
      所有「按时间倒序」的查询都用 `NULLS LAST` 兜底 —— 否则无时间的言论
      会挤在最前面，看起来像最新观点。
    """

    _TABLE_NAME = "insight.investor_quotes"
    _COLUMNS = INVESTOR_QUOTE_COLUMNS

    def insert(self, frame):
        """
        插入言论（quote_id 冲突忽略）

        Args:
            frame (pd.DataFrame): 言论行，须含全部契约列

        Returns:
            int: 写入行数
        """
        return self._insert_ignore_conflict(frame, ["quote_id"])

    def existing_hashes(self, hashes):
        """
        查出已入库的 content_hash（跨平台去重）

        Args:
            hashes (list): 待查的哈希列表

        Returns:
            set: 已存在的哈希集合
        """
        values = [str(item) for item in (hashes or []) if item]
        if not values:
            return set()
        conn = self._db.get_connection()
        placeholders = ", ".join("?" for _ in values)
        rows = conn.execute(
            "SELECT DISTINCT content_hash FROM %s WHERE content_hash IN (%s)"
            % (self._TABLE_NAME, placeholders),
            values,
        ).fetchall()
        return {row[0] for row in rows}

    def search(self, keyword=None, investor_codes=None, platforms=None,
               quote_types=None, themes=None, verification=None,
               stock_codes=None, since=None, order_by="captured_at",
               limit=50, offset=0):
        """
        多维筛选言论（页面的唯一查询入口）

        【为何不用参数化 IN 拼接而是逐个条件累加】
            条件全可选且组合多，用固定 SQL 片段拼接比构造 IN 列表更易读，
            且所有值都走占位符绑定，不存在注入面。

        Args:
            keyword (str, optional): 正文关键词（LIKE，含摘要与股票代码）
            investor_codes (list, optional): 投资人代码集合
            platforms (list, optional): 平台集合
            quote_types (list, optional): 言论类型集合
            themes (list, optional): 主题集合
            verification (str, optional): 单一核验状态
            stock_codes (list, optional): 相关标的集合
            since (str, optional): 只看 published_at 晚于该日期（YYYY-MM-DD）
            order_by (str): 排序字段，默认 captured_at（抓取时间，最可靠）
            limit (int): 返回条数上限
            offset (int): 偏移

        Returns:
            pd.DataFrame: 契约列序的言论表
        """
        conditions, params = ["1 = 1"], []

        if keyword:
            conditions.append(
                "(content LIKE ? OR summary LIKE ? OR stock_codes LIKE ?)")
            params.extend(["%%%s%%" % keyword] * 3)
        for column, values in (
            ("investor_code", investor_codes),
            ("platform", platforms),
            ("quote_type", quote_types),
            ("theme", themes),
            ("stock_codes", stock_codes),
        ):
            items = [str(item) for item in (values or []) if item]
            if not items:
                continue
            # stock_codes 是逗号分隔的一列，用 LIKE 精确匹配单个代码
            if column == "stock_codes":
                conditions.append("(" + " OR ".join(
                    ["%s LIKE ?" % column] * len(items)) + ")")
                params.extend(["%%%s%%" % item for item in items])
            else:
                conditions.append("%s IN (%s)" % (column, ", ".join(
                    "?" for _ in items)))
                params.extend(items)
        if verification:
            conditions.append("verification = ?")
            params.append(str(verification))
        if since:
            conditions.append("published_at >= ?")
            params.append(str(since))

        allowed_order = {
            "captured_at": "captured_at",
            "published_at": "published_at",
            "investor_code": "investor_code",
        }
        order_column = allowed_order.get(order_by, "captured_at")
        sql = "SELECT * FROM %s WHERE %s ORDER BY %s DESC NULLS LAST, quote_id LIMIT ? OFFSET ?" % (
            self._TABLE_NAME,
            " AND ".join(conditions),
            order_column,
        )
        params.extend([int(limit), int(offset)])
        conn = self._db.get_connection()
        return conn.execute(sql, params).fetchdf()

    def latest_published_at(self, investor_code):
        """
        查某投资人最新一条言论的发表时间（增量同步水位）

        【按投资人而不是按账号查水位的原因】
           一个人可能有多个平台账号（雪球 + 股吧）。若按账号各查各的水位，
           会出现「雪球侧水位很老 -> 把该投资人在雪球的全部历史重抓一遍，
           而这些内容 hash 已经在库里了」——白跑一遍还被限流。
           按投资人取水位，则跨平台共享一个进度。

        Args:
            investor_code (str): 投资人代码

        Returns:
            pd.Timestamp | None: 无言论时返回 None
        """
        conn = self._db.get_connection()
        row = conn.execute(
            "SELECT MAX(published_at) FROM %s WHERE investor_code = ?"
            % self._TABLE_NAME,
            [investor_code],
        ).fetchone()
        if not row or row[0] is None:
            return None
        try:
            parsed = pd.to_datetime(row[0])
        except (ValueError, TypeError):
            return None
        return None if pd.isna(parsed) else parsed

    def list_investors(self):
        """
        按言论条数降序列出投资人（做概览与画像用）

        Returns:
            pd.DataFrame: investor_code / name / quote_count / 各状态计数
        """
        conn = self._db.get_connection()
        return conn.execute(
            """
            SELECT i.investor_code,
                   i.name,
                   i.style_tags,
                   COUNT(q.quote_id) AS quote_count,
                   COUNT(*) FILTER (WHERE q.verification = 'verified')
                       AS verified_count,
                   MAX(q.captured_at) AS last_captured
            FROM insight.investors i
            LEFT JOIN insight.investor_quotes q
                   ON q.investor_code = i.investor_code
            GROUP BY i.investor_code, i.name, i.style_tags
            ORDER BY quote_count DESC, i.name
            """
        ).fetchdf()

    def list_themes(self, limit=30):
        """
        主题分布（页面筛选条用）

        【必须先拆逗号再分组】
           theme 是逗号分隔的多值列。若直接 GROUP BY theme，
           「估值,商业模式」会自成一桶 —— 于是点了「估值」的筛选
           找不到这些语录，且主题计数永远偏小。
           这里先用 string_split + unnest 展开成单值行再分组，
           与 search() 里 `theme LIKE '%x%'` 的命中语义保持一致。

        Args:
            limit (int): 返回主题上限

        Returns:
            pd.DataFrame: theme / quote_count，按条数降序
        """
        conn = self._db.get_connection()
        return conn.execute(
            "SELECT theme, COUNT(*) AS quote_count FROM ( "
            "    SELECT unnest(string_split(theme, ',')) AS theme "
            "    FROM %s "
            "    WHERE theme IS NOT NULL AND theme <> '' "
            ") "
            "GROUP BY theme "
            "HAVING theme <> '' "
            "ORDER BY quote_count DESC, theme "
            "LIMIT ?" % self._TABLE_NAME,
            [int(limit)],
        ).fetchdf()

    def count(self, verification=None):
        """
        言论总数

        Args:
            verification (str, optional): 只统计某一核验状态

        Returns:
            int: 行数
        """
        conn = self._db.get_connection()
        if verification:
            row = conn.execute(
                "SELECT COUNT(*) FROM %s WHERE verification = ?" % self._TABLE_NAME,
                [str(verification)],
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM %s" % self._TABLE_NAME).fetchone()
        return row[0]

    def verification_summary(self):
        """
        核验状态分布（页面上必须显式展示，见 domain/insight.py 说明）

        Returns:
            dict: {状态: 条数}
        """
        conn = self._db.get_connection()
        rows = conn.execute(
            "SELECT verification, COUNT(*) FROM %s GROUP BY verification"
            % self._TABLE_NAME
        ).fetchall()
        return {row[0]: row[1] for row in rows}

