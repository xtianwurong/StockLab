#!/usr/bin/env python3
"""
==============================================================================
StockLab - 行业估值 Repository (stocklab.persistence.repository.industry_valuation)
==============================================================================

【模块职责】
   封装 market.industry_valuations 表的读写操作。
   记录「某个行业在某个时点的估值水平」，不涉及任何外部数据源。

【本表解决的选股问题】
   个股估值分位只能回答「它相对自己历史上贵不贵」，回答不了「它相对同行业贵不贵」。
   本表提供行业级估值，用于：
     - 板块配置：一级行业估值对比，定位价值洼地板块；
     - 同业比较：将个股放入行业坐标，判断相对贵贱；
     - 双口径交叉验证：加权平均 PE 与中位数 PE 同时低，才算真洼地。

【为何同时存三个 PE 口径】
     加权平均 PE —— 按市值加权，反映龙头主导的估值水平；
     中位数   PE —— 反映「典型公司」的估值，抵御极端值干扰；
     算术平均 PE —— 完整分布参考。
   三者差异本身就是信息：加权远低于中位数，说明估值集中在少数权重股上。

【设计原则】
   - 批量 UPSERT，幂等：同一期数据重复写入行数不变
   - NULL 语义保留：亏损行业 PE 为负或 NULL，不强制填 0
==============================================================================
"""

import logging

import pandas as pd

from stocklab.persistence.repository.base import BaseRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "IndustryValuationRepository",
]


class IndustryValuationRepository(BaseRepository):
    """
    行业估值数据访问类
    """

    _TABLE_NAME = "market.industry_valuations"

    def upsert(self, industry_df):
        """
        批量写入或更新行业估值（幂等操作）

        使用 (industry_code, stat_date) 复合主键，
        重复同步同一期数据不会产生重复记录。

        Args:
            industry_df (pd.DataFrame): 行业估值表，
                                       需包含 industry_code 与 stat_date

        Returns:
            int: 实际写入的行数
        """
        return super().upsert(
            industry_df,
            conflict_columns=["industry_code", "stat_date"],
            update_columns=[
                "classification", "industry_level", "industry_name",
                "company_count", "priced_company_count", "total_market_value",
                "net_profit", "pe_weighted", "pe_median", "pe_arithmetic"
            ]
        )

    def find_by_date(self, stat_date, classification=None, industry_level=None):
        """
        查询某个时点的行业估值横截面

        Args:
            stat_date (str): 统计日期 YYYY-MM-DD
            classification (str, optional): 行业分类体系，如 "国证行业分类"
            industry_level (int, optional): 行业层级，1 为最粗

        Returns:
            pd.DataFrame: 行业估值表，按 PE 加权平均升序（便宜的在前）
        """
        conn = self._db.get_connection()
        sql = f"SELECT * FROM {self._TABLE_NAME} WHERE stat_date = ?"
        params = [stat_date]

        if classification:
            sql += " AND classification = ?"
            params.append(classification)
        if industry_level:
            sql += " AND industry_level = ?"
            params.append(industry_level)

        sql += " ORDER BY pe_weighted NULLS LAST"

        return conn.execute(sql, params).fetchdf()

    def find_series(self, industry_code):
        """
        查询单个行业的估值时间序列（按时间升序）

        Args:
            industry_code (str): 行业编码

        Returns:
            pd.DataFrame: 行业估值序列
        """
        conn = self._db.get_connection()
        return conn.execute(
            f"SELECT * FROM {self._TABLE_NAME} WHERE industry_code = ? ORDER BY stat_date",
            [industry_code],
        ).fetchdf()

    def find_cross_section_dates(self, classification=None):
        """
        列出已入库的行业估值日期清单

        Args:
            classification (str, optional): 行业分类体系过滤

        Returns:
            list: YYYY-MM-DD 形式的日期列表，升序
        """
        conn = self._db.get_connection()
        sql = f"SELECT DISTINCT stat_date FROM {self._TABLE_NAME}"
        params = []
        if classification:
            sql += " WHERE classification = ?"
            params.append(classification)
        sql += " ORDER BY stat_date"

        rows = conn.execute(sql, params).fetchall()
        return [str(row[0]) for row in rows]

    def list_industries(self, stat_date, classification=None):
        """
        列出某时点已入库的全部行业及其样本期数

        Args:
            stat_date (str): 统计日期 YYYY-MM-DD
            classification (str, optional): 行业分类体系过滤

        Returns:
            pd.DataFrame: 行业清单，含 industry_code / industry_name / period_count
        """
        conn = self._db.get_connection()
        sql = f"""
            SELECT industry_code,
                   MAX(classification) AS classification,
                   MAX(industry_level) AS industry_level,
                   MAX(industry_name) AS industry_name,
                   COUNT(*) AS period_count,
                   MIN(stat_date) AS first_date,
                   MAX(stat_date) AS last_date
            FROM {self._TABLE_NAME}
            WHERE stat_date = ?
        """
        params = [stat_date]
        if classification:
            sql += " AND classification = ?"
            params.append(classification)
        sql += " GROUP BY industry_code ORDER BY industry_level, industry_code"

        return conn.execute(sql, params).fetchdf()

    def count(self):
        """
        查询行业估值记录总数

        Returns:
            int: 记录总数
        """
        conn = self._db.get_connection()
        result = conn.execute(f"SELECT COUNT(*) FROM {self._TABLE_NAME}").fetchone()
        return int(result[0]) if result else 0
