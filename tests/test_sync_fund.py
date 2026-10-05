#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金域同步 CLI 测试 (tests/test_sync_fund.py)
==============================================================================

【功能用途】
  覆盖 app/scripts/sync_fund.py（全离线，四个采集器一律打桩）：
    1. 基金代码归一与解析（拼错的代码要在入口就拒掉）
    2. 最近交易日归属 —— 资金流是「即时」快照，假日跑同步不能把快照记成今天
    3. 四类写方（信息/净值/持仓/资金流）真的落库，以及失败时的退出码
    4. 登记清单只补新代码、不覆盖已有详情（否则每跑一次就把基金经理清空）

【运行方式】
  ./venv/bin/python -m pytest tests/test_sync_fund.py -v
"""

import os
import sys

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import closing
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from app.scripts import sync_fund as sf
from stocklab.domain import (
    CAPITAL_FLOW_DAILY_COLUMNS,
    FUND_HOLDING_COLUMNS,
    FUND_INFO_COLUMNS,
    FUND_NAV_HISTORY_COLUMNS,
    STOCK_INDUSTRY_MAPPING_COLUMNS,
)
from stocklab.persistence import Database
from stocklab.persistence.storage import initialize_database


# ---------------------------------------------------------------------------
# 库：只给路径，不长期占连接 —— sync() 自己会开连接，DuckDB 同一文件只允许
# 一个写连接，fixture 一直握着会在 sync() 里报 Could not set lock
# ---------------------------------------------------------------------------
@pytest.fixture
def db_path(tmp_path):
    return initialize_database(str(tmp_path / "sync_fund.duckdb"))


def _open(db_path):
    return Database(db_path)


def _query(db_path, sql, params=None):
    """短连接查询，用完即关"""
    db = _open(db_path)
    try:
        return db.get_connection().execute(sql, params or []).fetchdf()
    finally:
        db.close()


def _count(db_path, table):
    return int(_query(db_path, f"SELECT COUNT(*) AS n FROM {table}")["n"].iloc[0])


# ---------------------------------------------------------------------------
# 打桩数据
# ---------------------------------------------------------------------------
def _info_frame(code="000001.OF", manager="张明"):
    return pd.DataFrame([{
        "fund_code": code, "fund_name": "华夏成长混合", "fund_short_name": "华夏成长",
        "fund_type": "混合型-偏股", "manager_name": manager, "company_name": "华夏基金",
        "establish_date": date(2001, 12, 18), "benchmark": "沪深300*70%+中债综指*30%",
        "status": "active", "source": "akshare:fund_individual",
        "fetched_at": datetime.now(),
    }])[list(FUND_INFO_COLUMNS)]


def _nav_frame(code="000001.OF", days=5, start=date(2026, 9, 23)):
    dates = [start + timedelta(days=i) for i in range(days)]
    return pd.DataFrame({
        "fund_code": [code] * days,
        "nav_date": dates,
        "nav": [1.0 + i * 0.01 for i in range(days)],
        "acc_nav": [3.0 + i * 0.01 for i in range(days)],
        "change_pct": [0.1] * days,
        "source": ["eastmoney:lsjz"] * days,
        "fetched_at": [datetime.now()] * days,
    })[list(FUND_NAV_HISTORY_COLUMNS)]


def _holding_frame(code="000001.OF", stocks=("600519.SH", "000858.SZ")):
    return pd.DataFrame({
        "fund_code": [code] * len(stocks),
        "report_date": [date(2026, 6, 30)] * len(stocks),
        "report_type": ["2026中报"] * len(stocks),
        "stock_code": list(stocks),
        "stock_name": ["股票"] * len(stocks),
        "weight": [5.0] * len(stocks),
        "market_value": [1e9] * len(stocks),
        "rank": [1, 2][:len(stocks)],
        "source": ["test"] * len(stocks),
        "fetched_at": [datetime.now()] * len(stocks),
    })[list(FUND_HOLDING_COLUMNS)]


def _individual_frame():
    return pd.DataFrame({
        "stock_code": ["600519", "000858"],
        "stock_name": ["贵州茅台", "五粮液"],
        "inflow": [4.0e9, 2.0e9],
        "outflow": [3.0e9, 2.5e9],
        "net_inflow": [1.0e9, -5.0e8],
        "turnover": [7.0e9, 4.5e9],
    })


def _mapping_frame():
    return pd.DataFrame({
        "ts_code": ["600519.SH", "000858.SZ"],
        "sw_l1_code": ["801010", "801050"],
        "sw_l1_name": ["农林牧渔", "有色金属"],
        "sw_l2_code": ["801011", "801051"],
        "sw_l2_name": ["种植业", "能源金属"],
        "sw_l3_code": ["801012", "801052"],
        "sw_l3_name": ["种子", "锂"],
        "citics_l1_code": ["101010", "101020"],
        "citics_l1_name": ["农林牧渔", "有色金属"],
        "citics_l2_code": ["101011", "101021"],
        "citics_l2_name": ["林业", "稀有金属"],
        "wind_l1_code": ["100000", "100001"],
        "wind_l1_name": ["农林牧渔", "有色金属"],
        "updated_at": [datetime.now()] * 2,
    })[list(STOCK_INDUSTRY_MAPPING_COLUMNS)]


@pytest.fixture
def stub_sources(monkeypatch):
    """把四个采集器全部打桩；返回各源被调用次数"""
    calls = {"info": 0, "nav": 0, "holding": 0, "individual": 0, "list": 0}

    def fake_info(code):
        calls["info"] += 1
        return _info_frame(code)

    def fake_nav(code, start_date=None, end_date=None):
        calls["nav"] += 1
        return _nav_frame(code)

    def fake_holding(code, years=2):
        calls["holding"] += 1
        return _holding_frame(code)

    def fake_individual():
        calls["individual"] += 1
        return _individual_frame()

    def fake_list():
        calls["list"] += 1
        return pd.DataFrame({"fund_code": ["900001.OF", "900002.OF"],
                             "fund_name": ["清单甲", "清单乙"],
                             "fund_type": ["股票型", "债券型"]})

    monkeypatch.setattr(sf, "fetch_fund_info", fake_info)
    monkeypatch.setattr(sf, "fetch_fund_nav_history", fake_nav)
    monkeypatch.setattr(sf, "fetch_fund_holding_history", fake_holding)
    monkeypatch.setattr(sf, "fetch_stock_fund_flow_individual", fake_individual)
    monkeypatch.setattr(sf, "fetch_all_fund_codes", fake_list)
    monkeypatch.setattr(sf, "StockIndustryMappingRepository", lambda db: _MappingRepoStub())
    return calls


class _MappingRepoStub:
    def find_all(self):
        return _mapping_frame()


# ===========================================================================
# 1. 参数解析与代码归一
# ===========================================================================
class TestParseArgs:
    def test_defaults(self):
        args = sf.parse_args([])
        assert args.command == "sync"
        assert args.nav_days == 730
        assert args.holding_years == 2
        assert args.flow_levels == "1,2"
        assert args.skip_holdings is False
        assert args.skip_flow is False
        assert args.skip_nav is False
        assert args.dry_run is False

    def test_status_subcommand(self):
        assert sf.parse_args(["status"]).command == "status"

    @pytest.mark.parametrize("flag", ["--skip-holdings", "--skip-flow", "--skip-nav"])
    def test_skip_flags(self, flag):
        assert getattr(sf.parse_args(["sync", flag]), flag.replace("--", "").replace("-", "_")) is True


class TestNormalizeCodes:
    @pytest.mark.parametrize("raw,expect", [
        ("000001", ["000001.OF"]),
        ("1.OF", ["000001.OF"]),              # 单数字也要补零到 6 位
        ("000001.OF", ["000001.OF"]),         # 幂等
        ("000001,000001.OF,1.OF", ["000001.OF"]),  # 去重
        ("61725", ["061725.OF"]),
        ("000001, 161725 ,161725.OF", ["000001.OF", "161725.OF"]),
        ("", []),
        (None, []),
        (",,", []),
    ])
    def test_valid(self, raw, expect):
        assert sf._normalize_codes(raw) == expect

    @pytest.mark.parametrize("raw", ["ABC", "ABC.OF", "abc.def", "000001;161725"])
    def test_invalid_dropped(self, raw):
        """拼错的代码在入口就丢掉，不能一路传到采集层变成含糊的抓取失败"""
        assert sf._normalize_codes(raw) == []


# ===========================================================================
# 2. 基金代码解析来源
# ===========================================================================
class TestResolveCodes:
    class _Args:
        codes = None

    def test_explicit_beats_registered(self, db_path):
        db = _open(db_path)
        try:
            repo = sf.FundInfoRepository(db)
            repo.upsert(_info_frame("000002.OF"))
        finally:
            db.close()

        args = sf.parse_args(["--codes", "000001.OF"])
        with closing(_open(db_path)) as db:
            assert sf.resolve_codes(args, db) == ["000001.OF"]

    def test_falls_back_to_registered(self, db_path):
        db = _open(db_path)
        try:
            sf.FundInfoRepository(db).upsert(
                pd.concat([_info_frame("000001.OF"), _info_frame("000002.OF")],
                          ignore_index=True)
            )
        finally:
            db.close()

        with closing(_open(db_path)) as db:
            assert sf.resolve_codes(sf.parse_args([]), db) == ["000001.OF", "000002.OF"]

    def test_empty_db_without_codes(self, db_path):
        with closing(_open(db_path)) as db:
            assert sf.resolve_codes(sf.parse_args([]), db) == []


# ===========================================================================
# 3. 最近交易日归属
# ===========================================================================
class TestLatestTradeDate:
    def _seed_prices(self, db_path, trade_date):
        db = _open(db_path)
        try:
            db.get_connection().execute(
                "INSERT INTO market.daily_prices "
                "(ts_code, trade_date, open, high, low, close, volume, amount) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["600519.SH", trade_date, 100.0, 101.0, 99.0, 100.5, 1e6, 1e8],
            )
        finally:
            db.close()

    def test_uses_latest_market_date(self, db_path):
        """假日跑同步时，快照必须记在**最近交易日**而不是今天"""
        last_trade = date.today() - timedelta(days=2)
        self._seed_prices(db_path, last_trade)

        assert sf.latest_trade_date(_open(db_path)) == last_trade

    def test_stale_local_data_falls_back_to_today(self, db_path):
        """本地行情太旧（>7 天）说明没在同步，此时按今天归属并由调用方告警"""
        self._seed_prices(db_path, date.today() - timedelta(days=30))

        assert sf.latest_trade_date(_open(db_path)) == date.today()

    def test_no_local_data_falls_back_to_today(self, db_path):
        assert sf.latest_trade_date(_open(db_path)) == date.today()

    def test_takes_freshest_of_two_tables(self, db_path):
        old, new = date.today() - timedelta(days=3), date.today() - timedelta(days=1)
        self._seed_prices(db_path, old)
        db = _open(db_path)
        try:
            conn = db.get_connection()
            conn.execute(
                "INSERT INTO sw.index_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["801010", new, 100.0, 101.0, 99.0, 100.5, 1e8, 1e9,
                 "test", datetime.now()],
            )
        finally:
            db.close()

        assert sf.latest_trade_date(_open(db_path)) == new


# ===========================================================================
# 4. 同步主流程与退出码
# ===========================================================================
class TestSync:
    def test_no_codes_and_empty_db_exits_2(self, db_path):
        assert sf.main(["sync", "--db-path", db_path]) == 2

    def test_dry_run_writes_nothing(self, db_path, stub_sources):
        assert sf.main(["sync", "--codes", "000001.OF", "--dry-run",
                        "--db-path", db_path]) == 0
        assert _count(db_path, "fund.fund_info") == 0
        assert _count(db_path, "fund.nav_history") == 0
        assert stub_sources["info"] == 0     # dry-run 连采集都不该发

    def test_full_sync_writes_all_four(self, db_path, stub_sources):
        # 先落一行行情，让资金流有「最近交易日」可归属
        last_trade = date.today() - timedelta(days=1)
        db = _open(db_path)
        try:
            db.get_connection().execute(
                "INSERT INTO market.daily_prices "
                "(ts_code, trade_date, open, high, low, close, volume, amount) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["600519.SH", last_trade, 100.0, 101.0, 99.0, 100.5, 1e6, 1e8],
            )
        finally:
            db.close()

        code = sf.main(["sync", "--codes", "000001.OF", "--db-path", db_path])
        assert code == 0

        info = _query(db_path, "SELECT * FROM fund.fund_info")
        assert len(info) == 1 and info["fund_code"].iloc[0] == "000001.OF"
        assert info["manager_name"].iloc[0] == "张明"

        nav = _query(db_path, "SELECT * FROM fund.nav_history ORDER BY nav_date")
        assert len(nav) == 5
        assert nav["source"].eq("eastmoney:lsjz").all()

        assert _count(db_path, "fund.fund_holding") == 2

        # 资金流按**最近交易日**归属，L1+L2 各 2 行
        flow = _query(db_path, "SELECT * FROM capital.flow_daily ORDER BY sector_type")
        assert len(flow) == 4
        assert set(flow["sector_type"]) == {"sw_level1", "sw_level2"}
        # DATE 列经 fetchdf 变成 datetime64，比较前先落回 date
        assert pd.to_datetime(flow["trade_date"]).dt.date.eq(last_trade).all()
        assert flow["source"].eq("sina:individual_fund_flow").all()

        # 只抓一次个股流，L1/L2 共用（105 页分页，12 秒，不该抓两遍）
        assert stub_sources["individual"] == 1

    @pytest.mark.parametrize("flag,table", [
        ("--skip-nav", "fund.nav_history"),
        ("--skip-holdings", "fund.fund_holding"),
        ("--skip-flow", "capital.flow_daily"),
    ])
    def test_skip_flags(self, db_path, stub_sources, flag, table):
        code = sf.main(["sync", "--codes", "000001.OF", flag, "--db-path", db_path])
        assert code == 0
        assert _count(db_path, table) == 0

    def test_nav_failure_exits_1(self, db_path, stub_sources, monkeypatch):
        """净值是 RBSA 的输入：取不到必须以退出码 1 显式失败，不能静默当成功"""
        monkeypatch.setattr(sf, "fetch_fund_nav_history",
                            lambda code, start_date=None, end_date=None:
                            pd.DataFrame(columns=list(FUND_NAV_HISTORY_COLUMNS)))

        assert sf.main(["sync", "--codes", "000001.OF", "--db-path", db_path]) == 1

    def test_info_failure_does_not_block_nav(self, db_path, stub_sources, monkeypatch):
        """信息源（雪球）挂了不阻断净值（东财）——两者是不同上游"""
        def boom(code):
            raise RuntimeError("雪球 502")

        monkeypatch.setattr(sf, "fetch_fund_info", boom)

        assert sf.main(["sync", "--codes", "000001.OF", "--skip-holdings",
                        "--skip-flow", "--db-path", db_path]) == 1
        assert _count(db_path, "fund.nav_history") == 5   # 净值照常落库

    def test_holding_failure_exits_1(self, db_path, stub_sources, monkeypatch):
        monkeypatch.setattr(sf, "fetch_fund_holding_history",
                            lambda code, years=2: pd.DataFrame())
        assert sf.main(["sync", "--codes", "000001.OF", "--skip-flow",
                        "--db-path", db_path]) == 1

    @pytest.mark.parametrize("levels,expect", [
        ("1", {"sw_level1"}),
        ("2", {"sw_level2"}),
        ("1,2", {"sw_level1", "sw_level2"}),
    ])
    def test_flow_levels_selection(self, db_path, stub_sources, levels, expect):
        assert sf.main(["sync", "--codes", "000001.OF", "--skip-holdings",
                        "--flow-levels", levels, "--db-path", db_path]) == 0
        flow = _query(db_path, "SELECT DISTINCT sector_type FROM capital.flow_daily")
        assert set(flow["sector_type"]) == expect

    def test_bad_flow_level_exits_1(self, db_path, stub_sources):
        assert sf.main(["sync", "--codes", "000001.OF", "--skip-holdings",
                        "--flow-levels", "3", "--db-path", db_path]) == 1
        assert _count(db_path, "capital.flow_daily") == 0

    def test_missing_mapping_exits_1(self, db_path, stub_sources, monkeypatch):
        """没有股票行业映射就聚合不出申万口径 —— 必须失败，不能退回东财口径凑数"""
        monkeypatch.setattr(sf, "StockIndustryMappingRepository",
                            lambda db: _EmptyMappingRepo())
        assert sf.main(["sync", "--codes", "000001.OF", "--skip-holdings",
                        "--db-path", db_path]) == 1
        assert _count(db_path, "capital.flow_daily") == 0

    def test_individual_flow_failure_exits_1(self, db_path, stub_sources, monkeypatch):
        monkeypatch.setattr(sf, "fetch_stock_fund_flow_individual",
                            lambda: pd.DataFrame())
        assert sf.main(["sync", "--codes", "000001.OF", "--skip-holdings",
                        "--db-path", db_path]) == 1

    @pytest.mark.parametrize("nav_days,holding_years", [(0, 2), (730, -1)])
    def test_bad_periods_exit_2(self, db_path, nav_days, holding_years):
        assert sf.main(["sync", "--codes", "000001.OF",
                        "--nav-days", str(nav_days),
                        "--holding-years", str(holding_years),
                        "--db-path", db_path]) == 2


class _EmptyMappingRepo:
    def find_all(self):
        return pd.DataFrame(columns=list(STOCK_INDUSTRY_MAPPING_COLUMNS))


# ===========================================================================
# 5. 清单登记：只补新代码，不覆盖已有详情
# ===========================================================================
class TestRegister:
    def test_register_adds_only_new_codes(self, db_path, stub_sources):
        # 已有一只带详情的基金
        db = _open(db_path)
        try:
            sf.FundInfoRepository(db).upsert(_info_frame("000001.OF", manager="张明"))
        finally:
            db.close()

        assert sf.main(["sync", "--codes", "000001.OF", "--register",
                        "--skip-holdings", "--skip-flow",
                        "--db-path", db_path]) == 0

        info = _query(db_path, "SELECT fund_code, fund_name, manager_name "
                               "FROM fund.fund_info ORDER BY fund_code")
        assert set(info["fund_code"]) == {"000001.OF", "900001.OF", "900002.OF"}
        # 详情不能被清单覆盖成空：清单只有名称/类型，没有基金经理
        row = info[info["fund_code"] == "000001.OF"].iloc[0]
        assert row["manager_name"] == "张明"
        # 新登记的代码详情留空（不是编一个出来），状态先记 active
        assert pd.isna(info[info["fund_code"] == "900001.OF"].iloc[0]["manager_name"])

    def test_register_failure_is_recorded_but_does_not_abort(self, db_path, stub_sources,
                                                             monkeypatch):
        def boom():
            raise RuntimeError("东财清单 502")

        monkeypatch.setattr(sf, "fetch_all_fund_codes", boom)
        # 登记失败 -> 有失败项 -> 退出码 1，但指定的基金照常同步
        assert sf.main(["sync", "--codes", "000001.OF", "--register",
                        "--skip-holdings", "--skip-flow",
                        "--db-path", db_path]) == 1
        assert _count(db_path, "fund.fund_info") == 1


# ===========================================================================
# 6. 只读状态
# ===========================================================================
class TestStatus:
    def test_status_exits_0(self, db_path, caplog):
        assert sf.main(["status", "--db-path", db_path]) == 0

    def test_status_reports_counts(self, db_path, caplog):
        db = _open(db_path)
        try:
            sf.FundInfoRepository(db).upsert(_info_frame())
            sf.FundNavHistoryRepository(db).upsert(_nav_frame())
        finally:
            db.close()

        with caplog.at_level("INFO"):
            assert sf.main(["status", "--db-path", db_path]) == 0

        text = " ".join(r.message for r in caplog.records)
        assert "fund.fund_info" in text and "fund.nav_history" in text
