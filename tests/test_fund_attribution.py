#!/usr/bin/env python3
"""
==============================================================================
StockLab - Brinson 归因与持仓解析测试 (tests/test_fund_attribution.py)
==============================================================================

【功能用途】
  1. BrinsonAttribution 分解恒等式（Fachler 三效应 / BHB 两效应）
  2. 成分券权重 → 行业权重聚合（归一化 + 覆盖率，纯函数）
  3. 东财持仓解析（HTML 表格 / JS 字符串 / 报告期推导，全离线）
  4. FundAttributionEngine 端到端（临时库造数，allow_remote=False 不联网）
  5. /api/fund/attribution 与修复后的持仓接口契约（状态码 + error 字段）

【运行方式】
  ./venv/bin/python -m pytest tests/test_fund_attribution.py -v
  # 契约用例依赖真实库，须先 pkill -f serve_web.py
"""

import os
import sys

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta

import pandas as pd
import pytest

from stocklab.analytics.fund_attribution import (
    AttributionConfig,
    BrinsonAttribution,
    FundAttributionEngine,
)
from stocklab.datasource.benchmark_index import aggregate_industry_weights
from stocklab.datasource.fund_holding import (
    _extract_apidata_content,
    _normalize_stock_code,
    _parse_holding_html,
    _quarter_end_date,
    build_report_dates,
)
from stocklab.domain import (
    BENCHMARK_INDUSTRY_WEIGHT_COLUMNS,
    DAILY_PRICE_COLUMNS,
    FUND_HOLDING_COLUMNS,
    STOCK_INDUSTRY_MAPPING_COLUMNS,
    SW_INDEX_DAILY_COLUMNS,
)
from stocklab.persistence.repository.benchmark import BenchmarkIndustryWeightRepository
from stocklab.persistence.repository.daily_price import DailyPriceRepository
from stocklab.persistence.repository.fund_analysis import SWIndexDailyRepository
from stocklab.persistence.repository.fund_holding import (
    FundHoldingRepository,
    StockIndustryMappingRepository,
)


# =============================================================================
# 公共工具
# =============================================================================

def _contract_frame(rows, columns):
    """按列契约补齐缺列并清掉对象列 NaN（与 Repository 写入前的口径一致）"""
    frame = pd.DataFrame(rows).reindex(columns=list(columns))
    for col in frame.columns:
        if col.endswith("_at"):
            frame[col] = frame[col].fillna(pd.Timestamp.now())
        elif frame[col].dtype == object:
            frame[col] = frame[col].fillna("")
    return frame


def _trade_window(trade_date, days):
    """归因区间 (start, end)"""
    return trade_date - timedelta(days=days), trade_date


# =============================================================================
# 1. Brinson 分解恒等式
# =============================================================================

class TestBrinsonAttribution:
    """三效应 / 两效应分解的数学正确性"""

    @pytest.fixture
    def sample_data(self):
        return {
            "portfolio_weights": {"801010": 0.6, "801030": 0.4},
            "benchmark_weights": {"801010": 0.3, "801030": 0.7},
            "portfolio_returns": {"801010": 0.12, "801030": -0.05},
            "benchmark_returns": {"801010": 0.08, "801030": 0.01},
        }

    def test_fachler_three_effects_sum_to_excess(self, sample_data):
        """配置 + 选择 + 交互 == 超额收益（恒等式，误差 < 1e-12）"""
        engine = BrinsonAttribution(AttributionConfig(model="brinson-fachler"))
        result = engine.attribute(trade_date=date(2026, 9, 30), **sample_data)

        total_effects = (result.allocation_effect
                         + result.selection_effect
                         + result.interaction_effect)
        assert abs(total_effects - result.excess_return) < 1e-12

        expected_total = (0.6 * 0.12 + 0.4 * -0.05)
        expected_bench = (0.3 * 0.08 + 0.7 * 0.01)
        assert abs(result.total_return - expected_total) < 1e-12
        assert abs(result.benchmark_return - expected_bench) < 1e-12
        assert abs(result.excess_return - (expected_total - expected_bench)) < 1e-12

    def test_bhb_two_effects_sum_to_excess(self, sample_data):
        """BHB 口径下配置 + 选择 == 超额收益，交互为 0"""
        engine = BrinsonAttribution(AttributionConfig(model="brinson-hb"))
        result = engine.attribute(trade_date=date(2026, 9, 30), **sample_data)

        assert result.interaction_effect == 0.0
        total_effects = result.allocation_effect + result.selection_effect
        assert abs(total_effects - result.excess_return) < 1e-12

    def test_sector_details_reconstruct_total(self, sample_data):
        """各行业明细的 total_effect 求和 == 组合整体超额收益"""
        engine = BrinsonAttribution()
        result = engine.attribute(trade_date=date(2026, 9, 30), **sample_data)

        assert set(result.sector_details) == {"801010", "801030"}
        for sector, detail in result.sector_details.items():
            rebuilt = (detail["allocation_effect"]
                       + detail["selection_effect"]
                       + detail["interaction_effect"])
            assert abs(rebuilt - detail["total_effect"]) < 1e-15, sector

        summed = sum(d["total_effect"] for d in result.sector_details.values())
        assert abs(summed - result.excess_return) < 1e-12

    def test_missing_sector_fills_zero(self):
        """组合缺失的基准行业按 0 权重参与分解，不抛异常"""
        engine = BrinsonAttribution()
        result = engine.attribute(
            portfolio_weights={"801010": 1.0},
            benchmark_weights={"801010": 0.5, "801030": 0.5},
            portfolio_returns={"801010": 0.10},
            benchmark_returns={"801010": 0.04, "801030": -0.02},
            trade_date=date(2026, 9, 30),
        )
        assert "801030" in result.sector_details
        assert result.sector_details["801030"]["portfolio_weight"] == 0.0
        assert abs((result.allocation_effect + result.selection_effect
                    + result.interaction_effect) - result.excess_return) < 1e-12

    def test_rolling_attribution_matches_single(self, sample_data):
        """滚动归因逐期结果与单期调用一致"""
        engine = BrinsonAttribution()
        single = engine.attribute(trade_date=date(2026, 6, 30), **sample_data)
        rolling = engine.rolling_attribution(
            portfolio_weights_history=[sample_data["portfolio_weights"]] * 2,
            benchmark_weights_history=[sample_data["benchmark_weights"]] * 2,
            portfolio_returns_history=[sample_data["portfolio_returns"]] * 2,
            benchmark_returns_history=[sample_data["benchmark_returns"]] * 2,
            dates=[date(2026, 6, 30), date(2026, 9, 30)],
        )
        assert len(rolling) == 2
        assert abs(rolling[0].excess_return - single.excess_return) < 1e-15


# =============================================================================
# 2. 成分券权重 -> 行业权重（纯函数）
# =============================================================================

class TestAggregateIndustryWeights:

    def _mapping(self):
        return pd.DataFrame([
            {"ts_code": "600000.SH", "sw_l1_code": "801010", "sw_l1_name": "农林牧渔",
             "sw_l2_code": "801011", "sw_l2_name": "种植业"},
            {"ts_code": "000001.SZ", "sw_l1_code": "801010", "sw_l1_name": "农林牧渔",
             "sw_l2_code": "801012", "sw_l2_name": "渔业"},
            {"ts_code": "000002.SZ", "sw_l1_code": "801030", "sw_l1_name": "化工",
             "sw_l2_code": "801031", "sw_l2_name": "化学制品"},
        ])

    def test_weights_normalized_and_coverage(self):
        """已映射样本内归一化到 1，coverage = 已映射权重 / 总权重"""
        constituents = pd.DataFrame({
            "ts_code": ["600000.SH", "000001.SZ", "000002.SZ", "600519.SH"],
            "weight": [0.30, 0.20, 0.25, 0.25],   # 600519 无映射
        })
        frame = aggregate_industry_weights(constituents, self._mapping(), level=1)

        assert abs(frame["weight"].sum() - 1.0) < 1e-12
        assert abs(float(frame["coverage"].iloc[0]) - 0.75) < 1e-12

        weights = dict(zip(frame["sector_code"], frame["weight"]))
        assert abs(weights["801010"] - 0.5 / 0.75) < 1e-12   # 0.5 / 已映射 0.75
        assert abs(weights["801030"] - 0.25 / 0.75) < 1e-12
        assert dict(zip(frame["sector_code"], frame["sector_name"]))["801010"] == "农林牧渔"

    def test_level2_grouping(self):
        """level=2 按二级行业聚合"""
        constituents = pd.DataFrame({
            "ts_code": ["600000.SH", "000001.SZ", "000002.SZ"],
            "weight": [0.2, 0.3, 0.5],
        })
        frame = aggregate_industry_weights(constituents, self._mapping(), level=2)
        weights = dict(zip(frame["sector_code"], frame["weight"]))
        assert set(weights) == {"801011", "801012", "801031"}
        assert abs(weights["801011"] - 0.2) < 1e-12

    def test_all_unmapped_returns_empty(self):
        """全部成分券无行业映射时返回空表（而非伪造权重）"""
        constituents = pd.DataFrame({"ts_code": ["600519.SH"], "weight": [1.0]})
        frame = aggregate_industry_weights(constituents, self._mapping(), level=1)
        assert frame.empty
        assert list(frame.columns) == ["sector_code", "sector_name", "weight", "coverage"]

    def test_invalid_level_rejected(self):
        """level=3 暂不支持，返回空表"""
        constituents = pd.DataFrame({"ts_code": ["600000.SH"], "weight": [1.0]})
        assert aggregate_industry_weights(constituents, self._mapping(), level=3).empty

    def test_empty_inputs(self):
        """空输入直接返回空表"""
        assert aggregate_industry_weights(pd.DataFrame(), self._mapping()).empty
        assert aggregate_industry_weights(
            pd.DataFrame({"ts_code": ["600000.SH"], "weight": [1.0]}), pd.DataFrame()
        ).empty


# =============================================================================
# 3. 东财持仓解析（离线）
# =============================================================================

class TestEastmoneyHoldingParser:

    def test_normalize_stock_code(self):
        assert _normalize_stock_code("600000") == "600000.SH"
        assert _normalize_stock_code("688001") == "688001.SH"
        assert _normalize_stock_code("000001") == "000001.SZ"
        assert _normalize_stock_code("300750") == "300750.SZ"
        assert _normalize_stock_code("430047") == "430047.BJ"
        assert _normalize_stock_code("600000.SH") == "600000.SH"   # 已带后缀
        assert _normalize_stock_code("") == ""

    def test_parse_standard_table(self):
        """东财标准 6 列表：占比转小数、市值万元转元、序号进 rank"""
        html = """
        <table class="w782 comm tzxq">
          <tr><th>序号</th><th>股票代码</th><th>股票名称</th>
              <th>占净值比例（%）</th><th>持股数（万股）</th><th>持仓市值（万元）</th></tr>
          <tr><td>1</td><td>600519</td><td>贵州茅台</td>
              <td>9.77</td><td>600.12</td><td>94,838.96</td></tr>
          <tr><td>2</td><td>000333</td><td>美的集团</td>
              <td>9.31</td><td>1,200.5</td><td>90,372.87</td></tr>
        </table>
        """
        rows = _parse_holding_html(html)
        assert len(rows) == 2

        first = rows[0]
        assert first["stock_code"] == "600519.SH"
        assert first["stock_name"] == "贵州茅台"
        assert abs(first["weight"] - 0.0977) < 1e-12
        assert abs(first["market_value"] - 94838.96 * 10000) < 1e-6   # 万元 -> 元
        assert first["rank"] == 1

    def test_parse_table_without_rank_column(self):
        """无序号列时按行序编号，不会把股票代码当 rank"""
        html = """
        <table><tr><th>股票代码</th><th>股票名称</th><th>占净值比例(%)</th></tr>
        <tr><td>000858</td><td>五粮液</td><td>3.16</td></tr>
        </table>
        """
        rows = _parse_holding_html(html)
        assert len(rows) == 1
        assert rows[0]["stock_code"] == "000858.SZ"
        assert rows[0]["rank"] == 1

    def test_parse_no_table_returns_empty(self):
        assert _parse_holding_html("<div>暂无数据</div>") == []

    def test_extract_apidata_content(self):
        """东财返回 JS 字面量而非 JSON：键无引号、字符串含转义"""
        text = ('var apidata={ content:"<table class=\'tzxq\'><tr><td>a</td></tr></table>'
                '，说明 \\" 引号与 \\u0041 转义",arryear:[2026,2025],curyear:2026};')
        content = _extract_apidata_content(text)
        assert content is not None
        assert 'class=\'tzxq\'' in content
        assert '" 引号' in content          # \" 还原为 "
        assert "A 转义" in content      # A 还原为 A

    def test_extract_apidata_missing_returns_none(self):
        assert _extract_apidata_content("var apidata={ arryear:[2026] };") is None
        assert _extract_apidata_content('content:"未闭合') is None

    def test_quarter_end_and_report_dates(self):
        assert _quarter_end_date(2026, 1) == date(2026, 3, 31)
        assert _quarter_end_date(2026, 2) == date(2026, 6, 30)
        assert _quarter_end_date(2026, 3) == date(2026, 9, 30)
        assert _quarter_end_date(2026, 4) == date(2026, 12, 31)

        periods = build_report_dates(years=1, as_of=date(2026, 5, 1))
        # 截止 2026-05-01 只有 2026 一季报已披露，且按时间倒序
        assert [p[0] for p in periods] == [date(2026, 3, 31)]
        assert periods[0][2] == "quarterly"

        two_years = build_report_dates(years=2, as_of=date(2026, 5, 1))
        assert two_years[0][0] > two_years[-1][0]     # 倒序
        assert all(p[0] <= date(2026, 5, 1) for p in two_years)


# =============================================================================
# 4. FundAttributionEngine（临时库，不联网）
# =============================================================================

class TestFundAttributionEngine:

    FUND = "TEST0001.OF"
    BENCHMARK = "000300.SH"
    TRADE_DATE = date(2026, 9, 30)
    DAYS = 90

    def _seed(self, db):
        """造数：行业映射 + 持仓 + 个股日线 + 行业指数 + 基准权重快照"""
        now = pd.Timestamp.now()
        mapping = _contract_frame([
            {"ts_code": "600000.SH", "sw_l1_code": "801010", "sw_l1_name": "农林牧渔",
             "sw_l2_code": "801011", "sw_l2_name": "种植业", "updated_at": now},
            {"ts_code": "000001.SZ", "sw_l1_code": "801010", "sw_l1_name": "农林牧渔",
             "sw_l2_code": "801012", "sw_l2_name": "渔业", "updated_at": now},
            {"ts_code": "000002.SZ", "sw_l1_code": "801030", "sw_l1_name": "化工",
             "sw_l2_code": "801031", "sw_l2_name": "化学制品", "updated_at": now},
            {"ts_code": "000858.SZ", "sw_l1_code": "801030", "sw_l1_name": "化工",
             "sw_l2_code": "801031", "sw_l2_name": "化学制品", "updated_at": now},
        ], STOCK_INDUSTRY_MAPPING_COLUMNS)
        StockIndustryMappingRepository(db).upsert(mapping)

        holdings = _contract_frame([
            {"fund_code": self.FUND, "report_date": self.TRADE_DATE.replace(month=6, day=30),
             "report_type": "semi_annual", "stock_code": "600000.SH", "stock_name": "浦发银行",
             "weight": 0.40, "market_value": 4e8, "rank": 1,
             "source": "test", "fetched_at": now},
            {"fund_code": self.FUND, "report_date": self.TRADE_DATE.replace(month=6, day=30),
             "report_type": "semi_annual", "stock_code": "000001.SZ", "stock_name": "平安银行",
             "weight": 0.20, "market_value": 2e8, "rank": 2,
             "source": "test", "fetched_at": now},
            {"fund_code": self.FUND, "report_date": self.TRADE_DATE.replace(month=6, day=30),
             "report_type": "semi_annual", "stock_code": "000002.SZ", "stock_name": "万科A",
             "weight": 0.25, "market_value": 2.5e8, "rank": 3,
             "source": "test", "fetched_at": now},
            {"fund_code": self.FUND, "report_date": self.TRADE_DATE.replace(month=6, day=30),
             "report_type": "semi_annual", "stock_code": "000858.SZ", "stock_name": "五粮液",
             "weight": 0.15, "market_value": 1.5e8, "rank": 4,
             "source": "test", "fetched_at": now},
        ], FUND_HOLDING_COLUMNS)
        FundHoldingRepository(db).upsert(holdings)

        start, end = _trade_window(self.TRADE_DATE, self.DAYS)
        # 个股区间涨跌幅：600000 涨 10%，000001 涨 20%，000002 跌 5%，000858 涨 2%
        moves = {"600000.SH": 0.10, "000001.SZ": 0.20, "000002.SZ": -0.05, "000858.SZ": 0.02}
        price_rows = []
        for ts_code, move in moves.items():
            for trade_date, close in ((start, 100.0), (end, 100.0 * (1 + move))):
                price_rows.append({
                    "ts_code": ts_code, "trade_date": trade_date, "open": 100.0,
                    "high": 105.0, "low": 95.0, "close": close, "pre_close": 100.0,
                    "change": 0.0, "pct_chg": 0.0, "volume": 1e6, "amount": 1e8,
                })
        DailyPriceRepository(db).upsert(_contract_frame(price_rows, DAILY_PRICE_COLUMNS))

        # 行业指数：801010 涨 8%，801030 跌 2%
        index_rows = []
        for symbol, move in (("801010.SI", 0.08), ("801030.SI", -0.02)):
            for trade_date, close in ((start, 1000.0), (end, 1000.0 * (1 + move))):
                index_rows.append({
                    "symbol": symbol, "trade_date": trade_date, "open": 1000.0,
                    "high": 1010.0, "low": 990.0, "close": close,
                    "volume": 1e8, "amount": 1e9, "source": "test", "fetched_at": now,
                })
        SWIndexDailyRepository(db).upsert(_contract_frame(index_rows, SW_INDEX_DAILY_COLUMNS))

        # 基准权重快照
        snapshot = _contract_frame([
            {"benchmark_code": self.BENCHMARK, "level": 1, "sector_code": "801010",
             "sector_name": "农林牧渔", "weight": 0.4, "as_of_date": date(2026, 8, 31),
             "coverage": 0.98, "source": "csindex", "fetched_at": now},
            {"benchmark_code": self.BENCHMARK, "level": 1, "sector_code": "801030",
             "sector_name": "化工", "weight": 0.6, "as_of_date": date(2026, 8, 31),
             "coverage": 0.98, "source": "csindex", "fetched_at": now},
        ], BENCHMARK_INDUSTRY_WEIGHT_COLUMNS)
        BenchmarkIndustryWeightRepository(db).upsert(snapshot)

    @pytest.fixture
    def engine(self, fresh_db):
        _db_path, db = fresh_db
        self._seed(db)
        instance = FundAttributionEngine(db, allow_remote=False)
        yield instance, db
        instance.close()
        db.close()

    def test_attribute_fund_effects_are_consistent(self, engine):
        """端到端：三效应之和 == 超额收益，且明细与汇总自洽"""
        instance, _db = engine
        payload = instance.attribute_fund(
            fund_code=self.FUND,
            trade_date=self.TRADE_DATE,
            benchmark_code=self.BENCHMARK,
            period_days=self.DAYS,
            level=1,
        )

        assert "error" not in payload, payload.get("error")
        assert abs((payload["allocation_effect"] + payload["selection_effect"]
                    + payload["interaction_effect"]) - payload["excess_return"]) < 1e-9
        assert abs(payload["excess_return"]
                   - (payload["total_return"] - payload["benchmark_return"])) < 1e-9

        # 组合权重 0.6/0.4 落在 801010/801030，基准 0.4/0.6
        assert payload["sector_count"] == 2
        assert set(payload["sector_details"]) == {"801010", "801030"}
        assert payload["sector_details"]["801010"]["sector_name"] == "农林牧渔"
        assert abs(payload["coverage"]["portfolio"] - 1.0) < 1e-9
        assert abs(payload["coverage"]["benchmark"] - 1.0) < 1e-9
        assert payload["benchmark_meta"]["coverage"] == pytest.approx(0.98)
        assert payload["report_date"] == "2026-06-30"

        # 组合 801010 收益 = (0.4*10% + 0.2*20%) / 0.6 = 13.33%
        sector = payload["sector_details"]["801010"]
        assert sector["portfolio_return"] == pytest.approx((0.4 * 0.10 + 0.2 * 0.20) / 0.6)
        assert sector["benchmark_return"] == pytest.approx(0.08)

    def test_attribute_fund_bhb_model(self, engine):
        """BHB 口径：交互恒为 0，配置 + 选择 == 超额"""
        instance, _db = engine
        payload = instance.attribute_fund(
            fund_code=self.FUND, trade_date=self.TRADE_DATE,
            benchmark_code=self.BENCHMARK, period_days=self.DAYS, model="brinson-hb",
        )
        assert "error" not in payload
        assert payload["interaction_effect"] == 0.0
        assert abs((payload["allocation_effect"] + payload["selection_effect"])
                   - payload["excess_return"]) < 1e-9

    def test_attribute_fund_rejects_unknown_model(self, engine):
        instance, _db = engine
        payload = instance.attribute_fund(
            fund_code=self.FUND, trade_date=self.TRADE_DATE, model="capm",
        )
        assert "error" in payload and "model" in payload["error"]

    def test_attribute_fund_missing_holding(self, engine):
        """无持仓的基金返回 error，不抛异常"""
        instance, _db = engine
        payload = instance.attribute_fund(
            fund_code="NOSUCH.OF", trade_date=self.TRADE_DATE,
        )
        assert "error" in payload

    def test_attribute_fund_missing_benchmark_snapshot(self, engine, monkeypatch):
        """基准权重既无快照又回源失败时返回可读 reason（不编造权重）"""
        instance, _db = engine
        monkeypatch.setattr(
            "stocklab.facade.benchmark_data.fetch_index_constituent_weights",
            lambda *_args, **_kwargs: pd.DataFrame(
                columns=["ts_code", "weight", "as_of_date", "source"]
            ),
        )
        payload = instance.attribute_fund(
            fund_code=self.FUND, trade_date=self.TRADE_DATE, benchmark_code="000905.SH",
        )
        assert "error" in payload
        assert "基准权重" in payload["error"]

    def test_attribute_fund_no_price_data(self, engine, fresh_db):
        """区间内无个股行情时明确报错，而不是把缺失当 0 收益"""
        _db_path, db = fresh_db
        instance = FundAttributionEngine(db, allow_remote=False)
        try:
            payload = instance.attribute_fund(
                fund_code=self.FUND, trade_date=date(2020, 1, 1),
                period_days=30,
            )
            assert "error" in payload
        finally:
            instance.close()
            db.close()


    def test_underweight_benchmark_sector_participates(self, engine):
        """基金空配的基准行业必须进归因：配置效应正是靠这些行业度量低配"""
        instance, db = engine
        report_date = self.TRADE_DATE.replace(month=6, day=30)
        start, end = _trade_window(self.TRADE_DATE, self.DAYS)
        now = pd.Timestamp.now()

        # 快照改为 0.3 / 0.5 / 0.2，多出的 801050 是基金完全没持有的行业
        snapshot = _contract_frame([
            {"benchmark_code": self.BENCHMARK, "level": 1, "sector_code": "801010",
             "sector_name": "农林牧渔", "weight": 0.3, "as_of_date": date(2026, 8, 31),
             "coverage": 0.98, "source": "csindex", "fetched_at": now},
            {"benchmark_code": self.BENCHMARK, "level": 1, "sector_code": "801030",
             "sector_name": "化工", "weight": 0.5, "as_of_date": date(2026, 8, 31),
             "coverage": 0.98, "source": "csindex", "fetched_at": now},
            {"benchmark_code": self.BENCHMARK, "level": 1, "sector_code": "801050",
             "sector_name": "有色金属", "weight": 0.2, "as_of_date": date(2026, 8, 31),
             "coverage": 0.98, "source": "csindex", "fetched_at": now},
        ], BENCHMARK_INDUSTRY_WEIGHT_COLUMNS)
        BenchmarkIndustryWeightRepository(db).upsert(snapshot)

        # 该行业指数有行情（涨 5%），基金侧则没有持仓
        index_rows = [
            {"symbol": "801050.SI", "trade_date": d, "open": 1000.0, "high": 1010.0,
             "low": 990.0, "close": c, "volume": 1e8, "amount": 1e9,
             "source": "test", "fetched_at": now}
            for d, c in ((start, 1000.0), (end, 1050.0))
        ]
        SWIndexDailyRepository(db).upsert(_contract_frame(index_rows, SW_INDEX_DAILY_COLUMNS))

        payload = instance.attribute_fund(
            fund_code=self.FUND, trade_date=self.TRADE_DATE,
            benchmark_code=self.BENCHMARK, period_days=self.DAYS,
        )

        assert "error" not in payload, payload.get("error")
        assert payload["sector_count"] == 3
        assert set(payload["sector_details"]) == {"801010", "801030", "801050"}

        detail = payload["sector_details"]["801050"]
        assert detail["portfolio_weight"] == 0.0
        assert detail["benchmark_weight"] == pytest.approx(0.2)
        # 零持仓行业没有可测的组合收益：令其等于基准，选择与交互自然为 0
        assert detail["portfolio_return"] == pytest.approx(0.05)
        assert detail["selection_effect"] == 0.0
        assert detail["interaction_effect"] == 0.0
        # 该行业 +5% 跑赢基准 +2.4%，基金却空配 → 配置效应为负，低配的代价被如实计量
        assert detail["allocation_effect"] < 0

        assert payload["coverage"]["benchmark"] == pytest.approx(1.0)
        assert payload["coverage"]["portfolio"] == pytest.approx(1.0)
        assert abs((payload["allocation_effect"] + payload["selection_effect"]
                    + payload["interaction_effect"]) - payload["excess_return"]) < 1e-9

    def test_partial_local_prices_force_remote_refetch(self, engine, monkeypatch):
        """本地只剩一行时必须强制回源：门面按「有行就算命中」，会把残缺当完整"""
        _fixture_instance, db = engine
        start, end = _trade_window(self.TRADE_DATE, self.DAYS)

        conn = db.get_connection()
        conn.execute(
            "DELETE FROM market.daily_prices WHERE ts_code = ? AND trade_date <> ?",
            ["600000.SH", start],
        )

        # 回源只有在 allow_remote=True 时才可能发生，这里换一个允许联网的引擎；
        # 但门面被桩替换，测试全程不发网络请求
        instance = FundAttributionEngine(db, allow_remote=True)

        partial = pd.DataFrame({"trade_date": [start], "close": [100.0]})
        full = pd.DataFrame({"trade_date": [start, end], "close": [100.0, 110.0]})

        class _StubMarket:
            def __init__(self):
                self.calls = []

            def fetch_daily_prices(self, code, start_date, end_date, force_remote=False):
                self.calls.append(force_remote)
                return full if force_remote else partial

        stub = _StubMarket()
        monkeypatch.setattr(instance, "_market", lambda: stub)

        try:
            returns, missing = instance._load_stock_returns(["600000.SH"], start, end)
        finally:
            instance.close()

        assert stub.calls == [False, True], "本地残缺时应先试本地优先、再强制回源"
        assert "600000.SH" not in missing
        assert returns["600000.SH"] == pytest.approx(0.10)

    def test_industry_exposure_computed_on_cache_miss(self, engine):
        """预计算表为空时由持仓现算并回写，而不是回一句「没有行业暴露」"""
        _instance, db = engine
        from stocklab.facade import FundHoldingFacade
        from stocklab.persistence.repository.fund_holding import (
            FundIndustryExposureRepository,
        )

        report_date = self.TRADE_DATE.replace(month=6, day=30)
        facade = FundHoldingFacade(db)
        frame = facade.get_industry_exposure(self.FUND, report_date, level=1)

        assert not frame.empty
        assert abs(frame["weight"].sum() - 1.0) < 1e-9
        assert set(frame["sector_code"]) == {"801010", "801030"}

        stored = FundIndustryExposureRepository(db).find_by_fund_and_date(
            self.FUND, report_date, 1
        )
        assert not stored.empty, "现算结果应回写预计算表，下次读直接命中"

    def test_daily_exposure_never_predates_first_report(self, engine):
        """日线暴露不得早于最早报告期：拿 6 月持仓往前铺满全年是伪事实"""
        _instance, db = engine
        from stocklab.facade import FundHoldingFacade

        report_date = self.TRADE_DATE.replace(month=6, day=30)
        facade = FundHoldingFacade(db)
        daily = facade.get_industry_exposure_daily(
            self.FUND, level=1, since=date(2026, 1, 1), until=self.TRADE_DATE
        )

        assert not daily.empty
        assert pd.to_datetime(daily["trade_date"]).min() >= pd.Timestamp(report_date)
        # 只有两个有持仓的行业，逐日插值
        assert set(daily["sector_code"]) == {"801010", "801030"}


# =============================================================================
# 5. 接口契约（真实库）
# =============================================================================

def _json(response):
    return response.get_json()


class TestFundAttributionApi:
    """/api/fund/attribution 的参数校验与数据缺失分支"""

    def test_missing_code_returns_400(self, client):
        resp = client.get("/api/fund/attribution")
        assert resp.status_code == 400
        assert "error" in _json(resp)

    def test_unknown_fund_returns_400(self, client):
        resp = client.get("/api/fund/attribution?code=NOSUCH.OF")
        assert resp.status_code == 400
        assert "error" in _json(resp)

    @pytest.mark.parametrize("query", [
        "code=000001.OF&benchmark=NOPE",
        "code=000001.OF&model=capm",
        "code=000001.OF&days=abc",
        "code=000001.OF&days=9999",
        "code=000001.OF&days=1",
        "code=000001.OF&level=9",
        "code=000001.OF&date=2026-13-45",
    ])
    def test_invalid_params_return_400(self, client, query):
        resp = client.get("/api/fund/attribution?" + query)
        assert resp.status_code == 400, query
        assert "error" in _json(resp), query

    def test_benchmark_alias_accepted(self, client):
        """000300（无后缀）应被识别为 000300.SH 而不是 400"""
        resp = client.get("/api/fund/attribution?code=NOSUCH.OF&benchmark=000300")
        assert resp.status_code == 400
        assert "benchmark" not in _json(resp)["error"]


class TestRepairedExposureApi:
    """上一轮 500 的三个持仓接口（曾因缺 import pandas）"""

    @pytest.mark.parametrize("path", [
        "/api/fund/exposure?code=000001.OF&date=2026-06-30&level=1",
        "/api/fund/exposure/daily?code=000001.OF&level=1&start=2026-01-01",
        "/api/fund/holding/vs_rbsa?code=000001.OF&date=2026-06-30&window=60&level=1",
    ])
    def test_no_longer_server_error(self, client, path):
        resp = client.get(path)
        assert resp.status_code != 500, path
        assert resp.status_code in (200, 400), path
        body = _json(resp)
        if resp.status_code == 400:
            assert "error" in body, path
        else:
            assert "error" not in body, path

    @pytest.mark.parametrize("path", [
        "/api/fund/exposure",
        "/api/fund/exposure?code=000001.OF&level=abc",
        "/api/fund/exposure/daily?code=000001.OF&start=bad-date",
        "/api/fund/holding/vs_rbsa?code=000001.OF&window=0",
    ])
    def test_invalid_params_return_400(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 400, path
        assert "error" in _json(resp), path
