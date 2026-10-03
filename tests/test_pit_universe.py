#!/usr/bin/env python3
"""
==============================================================================
StockLab - Point-in-Time 与股票池测试 (tests/test_pit_universe.py)
==============================================================================

【功能用途】
  验证 V2 需求里最容易出错的两件事：
    1. 生命周期归一化：上市/退市日历 → 主表日期与 status + 事件帧
    2. 幸存者偏差防线：universe(as_of) 必须还原历史时点的证券集合，
       已退市证券在退市前必须在池内、退市后必须出池
    3. 字段归属：名录同步（不带日期）不得抹掉生命周期阶段回填的日期
    4. Point-in-Time：find_as_of / latest_as_of / cross_section_as_of
       必须以 available_date（公告日）为可见性判据，杜绝未来信息泄漏
    5. 指标派生：ROE / ROIC / 毛利率 / 同比的公式与「除零为 NaN」语义

【运行方式】
  python tests/test_pit_universe.py
  （全部使用临时数据库，不触碰 data/stocklab.duckdb；运行前请停掉 serve_web）
==============================================================================
"""


import os
import sys
import math

import pandas as pd
import pytest

# 允许直接执行本文件（./venv/bin/python tests/xxx.py）；走 pytest 时由
# pytest.ini 的 `pythonpath = .` 统一负责，不会重复插入。
if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.domain import (
    BALANCE_SHEET_COLUMNS,
    FINANCIAL_INDICATOR_COLUMNS,
    INCOME_STATEMENT_COLUMNS,
    SECURITY_COLUMNS,
    SECURITY_STATUSES,
    DataContractError,
)
from stocklab.fundamental import build_financial_indicators
from stocklab.normalization.exchange import (
    build_lifecycle_events,
    merge_lifecycle,
    normalize_delisting_calendar,
    normalize_listing_calendar,
)
from stocklab.persistence import (
    Database,
    IncomeStatementRepository,
    SecurityEventRepository,
    SecurityRepository,
    initialize_database,
)

# 样本证券（A/B/C 来自名录，E 只出现在退市日历里）
_LISTED_CODES = ["600519.SH", "000001.SZ", "300750.SZ"]
_DELISTED_CODE = "601234.SH"

def _listing_raw():
    """构造上交所/深交所上市日历源表（列名为交易所口径）"""
    return pd.DataFrame(
        {
            "A股代码": ["600519", "000001", "300750"],
            "A股简称": ["贵州茅台", "平安银行", "宁德时代"],
            "A股上市日期": ["1990-12-19", "1991-04-03", "2018-06-11"],
        }
    )

def _delisting_raw():
    """构造退市日历源表（含上市日期列，退市股的唯一上市日来源）"""
    return pd.DataFrame(
        {
            "证券代码": ["600519", "601234"],
            "证券简称": ["贵州茅台", "某某退"],
            "上市日期": ["1990-12-19", "2010-05-20"],
            "终止上市日期": ["2024-11-29", "2016-07-25"],
        }
    )

def _securities_catalog():
    """构造名录帧（东财口径：只有当前在市证券，且不含任何日期）"""
    return pd.DataFrame(
        {
            "ts_code": _LISTED_CODES,
            "symbol": ["600519", "000001", "300750"],
            "name": ["贵州茅台", "平安银行", "宁德时代"],
            "exchange": ["SH", "SZ", "SZ"],
            "market": ["主板", "主板", "创业板"],
            "industry": ["", "", ""],
            "area": ["", "", ""],
            "list_date": [None, None, None],
            "delist_date": [None, None, None],
            "status": ["LISTED", "LISTED", "LISTED"],
            "is_hs": ["", "", ""],
        }
    )

def _income_row(ts_code, report_period, announce_date, revenue, net_profit, eps):
    """构造利润表契约帧（单行，列序取自契约）"""
    values = {column: [None] for column in INCOME_STATEMENT_COLUMNS}
    values.update(
        {
            "ts_code": [ts_code],
            "report_period": [report_period],
            "announce_date": [announce_date],
            "available_date": [announce_date],
            "source": ["test"],
            "source_record_id": ["%s|%s" % (ts_code.split(".")[0], report_period)],
            "revenue": [revenue],
            "operating_cost": [revenue * 0.1 if revenue else None],
            "operating_profit": [revenue * 0.6 if revenue else None],
            "total_profit": [revenue * 0.6 if revenue else None],
            "income_tax": [revenue * 0.1 if revenue else None],
            "net_profit": [net_profit],
            "net_profit_attributable": [net_profit],
            "eps": [eps],
        }
    )
    return pd.DataFrame(values)

def _balance_row(ts_code, report_period, announce_date, equity, total_assets, debt):
    """构造资产负债表契约帧（单行，列序取自契约）"""
    values = {column: [None] for column in BALANCE_SHEET_COLUMNS}
    values.update(
        {
            "ts_code": [ts_code],
            "report_period": [report_period],
            "announce_date": [announce_date],
            "available_date": [announce_date],
            "source": ["test"],
            "source_record_id": ["%s|%s" % (ts_code.split(".")[0], report_period)],
            "equity": [equity],
            "total_assets": [total_assets],
            "interest_bearing_debt": [debt],
        }
    )
    return pd.DataFrame(values)

def _universe_codes(database, as_of_date):
    """取某历史时点的股票池（返回排序后的代码列表）"""
    frame = SecurityRepository(database).universe(as_of_date)
    return sorted(frame["ts_code"].tolist())

def _as_text(value):
    """把库内读回的日期统一成 YYYY-MM-DD 文本（DuckDB DATE 读回为 Timestamp）"""
    if value is None or value != value:
        return ""
    return str(pd.to_datetime(value).date())

@pytest.fixture
def listing_calendar():
    """归一化后的上市日历（后续合并 / 事件 / 幸存者偏差用例共用）"""
    return normalize_listing_calendar([_listing_raw()])

@pytest.fixture
def delisting_calendar():
    """归一化后的退市日历（自带上市日期回填来源）"""
    return normalize_delisting_calendar([_delisting_raw()])

@pytest.fixture
def merged_lifecycle(listing_calendar, delisting_calendar):
    """合并后的证券名录（日期回填 + 状态推导 + 补入名录外的退市股）"""
    return merge_lifecycle(_securities_catalog(), listing_calendar, delisting_calendar)

@pytest.fixture
def lifecycle_events(listing_calendar, delisting_calendar):
    return build_lifecycle_events(listing_calendar, delisting_calendar)

def test_listing_calendar_normalization(listing_calendar):
    """上市日历归一化：列名匹配交易所口径"""
    listing = listing_calendar
    assert list(listing.columns) == ["ts_code", "name", "list_date"]
    assert sorted(listing["ts_code"]) == sorted(_LISTED_CODES)
    listing_dates = dict(zip(listing["ts_code"], listing["list_date"]))
    assert str(listing_dates["600519.SH"]) == "1990-12-19"
    print("  -> 上市日历归一化 %d 条（列名自动匹配交易所口径）" % len(listing))

def test_delisting_calendar_normalization(delisting_calendar):
    """退市日历归一化，并回填上市日期来源"""
    delisting = delisting_calendar
    delisting_rows = delisting.set_index("ts_code")
    assert sorted(delisting["ts_code"]) == ["600519.SH", _DELISTED_CODE]
    assert str(delisting_rows.loc[_DELISTED_CODE, "delist_date"]) == "2016-07-25"
    assert str(delisting_rows.loc[_DELISTED_CODE, "list_date"]) == "2010-05-20"

def test_listing_calendar_rejects_missing_column():
    """缺关键源列必须显式失败"""
    with pytest.raises(DataContractError):
        normalize_listing_calendar([_listing_raw().drop(columns=["A股上市日期"])])

def test_lifecycle_merge(merged_lifecycle):
    """生命周期合并：日期回填、状态推导、补入不在名录中的退市股"""
    merged = merged_lifecycle
    assert list(merged.columns) == list(SECURITY_COLUMNS)

    rows = merged.set_index("ts_code")
    # 600519 同时出现在退市日历里：既有证券按日期推导状态
    assert rows.loc["600519.SH", "status"] == "DELISTED"
    assert str(rows.loc["600519.SH", "delist_date"]) == "2024-11-29"
    # 退市股的上市日只能来自退市日历（它早已不在「当前在市」日历里）
    assert str(rows.loc[_DELISTED_CODE, "list_date"]) == "2010-05-20"
    assert rows.loc[_DELISTED_CODE, "status"] == "DELISTED"
    # 名录里没有的退市股必须整行补入，否则永远进不了股票池
    assert _DELISTED_CODE in rows.index
    assert rows.loc["300750.SZ", "status"] == "LISTED"
    assert str(rows.loc["300750.SZ", "list_date"]) == "2018-06-11"
    assert set(merged["status"]) <= set(SECURITY_STATUSES)

def test_lifecycle_events(lifecycle_events):
    """生命周期事件帧：上市 / 退市事件与来源"""
    events = lifecycle_events
    assert list(events.columns) == ["ts_code", "event_date", "event_type", "detail", "source"]
    listed_events = set(events[events["event_type"] == "LISTED"]["ts_code"])
    delisted_events = set(events[events["event_type"] == "DELISTED"]["ts_code"])
    assert {"600519.SH", "000001.SZ", "300750.SZ"} <= listed_events
    assert _DELISTED_CODE in listed_events  # 上市事件来自退市日历自带的上市日
    assert delisted_events == {"600519.SH", _DELISTED_CODE}
    print("  -> 事件帧 %d 条：LISTED %d、DELISTED %d"
          % (len(events), len(listed_events), len(delisted_events)))

def test_survivorship_universe(merged_lifecycle, lifecycle_events, fresh_db):
    """幸存者偏差防线：universe(as_of) 还原历史时点的证券集合"""
    merged, events = merged_lifecycle, lifecycle_events
    db_path, _ = fresh_db
    with Database(db_path) as database:
        assert SecurityRepository(database).upsert_lifecycle(merged) == len(merged)
        assert SecurityEventRepository(database).upsert(events) == len(events)

        # 退市股（不在当前名录中）在退市前必须在池内，退市后必须出池
        assert _universe_codes(database, "1989-01-01") == []
        universe_2012 = _universe_codes(database, "2012-01-01")
        assert universe_2012 == ["000001.SZ", "600519.SH", _DELISTED_CODE], universe_2012
        assert "300750.SZ" not in universe_2012  # 2018 年才上市

        # 2016-07-25 当天已退市：不得再进入池子
        universe_2016 = _universe_codes(database, "2016-07-25")
        assert universe_2016 == ["000001.SZ", "600519.SH"], universe_2016

        # 当前时点 = 名录规模（退市股全部出池，历史样本仍可还原）
        assert _universe_codes(database, "2030-01-01") == ["000001.SZ", "300750.SZ"]
        print("  -> as-of 股票池按上市/退市日正确进出池（退市股不出样本）")

        # 事件按 as-of 可见
        event_repo = SecurityEventRepository(database)
        early = event_repo.find(as_of_date="1991-12-31")
        assert _DELISTED_CODE not in set(early["ts_code"])
        assert set(early["ts_code"]) == {"600519.SH", "000001.SZ"}
        late = event_repo.find(event_type="DELISTED", as_of_date="2030-01-01")
        assert len(late) == 2
        print("  -> 生命周期事件可按 as-of 复核（%d → %d 条）" % (len(early), len(late)))

        # 字段归属：名录同步（不带日期）不得抹掉生命周期阶段回填的日期
        SecurityRepository(database).upsert(_securities_catalog())
        repo = SecurityRepository(database)
        refreshed = repo.find_by_code("600519.SH").iloc[0]
        assert _as_text(refreshed["list_date"]) == "1990-12-19"
        assert _as_text(refreshed["delist_date"]) == "2024-11-29"
        assert refreshed["status"] == "DELISTED"
        assert _universe_codes(database, "2012-01-01") == universe_2012
        print("  -> 名录同步不会清空已回填的上市日/退市日/状态")

def test_point_in_time(fresh_db):
    """测试 Point-in-Time 查询：可见性判据必须是公告日而非报告期"""
    db_path, _ = fresh_db
    with Database(db_path) as database:
        repository = IncomeStatementRepository(database)
        income = pd.concat(
            [
                _income_row("600519.SH", "2024-12-31", "2025-04-01", 170.9, 86.2, 6.87),
                _income_row("600519.SH", "2025-06-30", "2025-08-15", 90.7, 46.0, 3.66),
                _income_row("000001.SZ", "2024-12-31", "2025-03-25", 160.0, 44.0, 2.25),
            ],
            ignore_index=True,
        )
        assert repository.upsert(income) == 3

        # 幂等：重复写入不产生重复行
        assert repository.upsert(income) == 3
        assert len(repository.find_by_code("600519.SH")) == 2

        # 报告期早于 as-of，但 4 月才公告 → 3 月底不可见（look-ahead 防线）
        hidden = repository.find_as_of("600519.SH", "2025-03-31")
        assert len(hidden) == 0, hidden
        assert repository.latest_as_of("600519.SH", "2025-03-31").empty
        print("  -> as-of 2025-03-31 不可见年报（4 月才公告），无未来泄漏")

        # 公告当日可见；半年报要到 8 月才可见
        visible = repository.find_as_of("600519.SH", "2025-05-01")
        assert list(visible["report_period"].dt.strftime("%Y-%m-%d")) == ["2024-12-31"]
        latest = repository.latest_as_of("600519.SH", "2025-05-01")
        assert len(latest) == 1
        assert str(pd.to_datetime(latest["report_period"].iloc[0]).date()) == "2024-12-31"
        assert len(repository.find_as_of("600519.SH", "2025-08-15")) == 2
        print("  -> as-of 2025-05-01 最新可见期 = 2024 年报；公告当日即可见")

        # 横截面：只含当时已公告的证券
        cross_before = repository.cross_section_as_of("2025-03-31")
        assert sorted(cross_before["ts_code"]) == ["000001.SZ"]
        cross_after = repository.cross_section_as_of("2025-04-01")
        assert sorted(cross_after["ts_code"]) == ["000001.SZ", "600519.SH"]
        cross_period = repository.cross_section_as_of(
            "2025-08-31", report_period="2025-06-30"
        )
        assert list(cross_period["ts_code"]) == ["600519.SH"]
        print("  -> 横截面按 available_date 过滤（%d → %d 只，可限定报告期）"
              % (len(cross_before), len(cross_after)))

def test_indicator_derivation():
    """测试财务指标派生：公式、同比方向、除零语义、公告日取较晚者"""

    income = pd.concat(
    [
        _income_row("600519.SH", "2024-12-31", "2025-04-01", 100.0, 50.0, 4.0),
        _income_row("600519.SH", "2025-12-31", "2026-04-02", 120.0, 60.0, 4.8),
        # 与 2025-12-31 同比：收入 -50%，验证同比方向不是「与明年比」
        _income_row("000001.SZ", "2024-12-31", "2025-03-25", 200.0, 80.0, 2.0),
        _income_row("000001.SZ", "2025-12-31", "2026-03-20", 100.0, 40.0, 1.0),
    ],
    ignore_index=True,
    )
    balance = pd.concat(
    [
        _balance_row("600519.SH", "2024-12-31", "2025-03-30", 500.0, 900.0, 100.0),
        _balance_row("600519.SH", "2025-12-31", "2026-04-01", 550.0, 950.0, 0.0),
        _balance_row("000001.SZ", "2024-12-31", "2025-03-25", 400.0, 800.0, 0.0),
        _balance_row("000001.SZ", "2025-12-31", "2026-03-18", 420.0, 820.0, 0.0),
    ],
    ignore_index=True,
    )

    indicators = build_financial_indicators(income, balance)
    assert list(indicators.columns) == list(FINANCIAL_INDICATOR_COLUMNS)
    # 报告期是 date 对象，用字符串做索引键避免与 Timestamp 的比较歧义
    lookup = indicators.copy()
    lookup["report_period"] = lookup["report_period"].astype(str)
    rows = lookup.set_index(["ts_code", "report_period"])

    first = rows.loc[("600519.SH", "2024-12-31")]
    second = rows.loc[("600519.SH", "2025-12-31")]

    # 2024：roe = 50/500，roa = 50/900，roic = 营业利润*(1-税) / (500+100)
    # 有效税率 = income_tax / total_profit = 10/60 = 1/6 → NOPAT = 60 * 5/6 = 50
    assert math.isclose(first["roe"], 0.1, rel_tol=1e-9)
    assert math.isclose(first["roa"], 50.0 / 900.0, rel_tol=1e-9)
    assert math.isclose(first["roic"], 50.0 / 600.0, rel_tol=1e-9)
    assert math.isclose(first["gross_margin"], 0.9, rel_tol=1e-9)
    assert math.isclose(first["net_margin"], 0.5, rel_tol=1e-9)
    print("  -> ROE / ROA / ROIC / 毛利率 / 净利率与手算一致")

    # 同比 = (本期 - 去年同期) / |去年同期|，首年无对比 → NaN
    assert first["revenue_yoy"] != first["revenue_yoy"]
    assert math.isclose(second["revenue_yoy"], 0.2, rel_tol=1e-9)
    assert math.isclose(second["profit_yoy"], 0.2, rel_tol=1e-9)
    assert math.isclose(second["eps_yoy"], 0.2, rel_tol=1e-9)
    decline = rows.loc[("000001.SZ", "2025-12-31")]
    assert math.isclose(decline["revenue_yoy"], -0.5, rel_tol=1e-9)
    print("  -> 同比方向正确（+20% / -50%），首年同比为 NaN")

    # 公告日取利润表与资产负债表的较晚者（任何一方未公告都不得视为可用）
    assert str(first["announce_date"]) == "2025-04-01"  # 资产负债表 3-30，利润表 4-01
    assert str(second["announce_date"]) == "2026-04-02"  # 资产负债表 4-01，利润表 4-02
    assert str(first["available_date"]) == str(first["announce_date"])
    print("  -> announce_date / available_date 取两表较晚者（Point-in-Time 正确）")

    # 除零与缺表 → NaN，不产生 inf
    zero_balance = pd.concat(
    [
        _balance_row("000001.SZ", "2024-12-31", "2025-03-25", 0.0, 0.0, 0.0),
        _balance_row("000001.SZ", "2025-12-31", "2026-03-18", 0.0, 0.0, 0.0),
    ],
    ignore_index=True,
    )
    zero_case = build_financial_indicators(
    income[income["ts_code"] == "000001.SZ"], zero_balance
    )
    assert zero_case["roe"].isna().all() and zero_case["roa"].isna().all()

    no_balance = pd.DataFrame(columns=list(BALANCE_SHEET_COLUMNS))
    missing_balance = build_financial_indicators(income, no_balance)
    assert missing_balance["roe"].isna().all()
    assert missing_balance["roic"].isna().all()
    assert not any(math.isinf(value) for value in indicators["roic"])
    print("  -> 分母为 0 或缺表时指标为 NaN，不产生 inf")

    # 利润表为空 → 返回契约列序空表（调用方据此跳过写入）
    empty = build_financial_indicators(pd.DataFrame(columns=list(INCOME_STATEMENT_COLUMNS)),
                                   no_balance)
    assert empty.empty and list(empty.columns) == list(FINANCIAL_INDICATOR_COLUMNS)
    print("  -> 空利润表返回契约列序空表")


if __name__ == "__main__":
    # 保住旧的直接执行入口：委托给 pytest，退出码语义一致
    raise SystemExit(pytest.main([__file__, "-v"]))
