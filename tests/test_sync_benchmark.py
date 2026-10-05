#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基准快照同步 CLI 测试 (tests/test_sync_benchmark.py)
==============================================================================

【功能用途】
  覆盖 app/scripts/sync_benchmark.py 的三件事（全离线，akshare 一律打桩）：
    1. 参数解析与基准白名单（非法基准必须退出码 2）
    2. 申万指数目录刷新（读写成对：/api/sw/indices 读 sw.industry_mapping，
       过去全项目没有任何写方，接口恒回空列表）
    3. 行业指数日线同步要**跨层级**收集行业代码（二级行业归因要指数收益）

【运行方式】
  ./venv/bin/python -m pytest tests/test_sync_benchmark.py -v
"""

import os
import sys

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta

import pandas as pd
import pytest

import argparse

from app.scripts import sync_benchmark as sb
from stocklab.domain import (
    BENCHMARK_INDUSTRY_WEIGHT_COLUMNS,
    STOCK_INDUSTRY_MAPPING_COLUMNS,
    SW_INDEX_DAILY_COLUMNS,
    SW_INDUSTRY_MAPPING_COLUMNS,
)
from stocklab.persistence import Database
from stocklab.persistence.repository.benchmark import BenchmarkIndustryWeightRepository
from stocklab.persistence.repository.fund_analysis import (
    SWIndexDailyRepository,
    SWIndustryMappingRepository,
)
from stocklab.persistence.storage import initialize_database


@pytest.fixture
def database(tmp_path):
    """临时库：先跑迁移再开连接（与 sync_benchmark 的 main() 同一顺序）"""
    db_path = initialize_database(str(tmp_path / "sync_benchmark.duckdb"))
    db = Database(db_path)
    yield db
    db.close()


def _frame(columns, **values):
    return pd.DataFrame(values, columns=list(columns))


def _weight_rows(benchmark, level, sectors):
    now = pd.Timestamp.now()
    return _frame(
        BENCHMARK_INDUSTRY_WEIGHT_COLUMNS,
        benchmark_code=[benchmark] * len(sectors),
        level=[level] * len(sectors),
        sector_code=sectors,
        sector_name=[f"行业{s}" for s in sectors],
        weight=[1.0 / len(sectors)] * len(sectors),
        as_of_date=[date(2026, 8, 31)] * len(sectors),
        coverage=[0.99] * len(sectors),
        source=["test"] * len(sectors),
        fetched_at=[now] * len(sectors),
    )


def _index_frame(symbol):
    today = date.today()
    return _frame(
        SW_INDEX_DAILY_COLUMNS,
        symbol=[symbol, symbol],
        trade_date=[today - timedelta(days=1), today],
        open=[1000.0, 1010.0],
        high=[1015.0, 1015.0],
        low=[995.0, 1005.0],
        close=[1010.0, 1020.0],
        volume=[1e8, 1e8],
        amount=[1e9, 1e9],
        source=["test"] * 2,
        fetched_at=[pd.Timestamp.now()] * 2,
    )


# ---------------------------------------------------------------------------
# 1. 参数解析
# ---------------------------------------------------------------------------
class TestParseArgs:
    def test_defaults(self):
        args = sb.parse_args([])
        assert args.command == "sync"
        assert args.level is None          # 缺省 1 和 2 都刷
        assert args.with_mapping is False
        assert args.with_sw_indices is False
        assert args.dry_run is False
        assert args.benchmark is None

    def test_level_accepts_repeats(self):
        assert sb.parse_args(["sync", "--level", "1", "--level", "2"]).level == [1, 2]
        assert sb.parse_args(["sync", "--level", "2"]).level == [2]

    @pytest.mark.parametrize("bad", [["--level", "3"], ["--level", "abc"]])
    def test_level_out_of_range_exits(self, bad):
        with pytest.raises(SystemExit):
            sb.parse_args(bad)

    def test_status_subcommand(self):
        assert sb.parse_args(["status"]).command == "status"


class TestResolveBenchmarks:
    def test_defaults_when_unspecified(self):
        assert sb._resolve_benchmarks(None) == list(sb._DEFAULT_BENCHMARKS)

    @pytest.mark.parametrize("raw,expected", [
        (["000300"], ["000300.SH"]),
        (["000300.SH"], ["000300.SH"]),
        (["000905", "000905.SH"], ["000905.SH"]),   # 去重
    ])
    def test_known_codes_normalized(self, raw, expected):
        assert sb._resolve_benchmarks(raw) == expected

    @pytest.mark.parametrize("bad", [["399001.SZ"], ["000001"]])
    def test_unknown_code_exits_with_2(self, bad):
        """非中证指数取不到成分权重，必须在起跑前退出码 2 报清楚，而不是抓到一半失败"""
        with pytest.raises(SystemExit) as excinfo:
            sb._resolve_benchmarks(bad)
        assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# 2. 申万指数目录（读写成对）
# ---------------------------------------------------------------------------
class TestSyncSwCatalog:
    def test_catalog_is_written(self, monkeypatch, database):
        catalog = _frame(
            SW_INDUSTRY_MAPPING_COLUMNS,
            index_code=["801010", "801016"],
            index_name=["农林牧渔", "种植业"],
            level=[1, 2],
            parent_code=["", "801010"],
            sw_first_code=["", "801010"],
            description=["", ""],
        )
        monkeypatch.setattr(sb, "fetch_sw_industry_mapping", lambda: catalog)

        sb._sync_sw_catalog(database)

        stored = SWIndustryMappingRepository(database).find_all()
        assert len(stored) == 2
        assert dict(zip(stored["index_code"], stored["parent_code"]))["801016"] == "801010"

    def test_empty_catalog_leaves_table_empty(self, monkeypatch, database):
        monkeypatch.setattr(sb, "fetch_sw_industry_mapping", lambda: pd.DataFrame(
            columns=list(SW_INDUSTRY_MAPPING_COLUMNS)))

        sb._sync_sw_catalog(database)

        assert SWIndustryMappingRepository(database).find_all().empty

    def test_failure_does_not_break_index_sync(self, monkeypatch, database):
        def boom():
            raise RuntimeError("legulegu 偶发 NoneType")
        monkeypatch.setattr(sb, "fetch_sw_industry_mapping", boom)

        sb._sync_sw_catalog(database)     # 不抛即通过：目录失败不该拖垮指数日线

        assert SWIndustryMappingRepository(database).find_all().empty


# ---------------------------------------------------------------------------
# 3. 行业指数日线：跨层级收集
# ---------------------------------------------------------------------------
class TestSyncSwIndices:
    @staticmethod
    def _stub_daily(monkeypatch, fail_for=()):
        calls = []

        def fake(symbol, start, end):
            calls.append(symbol)
            if symbol in fail_for:
                raise RuntimeError("接口 502")
            return _index_frame(symbol)

        monkeypatch.setattr(sb, "fetch_sw_index_daily", fake)
        monkeypatch.setattr(sb, "fetch_sw_industry_mapping", lambda: pd.DataFrame(
            columns=list(SW_INDUSTRY_MAPPING_COLUMNS)))
        return calls

    def test_collects_sectors_from_both_levels(self, monkeypatch, database):
        calls = self._stub_daily(monkeypatch)
        repo = BenchmarkIndustryWeightRepository(database)
        repo.upsert(_weight_rows("000300.SH", 1, ["801010", "801030"]))
        repo.upsert(_weight_rows("000300.SH", 2, ["801016"]))

        failures = sb._sync_sw_indices(database, ["000300.SH"], days=30)

        # 二级行业也在内：只收 level=1 会让二级归因算不出基准收益
        assert set(calls) == {"801010.SI", "801030.SI", "801016.SI"}
        assert failures == []
        assert not SWIndexDailyRepository(database).load_series(
            ["801016.SI"], since=date.today() - timedelta(days=3)
        ).empty

    def test_single_failure_reported_others_still_written(self, monkeypatch, database):
        calls = self._stub_daily(monkeypatch, fail_for=("801030.SI",))
        BenchmarkIndustryWeightRepository(database).upsert(
            _weight_rows("000300.SH", 1, ["801010", "801030"]))

        failures = sb._sync_sw_indices(database, ["000300.SH"], days=30)

        # 一个指数挂掉要报进失败清单（退出码 1），但不能把其他指数也写失败
        assert failures == ["801030.SI"]
        assert len(calls) == 2
        assert not SWIndexDailyRepository(database).load_series(
            ["801010.SI"], since=date.today() - timedelta(days=3)
        ).empty

    def test_empty_frame_counts_as_failure(self, monkeypatch, database):
        monkeypatch.setattr(sb, "fetch_sw_index_daily",
                            lambda symbol, start, end: pd.DataFrame())
        monkeypatch.setattr(sb, "fetch_sw_industry_mapping", lambda: pd.DataFrame(
            columns=list(SW_INDUSTRY_MAPPING_COLUMNS)))
        BenchmarkIndustryWeightRepository(database).upsert(
            _weight_rows("000300.SH", 1, ["801010"]))

        assert sb._sync_sw_indices(database, ["000300.SH"], days=30) == ["801010.SI"]

    def test_no_snapshot_short_circuits(self, monkeypatch, database):
        calls = self._stub_daily(monkeypatch)

        assert sb._sync_sw_indices(database, ["000300.SH"], days=30) == []
        assert calls == []


# ---------------------------------------------------------------------------
# 4. sync() 编排：退出码语义（0 全成 / 1 有失败 / 2 参数非法）
# ---------------------------------------------------------------------------
def _args(**overrides):
    base = dict(command="sync", benchmark=None, with_mapping=False,
                with_sw_indices=False, days=30, db_path=None, dry_run=False,
                level=None)
    base.update(overrides)
    return argparse.Namespace(**base)


class _FakeFacade:
    """只关心 refresh_industry_weights 的调用与返回形状（frame + meta）"""

    def __init__(self, database, fail_levels=(), empty_levels=()):
        self.database = database
        self.calls = []
        self.fail_levels = tuple(fail_levels)
        self.empty_levels = tuple(empty_levels)

    def refresh_industry_weights(self, code, level=1):
        self.calls.append((code, level))
        now = pd.Timestamp.now()
        if (code, level) in self.empty_levels:
            return {"frame": pd.DataFrame(), "meta": {"reason": "成分券抓取失败"}}
        frame = _weight_rows(code, level, ["801010"])
        return {"frame": frame, "meta": {
            "benchmark_code": code, "level": level, "sector_count": len(frame),
            "as_of_date": "2026-08-31", "coverage": 0.99, "cached": False,
        }}


@pytest.fixture
def fake_facade(monkeypatch):
    holder = {}

    def factory(database):
        facade = _FakeFacade(database)
        holder["facade"] = facade
        return facade

    monkeypatch.setattr(sb, "BenchmarkDataFacade", factory)
    monkeypatch.setattr(sb, "fetch_sw_industry_mapping",
                        lambda: pd.DataFrame(columns=list(SW_INDUSTRY_MAPPING_COLUMNS)))
    return holder


class TestSync:
    def test_dry_run_touches_nothing(self, fake_facade, tmp_path):
        args = _args(dry_run=True, db_path=str(tmp_path / "dry.duckdb"))

        assert sb.sync(args) == 0
        assert "facade" not in fake_facade, "dry-run 不该建立门面、更不该联网"

    def test_both_levels_success_returns_0(self, fake_facade, tmp_path):
        args = _args(db_path=str(tmp_path / "ok.duckdb"), benchmark=["000300.SH"])

        assert sb.sync(args) == 0
        assert fake_facade["facade"].calls == [("000300.SH", 1), ("000300.SH", 2)]

    def test_level_restriction_respected(self, fake_facade, tmp_path):
        args = _args(db_path=str(tmp_path / "l2.duckdb"), benchmark=["000300.SH"],
                     level=[2])

        assert sb.sync(args) == 0
        assert fake_facade["facade"].calls == [("000300.SH", 2)]

    def test_level1_failure_fails_run(self, fake_facade, tmp_path):
        """一级失败 = 主链路失败，退出码必须是 1，否则 CI 会当成功放过"""
        args = _args(db_path=str(tmp_path / "f1.duckdb"), benchmark=["000300.SH"])
        # 门面工厂先建好再注入失败条件
        real_factory = fake_facade

        def factory(database):
            facade = _FakeFacade(database, empty_levels={("000300.SH", 1)})
            real_factory["facade"] = facade
            return facade

        sb.BenchmarkDataFacade = factory   # fake_facade 已 patch 过该属性
        assert sb.sync(args) == 1

    def test_level2_failure_does_not_fail_run(self, fake_facade, tmp_path):
        def factory(database):
            facade = _FakeFacade(database, empty_levels={("000300.SH", 2)})
            fake_facade["facade"] = facade
            return facade

        sb.BenchmarkDataFacade = factory
        args = _args(db_path=str(tmp_path / "f2.duckdb"), benchmark=["000300.SH"])

        assert sb.sync(args) == 0, "二级只是增强，失败不该把一级成功判成失败"

    def test_with_mapping_writes_rows(self, fake_facade, monkeypatch, tmp_path):
        mapping = _frame(
            STOCK_INDUSTRY_MAPPING_COLUMNS,
            ts_code=["600000.SH", "000001.SZ"],
            sw_l1_code=["801780", "801780"],
            sw_l1_name=["银行", "银行"],
            sw_l2_code=["801780", "801780"],
            sw_l2_name=["银行", "银行"],
            updated_at=[pd.Timestamp.now()] * 2,
        )
        monkeypatch.setattr(sb, "fetch_stock_industry_mapping", lambda: mapping)
        args = _args(db_path=str(tmp_path / "map.duckdb"), benchmark=["000300.SH"],
                     with_mapping=True)

        assert sb.sync(args) == 0

        from stocklab.persistence.repository.fund_holding import (
            StockIndustryMappingRepository,
        )
        db_path = initialize_database(str(tmp_path / "map.duckdb"))
        db = Database(db_path)
        try:
            assert len(StockIndustryMappingRepository(db).find_all()) == 2
        finally:
            db.close()

    def test_sw_index_failure_fails_run(self, fake_facade, monkeypatch, tmp_path):
        # 库文件名不能叫 sw.duckdb：DuckDB 拿文件名 stem 当 catalog 名，
        # 与迁移建的 schema `sw` 同名后，所有 `sw.xxx` 引用都变成歧义错误
        monkeypatch.setattr(sb, "_sync_sw_indices",
                            lambda database, benchmarks, days: ["801010.SI"])
        args = _args(db_path=str(tmp_path / "swindex.duckdb"),
                     benchmark=["000300.SH"], with_sw_indices=True)

        assert sb.sync(args) == 1


# ---------------------------------------------------------------------------
# 5. status 与 main()
# ---------------------------------------------------------------------------
def test_show_status_returns_zero_without_network(tmp_path):
    args = _args(command="status", db_path=str(tmp_path / "status.duckdb"))
    assert sb.show_status(args) == 0


def test_main_status_subcommand(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "install_browser_user_agent", lambda: None)
    assert sb.main(["status", "--db-path", str(tmp_path / "main.duckdb")]) == 0


def test_main_sync_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "install_browser_user_agent", lambda: None)
    assert sb.main(["sync", "--dry-run", "--db-path",
                    str(tmp_path / "main2.duckdb")]) == 0
