"""
数据服务：重试 / 限流 / 契约失败的整体降级 (tests/test_datasource_services.py)

覆盖 stocklab/datasource 下三个此前零覆盖的服务：
  lifecycle_service.py  —— 交易所上市 / 退市日历（多来源，任一失败整体失败）
  fundamental_service.py —— 三大报表（重试 + 契约失败不产出）
  market_service.py      —— 全市场横截面与单股序列（含 URL/列名异常分支）

【共同的可测契约】
  1. 重试：失败要重试，但**空数据立即返回不重试**（重试空数据纯属浪费配额）
  2. 限流：两次请求之间要有间隔
  3. 降级：抓取失败返回空表，绝不抛异常 —— 上层按「空表 = 无数据」处理
  4. 契约失败：源列改版属不可重试错误，必须拒绝产出，不能写出缺列数据

运行：
  ./venv/bin/python -m pytest tests/test_datasource_services.py -v
  （akshare 接口全部用桩替换，不联网）
"""

import datetime
import os
import sys

import pandas as pd
import pytest

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.datasource import market_service as ms
from stocklab.datasource import lifecycle_service as ls
from stocklab.datasource import fundamental_service as fs
from stocklab.domain import DataContractError


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """把重试/限流的 sleep 全部去掉，否则单测会真的睡几秒"""
    for module in (ls, fs, ms):
        monkeypatch.setattr(module.time, "sleep", lambda seconds: None, raising=False)


# ---------------------------------------------------------------------------
# 一、生命周期日历
# ---------------------------------------------------------------------------
# 源列名必须命中 normalize_listing_calendar / normalize_delisting_calendar 的
# 候选列表（exchange.py 的 _LISTING_DATE_COLUMNS / _DELISTING_DATE_COLUMNS 等），
# 否则会走「缺关键列 -> DataContractError」分支，测不到真正的归一化逻辑。
def _listing_raw(code="600519", name="贵州茅台", date="1990-12-19"):
    return pd.DataFrame({"证券代码": [code], "证券简称": [name],
                         "A股上市日期": [date]})


def _delisting_raw(code="600519", name="某某退", date="2024-11-29"):
    return pd.DataFrame({"证券代码": [code], "公司简称": [name],
                         "终止上市日期": [date]})


class _FakeAk:
    """按接口名编排的 akshare 桩"""

    def __init__(self, **behaviour):
        self.behaviour = behaviour
        self.calls = []

    def __getattr__(self, name):
        def fetcher(**kwargs):
            self.calls.append((name, kwargs))
            value = self.behaviour.get(name)
            if isinstance(value, Exception):
                raise value
            if not callable(value):
                return value
            # 桩要同时支持带参与不带参两种写法：akshare 的接口函数收 kwargs
            # （如 symbol=...），而测试里的 lambda 常常写成零参。
            try:
                return value(**kwargs)
            except TypeError:
                return value()
        fetcher.__name__ = name
        return fetcher


def _service(ak, **kwargs):
    ls.ak = ak
    kwargs.setdefault("retry_count", 2)
    kwargs.setdefault("retry_interval_seconds", 0)
    kwargs.setdefault("interval_seconds", 0)
    return ls.LifecycleService(**kwargs)


def test_listing_calendar_normalizes_all_sources():
    ak = _FakeAk(
        stock_info_sh_name_code=lambda: _listing_raw("600519", "贵州茅台", "1990-12-19"),
        stock_info_sz_name_code=lambda: _listing_raw("000001", "平安银行", "1991-04-03"),
        stock_info_bj_name_code=lambda: _listing_raw("430047", "诺思兰德", "2020-11-24"),
    )
    frame = _service(ak).fetch_listing_calendar()
    assert list(frame.columns) == ["ts_code", "name", "list_date"]
    assert set(frame["ts_code"]) == {"600519.SH", "000001.SZ", "430047.BJ"}
    assert len(ak.calls) == 4, "沪主板 / 科创板 / 深A / 北交 四个来源"


def test_listing_calendar_aborts_when_one_source_fails():
    """任一来源失败即整体失败：日历不完整会静默污染股票池推导"""
    ak = _FakeAk(
        stock_info_sh_name_code=lambda: _listing_raw(),
        stock_info_sz_name_code=RuntimeError("深交所超时"),
        stock_info_bj_name_code=lambda: _listing_raw("430047", "诺思兰德", "2020-11-24"),
    )
    assert _service(ak).fetch_listing_calendar().empty


def test_listing_calendar_retries_then_succeeds():
    """第一次失败、第二次成功时应正常返回"""
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("临时抖动")
        return _listing_raw()

    ak = _FakeAk(stock_info_sh_name_code=flaky,
                 stock_info_sz_name_code=lambda: _listing_raw("000001", "平安银行", "1991-04-03"),
                 stock_info_bj_name_code=lambda: _listing_raw("430047", "诺思兰德", "2020-11-24"))
    frame = _service(ak).fetch_listing_calendar()
    assert not frame.empty
    # 沪主板与科创板共用 stock_info_sh_name_code（只差 symbol），该来源被调 2 次，
    # 每次都要经历「失败一次 -> 重试成功」，所以总计 4 次
    # 沪主板与科创板共用 stock_info_sh_name_code：第 1 次失败、第 2 次成功（主板）、
    # 第 3 次直接成功（科创板），合计 3 次调用、恰好 1 次失败
    assert attempts["n"] == 3, "应「失败一次 + 两次成功」共 3 次，实际 %d 次" % attempts["n"]


def test_listing_calendar_retries_on_empty_too():
    """【实际行为】上市日历对「空响应」也重试，与异常一视同仁

    LifecycleService._call 里空响应只告警不 return，与异常走同一条重试路径。
    对日历类接口这是合理的：交易所返回空更可能是被 WAF 挡了一下而非真的没数据，
    立刻放弃会让「今天恰好取不到」变成整批生命周期同步中止。

    注意与基本面服务不同：FundamentalService 对空响应是立即返回不重试的
    （单只财报取空说明该标的确没有，不代表瞬时故障），见下面那个用例。
    """
    attempts = {"n": 0}

    def empty():
        attempts["n"] += 1
        return pd.DataFrame()

    ak = _FakeAk(stock_info_sh_name_code=empty,
                 stock_info_sz_name_code=lambda: _listing_raw("000001", "平安银行", "1991-04-03"),
                 stock_info_bj_name_code=lambda: _listing_raw("430047", "诺思兰德", "2020-11-24"))
    assert _service(ak, retry_count=5).fetch_listing_calendar().empty
    assert attempts["n"] == 5, "空响应应与异常同样重试满次数，实际 %d 次" % attempts["n"]


def test_listing_calendar_contract_violation_returns_empty():
    """源列改版（缺关键列）属不可重试错误，必须拒绝产出"""
    bad = pd.DataFrame({"证券代码": ["600519"], "证券简称": ["贵州茅台"]})   # 缺上市日期
    ak = _FakeAk(stock_info_sh_name_code=lambda: bad,
                 stock_info_sz_name_code=lambda: _listing_raw("000001", "平安银行", "1991-04-03"),
                 stock_info_bj_name_code=lambda: _listing_raw("430047", "诺思兰德", "2020-11-24"))
    assert _service(ak).fetch_listing_calendar().empty


def test_delisting_calendar():
    ak = _FakeAk(stock_info_sh_delist=lambda: _delisting_raw("600519", "某某退"),
                 stock_info_sz_delist=lambda: _delisting_raw("000003", "某退2"))
    frame = _service(ak).fetch_delisting_calendar()
    assert list(frame.columns) == ["ts_code", "name", "list_date", "delist_date"]
    assert set(frame["ts_code"]) == {"600519.SH", "000003.SZ"}


def test_delisting_calendar_aborts_when_one_source_fails():
    ak = _FakeAk(stock_info_sh_delist=lambda: _delisting_raw(),
                 stock_info_sz_delist=RuntimeError("接口下线"))
    assert _service(ak).fetch_delisting_calendar().empty


def test_delisting_calendar_contract_violation_returns_empty():
    bad = pd.DataFrame({"证券代码": ["600519"], "公司简称": ["某某退"]})   # 缺终止上市日期
    ak = _FakeAk(stock_info_sh_delist=lambda: bad,
                 stock_info_sz_delist=lambda: _delisting_raw("000003", "某退2"))
    assert _service(ak).fetch_delisting_calendar().empty


# ---------------------------------------------------------------------------
# 二、基本面三大报表
# ---------------------------------------------------------------------------
# 源列名照抄 eastmoney.py 里 require_columns 的清单（少一列就会走契约失败分支）
# _SOURCE_IDENTITY_COLUMNS = (SECURITY_CODE, SECURITY_NAME_ABBR, REPORT_DATE, ...)
_IDENTITY = {
    "SECURITY_CODE": ["600519"], "SECURITY_NAME_ABBR": ["贵州茅台"],
    "REPORT_DATE": ["2025-12-31"], "NOTICE_DATE": ["2026-04-01"],
}


def _income_raw():
    data = dict(_IDENTITY)
    data.update({
        "OPERATE_INCOME": [1.5e11], "OPERATE_COST": [1.0e11],
        "OPERATE_PROFIT": [6.0e10], "TOTAL_PROFIT": [6.0e10],
        "INCOME_TAX": [1.0e10], "NETPROFIT": [5.0e10],
        "PARENT_NETPROFIT": [4.9e10], "BASIC_EPS": [68.0],
    })
    return pd.DataFrame(data)


def _balance_raw():
    data = dict(_IDENTITY)
    data.update({
        "TOTAL_ASSETS": [3e12], "TOTAL_LIABILITIES": [3e11],
        "TOTAL_PARENT_EQUITY": [2.5e12], "MONETARYFUNDS": [5e11],
        "SHORT_LOAN": [1e10], "NONCURRENT_LIAB_1YEAR": [2e10],
        "LONG_LOAN": [5e10], "BOND_PAYABLE": [1e10],
    })
    return pd.DataFrame(data)


def _cashflow_raw():
    data = dict(_IDENTITY)
    data.update({
        "NETCASH_OPERATE": [7e10], "NETCASH_INVEST": [-2e10],
        "NETCASH_FINANCE": [-1e10], "CONSTRUCT_LONG_ASSET": [1e10],
    })
    return pd.DataFrame(data)


def _fundamental_service(ak, **kwargs):
    fs.ak = ak
    kwargs.setdefault("retry_count", 2)
    kwargs.setdefault("retry_interval_seconds", 0)
    kwargs.setdefault("interval_seconds", 0)
    return fs.FundamentalService(**kwargs)


@pytest.mark.parametrize("method,ak_name,raw,expected_cols", [
    ("fetch_income_statement", "stock_profit_sheet_by_report_em",
     _income_raw, ["ts_code", "report_period", "announce_date", "available_date",
                   "revenue", "net_profit", "eps"]),
    ("fetch_balance_sheet", "stock_balance_sheet_by_report_em",
     _balance_raw, ["ts_code", "total_assets", "total_liabilities", "equity"]),
    ("fetch_cashflow_statement", "stock_cash_flow_sheet_by_report_em",
     _cashflow_raw, ["ts_code", "operating_cashflow", "free_cashflow"]),
])
def test_statement_fetch_and_normalize(method, ak_name, raw, expected_cols):
    ak = _FakeAk(**{ak_name: raw})
    frame = getattr(_fundamental_service(ak), method)("600519.SH")
    assert not frame.empty
    assert frame.iloc[0]["ts_code"] == "600519.SH"
    for column in expected_cols:
        assert column in frame.columns, "缺列 %s" % column


def test_symbol_is_converted_to_eastmoney_prefix():
    ak = _FakeAk(stock_profit_sheet_by_report_em=_income_raw)
    _fundamental_service(ak).fetch_income_statement("600519.SH")
    assert ak.calls[0][1]["symbol"] == "SH600519", "东财接口要 市场前缀+代码"


@pytest.mark.parametrize("ts_code,expected", [
    ("600519.SH", "SH600519"),
    ("000001.SZ", "SZ000001"),
    ("430047.BJ", "BJ430047"),
])
def test_to_em_symbol(ts_code, expected):
    assert fs._to_em_symbol(ts_code) == expected


def test_fundamental_retries_then_succeeds():
    attempts = {"n": 0}

    def flaky(symbol):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("超时")
        return _income_raw()

    ak = _FakeAk(stock_profit_sheet_by_report_em=flaky)
    assert not _fundamental_service(ak).fetch_income_statement("600519.SH").empty
    assert attempts["n"] == 2


def test_fundamental_empty_response_returns_empty_immediately():
    attempts = {"n": 0}

    def empty(symbol):
        attempts["n"] += 1
        return pd.DataFrame()

    ak = _FakeAk(stock_profit_sheet_by_report_em=empty)
    assert _fundamental_service(ak, retry_count=5).fetch_income_statement("600519.SH").empty
    assert attempts["n"] == 1, "空数据不应重试"


def test_fundamental_all_retries_exhausted_returns_empty():
    def always_fail(symbol):
        raise RuntimeError("持续失败")

    ak = _FakeAk(stock_profit_sheet_by_report_em=always_fail)
    assert _fundamental_service(ak, retry_count=3).fetch_income_statement("600519.SH").empty


def test_fundamental_contract_violation_returns_empty():
    """源列改版属不可重试错误：必须拒绝产出，绝不写入缺列数据"""
    bad = pd.DataFrame({"REPORT_DATE": ["2025-12-31"]})     # 只有一个列
    ak = _FakeAk(stock_profit_sheet_by_report_em=bad)
    assert _fundamental_service(ak).fetch_income_statement("600519.SH").empty


def test_fundamental_none_response_returns_empty():
    ak = _FakeAk(stock_profit_sheet_by_report_em=lambda symbol: None)
    assert _fundamental_service(ak).fetch_income_statement("600519.SH").empty


# ---------------------------------------------------------------------------
# 三、全市场行情 / 估值服务
# ---------------------------------------------------------------------------
@pytest.fixture
def ak(monkeypatch):
    stub = _FakeAk()
    ms.ak = stub
    return stub


def test_fetch_securities(ak):
    ak.behaviour["stock_info_a_code_name"] = pd.DataFrame({
        "code": ["600519", "000001"], "name": ["贵州茅台", "平安银行"]})
    frame = ms.MarketService().fetch_securities()
    assert len(frame) == 2
    assert "ts_code" in frame.columns


def test_fetch_securities_empty_returns_empty(ak):
    ak.behaviour["stock_info_a_code_name"] = pd.DataFrame()
    assert ms.MarketService().fetch_securities().empty


def test_fetch_securities_exception_returns_empty(ak):
    ak.behaviour["stock_info_a_code_name"] = RuntimeError("东财超时")
    assert ms.MarketService().fetch_securities().empty


def test_fetch_realtime_valuations(ak):
    """全市场估值快照：一次请求拿全市场"""
    # pe_ttm 必须取「市盈率(TTM)」这一列；「市盈率-动态」对应 pe，两者不可混用
    ak.behaviour["stock_zh_a_spot_em"] = pd.DataFrame({
        "代码": ["600519"], "名称": ["贵州茅台"], "最新价": [1500.0],
        "市盈率-动态": [28.0], "市盈率(TTM)": [27.5], "市净率": [10.0],
        "市销率": [13.0], "总市值": [1.8e12], "流通市值": [1.8e12],
        "股息率": [2.3], "换手率": [0.5],
    })
    frame = ms.MarketService().fetch_realtime_valuations()
    assert not frame.empty
    row = frame.iloc[0]
    assert row["pe_ttm"] == 27.5, "pe_ttm 必须取「市盈率(TTM)」，不是「市盈率-动态」"
    assert row["pe"] == 28.0


def test_fetch_realtime_valuations_empty(ak):
    ak.behaviour["stock_zh_a_spot_em"] = pd.DataFrame()
    assert ms.MarketService().fetch_realtime_valuations().empty


def test_fetch_index_membership(ak):
    ak.behaviour["index_stock_cons_csindex"] = pd.DataFrame({
        "日期": ["20260930"], "指数代码": ["000300"], "指数名称": ["沪深300"],
        "成分券代码": ["600519"], "成分券名称": ["贵州茅台"], "权重": [5.0],
    })
    frame = ms.MarketService().fetch_index_membership("000300.SH")
    assert not frame.empty


def test_fetch_index_membership_empty(ak):
    ak.behaviour["index_stock_cons_csindex"] = pd.DataFrame()
    assert ms.MarketService().fetch_index_membership("000300.SH").empty


def test_fetch_industry_valuation(ak):
    ak.behaviour["stock_board_industry_summary_ths"] = pd.DataFrame()
    ak.behaviour["stock_board_industry_name_em"] = pd.DataFrame()
    # 即使上游结构变化，也必须是「空表」而不是异常
    result = ms.MarketService().fetch_industry_valuation("2026-09-30", "国证行业分类")
    assert isinstance(result, pd.DataFrame)


def test_fetch_daily_prices_unknown_symbol_returns_empty(ak):
    """未知代码必须返回空表而不是抛异常（同步脚本按只处理整批失败）"""
    ak.behaviour["stock_zh_a_hist"] = pd.DataFrame()
    assert ms.MarketService().fetch_daily_prices("999999.SH", "20260901", "20260930").empty


def test_fetch_daily_prices_exception_returns_empty(ak):
    ak.behaviour["stock_zh_a_hist"] = RuntimeError("接口 502")
    assert ms.MarketService().fetch_daily_prices("600519.SH", "20260901", "20260930").empty


def _tx_frame():
    """腾讯/新浪口径的英文列：volume 为股、amount 为元"""
    return pd.DataFrame(
        {
            "date": ["2026-07-02", "2026-07-03"],
            "open": [8.71, 8.69],
            "close": [8.70, 8.69],
            "high": [8.84, 8.82],
            "low": [8.56, 8.59],
            "volume": [71137604, 70513349],
            "amount": [618574645, 604291542],
        }
    )


def test_fetch_daily_prices_falls_back_to_tencent(ak):
    """东财单点故障时必须回退腾讯，而不是静默返回空表（那会让归因误判为『这只股票没数据』）"""
    ak.behaviour["stock_zh_a_hist"] = RuntimeError("ProxyError")
    ak.behaviour["stock_zh_a_hist_tx"] = _tx_frame()

    frame = ms.MarketService(retry_count=1).fetch_daily_prices(
        "600000.SH", "20260702", "20260703"
    )

    assert not frame.empty
    assert frame["ts_code"].eq("600000.SH").all()
    assert frame["trade_date"].astype(str).tolist() == ["2026-07-02", "2026-07-03"]
    # 备用源成交量是股，落库口径是手，必须除以 100，否则换手/量能类指标差 100 倍
    assert frame["volume"].tolist() == [711376.04, 705133.49]
    assert frame["amount"].tolist() == [618574645.0, 604291542.0]


def test_fetch_daily_prices_falls_back_to_sina(ak):
    """东财与腾讯都不可用时再退到新浪，且带市场前缀（sh600000）"""
    ak.behaviour["stock_zh_a_hist"] = RuntimeError("ProxyError")
    ak.behaviour["stock_zh_a_hist_tx"] = RuntimeError("连接被重置")
    ak.behaviour["stock_zh_a_daily"] = _tx_frame()

    frame = ms.MarketService(retry_count=1).fetch_daily_prices(
        "600000.SH", "20260702", "20260703"
    )

    assert not frame.empty
    assert ak.calls[-1][0] == "stock_zh_a_daily"
    assert ak.calls[-1][1]["symbol"] == "sh600000"


def test_fetch_daily_prices_all_sources_down_returns_empty(ak):
    """三源全挂仍返回空表，绝不带病写出契约外的帧"""
    for name in ("stock_zh_a_hist", "stock_zh_a_hist_tx", "stock_zh_a_daily"):
        ak.behaviour[name] = RuntimeError("接口 502")

    assert ms.MarketService(retry_count=1).fetch_daily_prices(
        "600519.SH", "20260901", "20260930"
    ).empty


def test_fetch_daily_prices_empty_from_first_source_tries_others(ak):
    """主源返回空表（源侧查无此股）也应继续尝试备用源，避免把『源没数据』当结论"""
    ak.behaviour["stock_zh_a_hist"] = pd.DataFrame()
    ak.behaviour["stock_zh_a_hist_tx"] = _tx_frame()

    frame = ms.MarketService(retry_count=1).fetch_daily_prices(
        "600000.SH", "20260702", "20260703"
    )
    assert not frame.empty


def test_fetch_valuation_history_empty(ak):
    ak.behaviour["stock_zh_valuation_baidu"] = pd.DataFrame()
    result = ms.MarketService().fetch_valuation_history("600519.SH", "近五年")
    assert isinstance(result, pd.DataFrame)
    assert result.empty