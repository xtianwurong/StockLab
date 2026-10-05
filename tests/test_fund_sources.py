#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金/行业/资金流数据源采集测试 (tests/test_fund_sources.py)
==============================================================================

【功能用途】
  离线覆盖纯采集函数（外部接口一律用桩替掉，不发任何网络请求）：
    - datasource/fund.py            基金列表 / 基金信息 / 净值历史
    - datasource/capital_flow.py    板块资金流（列映射 + 衍生字段 + top_n 截断）
    - datasource/benchmark_index.py 成分券权重（百分比↔小数的单位判定、非中证指数拒收）
    - datasource/sw_indices.py      申万指数日线与映射（新旧接口回退、缺列拒收）
    - datasource/stock_industry.py  股票行业映射（并发成分采集、二级回填一级、代码带后缀）
    - datasource/fund_holding.py    持仓 HTTP 抓取链路（重试、报文解析、报告期聚合）

【为什么这些必须离线测】
  这些函数的失败模式是「上游悄悄换列名 / 单位变了」，一旦没人断言，
  写进库的数据会长期错着而表面一切正常（权重差 100 倍、映射 join 0 行）。

【运行方式】
  ./venv/bin/python -m pytest tests/test_fund_sources.py -v
"""

import os
import sys
import types
from datetime import date

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from stocklab.datasource import capital_flow, stock_industry, sw_indices
from stocklab.datasource import fund as fund_source
from stocklab.datasource.benchmark_index import fetch_index_constituent_weights
from stocklab.datasource.fund_holding import (
    _fetch_eastmoney_holding_page,
    fetch_fund_holding_by_report_date,
    fetch_fund_holding_history,
    fetch_latest_fund_holding,
)
from stocklab.domain import (
    CAPITAL_FLOW_DAILY_COLUMNS,
    FUND_HOLDING_COLUMNS,
    FUND_INFO_COLUMNS,
    FUND_NAV_HISTORY_COLUMNS,
    STOCK_INDUSTRY_MAPPING_COLUMNS,
    SW_INDUSTRY_MAPPING_COLUMNS,
    SW_INDEX_DAILY_COLUMNS,
)


# ---------------------------------------------------------------------------
# 桩：把 akshare 整个模块换掉（这些函数在体内 `import akshare as ak`）
# ---------------------------------------------------------------------------
def _fake_ak(monkeypatch, **attrs):
    """替换 sys.modules 里的 akshare，返回被替换的桩（可事后断言调用）"""
    module = types.ModuleType("akshare")
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "akshare", module)
    return module


# ===========================================================================
# 1. datasource/fund.py
# ===========================================================================
class TestFundSource:
    def test_fetch_all_fund_codes_normalizes_and_dedups(self, monkeypatch):
        raw = pd.DataFrame({
            "基金代码": ["1", "000001", "161725"],
            "基金简称": ["华夏成长", "华夏现金增利", "招商中证白酒"],
            "基金类型": ["混合型", "货币型", "指数型"],
        })
        _fake_ak(monkeypatch, fund_name_em=lambda: raw)

        frame = fund_source.fetch_all_fund_codes()

        # 裸码补零 + 统一 .OF 后缀；否则下游按 6 位码 join 全部落空
        assert frame["fund_code"].tolist() == ["000001.OF", "000001.OF", "161725.OF"]
        assert list(frame.columns) == ["fund_code", "fund_name", "fund_type"]
        # 去重看的是三列整体：代码同但名称/类型不同（联接 A/C）不能被吞掉
        assert len(frame) == 3

    def test_fetch_all_fund_codes_empty_returns_contract(self, monkeypatch):
        _fake_ak(monkeypatch, fund_name_em=lambda: pd.DataFrame())
        frame = fund_source.fetch_all_fund_codes()
        assert frame.empty
        assert list(frame.columns) == ["fund_code", "fund_name", "fund_type"]

    def test_fetch_fund_info_parses_xq_fields(self, monkeypatch):
        raw = pd.DataFrame({
            "item": ["基金名称", "基金简称", "基金类型", "基金经理", "基金公司",
                     "成立日期", "业绩比较基准", "基金状态"],
            "value": ["华夏成长混合", "华夏成长", "混合型", "张明", "华夏基金",
                      "1998-03-05", "沪深300", "正常"],
        })
        _fake_ak(monkeypatch, fund_individual_basic_info_xq=lambda symbol: raw)

        frame = fund_source.fetch_fund_info("000001.OF")

        assert list(frame.columns) == list(FUND_INFO_COLUMNS)
        row = frame.iloc[0]
        assert row["fund_code"] == "000001.OF"
        assert row["fund_name"] == "华夏成长混合"
        assert row["establish_date"] == date(1998, 3, 5)
        assert row["status"] == "active"

    def test_fetch_fund_info_terminated_status(self, monkeypatch):
        raw = pd.DataFrame({"item": ["基金状态"], "value": ["清盘"]})
        _fake_ak(monkeypatch, fund_individual_basic_info_xq=lambda symbol: raw)

        frame = fund_source.fetch_fund_info("000001.OF")
        assert frame.iloc[0]["status"] == "terminated"

    @pytest.mark.parametrize("behaviour", ["raises", "empty"])
    def test_fetch_fund_info_failure_returns_empty_contract(self, monkeypatch, behaviour):
        if behaviour == "raises":
            def boom(symbol):
                raise RuntimeError("接口 502")
        else:
            boom = lambda symbol: pd.DataFrame()
        _fake_ak(monkeypatch, fund_individual_basic_info_xq=boom)

        frame = fund_source.fetch_fund_info("000001.OF")
        assert frame.empty
        assert list(frame.columns) == list(FUND_INFO_COLUMNS)

    # -- 净值历史：双源回退（lsjz 主源 / pingzhongdata 备源） ------------------
    @staticmethod
    def _nav_raw():
        return pd.DataFrame({
            "净值日期": ["2026-01-03", "2026-01-02", "2025-12-31"],
            "单位净值": ["1.234", "1.230", "1.220"],
            "累计净值": ["2.334", "2.330", "2.320"],
            "日增长率": ["0.32%", "-0.10%", "1.00%"],
        })

    @staticmethod
    def _stub_nav_sources(monkeypatch, primary=None, backup=None):
        """桩掉两个净值源，返回各自被调用次数的字典"""
        seen = {"primary": 0, "backup": 0}

        def _primary_impl(code, start_date=None, max_pages=None):
            seen["primary"] += 1
            if isinstance(primary, Exception):
                raise primary
            return primary

        def _backup_impl(code, **kwargs):
            seen["backup"] += 1
            if isinstance(backup, Exception):
                raise backup
            return backup

        monkeypatch.setattr(fund_source, "_nav_raw_from_lsjz", _primary_impl)
        monkeypatch.setattr(fund_source, "_nav_raw_from_pingzhongdata", _backup_impl)
        return seen

    def test_fetch_nav_history_filters_and_sorts(self, monkeypatch):
        self._stub_nav_sources(monkeypatch, primary=self._nav_raw())

        frame = fund_source.fetch_fund_nav_history(
            "000001.OF", start_date="2026-01-01", end_date="2026-12-31"
        )

        assert list(frame.columns) == list(FUND_NAV_HISTORY_COLUMNS)
        # 区间过滤 + 按日期升序 + 百分号剥离成小数
        assert frame["nav_date"].tolist() == [date(2026, 1, 2), date(2026, 1, 3)]
        assert frame["change_pct"].tolist() == [pytest.approx(-0.10), pytest.approx(0.32)]
        assert frame["fund_code"].eq("000001.OF").all()
        # source 必须写**实际**取到数据的源，否则「拿备源冒充主源」查不出来
        assert frame["source"].eq("eastmoney:lsjz").all()

    def test_fetch_nav_history_drops_non_positive(self, monkeypatch):
        raw = pd.DataFrame({
            "净值日期": ["2026-01-02", "2026-01-03"],
            "单位净值": ["0", "1.5"],
            "累计净值": ["0", "2.5"],
            "日增长率": ["0%", "0%"],
        })
        self._stub_nav_sources(monkeypatch, primary=raw)

        frame = fund_source.fetch_fund_nav_history("000001.OF")
        # 净值必须 > 0：0 是「没取到」被 to_numeric 后的样子，混进来会让涨跌幅变 NaN
        assert frame["nav"].tolist() == [pytest.approx(1.5)]

    def test_fetch_nav_history_without_acc_nav_column(self, monkeypatch):
        """akshare 1.18 起只回「净值日期/单位净值/日增长率」——缺累计净值列不能 KeyError

        这是真实发生过的崩溃：老代码在 try 之外做 df["acc_nav"]，源少一列就
        把整个同步脚本带崩（而不是降级成 acc_nav 为空）。
        """
        raw = pd.DataFrame({
            "净值日期": ["2026-01-03", "2026-01-02"],
            "单位净值": ["1.234", "1.230"],
            "日增长率": ["0.32%", "-0.10%"],
        })
        self._stub_nav_sources(monkeypatch, primary=raw)

        frame = fund_source.fetch_fund_nav_history("000001.OF")

        assert list(frame.columns) == list(FUND_NAV_HISTORY_COLUMNS)
        assert len(frame) == 2
        assert frame["acc_nav"].isna().all()   # 缺就留空，不编一个值出来

    def test_fetch_nav_history_falls_back_to_backup(self, monkeypatch):
        """主源（lsjz）失败时必须用备源，而不是直接回空 —— 单源挂掉=净值恒空"""
        seen = self._stub_nav_sources(monkeypatch, primary=None, backup=self._nav_raw())

        frame = fund_source.fetch_fund_nav_history("000001.OF")

        assert not frame.empty
        assert seen["backup"] == 1
        assert frame["source"].eq("akshare:pingzhongdata").all()

    @pytest.mark.parametrize("behaviour", ["none", "raises", "empty_after_filter"])
    def test_fetch_nav_history_both_sources_failed_returns_contract(self, monkeypatch, behaviour):
        if behaviour == "none":
            self._stub_nav_sources(monkeypatch, primary=None, backup=None)
        elif behaviour == "raises":
            self._stub_nav_sources(
                monkeypatch,
                primary=RuntimeError("接口 502"),
                backup=RuntimeError("接口 502"),
            )
        else:
            # 两源都取到了数，但全落在请求区间外 —— 一样是「没有可用净值」
            outside = pd.DataFrame({"净值日期": ["2020-01-02"], "单位净值": ["1.0"],
                                    "日增长率": ["0%"]})
            self._stub_nav_sources(monkeypatch, primary=outside, backup=outside)

        frame = fund_source.fetch_fund_nav_history(
            "000001.OF", start_date="2026-06-01", end_date="2026-12-31"
        )
        assert frame.empty
        assert list(frame.columns) == list(FUND_NAV_HISTORY_COLUMNS)

    # -- lsjz 分页本身 ------------------------------------------------------
    def test_lsjz_stops_paging_at_start_date(self, monkeypatch):
        """按起始日翻页：翻到早于起点的行就停，不能把全历史几百页都拉下来"""
        pages = {
            1: ["2026-01-05", "2026-01-04"],
            2: ["2026-01-03", "2026-01-02"],
            3: ["2026-01-01", "2025-12-31"],
        }
        calls = []

        class _Resp:
            def __init__(self, rows):
                self._rows = rows

            def raise_for_status(self):
                pass

            def json(self):
                return {"ErrCode": 0, "Data": {"LSJZList": self._rows}}

        def fake_get(url, params=None, headers=None, timeout=None):
            page = int(params["pageIndex"])
            calls.append(page)
            rows = pages.get(page, [])
            return _Resp([{"FSRQ": d, "DWJZ": "1.0", "LJJZ": "2.0", "JZZZL": "0.1"}
                          for d in rows])

        monkeypatch.setattr("requests.get", fake_get)

        raw = fund_source._nav_raw_from_lsjz("000001", start_date="2026-01-01")

        assert raw is not None
        # 第 3 页最老 2025-12-31 < 起点 -> 停；不该再翻第 4 页
        assert calls == [1, 2, 3]
        assert len(raw) == 6

    def test_lsjz_page_failure_discards_partial_series(self, monkeypatch):
        """任一页失败 -> 整源判失败返回 None

        半截历史比没有更糟：相关性会拿不完整的序列算出一个错值，却看不出来。
        """
        page = {"n": 0}

        class _Resp:
            def raise_for_status(self):
                if page["n"] >= 2:
                    raise RuntimeError("502")

            def json(self):
                return {"ErrCode": 0, "Data": {"LSJZList": [
                    {"FSRQ": "2026-01-05", "DWJZ": "1.0", "LJJZ": "2.0", "JZZZL": "0"}]}}

        def fake_get(url, params=None, headers=None, timeout=None):
            page["n"] += 1
            return _Resp()

        monkeypatch.setattr("requests.get", fake_get)
        assert fund_source._nav_raw_from_lsjz("000001", start_date="2026-01-01") is None

    def test_lsjz_err_code_returns_none(self, monkeypatch):
        """没有 Referer 时东财回 ErrCode=-999、Data 为空串 -> 判失败，不能当成「净值为空」"""

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"Data": "", "ErrCode": -999, "ErrMsg": "禁止访问"}

        monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
        assert fund_source._nav_raw_from_lsjz("000001") is None

    def test_parse_date(self):
        assert fund_source._parse_date("2026-03-31") == date(2026, 3, 31)
        assert fund_source._parse_date("") is None
        assert fund_source._parse_date(None) is None
        assert fund_source._parse_date("not-a-date") is None


# ===========================================================================
# 2. datasource/capital_flow.py
# ===========================================================================
class TestCapitalFlowSource:
    @staticmethod
    def _raw():
        return pd.DataFrame({
            "板块名称": ["银行", "白酒"],
            "板块代码": ["BK0475", "BK0896"],
            "今日涨跌幅": ["1.20", "-0.50"],
            "主力净流入": ["1200000000", "-300000000"],
            "主力净流入占比": ["12.3", "-4.5"],
            "超大单净流入": ["500000000", "-100000000"],
            "大单净流入": ["400000000", "-100000000"],
            "中单净流入": ["200000000", "100000000"],
            "小单净流入": ["100000000", "200000000"],
        })

    def test_fetch_sector_capital_flow_maps_columns(self, monkeypatch):
        _fake_ak(monkeypatch, stock_sector_fund_flow_rank=lambda **kw: self._raw())

        frame = capital_flow.fetch_sector_capital_flow("industry", "2026-09-30")

        assert list(frame.columns) == list(CAPITAL_FLOW_DAILY_COLUMNS)
        assert frame["sector_type"].eq("industry").all()
        assert frame["source"].eq(capital_flow.CAPITAL_FLOW_SOURCE_AKSHARE).all()
        # 12亿 = 主力(12亿) + 超大单(5亿) + 大单(4亿) + 中单(2亿) + 小单(1亿)
        assert frame.loc[0, "net_inflow"] == pytest.approx(1200000000 + 500000000
                                                          + 400000000 + 200000000 + 100000000)
        assert frame.loc[0, "inflow"] == pytest.approx(1200000000 + 500000000 + 400000000)
        assert frame.loc[0, "outflow"] == pytest.approx(200000000 + 100000000)
        assert frame.loc[0, "retail_net_inflow"] == pytest.approx(100000000)
        # 净流入率直接用源自带的「主力净流入占比」（百分比）。
        # 早先写的 net_inflow/1e8 把 1 亿当成成交额，量纲既不是百分比也不是小数，
        # 下游拿它设门槛等于在随机数上做判断。
        assert frame.loc[0, "net_inflow_rate"] == pytest.approx(12.3)

    def test_eastmoney_has_no_sw_sector_type(self, monkeypatch):
        """东财板块资金流没有申万口径：返回空表并指路，不能悄悄返回别的口径冒充

        旧实现传 "sw1" 给 akshare -> KeyError 被 except 吞成空表，接口 200 + 0 行，
        调用方完全看不出是「源不支持」还是「当天没数据」。
        """
        _fake_ak(monkeypatch, stock_sector_fund_flow_rank=lambda **kw: self._raw())

        for sector_type in ("sw_level1", "sw_level2"):
            frame = capital_flow.fetch_sector_capital_flow(sector_type, "2026-09-30")
            assert frame.empty
            assert list(frame.columns) == list(CAPITAL_FLOW_DAILY_COLUMNS)

    @pytest.mark.parametrize("sector_type,expect_sub_type", [
        ("industry", "行业资金流"),
        ("concept", "概念资金流"),
    ])
    def test_eastmoney_sector_type_uses_legal_labels(self, monkeypatch, sector_type, expect_sub_type):
        """akshare 1.x 只认中文标签；传旧的 industry/concept 会被 KeyError 掐掉"""
        seen = {}

        def fake_rank(**kwargs):
            seen.update(kwargs)
            return self._raw()

        _fake_ak(monkeypatch, stock_sector_fund_flow_rank=fake_rank)
        capital_flow.fetch_sector_capital_flow(sector_type, "2026-09-30")
        assert seen["sector_type"] == expect_sub_type

    def test_fetch_sector_capital_flow_respects_top_n(self, monkeypatch):
        raw = pd.DataFrame({
            "板块名称": [f"板块{i}" for i in range(10)],
            "板块代码": [f"BK{i:04d}" for i in range(10)],
        })
        _fake_ak(monkeypatch, stock_sector_fund_flow_rank=lambda **kw: raw)

        frame = capital_flow.fetch_sector_capital_flow("concept", "2026-09-30", top_n=3)

        assert len(frame) == 3
        # 只有名称/代码的最小响应也不能炸：缺失字段按契约补空
        assert list(frame.columns) == list(CAPITAL_FLOW_DAILY_COLUMNS)
        assert frame["net_inflow"].notna().all()

    @pytest.mark.parametrize("behaviour", ["raises", "empty"])
    def test_fetch_sector_capital_flow_failure_returns_empty(self, monkeypatch, behaviour):
        if behaviour == "raises":
            def boom(**kwargs):
                raise RuntimeError("接口 502")
        else:
            boom = lambda **kwargs: pd.DataFrame()
        _fake_ak(monkeypatch, stock_sector_fund_flow_rank=boom)

        frame = capital_flow.fetch_sector_capital_flow("industry", "2026-09-30")
        assert frame.empty
        assert list(frame.columns) == list(CAPITAL_FLOW_DAILY_COLUMNS)

    def test_concept_wrapper_delegates(self, monkeypatch):
        _fake_ak(monkeypatch, stock_sector_fund_flow_rank=lambda **kw: self._raw())

        frame = capital_flow.fetch_concept_capital_flow("2026-09-30")

        assert not frame.empty
        assert frame["sector_type"].eq("concept").all()


# ---------------------------------------------------------------------------
# 新浪个股资金流 -> 申万聚合（东财 push2 不可用期间的可用口径）
# ---------------------------------------------------------------------------
class TestSinaSwCapitalFlow:
    @staticmethod
    def _individual():
        """新浪返回的原始表（金额带单位、代码可能不补零）"""
        return pd.DataFrame({
            "序号": [1, 2, 3, 4],
            "股票代码": ["600519", "000858", "1246", "N/A"],
            "股票简称": ["贵州茅台", "五粮液", "主力未匹配", "坏行"],
            "流入资金": ["40.00亿", "20.00亿", "1000.00万", "1.00亿"],
            "流出资金": ["30.00亿", "25.00亿", "500.00万", "1.00亿"],
            "净额": ["10.00亿", "-5.00亿", "500.00万", "0"],
            "成交额": ["70.00亿", "45.00亿", "0", "0"],
        })

    @staticmethod
    def _normalized():
        """fetch_stock_fund_flow_individual() 的输出形态（金额已换成元、代码已补零）"""
        return pd.DataFrame({
            "stock_code": ["600519", "000858", "001246"],
            "stock_name": ["贵州茅台", "五粮液", "未匹配"],
            "inflow": [4.0e9, 2.0e9, 1.0e7],
            "outflow": [3.0e9, 2.5e9, 5.0e6],
            "net_inflow": [1.0e9, -5.0e8, 5.0e6],
            "turnover": [7.0e9, 4.5e9, 0.0],
        })

    @staticmethod
    def _mapping():
        return pd.DataFrame({
            "ts_code": ["600519.SH", "000858.SZ"],
            "sw_l1_code": ["801010", "801050"],
            "sw_l1_name": ["农林牧渔", "有色金属"],
            "sw_l2_code": ["801011", "801051"],
            "sw_l2_name": ["种植业", "能源金属"],
        })

    def test_parse_amount_units(self):
        assert capital_flow._parse_amount("39.39亿") == pytest.approx(3.939e9)
        assert capital_flow._parse_amount("696.46万") == pytest.approx(6.9646e6)
        assert capital_flow._parse_amount("12345") == pytest.approx(12345.0)
        assert capital_flow._parse_amount("1.5万亿") == pytest.approx(1.5e12)
        # 解析不了返回 None 而不是 0：0 会参与求和，把整个板块金额做小
        assert capital_flow._parse_amount("--") is None
        assert capital_flow._parse_amount(None) is None
        assert capital_flow._parse_amount(float("nan")) is None

    def test_fetch_individual_normalizes_codes(self, monkeypatch):
        _fake_ak(monkeypatch, stock_fund_flow_individual=lambda symbol: self._individual())

        frame = capital_flow.fetch_stock_fund_flow_individual()

        assert list(frame.columns) == ["stock_code", "stock_name", "inflow", "outflow",
                                       "net_inflow", "turnover"]
        # 代码补零到 6 位（源会回 1246 这种不补零的），否则与本地映射 join 全部落空
        assert frame["stock_code"].tolist()[:3] == ["600519", "000858", "001246"]
        # 非法代码整行丢弃
        assert "N/A" not in frame["stock_code"].tolist()
        # 金额带单位必须换成元
        assert frame.loc[frame["stock_code"] == "600519", "net_inflow"].iloc[0] == pytest.approx(1e9)

    @pytest.mark.parametrize("behaviour", ["raises", "empty"])
    def test_fetch_individual_failure_returns_empty(self, monkeypatch, behaviour):
        if behaviour == "raises":
            def boom(symbol):
                raise RuntimeError("网络失败")
        else:
            boom = lambda symbol: pd.DataFrame()
        _fake_ak(monkeypatch, stock_fund_flow_individual=boom)

        frame = capital_flow.fetch_stock_fund_flow_individual()
        assert frame.empty

    @pytest.mark.parametrize("level", [1, 2])
    def test_aggregate_sw_flow_sums_by_level(self, level):
        frame, meta = capital_flow.aggregate_stock_flow_to_sw(
            self._normalized(), self._mapping(), level=level, trade_date=date(2026, 9, 30)
        )

        assert list(frame.columns) == list(CAPITAL_FLOW_DAILY_COLUMNS)
        assert len(frame) == 2
        assert frame["trade_date"].eq(date(2026, 9, 30)).all()
        assert frame["sector_type"].eq(f"sw_level{level}").all()
        # 未匹配的第三只（001246）不能被算进来，也不能凭空补一个行业
        expected = {"801010", "801050"} if level == 1 else {"801011", "801051"}
        assert set(frame["sector_code"]) == expected
        # 金额是分组求和（单位元）
        first = frame[frame["sector_code"] == ("801010" if level == 1 else "801011")]
        assert first["net_inflow"].iloc[0] == pytest.approx(1e9)
        assert first["inflow"].iloc[0] == pytest.approx(4e9)
        assert first["outflow"].iloc[0] == pytest.approx(3e9)
        # 净流入率 = 净流入 / 成交额（百分比）：成交额 70 亿 -> 1e9/7e9*100
        assert first["net_inflow_rate"].iloc[0] == pytest.approx(100 / 7)
        # 覆盖率如实披露（3 只里匹配 2 只）
        assert meta["stock_total"] == 3
        assert meta["stock_matched"] == 2
        # 覆盖率保留 4 位小数（对外只披露到万分位）
        assert meta["stock_coverage"] == pytest.approx(2 / 3, abs=1e-4)

    def test_aggregate_leaves_main_retail_none(self):
        """新浪是全单口径（流入+流出≈成交额），源没有主力/散户拆分 -> 留 None

        填 0 会让下游读成「主力净流入为 0」，填 net_inflow 则是把全单当主力。
        """
        frame, _ = capital_flow.aggregate_stock_flow_to_sw(
            self._normalized(), self._mapping(), level=1, trade_date=date(2026, 9, 30)
        )
        assert frame["main_net_inflow"].isna().all()
        assert frame["retail_net_inflow"].isna().all()
        assert frame["source"].eq(capital_flow.CAPITAL_FLOW_SOURCE_SINA_INDIVIDUAL).all()

    def test_aggregate_zero_turnover_rate_is_none(self):
        """成交额为 0 时净流入率留 None —— 除以 0 得到的 inf/NaN 都是假结论"""
        individual = pd.DataFrame({
            "stock_code": ["600519"], "stock_name": ["贵州茅台"],
            "inflow": [1e8], "outflow": [5e7], "net_inflow": [5e7], "turnover": [0.0],
        })
        frame, _ = capital_flow.aggregate_stock_flow_to_sw(
            individual, self._mapping(), level=1, trade_date=date(2026, 9, 30)
        )
        assert frame["net_inflow_rate"].isna().all()

    @pytest.mark.parametrize("level", [0, 3])
    def test_aggregate_rejects_bad_level(self, level):
        with pytest.raises(ValueError):
            capital_flow.aggregate_stock_flow_to_sw(
                self._individual(), self._mapping(), level=level
            )

    @pytest.mark.parametrize("case", ["individual_none", "individual_empty", "mapping_empty"])
    def test_aggregate_empty_inputs_return_contract(self, case):
        """输入缺失时返回契约空表 + 覆盖率 0，不抛异常也不编行业"""
        individual = self._normalized()
        mapping = self._mapping()
        if case == "individual_none":
            individual = None
        elif case == "individual_empty":
            individual = pd.DataFrame()
        else:
            mapping = pd.DataFrame()

        frame, meta = capital_flow.aggregate_stock_flow_to_sw(individual, mapping, level=1)

        assert frame.empty
        assert list(frame.columns) == list(CAPITAL_FLOW_DAILY_COLUMNS)
        assert meta["sector_count"] == 0
        assert meta["stock_coverage"] == 0.0

    def test_aggregate_missing_mapping_column_returns_empty(self):
        """映射缺 sw_l2 列时（只刷过一级）必须明确失败，不能当二级全无覆盖"""
        mapping = self._mapping()[["ts_code", "sw_l1_code", "sw_l1_name"]]
        frame, meta = capital_flow.aggregate_stock_flow_to_sw(
            self._normalized(), mapping, level=2
        )
        assert frame.empty
        assert meta["sector_count"] == 0

    def test_sw_wrappers_return_frame_and_meta(self, monkeypatch):
        _fake_ak(monkeypatch, stock_fund_flow_individual=lambda symbol: self._individual())

        frame, meta = capital_flow.fetch_sw_level1_capital_flow(
            self._mapping(), trade_date=date(2026, 9, 30)
        )
        assert not frame.empty
        assert frame["sector_type"].eq("sw_level1").all()
        assert meta["stock_coverage"] > 0

        frame2, meta2 = capital_flow.fetch_sw_level2_capital_flow(
            self._mapping(), trade_date=date(2026, 9, 30)
        )
        assert frame2["sector_type"].eq("sw_level2").all()

    def test_fetch_sw_capital_flow_respects_top_n(self, monkeypatch):
        individual = pd.DataFrame({
            "stock_code": [f"60000{i}" for i in range(8)],
            "stock_name": [f"股{i}" for i in range(8)],
            "inflow": [float(i) for i in range(8)],
            "outflow": [0.0] * 8,
            "net_inflow": [float(i) for i in range(8)],
            "turnover": [1e9] * 8,
        })
        mapping = pd.DataFrame({
            "ts_code": [f"60000{i}.SH" for i in range(8)],
            "sw_l1_code": [f"8010{20 + i}" for i in range(8)],
            "sw_l1_name": [f"行业{i}" for i in range(8)],
        })
        _fake_ak(monkeypatch, stock_fund_flow_individual=lambda symbol: individual)

        frame, _ = capital_flow.fetch_sw_capital_flow(mapping, level=1, top_n=3)

        assert len(frame) == 3
        # 按绝对净流入排序取前 3，方向不能反
        assert frame["net_inflow"].abs().is_monotonic_decreasing

# ===========================================================================
# 3. datasource/benchmark_index.py：成分券权重
# ===========================================================================
def _weight_raw(weights, date_text="2026-08-31", codes=("600000", "000001")):
    return pd.DataFrame({
        "成分券代码": list(codes),
        "权重": list(weights),
        "日期": [date_text] * len(weights),
    })


class TestConstituentWeights:
    def test_percentage_weights_are_converted_to_decimal(self, monkeypatch):
        # 中证口径是百分比（合计≈100）：不换算会让行业权重整体放大 100 倍
        _fake_ak(monkeypatch, index_stock_cons_weight_csindex=lambda symbol: _weight_raw([4.33, 3.21]))

        frame = fetch_index_constituent_weights("000300.SH")

        assert frame["weight"].tolist() == [pytest.approx(0.0433), pytest.approx(0.0321)]
        assert frame["as_of_date"].eq("2026-08-31").all()
        assert frame["ts_code"].tolist() == ["600000.SH", "000001.SZ"]

    def test_already_decimal_weights_untouched(self, monkeypatch):
        # 合计 < 1.5 说明已是小数（中证1000 单票普遍 <1.5%，按最大值判断会误除）
        _fake_ak(monkeypatch, index_stock_cons_weight_csindex=lambda symbol: _weight_raw([0.0043, 0.0032]))

        frame = fetch_index_constituent_weights("000852.SH")
        assert frame["weight"].tolist() == [pytest.approx(0.0043), pytest.approx(0.0032)]

    def test_non_csi_index_rejected_without_network(self, monkeypatch):
        def boom(symbol):
            raise AssertionError("非中证指数不应发起任何请求")
        _fake_ak(monkeypatch, index_stock_cons_weight_csindex=boom)

        frame = fetch_index_constituent_weights("399001.SZ")
        assert frame.empty
        assert list(frame.columns) == ["ts_code", "weight", "as_of_date", "source"]

    @pytest.mark.parametrize("behaviour", ["raises", "empty", "no_weight_column"])
    def test_fetch_failure_returns_empty(self, monkeypatch, behaviour):
        if behaviour == "raises":
            def boom(symbol):
                raise RuntimeError("ProxyError")
            func = boom
        elif behaviour == "empty":
            func = lambda symbol: pd.DataFrame()
        else:
            func = lambda symbol: pd.DataFrame({"成分券代码": ["600000"], "日期": ["2026-08-31"]})
        _fake_ak(monkeypatch, index_stock_cons_weight_csindex=func)

        frame = fetch_index_constituent_weights("000300.SH")
        assert frame.empty
        assert list(frame.columns) == ["ts_code", "weight", "as_of_date", "source"]


# ===========================================================================
# 4. datasource/sw_indices.py
# ===========================================================================
def _sw_raw():
    return pd.DataFrame({
        "日期": ["2026-07-02", "2026-07-03", "2026-06-30"],
        "开盘": [1000.0, 1010.0, 990.0],
        "收盘": [1010.0, 1020.0, 995.0],
        "最高": [1015.0, 1025.0, 1000.0],
        "最低": [995.0, 1005.0, 985.0],
        "成交量": [1e8, 1.1e8, 0.9e8],
        "成交额": [1e9, 1.1e9, 0.9e9],
    })


class TestSwIndices:
    def test_fetch_daily_maps_and_clips_window(self, monkeypatch):
        _fake_ak(monkeypatch, index_hist_sw=lambda **kw: _sw_raw())

        frame = sw_indices.fetch_sw_index_daily("801010.SI", "2026-07-01", "2026-07-31")

        assert list(frame.columns) == list(SW_INDEX_DAILY_COLUMNS)
        # 新版接口回全历史，必须按调用区间裁剪（6-30 落在窗口外）
        assert frame["trade_date"].tolist() == [date(2026, 7, 2), date(2026, 7, 3)]
        assert frame["symbol"].eq("801010.SI").all()
        assert frame["close"].tolist() == [pytest.approx(1010.0), pytest.approx(1020.0)]

    def test_falls_back_to_legacy_interface(self, monkeypatch):
        def new_api(**kwargs):
            raise RuntimeError("接口已下线")
        _fake_ak(
            monkeypatch,
            index_hist_sw=new_api,
            sw_index_daily=lambda **kwargs: _sw_raw(),
        )

        frame = sw_indices.fetch_sw_index_daily("801010.SI", "2026-07-01", "2026-07-31")
        assert not frame.empty

    @pytest.mark.parametrize("behaviour", ["all_fail", "missing_column", "no_valid_rows"])
    def test_unusable_response_returns_empty_contract(self, monkeypatch, behaviour):
        if behaviour == "all_fail":
            def fail_new(**kwargs):
                raise RuntimeError("接口已下线")
            def fail_old(**kwargs):
                raise RuntimeError("接口已下线")
            _fake_ak(monkeypatch, index_hist_sw=fail_new, sw_index_daily=fail_old)
        elif behaviour == "missing_column":
            _fake_ak(monkeypatch,
                     index_hist_sw=lambda **kw: pd.DataFrame({"日期": ["2026-07-02"], "收盘": [10.0]}))
        else:
            # 关闭价全为 0 -> 归一化后无有效行
            bad = _sw_raw()
            bad["收盘"] = 0.0
            _fake_ak(monkeypatch, index_hist_sw=lambda **kw: bad)

        frame = sw_indices.fetch_sw_index_daily("801010.SI", "2026-07-01", "2026-07-31")
        assert frame.empty
        assert list(frame.columns) == list(SW_INDEX_DAILY_COLUMNS)

    def test_mapping_collects_both_levels_and_parents(self, monkeypatch):
        def realtime(symbol):
            if symbol == "一级行业":
                return pd.DataFrame({"指数代码": ["801010", "801080"],
                                     "指数名称": ["农林牧渔", "银行"]})
            return pd.DataFrame({"指数代码": ["801016", "801780"],
                                 "指数名称": ["种植业", "银行"]})

        _fake_ak(
            monkeypatch,
            index_realtime_sw=realtime,
            sw_index_second_info=lambda: pd.DataFrame({
                "行业代码": ["801016", "801780"],
                "上级行业": ["农林牧渔", "银行"],
            }),
        )

        frame = sw_indices.fetch_sw_industry_mapping()

        assert list(frame.columns) == list(SW_INDUSTRY_MAPPING_COLUMNS)
        assert set(frame["level"]) == {1, 2}
        by_code = {str(r.index_code): r for r in frame.itertuples()}
        # 二级要能回查到一级（名称 -> 代码），否则父子链断在半路
        assert by_code["801016"].parent_code == "801010"
        assert by_code["801780"].parent_code == "801080"
        assert by_code["801010"].parent_code == ""

    def test_mapping_parent_lookup_failure_still_returns_rows(self, monkeypatch):
        def boom():
            raise RuntimeError("legulegu 偶发 NoneType")
        _fake_ak(
            monkeypatch,
            index_realtime_sw=lambda symbol: pd.DataFrame(
                {"指数代码": ["801010"], "指数名称": ["农林牧渔"]}
            ),
            sw_index_second_info=boom,
        )

        frame = sw_indices.fetch_sw_industry_mapping()
        assert not frame.empty
        assert frame["parent_code"].fillna("").eq("").all()

    def test_mapping_without_any_interface_returns_contract(self, monkeypatch):
        _fake_ak(monkeypatch)   # 一个接口都没有的旧版 akshare

        frame = sw_indices.fetch_sw_industry_mapping()
        assert frame.empty
        assert list(frame.columns) == list(SW_INDUSTRY_MAPPING_COLUMNS)


# ===========================================================================
# 5. datasource/stock_industry.py：全市场股票行业映射
# ===========================================================================
class TestStockIndustryMapping:
    @staticmethod
    def _listing():
        return pd.DataFrame({
            "index_code": ["801010.SI", "801080.SI", "801016.SI"],
            "index_name": ["农林牧渔", "银行", "种植业"],
            "level": [1, 1, 2],
            "parent_code": ["", "", "801010"],
            "sw_first_code": ["", "", "801010"],
            "description": ["", "", ""],
        })

    @staticmethod
    def _ak(monkeypatch, components, parents=None, fail_for=()):
        def component_sw(symbol):
            if symbol in fail_for:
                raise RuntimeError("接口 502")
            return pd.DataFrame({"证券代码": components.get(symbol, [])})

        attrs = dict(index_component_sw=component_sw)
        if parents is not None:
            attrs["sw_index_second_info"] = lambda: parents
        return _fake_ak(monkeypatch, **attrs)

    @pytest.fixture(autouse=True)
    def _stub_listing(self, monkeypatch):
        monkeypatch.setattr(stock_industry, "fetch_sw_industry_mapping", self._listing)

    def test_mapping_backfills_l1_from_l2_parent(self, monkeypatch):
        """只出现在二级清单里的股票，要按父子关系反推一级 —— 否则 sw_l1_* 恒为空"""
        self._ak(
            monkeypatch,
            components={
                "801010": ["600000", "000001"],   # 一级成分（裸码）
                "801080": ["601398"],
                "801016": ["600000", "300750"],   # 300750 只在二级里
            },
            parents=pd.DataFrame({"行业代码": ["801016"], "上级行业": ["农林牧渔"]}),
        )

        frame = stock_industry.fetch_stock_industry_mapping(max_workers=2)

        assert list(frame.columns) == list(STOCK_INDUSTRY_MAPPING_COLUMNS)
        by_code = {r.ts_code: r for r in frame.itertuples()}
        # 必须带交易所后缀：裸 6 位码 join 其他表恒为 0 行，还「看起来有数据」
        assert set(by_code) == {"600000.SH", "000001.SZ", "601398.SH", "300750.SZ"}
        assert by_code["600000.SH"].sw_l1_code == "801010"
        assert by_code["600000.SH"].sw_l2_code == "801016"
        assert by_code["300750.SZ"].sw_l1_code == "801010"
        assert by_code["300750.SZ"].sw_l1_name == "农林牧渔"
        assert by_code["601398.SH"].sw_l2_code == ""

    def test_single_index_failure_does_not_discard_table(self, monkeypatch):
        self._ak(
            monkeypatch,
            components={"801010": ["600000"], "801016": ["300750"]},
            parents=pd.DataFrame({"行业代码": ["801016"], "上级行业": ["农林牧渔"]}),
            fail_for=("801080",),
        )

        frame = stock_industry.fetch_stock_industry_mapping(max_workers=2)

        assert not frame.empty
        assert {"600000.SH", "300750.SZ"} == set(frame["ts_code"])

    def test_no_listing_returns_contract(self, monkeypatch):
        monkeypatch.setattr(
            stock_industry, "fetch_sw_industry_mapping",
            lambda: pd.DataFrame(columns=list(SW_INDUSTRY_MAPPING_COLUMNS)),
        )
        frame = stock_industry.fetch_stock_industry_mapping()
        assert frame.empty
        assert list(frame.columns) == list(STOCK_INDUSTRY_MAPPING_COLUMNS)

    def test_no_components_at_all_returns_contract(self, monkeypatch):
        self._ak(monkeypatch, components={}, parents=None)
        frame = stock_industry.fetch_stock_industry_mapping(max_workers=1)
        assert frame.empty
        assert list(frame.columns) == list(STOCK_INDUSTRY_MAPPING_COLUMNS)

    @pytest.mark.parametrize("raw,expected", [
        ("600000", "600000.SH"),
        ("000001", "000001.SZ"),
        ("1", "000001.SZ"),
        ("600000.SH", "600000.SH"),
        ("", ""),
        ("nan", ""),
        ("None", ""),
        ("--", "--"),
    ])
    def test_clean_stock_code(self, raw, expected):
        assert stock_industry._clean_stock_code(raw) == expected

    def test_plain_code_strips_suffix(self):
        assert stock_industry._plain_code("801016.SI") == "801016"
        assert stock_industry._plain_code("801016") == "801016"
        assert stock_industry._plain_code(None) == ""

    def test_parent_names_failure_returns_empty(self, monkeypatch):
        def boom():
            raise RuntimeError("网络抖动")
        ak = _fake_ak(monkeypatch, sw_index_second_info=boom)
        assert stock_industry._sw_l2_parent_names(ak) == {}

    def test_parent_names_missing_columns_returns_empty(self, monkeypatch):
        ak = _fake_ak(monkeypatch,
                      sw_index_second_info=lambda: pd.DataFrame({"行业代码": ["801016"]}))
        assert stock_industry._sw_l2_parent_names(ak) == {}

    def test_fetch_components_without_interface_returns_none(self, monkeypatch):
        ak = _fake_ak(monkeypatch)
        assert stock_industry._fetch_components(ak, (1, "801010", "农林牧渔")) is None

    def test_fetch_components_bad_payload_returns_none(self, monkeypatch):
        ak = _fake_ak(monkeypatch, index_component_sw=lambda symbol: pd.DataFrame({"其它列": [1]}))
        assert stock_industry._fetch_components(ak, (1, "801010", "农林牧渔")) is None


# ===========================================================================
# 6. datasource/fund_holding.py：HTTP 抓取链路
# ===========================================================================
_HOLDING_HTML = (
    "<table class='w782 comm tzxq'>"
    "<tr><th>序号</th><th>股票代码</th><th>股票名称</th>"
    "<th>占净值比例（%）</th><th>持股数（万股）</th><th>持仓市值（万元）</th></tr>"
    "<tr><td>1</td><td>600519</td><td>贵州茅台</td>"
    "<td>9.77</td><td>600.12</td><td>94,838.96</td></tr>"
    "</table>"
)


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class TestFundHoldingFetch:
    @pytest.fixture(autouse=True)
    def _no_delay(self, monkeypatch):
        from stocklab.datasource import fund_holding as fh
        monkeypatch.setattr(fh, "_REQUEST_DELAY", 0)
        monkeypatch.setattr(fh.time, "sleep", lambda seconds: None, raising=False)

    @staticmethod
    def _stub_http(monkeypatch, bodies, fail_times=0):
        """bodies: 依次返回的响应体；fail_times: 前 N 次抛 RequestException"""
        from stocklab.datasource import fund_holding as fh
        import requests as requests_lib

        calls = {"n": 0}

        def get(url, **kwargs):
            index = calls["n"]
            calls["n"] += 1
            if index < fail_times:
                raise requests_lib.RequestException("连接被重置")
            body = bodies[min(index - fail_times, len(bodies) - 1)]
            if isinstance(body, Exception):
                raise body
            return _Resp(body)

        monkeypatch.setattr(fh.requests, "get", get)
        return calls

    @staticmethod
    def _payload(html):
        return 'var apidata={ content:"%s",arryear:[2026],curyear:2026};' % html

    def test_fetch_single_period_builds_contract_frame(self, monkeypatch):
        self._stub_http(monkeypatch, [self._payload(_HOLDING_HTML)])

        frame = fetch_fund_holding_by_report_date("000001.OF", "2026-06-30")

        assert list(frame.columns) == list(FUND_HOLDING_COLUMNS)
        row = frame.iloc[0]
        assert row["fund_code"] == "000001.OF"
        assert row["report_date"] == date(2026, 6, 30)
        assert row["report_type"] == "semi_annual"   # 6 月末推断为半年报
        assert row["stock_code"] == "600519.SH"
        assert row["weight"] == pytest.approx(0.0977)      # 百分比 -> 小数
        assert row["market_value"] == pytest.approx(94838.96 * 10000)  # 万元 -> 元

    def test_fetch_single_period_quarterly_type(self, monkeypatch):
        self._stub_http(monkeypatch, [self._payload(_HOLDING_HTML)])
        frame = fetch_fund_holding_by_report_date("000001.OF", "2026-03-31")
        assert frame.iloc[0]["report_type"] == "quarterly"

    def test_fetch_single_period_no_data_returns_empty(self, monkeypatch):
        self._stub_http(monkeypatch, ["var apidata={ arryear:[2026] };"])
        frame = fetch_fund_holding_by_report_date("000001.OF", "2026-06-30")
        assert frame.empty
        assert list(frame.columns) == list(FUND_HOLDING_COLUMNS)

    def test_fetch_retries_on_network_error(self, monkeypatch):
        calls = self._stub_http(monkeypatch, [self._payload(_HOLDING_HTML)], fail_times=1)

        frame = fetch_fund_holding_by_report_date("000001.OF", "2026-06-30")

        assert not frame.empty
        assert calls["n"] == 2, "第一次网络失败后应重试"

    def test_fetch_gives_up_after_max_retries(self, monkeypatch):
        import requests as requests_lib
        from stocklab.datasource import fund_holding as fh
        calls = self._stub_http(
            monkeypatch, [requests_lib.RequestException("永远失败")], fail_times=0
        )

        frame = fh._fetch_eastmoney_holding_page("000001.OF", date(2026, 6, 30),
                                                 max_retries=2)

        assert frame == []
        assert calls["n"] == 2, "重试耗尽即停，不能无限重试"

    def test_fetch_unparsable_response_returns_empty(self, monkeypatch):
        self._stub_http(monkeypatch, ["<html>502</html>"])
        frame = fetch_fund_holding_by_report_date("000001.OF", "2026-06-30")
        assert frame.empty

    def test_fetch_history_aggregates_periods(self, monkeypatch):
        self._stub_http(monkeypatch, [self._payload(_HOLDING_HTML)])

        frame = fetch_fund_holding_history("000001.OF", years=1)

        assert not frame.empty
        assert frame["fund_code"].eq("000001.OF").all()
        # 跨报告期去重键是 (fund, report_date, stock_code)：同一只股在不同期各留一行
        assert frame.duplicated(subset=["report_date", "stock_code"]).sum() == 0
        assert frame["report_date"].is_monotonic_increasing

    def test_fetch_history_all_periods_empty(self, monkeypatch):
        self._stub_http(monkeypatch, ["var apidata={ arryear:[2026] };"])
        frame = fetch_fund_holding_history("000001.OF", years=1)
        assert frame.empty
        assert list(frame.columns) == list(FUND_HOLDING_COLUMNS)

    def test_fetch_latest_stops_at_first_hit(self, monkeypatch):
        calls = self._stub_http(monkeypatch, [self._payload(_HOLDING_HTML)])

        frame = fetch_latest_fund_holding("000001.OF", years=1)

        assert not frame.empty
        assert calls["n"] == 1, "命中最近一期就该停，不能把四个报告期都抓一遍"

    def test_fetch_latest_all_miss(self, monkeypatch):
        self._stub_http(monkeypatch, ["var apidata={ arryear:[2026] };"])
        frame = fetch_latest_fund_holding("000001.OF", years=1)
        assert frame.empty
        assert list(frame.columns) == list(FUND_HOLDING_COLUMNS)
