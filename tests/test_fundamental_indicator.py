#!/usr/bin/env python3
"""
==============================================================================
StockLab - 财务指标派生测试 (tests/test_fundamental_indicator.py)
==============================================================================

【功能用途】
  覆盖 stocklab.fundamental.indicator 的纯计算口径：
  1. 有效税率在亏损（利润总额 <= 0）时取 0，不得符号翻转
  2. 毛利率 / 净利率等分母为零时返回 NaN，不臆造数值
  3. 同比口径使用 |去年同期| 作分母
  4. Point-in-Time：announce_date 取利润表与资产负债表公告日的较晚者
  5. 空利润表返回带契约列的空帧

【运行方式】
  ./venv/bin/python -m pytest tests/test_fundamental_indicator.py -v
"""

import os
import sys

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from stocklab.domain import FINANCIAL_INDICATOR_COLUMNS
from stocklab.fundamental.indicator import build_financial_indicators


def _income_row(ts_code, report_period, announce_date, **overrides):
    """构造一行利润表契约数据"""
    row = {
        "ts_code": ts_code,
        "report_period": report_period,
        "announce_date": announce_date,
        "available_date": announce_date,
        "source": "test",
        "source_record_id": "inc-%s" % report_period,
        "revenue": 1000.0,
        "operating_cost": 600.0,
        "operating_profit": 200.0,
        "total_profit": 250.0,
        "income_tax": 25.0,
        "net_profit": 180.0,
        "net_profit_attributable": 150.0,
        "eps": 0.5,
    }
    row.update(overrides)
    return row


def _balance_row(ts_code, report_period, announce_date, **overrides):
    """构造一行资产负债表契约数据"""
    row = {
        "ts_code": ts_code,
        "report_period": report_period,
        "announce_date": announce_date,
        "available_date": announce_date,
        "source": "test",
        "source_record_id": "bal-%s" % report_period,
        "total_assets": 5000.0,
        "total_liabilities": 2000.0,
        "equity": 3000.0,
        "cash": 500.0,
        "interest_bearing_debt": 1000.0,
    }
    row.update(overrides)
    return row


class TestTaxRate:
    """回归：亏损公司有效税率必须取 0，不能符号翻转"""

    def test_negative_total_profit_uses_zero_tax_rate(self):
        """利润总额为负时 roic 按零税率算（nopat = operating_profit）"""
        income = pd.DataFrame([
            _income_row("000001.SZ", "2024-12-31", "2025-03-01",
                        operating_profit=200.0, total_profit=-250.0, income_tax=25.0),
        ])
        balance = pd.DataFrame([_balance_row("000001.SZ", "2024-12-31", "2025-03-20")])

        result = build_financial_indicators(income, balance)

        # 200 / (3000 + 1000) = 0.05；若税率误用 25 / -250 = -0.1 会得到 0.055
        assert abs(result["roic"].iloc[0] - 0.05) < 1e-12

    def test_positive_total_profit_uses_effective_tax_rate(self):
        """利润总额为正时 roic 扣税后计算"""
        income = pd.DataFrame([
            _income_row("000001.SZ", "2024-12-31", "2025-03-01",
                        operating_profit=200.0, total_profit=250.0, income_tax=25.0),
        ])
        balance = pd.DataFrame([_balance_row("000001.SZ", "2024-12-31", "2025-03-20")])

        result = build_financial_indicators(income, balance)

        # nopat = 200 * (1 - 25/250) = 180；180 / 4000 = 0.045
        assert abs(result["roic"].iloc[0] - 0.045) < 1e-12

    def test_missing_income_tax_keeps_roic_nan(self):
        """所得税缺失（利润总额为正）绝不能当成 0% 免税：roic 应为 NaN（宁缺勿错）"""
        income = pd.DataFrame([
            _income_row("000001.SZ", "2024-12-31", "2025-03-01",
                        operating_profit=200.0, total_profit=250.0, income_tax=None),
        ])
        balance = pd.DataFrame([_balance_row("000001.SZ", "2024-12-31", "2025-03-20")])

        result = build_financial_indicators(income, balance)

        assert pd.isna(result["roic"].iloc[0])

    def test_missing_total_profit_keeps_roic_nan(self):
        """利润总额缺失时不得按免税算：roic 应为 NaN"""
        income = pd.DataFrame([
            _income_row("000001.SZ", "2024-12-31", "2025-03-01",
                        operating_profit=200.0, total_profit=None, income_tax=25.0),
        ])
        balance = pd.DataFrame([_balance_row("000001.SZ", "2024-12-31", "2025-03-20")])

        result = build_financial_indicators(income, balance)

        assert pd.isna(result["roic"].iloc[0])


class TestMarginGuards:
    """分母为零时返回 NaN，不臆造数值"""

    def test_zero_revenue_gives_nan_margin(self):
        """营收为 0 时毛利率为 NaN"""
        income = pd.DataFrame([
            _income_row("000001.SZ", "2024-12-31", "2025-03-01", revenue=0.0),
        ])

        result = build_financial_indicators(income, pd.DataFrame())

        assert pd.isna(result["gross_margin"].iloc[0])


class TestYearOverYear:
    """同比口径：分母取去年同期绝对值"""

    def test_revenue_yoy_against_negative_base(self):
        """去年同期为负时，同比仍以 |去年同期| 作分母"""
        income = pd.DataFrame([
            _income_row("000001.SZ", "2023-12-31", "2024-03-01", revenue=-200.0),
            _income_row("000001.SZ", "2024-12-31", "2025-03-01", revenue=100.0),
        ])

        result = build_financial_indicators(income, pd.DataFrame())

        latest = result.loc[result["report_period"] == pd.Timestamp("2024-12-31").date()]
        # (100 - (-200)) / 200 = 1.5
        assert abs(latest["revenue_yoy"].iloc[0] - 1.5) < 1e-12


class TestPointInTime:
    """announce_date 取两张表公告日的较晚者"""

    def test_announce_date_takes_later_side(self):
        """资产负债表公告较晚时，指标公告日以它为准"""
        income = pd.DataFrame([
            _income_row("000001.SZ", "2024-12-31", "2025-03-01"),
        ])
        balance = pd.DataFrame([
            _balance_row("000001.SZ", "2024-12-31", "2025-03-20"),
        ])

        result = build_financial_indicators(income, balance)

        assert result["announce_date"].iloc[0] == pd.Timestamp("2025-03-20").date()


class TestEmptyIncome:
    """空利润表返回带契约列的空帧"""

    def test_empty_income_returns_contract_columns(self):
        """空输入不抛异常，列序等于契约列"""
        result = build_financial_indicators(pd.DataFrame(), pd.DataFrame())

        assert result.empty
        assert list(result.columns) == list(FINANCIAL_INDICATOR_COLUMNS)
