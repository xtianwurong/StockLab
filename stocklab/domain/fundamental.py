#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基本面领域契约 (stocklab.domain.fundamental)
==============================================================================

【模块职责】
   定义 Point-in-Time 基本面域的列契约（列名 + 列序，与 002_fundamental.sql 同序）：
     - INCOME_STATEMENT_COLUMNS    fundamental.income_statements（利润表）
     - BALANCE_SHEET_COLUMNS       fundamental.balance_sheets（资产负债表）
     - CASHFLOW_STATEMENT_COLUMNS  fundamental.cashflow_statements（现金流量表）
     - FINANCIAL_INDICATOR_COLUMNS fundamental.financial_indicators（质量/成长指标）

【Point-in-Time 核心字段】
   四张表一律以 FUNDAMENTAL_PIT_COLUMNS 开头，缺一不可：
     - ts_code          证券标识（V2 文档 §3.2 写作 symbol，本项目统一用带交易所后缀的
                        ts_code；6 位 symbol 由 reference.securities 按 ts_code 取得，
                        故不在四张表里冗余存两份代码）
     - report_period    报告期（会计期间，如 2024-03-31）
     - announce_date    公告日期（数据对外披露的时间）
     - available_date   可见日期（研究视角首次可见的时间，默认等于 announce_date）
     - source           数据源标识（如 eastmoney / sina）
     - source_record_id 数据源行标识（用于回溯核对）
   任何历史查询必须带 available_date <= as_of 条件，否则会把「报告期早于
   as-of、但当时尚未公告」的未来信息泄漏进研究结果。

【指标归属】
   - 盈利 / 现金流 / 资产负债 原始科目 → 三张报表；
   - 质量（ROE/ROA/ROIC/毛利率/营业利润率/净利率）与成长（营收/利润/EPS 同比）
     → financial_indicators，由报表纯计算派生（见 stocklab.fundamental）。
"""

__all__ = [
    "FUNDAMENTAL_PIT_COLUMNS",
    "INCOME_STATEMENT_COLUMNS",
    "BALANCE_SHEET_COLUMNS",
    "CASHFLOW_STATEMENT_COLUMNS",
    "FINANCIAL_INDICATOR_COLUMNS",
]

# 四张基本面表共有的 Point-in-Time 字段前缀
FUNDAMENTAL_PIT_COLUMNS = (
    "ts_code",
    "report_period",
    "announce_date",
    "available_date",
    "source",
    "source_record_id",
)

# 利润表（盈利 + 派生质量指标所需的收入/成本/税）
INCOME_STATEMENT_COLUMNS = FUNDAMENTAL_PIT_COLUMNS + (
    "revenue",
    "operating_cost",
    "operating_profit",
    "total_profit",
    "income_tax",
    "net_profit",
    "net_profit_attributable",
    "eps",
)

# 资产负债表
BALANCE_SHEET_COLUMNS = FUNDAMENTAL_PIT_COLUMNS + (
    "total_assets",
    "total_liabilities",
    "equity",
    "cash",
    "interest_bearing_debt",
)

# 现金流量表
CASHFLOW_STATEMENT_COLUMNS = FUNDAMENTAL_PIT_COLUMNS + (
    "operating_cashflow",
    "investing_cashflow",
    "financing_cashflow",
    "free_cashflow",
)

# 质量与成长指标（由报表派生，announce_date 取自其来源报表）
FINANCIAL_INDICATOR_COLUMNS = FUNDAMENTAL_PIT_COLUMNS + (
    "roe",
    "roa",
    "roic",
    "gross_margin",
    "operating_margin",
    "net_margin",
    "revenue_yoy",
    "profit_yoy",
    "eps_yoy",
)
