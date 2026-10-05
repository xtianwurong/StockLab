#!/usr/bin/env python3
"""
==============================================================================
StockLab - 大宗商品域测试 (tests/test_commodities.py)
==============================================================================

【功能用途】
  覆盖大宗商品从「源站返回」到「页面拿到评级」的每一层契约：
    1. 品种登记表        —— 代号/合约/分类/单位不可重复、不可为空
    2. 领域契约 vs DDL   —— COMMODITY_PRICE_COLUMNS 与 007 建表语句同序
    3. Repository        —— UPSERT 语义（重抓覆盖而不是拒绝）、窗口读取
    4. 采集归一化        —— 脏行剔除、源站改版必须显式失败
    5. 门面              —— 陈旧/样本不足时**不给分位**（本域最危险的失败模式）
    6. 分位分析器扩展    —— indicators 参数不改变默认口径
    7. 接口              —— 七档映射与 store.percentile_level 逐条一致
    8. 同步 CLI          —— 未知代号退出码 2、dry-run 不写库

【为什么陈旧抑制值得单独一组用例】
  拿 2022 年的价格算「当前在近 5 年的分位」，会输出一个数字上看完全正常、
  实际基于四年前数据的评级 —— 数字不会报错，只会误导。动力煤（郑商所 ZC）
  自 2022 年起交易受限，正是因此被移出首版品种。这条约束一旦在重构中被
  「顺手」去掉，页面会静默地重新开始说谎，所以必须有用例盯着。

【运行方式】
  ./venv/bin/python -m pytest tests/test_commodities.py -v
  （不联网：源站调用一律用 fake akshare 注入 sys.modules）
"""

import os
import re
import sys
import types
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.analytics import ValuationPercentileAnalyzer
from stocklab.datasource.commodity import (
    CATEGORIES,
    CommoditySourceError,
    by_symbol,
    commodities,
    fetch_main_history,
)
from stocklab.datasource.commodity import source as source_module
from stocklab.domain import COMMODITY_PRICE_COLUMNS
from stocklab.facade import CommodityDataFacade
from stocklab.persistence.repository.commodity import CommodityPriceRepository

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_PATH = os.path.join(
    BASE_DIR, "stocklab", "persistence", "migrations", "007_commodities.sql"
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _series_frame(symbol, start, days, base=100.0, step=1.0):
    """构造一段等差价格序列（契约列序）"""
    dates = [start + timedelta(days=i) for i in range(days)]
    return pd.DataFrame({
        "symbol": symbol,
        "trade_date": dates,
        "close": [base + step * i for i in range(days)],
        "source": "test:unit",
        "fetched_at": datetime(2026, 10, 4, 12, 0, 0),
    }).loc[:, list(COMMODITY_PRICE_COLUMNS)]


def _fake_akshare(monkeypatch, frame=None, error=None, columns=None):
    """
    把 fake 的 akshare 注入 sys.modules

    source 模块在函数体内 `import akshare as ak`，因此换 sys.modules 即可，
    不必改源码 —— 这样测的仍是真实的公开入口 fetch_main_history()。
    """
    def futures_main_sina(symbol):
        if error is not None:
            raise error
        out = frame.copy()
        if columns is not None:
            out.columns = columns
        return out

    monkeypatch.setitem(
        sys.modules, "akshare",
        types.SimpleNamespace(futures_main_sina=futures_main_sina),
    )


def _raw_frame(n=200, start=date(2026, 1, 1)):
    """源站形态的原始帧：中文列名 + 字符串日期与价格"""
    dates = [(start + timedelta(days=i)).isoformat() for i in range(n)]
    return pd.DataFrame({
        "日期": dates,
        "开盘价": [str(100.0 + i) for i in range(n)],
        "最高价": [str(101.0 + i) for i in range(n)],
        "最低价": [str(99.0 + i) for i in range(n)],
        "收盘价": [str(100.5 + i) for i in range(n)],
        "成交量": ["1000"] * n,
        "持仓量": ["500"] * n,
        "动态结算价": [str(100.4 + i) for i in range(n)],
    })


def _ddl_column_names():
    """解析 007 建表语句里的列名（保持 DDL 中出现的顺序）

    做成模块级函数而不是 class fixture：它只是读一个文件，没有任何状态，
    而 pytest 已开始弃用「类内实例方法上定义的 class 级 fixture」。
    """
    with open(MIGRATION_PATH, encoding="utf-8") as handle:
        content = handle.read()
    block = re.search(
        r"CREATE TABLE IF NOT EXISTS commodity\.price_history\s*\((.*?)\);",
        content, re.S,
    )
    assert block, "未在 %s 找到建表语句" % MIGRATION_PATH
    columns = []
    for line in block.group(1).splitlines():
        match = re.match(
            r"\s*([a-z_]+)\s+(VARCHAR|DATE|DOUBLE|TIMESTAMP|BOOLEAN)\b", line
        )
        if match:
            columns.append(match.group(1))
    return columns


# ===========================================================================
# 1. 品种登记表
# ===========================================================================
class TestRegistry:
    """登记表是抓取策略与元信息的唯一来源，重复或缺项会直接写错库"""

    def test_ten_commodities_registered(self):
        assert len(commodities()) == 10

    def test_symbols_are_unique(self):
        symbols = [spec.symbol for spec in commodities(include_inactive=True)]
        assert len(symbols) == len(set(symbols)), "symbol 是主键，必须唯一"

    def test_source_symbols_are_unique(self):
        """两个品种指向同一个合约，会让其中一个的序列被另一个覆盖"""
        codes = [spec.source_symbol for spec in commodities(include_inactive=True)]
        assert len(codes) == len(set(codes)), "合约代码重复： %s" % codes

    def test_categories_are_known(self):
        for spec in commodities(include_inactive=True):
            assert spec.category in CATEGORIES, "%s 的分类 %s 不在 CATEGORIES" % (
                spec.symbol, spec.category)

    def test_sort_order_increases(self):
        orders = [spec.sort_order for spec in commodities(include_inactive=True)]
        assert orders == sorted(orders)
        assert len(orders) == len(set(orders))

    @pytest.mark.parametrize("attr", ["symbol", "name", "unit", "unit_note",
                                      "source_symbol", "exchange", "description"])
    def test_required_text_fields_not_blank(self, attr):
        """这些字段里任何一个是空串，页面上就会出现一个不解释自己的数字"""
        for spec in commodities(include_inactive=True):
            value = getattr(spec, attr)
            assert isinstance(value, str) and value.strip(), \
                "%s.%s 为空" % (spec.symbol, attr)

    def test_source_symbol_format(self):
        """新浪主力连续是「合约代码 + 0」，形如 RB0 / SC0"""
        for spec in commodities(include_inactive=True):
            assert re.fullmatch(r"[A-Z]{1,2}\d", spec.source_symbol), \
                "%s 的合约代码 %s 不像主力连续" % (spec.symbol, spec.source_symbol)

    def test_by_symbol_lookup(self):
        assert by_symbol("crude_oil").name == "原油"
        assert by_symbol("not_registered") is None

    def test_inactive_filter(self, monkeypatch):
        """停用一个品种后，同步默认不应再抓它"""
        from stocklab.datasource.commodity import registry as registry_module
        patched = tuple(
            type(spec)(**{**spec.__dict__, "is_active": idx != 0})
            for idx, spec in enumerate(registry_module.COMMODITIES)
        )
        monkeypatch.setattr(registry_module, "COMMODITIES", patched)

        active = registry_module.commodities()
        all_specs = registry_module.commodities(include_inactive=True)
        assert len(all_specs) == 10
        assert len(active) == 9
        assert active[0].symbol != all_specs[0].symbol


# ===========================================================================
# 2. 领域契约 vs 建表语句
# ===========================================================================
class TestContractMatchesDdl:
    """
    契约列序必须与 migration DDL 同序

    BaseRepository 的 INSERT 是 `SELECT {契约列} FROM tmp`，列序一旦与表
    不一致，值会写进错误的列 —— 而 close 挂到 source 上会在下一次查询时
    以「类型错误」炸出来，不是静默错位；trade_date 挂到 close 上更糟，
    会变成一个看起来像价格的日期序号。
    """

    def test_column_names_and_order_match(self):
        assert list(COMMODITY_PRICE_COLUMNS) == _ddl_column_names(), (
            "契约 %s 与 DDL %s 不一致"
            % (list(COMMODITY_PRICE_COLUMNS), _ddl_column_names())
        )

    def test_symbol_and_date_form_primary_key(self):
        with open(MIGRATION_PATH, encoding="utf-8") as handle:
            content = handle.read()
        assert "PRIMARY KEY (symbol, trade_date)" in content


# ===========================================================================
# 3. Repository
# ===========================================================================
class TestRepository:
    """写入语义是 UPSERT：重抓必须能覆盖，而不是被主键拒绝"""

    @pytest.fixture
    def repo(self, fresh_db):
        db_path, database = fresh_db
        yield CommodityPriceRepository(database)
        database.close()

    def test_insert_then_count(self, repo):
        frame = _series_frame("crude_oil", date(2026, 1, 1), 10)
        assert repo.insert(frame) == 10
        assert repo.counts_by_symbol() == {"crude_oil": 10}

    def test_reinsert_updates_not_duplicates(self, repo):
        """源站补数据/修坏行时，同一 (symbol, trade_date) 应被覆盖"""
        repo.insert(_series_frame("crude_oil", date(2026, 1, 1), 5, base=100.0))
        changed = _series_frame("crude_oil", date(2026, 1, 1), 5, base=999.0)
        repo.insert(changed)

        counts = repo.counts_by_symbol()
        assert counts["crude_oil"] == 5, "重抓后行数必须不变（覆盖而非追加）"

        series = repo.load_series(symbols=["crude_oil"])
        assert float(series["close"].iloc[0]) == 999.0, "旧值应被新值覆盖"

    def test_contract_violation_is_rejected(self, repo):
        """缺契约列必须拒绝写入，不能猜列序"""
        bad = pd.DataFrame({
            "symbol": ["crude_oil"],
            "trade_date": [date(2026, 1, 1)],
            "close": [1.0],
            # 缺 source / fetched_at
        })
        assert repo.insert(bad) == 0
        assert repo.counts_by_symbol() == {}

    def test_load_series_symbol_filter(self, repo):
        repo.insert(_series_frame("crude_oil", date(2026, 1, 1), 4))
        repo.insert(_series_frame("gold", date(2026, 1, 1), 6))

        only_gold = repo.load_series(symbols=["gold"])
        assert set(only_gold["symbol"]) == {"gold"}
        assert len(only_gold) == 6

    def test_load_series_since_filter(self, repo):
        repo.insert(_series_frame("crude_oil", date(2026, 1, 1), 30))
        cut = repo.load_series(symbols=["crude_oil"], since=date(2026, 1, 20))
        assert len(cut) == 11, "1/20 起含当天，共 11 行"

    def test_load_series_empty_symbol_list_short_circuits(self, repo):
        """显式传空列表 = 「没有要查的」，不是「查全部」——两者混用会让
        「按自选股筛选」在空列表时意外返回整库"""
        repo.insert(_series_frame("crude_oil", date(2026, 1, 1), 3))
        assert repo.load_series(symbols=[]).empty

    def test_load_series_on_empty_table_returns_contract_frame(self, repo):
        frame = repo.load_series()
        assert list(frame.columns) == list(COMMODITY_PRICE_COLUMNS)
        assert frame.empty

    def test_latest_and_as_of(self, repo):
        assert repo.as_of() is None
        repo.insert(_series_frame("crude_oil", date(2026, 1, 1), 5))
        repo.insert(_series_frame("gold", date(2026, 2, 1), 5))

        assert repo.as_of() == "2026-02-05"
        latest = repo.latest_by_symbol()
        assert set(latest["symbol"]) == {"crude_oil", "gold"}

    def test_counts_by_symbol(self, repo):
        repo.insert(_series_frame("crude_oil", date(2026, 1, 1), 7))
        repo.insert(_series_frame("gold", date(2026, 1, 1), 3))
        assert repo.counts_by_symbol() == {"crude_oil": 7, "gold": 3}


# ===========================================================================
# 4. 采集归一化
# ===========================================================================
class TestSourceNormalization:
    """归一化是唯一理解源站列名的地方；猜列会让错数看起来完全正常"""

    @pytest.fixture(autouse=True)
    def fast_retries(self, monkeypatch):
        """失败用例把重试关掉，避免测试等退避睡眠"""
        monkeypatch.setattr(source_module, "_RETRIES", 0)

    def test_normalizes_to_contract(self, monkeypatch):
        _fake_akshare(monkeypatch, frame=_raw_frame(30))
        spec = by_symbol("crude_oil")

        frame = fetch_main_history(spec)
        assert list(frame.columns) == list(COMMODITY_PRICE_COLUMNS)
        assert len(frame) == 30
        assert frame["symbol"].eq(spec.symbol).all()
        assert frame["source"].eq("akshare:futures_main_sina").all()
        # 日期升序
        assert frame["trade_date"].is_monotonic_increasing
        assert frame["close"].dtype.kind == "f"
        assert (frame["close"] > 0).all()

    def test_drops_zero_and_negative_close(self, monkeypatch):
        raw = _raw_frame(10)
        raw.loc[3, "收盘价"] = "0"
        raw.loc[5, "收盘价"] = "-1.5"
        _fake_akshare(monkeypatch, frame=raw)

        frame = fetch_main_history(by_symbol("crude_oil"))
        assert len(frame) == 8, "非正收盘价必须剔除，否则分位分母被污染"

    def test_drops_unparsable_rows(self, monkeypatch):
        raw = _raw_frame(10)
        raw.loc[2, "日期"] = "not-a-date"
        raw.loc[4, "收盘价"] = "—"
        _fake_akshare(monkeypatch, frame=raw)

        frame = fetch_main_history(by_symbol("crude_oil"))
        assert len(frame) == 8

    def test_dedupes_same_date_keeping_last(self, monkeypatch):
        raw = _raw_frame(5)
        extra = raw.iloc[[4]].copy()
        extra["收盘价"] = ["888.0"]
        _fake_akshare(monkeypatch, frame=pd.concat([raw, extra], ignore_index=True))

        frame = fetch_main_history(by_symbol("crude_oil"))
        assert len(frame) == 5
        assert float(frame["close"].iloc[-1]) == 888.0

    def test_all_rows_invalid_raises(self, monkeypatch):
        raw = _raw_frame(5)
        raw["收盘价"] = ["—"] * 5
        _fake_akshare(monkeypatch, frame=raw)

        with pytest.raises(CommoditySourceError) as exc:
            fetch_main_history(by_symbol("crude_oil"))
        assert "0 条有效行" in str(exc.value)

    def test_renamed_column_raises_rather_than_guesses(self, monkeypatch):
        """源站改版：必须失败，不能把开盘价当收盘价写进库"""
        raw = _raw_frame(5)
        _fake_akshare(monkeypatch, frame=raw, columns=["Date", "Open", "High",
                                                       "Low", "Close", "Volume",
                                                       "OI", "Settle"])
        with pytest.raises(CommoditySourceError) as exc:
            fetch_main_history(by_symbol("crude_oil"))
        assert "源站缺列" in str(exc.value)

    def test_network_failure_raises(self, monkeypatch):
        _fake_akshare(monkeypatch, error=ConnectionError("no route to host"))
        with pytest.raises(CommoditySourceError):
            fetch_main_history(by_symbol("crude_oil"))

    def test_inactive_spec_is_skipped(self):
        from stocklab.datasource.commodity.registry import CommoditySpec
        spec = CommoditySpec(
            symbol="dead", name="停用", category="能源", unit="元/吨",
            unit_note="", source_symbol="ZZ0", exchange="x",
            description="", sort_order=999, is_active=False,
        )
        with pytest.raises(CommoditySourceError) as exc:
            fetch_main_history(spec)
        assert "已停用" in str(exc.value)

    def test_empty_response_raises(self, monkeypatch):
        _fake_akshare(monkeypatch, frame=pd.DataFrame({"日期": [], "收盘价": []}))
        with pytest.raises(CommoditySourceError):
            fetch_main_history(by_symbol("crude_oil"))


# ===========================================================================
# 5. 门面
# ===========================================================================
class TestFacade:
    """分位在门面算；陈旧与样本不足一律不给分位"""

    @pytest.fixture
    def facade(self, fresh_db):
        db_path, database = fresh_db
        yield CommodityDataFacade(database), CommodityPriceRepository(database)
        database.close()

    def test_empty_table_returns_placeholders(self, facade):
        f, _ = facade
        catalog = f.catalog()
        assert len(catalog["items"]) == 10
        assert catalog["as_of"] is None
        for item in catalog["items"]:
            assert item["close"] is None
            assert item["percentile"] is None
            assert item["stale"] is True, "没有数据就必须是陈旧的"
            assert set(item["changes"]) == {"1m", "3m", "12m"}

    def test_catalog_computes_percentile(self, facade):
        f, repo = facade
        # 120 个交易日的递增序列，末日对齐今天（否则 lag > 15 会被判陈旧）
        start = date.today() - timedelta(days=119)
        repo.insert(_series_frame("crude_oil", start, 120, base=100.0, step=1.0))

        catalog = f.catalog()
        item = next(i for i in catalog["items"] if i["symbol"] == "crude_oil")
        assert item["close"] == 219.0
        assert item["sample_count"] == 120
        assert item["percentile"] is not None
        # CDF 口径：严格小于当前值的样本 = 119，分母 120
        assert item["percentile"] == pytest.approx(119 / 120 * 100.0)

        other = next(i for i in catalog["items"] if i["symbol"] == "gold")
        assert other["percentile"] is None, "没插数据的品种不该有分位"

    def test_stale_data_yields_no_percentile(self, facade):
        """【核心】2022 年的价格不得输出「当前分位」"""
        f, repo = facade
        old = date.today() - timedelta(days=400)
        repo.insert(_series_frame("crude_oil", old, 200, base=100.0, step=0.5))

        item = next(i for i in f.catalog()["items"] if i["symbol"] == "crude_oil")
        assert item["stale"] is True
        assert item["lag_days"] > 15
        assert item["percentile"] is None, "陈旧数据给了分位 = 页面会说谎"

    def test_fresh_data_is_not_stale(self, facade):
        f, repo = facade
        recent = date.today() - timedelta(days=79)
        repo.insert(_series_frame("crude_oil", recent, 80, base=100.0, step=0.5))

        item = next(i for i in f.catalog()["items"] if i["symbol"] == "crude_oil")
        assert item["stale"] is False
        assert item["percentile"] is not None

    def test_insufficient_samples_yield_no_percentile(self, facade):
        f, repo = facade
        recent = date.today() - timedelta(days=19)
        repo.insert(_series_frame("crude_oil", recent, 20, base=100.0))

        item = next(i for i in f.catalog()["items"] if i["symbol"] == "crude_oil")
        assert item["sample_count"] < 60
        assert item["percentile"] is None, "20 个样本撑不起「近 N 年的分位」"

    def test_changes_are_relative_to_series_end(self, facade):
        f, repo = facade
        # 构造：起点 100，末点 150 -> 12 月涨幅 +50%
        recent = date.today() - timedelta(days=400)
        frame = _series_frame("crude_oil", recent, 400, base=100.0, step=0.125)
        repo.insert(frame)

        item = next(i for i in f.catalog()["items"] if i["symbol"] == "crude_oil")
        # 12 月前的锚点仍在序列内
        assert item["changes"]["12m"] is not None
        assert item["changes"]["12m"] > 0, "序列整体上行时 12 月涨幅为正"

    def test_window_filter_changes_percentile(self, facade):
        f, repo = facade
        recent = date.today() - timedelta(days=379)
        repo.insert(_series_frame("crude_oil", recent, 380, base=100.0, step=1.0))

        five = next(i for i in f.catalog(window_years=5)["items"]
                    if i["symbol"] == "crude_oil")
        one = next(i for i in f.catalog(window_years=1)["items"]
                   if i["symbol"] == "crude_oil")
        assert one["sample_count"] <= five["sample_count"]
        assert one["percentile"] is not None

    def test_history_returns_sorted_series(self, facade):
        f, repo = facade
        recent = date.today() - timedelta(days=30)
        repo.insert(_series_frame("gold", recent, 30, base=500.0, step=1.0))

        payload = f.history("gold")
        dates = [point["date"] for point in payload["series"]]
        assert len(dates) == 30
        assert dates == sorted(dates), "序列必须升序，否则趋势图方向反了"
        assert payload["unit"] == "元/克"
        assert payload["series"][-1]["close"] == 529.0

    def test_history_unknown_symbol_raises(self, facade):
        f, _ = facade
        with pytest.raises(ValueError):
            f.history("not_registered")

    def test_history_on_empty_symbol_returns_empty_series(self, facade):
        f, _ = facade
        payload = f.history("crude_oil")
        assert payload["series"] == []
        assert payload["close"] is None


# ===========================================================================
# 6. 分位分析器扩展
# ===========================================================================
class TestAnalyzerIndicators:
    """indicators 参数必须是纯扩展：默认口径一个字都不能变"""

    @pytest.fixture
    def valuation_frame(self):
        rng = pd.Series(range(1, 101))
        return pd.DataFrame({
            "trade_date": pd.date_range("2025-01-01", periods=100, freq="B"),
            "pe_ttm": rng * 1.0,
            "pb": rng * 0.1,
        })

    def test_default_still_uses_valuation_columns(self, valuation_frame):
        results = ValuationPercentileAnalyzer().analyze(valuation_frame)
        assert [r.indicator for r in results] == ["pe_ttm", "pb"]

    def test_indicators_restricts_columns(self, valuation_frame):
        valuation_frame["close"] = rng_series(100)
        results = ValuationPercentileAnalyzer().analyze(
            valuation_frame, indicators=["close"]
        )
        assert [r.indicator for r in results] == ["close"]

    def test_indicators_missing_column_yields_empty(self, valuation_frame):
        """显式要求的列不存在时返回空列表，而不是退回默认五列 ——
        退回会让调用方拿到 pe_ttm 的分位并当成 close 的结果用"""
        results = ValuationPercentileAnalyzer().analyze(
            valuation_frame, indicators=["close"]
        )
        assert results == []

    def test_empty_frame_builds_results_for_requested_indicator(self):
        results = ValuationPercentileAnalyzer().analyze(
            pd.DataFrame(), indicators=["close"]
        )
        assert [r.indicator for r in results] == ["close"]
        assert results[0].percentile is None

    def test_cdf_uses_strict_less_than(self):
        """分位口径的锚：严格小于当前值的样本占比"""
        values = [10.0, 20.0, 30.0, 40.0]
        frame = pd.DataFrame({
            "trade_date": pd.date_range("2025-01-01", periods=4, freq="D"),
            "close": values,
        })
        results = ValuationPercentileAnalyzer().analyze(frame, indicators=["close"])
        assert results[0].percentile == pytest.approx(3 / 4 * 100.0)


def rng_series(n):
    """确定性递增序列（避免测试里引随机数）"""
    return pd.Series(range(1, n + 1), dtype="float64")


# ===========================================================================
# 7. 接口
# ===========================================================================
class TestWebApi:
    """接口层：七档映射必须与 store.percentile_level 逐条一致"""

    def test_page_renders(self, client):
        response = client.get("/commodities")
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        for element_id in ("cm-groups", "cm-body", "commodity-chart",
                           "detail-card", "level-legend", "stat-grid"):
            assert 'id="%s"' % element_id in text, "缺少页面要素 %s" % element_id
        assert 'class="nav-item active"' in text, "侧边栏未高亮"

    def test_catalog_shape(self, client):
        response = client.get("/api/commodities")
        assert response.status_code == 200
        payload = response.get_json()

        assert len(payload["items"]) == 10
        assert payload["window_years"] == 5
        assert payload["stale_days"] == 15
        for item in payload["items"]:
            for key in ("symbol", "name", "category", "unit", "unit_note",
                        "exchange", "description", "trade_date", "close",
                        "lag_days", "stale", "percentile", "level7",
                        "sample_count", "interval_text", "median_value",
                        "min_value", "max_value", "changes"):
                assert key in item, "%s 缺字段 %s" % (item["symbol"], key)

    def test_level7_matches_backend_mapping(self, client):
        from app.web import store

        payload = client.get("/api/commodities").get_json()
        for item in payload["items"]:
            assert item["level7"] == store.percentile_level(item["percentile"]), \
                "%s 的七档映射与 store.percentile_level 不一致" % item["symbol"]

    def test_level7_is_dash_when_percentile_missing(self, client):
        """无分位不得给「中性」—— 那是一个结论，而这里恰恰是得不出结论"""
        payload = client.get("/api/commodities").get_json()
        for item in payload["items"]:
            if item["percentile"] is None:
                assert item["level7"] == "-", \
                    "%s 无分位却给了评级 %s" % (item["symbol"], item["level7"])

    @pytest.mark.parametrize("query", ["?years=0", "?years=99", "?years=abc"])
    def test_catalog_rejects_bad_window(self, client, query):
        response = client.get("/api/commodities" + query)
        assert response.status_code == 400
        assert "error" in response.get_json()

    def test_history_requires_symbol(self, client):
        response = client.get("/api/commodity/history")
        assert response.status_code == 400
        assert "symbol" in response.get_json()["error"]

    def test_history_rejects_unregistered_symbol(self, client):
        response = client.get("/api/commodity/history?symbol=not_registered")
        assert response.status_code == 400
        assert "未登记" in response.get_json()["error"]

    def test_history_returns_series(self, client):
        response = client.get("/api/commodity/history?symbol=crude_oil")
        if response.get_json().get("close") is None:
            pytest.skip("本地未同步原油数据")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["series"], "有数据时必须返回序列"
        dates = [point["date"] for point in payload["series"]]
        assert dates == sorted(dates), "序列必须升序，否则趋势图方向反了"
        assert "level7" in payload

    def test_routes_are_registered(self):
        with open(os.path.join(BASE_DIR, "app", "web", "server.py"),
                  encoding="utf-8") as handle:
            content = handle.read()
        for rule in ("/commodities", "/api/commodities", "/api/commodity/history"):
            assert rule in content, "缺少路由 %s" % rule

    def test_commodity_api_reads_through_facade(self):
        """接口层不得直接 import persistence —— 与 insight 的同一道约束"""
        import ast
        path = os.path.join(BASE_DIR, "app", "web", "commodity_api.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        assert "stocklab.facade" in imported
        assert not [m for m in imported if m.startswith("stocklab.persistence")], \
            "commodity_api 不得 import stocklab.persistence"


# ===========================================================================
# 8. 同步 CLI
# ===========================================================================
class TestSyncCli:
    """退出码是给定时任务看的：输入错误必须是 2，不能混进「有品种失败」的 1"""

    def test_defaults_to_sync_all(self):
        from app.scripts import sync_commodities as cli
        args = cli.parse_args([])
        assert args.command == "sync"
        assert args.symbols is None
        assert args.dry_run is False

    def test_status_subcommand(self):
        from app.scripts import sync_commodities as cli
        assert cli.parse_args(["status"]).command == "status"

    def test_symbol_repeatable(self):
        from app.scripts import sync_commodities as cli
        args = cli.parse_args(["sync", "--symbol", "gold", "--symbol", "copper"])
        assert args.symbols == ["gold", "copper"]

    def test_resolve_specs_all(self):
        from app.scripts import sync_commodities as cli
        assert len(cli._resolve_specs(None)) == 10

    def test_resolve_specs_unknown_raises(self):
        from app.scripts import sync_commodities as cli
        with pytest.raises(ValueError) as exc:
            cli._resolve_specs(["gold", "nonsense"])
        assert "nonsense" in str(exc.value)

    def test_resolve_specs_dedupes_and_keeps_registry_order(self):
        from app.scripts import sync_commodities as cli
        specs = cli._resolve_specs(["gold", "crude_oil", "gold"])
        assert [spec.symbol for spec in specs] == ["crude_oil", "gold"]

    def test_main_exits_2_on_unknown_symbol(self):
        from app.scripts import sync_commodities as cli
        assert cli.main(["sync", "--symbol", "nonsense"]) == 2

    def test_dry_run_does_not_write(self, fresh_db):
        from app.scripts import sync_commodities as cli

        db_path, database = fresh_db
        repo = CommodityPriceRepository(database)
        specs = cli._resolve_specs(["crude_oil"])

        # 注入 fake 源，避免联网
        # patch 模块内被 import 进 cli 命名空间的那个名字 ——
        # cli 是 `from ... import fetch_main_history`，改 source 模块不影响它
        original = cli.fetch_main_history
        cli.fetch_main_history = (
            lambda spec: _series_frame(spec.symbol, date(2026, 1, 1), 20)
        )
        try:
            ok, failed = cli.sync(database, specs, dry_run=True)
        finally:
            cli.fetch_main_history = original

        assert (ok, failed) == (1, 0)
        assert repo.counts_by_symbol() == {}, "dry-run 不得写库"
        database.close()

    def test_one_failure_does_not_abort_the_rest(self, fresh_db):
        """十个品种是十个独立请求，一个源站故障不该让其余九个作废"""
        from app.scripts import sync_commodities as cli

        db_path, database = fresh_db
        repo = CommodityPriceRepository(database)
        specs = cli._resolve_specs(None)

        def fake_fetch(spec):
            if spec.symbol == "crude_oil":
                raise CommoditySourceError("模拟源站改版")
            return _series_frame(spec.symbol, date(2026, 1, 1), 15)

        original = cli.fetch_main_history
        cli.fetch_main_history = fake_fetch
        try:
            ok, failed = cli.sync(database, specs)
        finally:
            cli.fetch_main_history = original

        assert (ok, failed) == (9, 1)
        assert len(repo.counts_by_symbol()) == 9
        assert "crude_oil" not in repo.counts_by_symbol()
        database.close()
