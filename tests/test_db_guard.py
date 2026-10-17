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

    两条独立检查：
      1) 动态发现所有带 source / verification 列的表并扫描（新增表自动纳入，
         不再依赖手工清单 —— 旧清单曾漏掉 commodity / announcements / fund_info）；
      2) 行情与估值表**没有 source 列**，无法按 source 识别污染，改用「未来日期」
         启发式兜底：测试 fixture 常把日期推到很远的未来（2099 之类），真实库不会。
    """
    import duckdb

    db_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "stocklab.duckdb"
    )
    if not os.path.exists(db_path):
        pytest.skip("data/stocklab.duckdb 不存在（CI 首次跑前未初始化）")

    sentinels = "('test','fake','mock','fixture','sample','smoke','demo')"
    con = duckdb.connect(db_path, read_only=True)
    try:
        bad_total = 0
        details = []

        # 1) 所有带 source / verification 列的表（含 verification 的 insight 表）
        tagged = con.execute(
            "select table_schema || '.' || table_name, column_name "
            "from information_schema.columns "
            "where column_name in ('source', 'verification') "
            "order by 1, 2"
        ).fetchall()
        for table, col in tagged:
            cnt = con.execute(
                f"select count(*) from {table} "
                f"where lower(cast({col} as varchar)) in {sentinels}"
            ).fetchone()[0]
            if cnt:
                details.append(f"{table}.{col}={cnt}")
                bad_total += cnt

        # 2) 无 source 列的行情/估值表：用未来日期兜底
        horizon = con.execute("select current_date + 30").fetchone()[0]
        for table in (
            "market.daily_prices",
            "market.daily_valuations",
            "market.industry_valuations",
            "market.valuation_history",
        ):
            try:
                cnt = con.execute(
                    f"select count(*) from {table} where trade_date > ?", [horizon]
                ).fetchone()[0]
            except Exception:
                continue  # 表/列不存在忽略
            if cnt:
                details.append(f"{table}.trade_date>{horizon}={cnt}")
                bad_total += cnt

        if bad_total:
            pytest.fail(
                "真实库 data/stocklab.duckdb 含合成/测试数据残留："
                + "; ".join(details)
                + f"（共 {bad_total} 行）。请清理后再提交。"
            )
    finally:
        con.close()
