#!/usr/bin/env python3
"""
==============================================================================
StockLab - 真实库污染守卫 (tests/test_db_guard.py)
==============================================================================

【功能用途】
  全量测试跑完后（必须最后跑，pytest 默认按字母序），检查
  data/stocklab.duckdb 中**没有任何** source='test' / 'fake' / 'mock' 等
  合成数据残留。

  这种残留最隐蔽的危害：测试造的净值/资金流/指数日线日期比真实数据还新，
  导致分析层（RBSA / 归因 / 选股器）在计算相关性、滚动回归时拿到的是
  一半真、一半伪的混合序列 → 算出的权重、信号全是假的，却看不出报错。

【运行方式】
  ./venv/bin/python -m pytest tests/test_db_guard.py -v
  （会被 pytest 自然排在最后，或可显式 `pytest tests/ tests/test_db_guard.py`）
==============================================================================
"""

import os

import pytest


def test_real_db_has_no_test_data():
    """真实库不得含有 source='test/fake/mock...' 的行

    覆盖所有有 source/verification 列且可能被测试写脏的表。
    表/列不存在忽略（兼容旧库/部分 schema）。
    """
    import duckdb

    db_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "stocklab.duckdb"
    )
    if not os.path.exists(db_path):
        pytest.skip("data/stocklab.duckdb 不存在（CI 首次跑前未初始化）")

    con = duckdb.connect(db_path, read_only=True)
    try:
        tables = [
            ("fund.nav_history", "source"),
            ("fund.fund_holding", "source"),
            ("sw.index_daily", "source"),
            ("capital.flow_daily", "source"),
            ("fund.fund_industry_exposure", "source"),
            ("fund.fund_industry_exposure_daily", "source"),
            ("fund.allocation_analysis", "source"),
            ("fundamental.income_statements", "source"),
            ("fundamental.balance_sheets", "source"),
            ("fundamental.cashflow_statements", "source"),
            ("fundamental.financial_indicators", "source"),
            ("reference.benchmark_industry_weights", "source"),
        ]
        bad_total = 0
        details = []
        for table, col in tables:
            try:
                cnt = con.execute(
                    f"select count(*) from {table} where lower({col}) in "
                    f"('test','fake','mock','fixture','sample','smoke','demo')"
                ).fetchone()[0]
                if cnt:
                    details.append(f"{table}.{col}={cnt}")
                    bad_total += cnt
            except Exception:
                pass  # 表/列不存在忽略
        if bad_total:
            pytest.fail(
                "真实库 data/stocklab.duckdb 含合成/测试数据残留："
                + "; ".join(details)
                + f"（共 {bad_total} 行）。请清理后再提交。"
            )
    finally:
        con.close()