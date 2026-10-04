"""
取数门面的优先级路由与 cache-aside (tests/test_facade_routing.py)

覆盖 stocklab/facade/market_data.py —— 全项目最关键的一层：
「本地库 vs 远端接口」按优先级决定查询顺序、未命中自动回退、取到远端数据后回写本地。

【为什么这一层最值得测】
  它错了不会报错，只会「安静地给出另一批数据」：
    · local_first 本地没命中却没回退远端 -> 分析基于空表出结论
    · remote_first 远端失败却没回退本地 -> 本来能算的变成算不了
    · 回写漏了 -> 每次都重新走网络，慢且容易在源抖动时整体失败
  三个方向都测；不测就只能靠「跑一遍看看」。

运行：
  ./venv/bin/python -m pytest tests/test_facade_routing.py -v
  （fake 远端服务 + 临时库，不联网）
"""

import datetime
import os
import sys

import pandas as pd
import pytest

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.facade import market_data as facade_module
from stocklab.facade.market_data import MarketDataFacade
from stocklab.domain import (
    DAILY_PRICE_COLUMNS,
    DAILY_VALUATION_COLUMNS,
    SECURITY_COLUMNS,
    VALUATION_HISTORY_COLUMNS,
)


def _row(columns, values):
    row = {name: None for name in columns}
    row.update(values)
    return row


def _frame(columns, *rows):
    return pd.DataFrame(list(rows), columns=list(columns))


def _securities_frame():
    return _frame(
        SECURITY_COLUMNS,
        _row(SECURITY_COLUMNS, {"ts_code": "600519.SH", "symbol": "600519",
                                "name": "贵州茅台", "exchange": "SH",
                                "market": "主板", "status": "LISTED",
                                "is_hs": False}))


def _prices_frame(ts_code="600519.SH"):
    return _frame(
        DAILY_PRICE_COLUMNS,
        _row(DAILY_PRICE_COLUMNS,
             {"ts_code": ts_code, "trade_date": datetime.date(2026, 9, 29),
              "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
              "pre_close": 10.0, "change": 0.5, "pct_chg": 0.05,
              "volume": 1000.0, "amount": 10500.0}))


def _valuations_frame(day=datetime.date(2026, 9, 30)):
    return _frame(
        DAILY_VALUATION_COLUMNS,
        _row(DAILY_VALUATION_COLUMNS,
             {"ts_code": "600519.SH", "trade_date": day,
              "pe_ttm": 28.0, "pb": 10.0, "ps": 13.0}))


def _history_frame(ts_code="600519.SH"):
    return _frame(
        VALUATION_HISTORY_COLUMNS,
        _row(VALUATION_HISTORY_COLUMNS,
             {"ts_code": ts_code, "trade_date": datetime.date(2025, 1, 2),
              "pe_ttm": 28.0, "pb": 10.0, "ps": 13.0, "pe_static": 30.0, "pcf": 20.0}))


class FakeMarketService:
    """可编排的远端行情服务：逐方法配置返回值或异常"""

    def __init__(self, **behaviour):
        self.behaviour = behaviour
        self.calls = []

    def _answer(self, name, default):
        value = self.behaviour.get(name, default)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value

    def fetch_securities(self):
        self.calls.append("securities")
        return self._answer("securities", _securities_frame())

    def fetch_daily_prices(self, ts_code, start_date, end_date):
        self.calls.append(("prices", ts_code, start_date, end_date))
        return self._answer("prices", _prices_frame(ts_code))

    def fetch_realtime_valuations(self):
        self.calls.append("valuations")
        return self._answer("valuations", _valuations_frame())

    def fetch_valuation_history(self, ts_code, period="全部"):
        self.calls.append(("history", ts_code, period))
        return self._answer("history", _history_frame(ts_code))


class FakeTencentClient:
    def __init__(self):
        self.calls = []

    def fetch_multi_monthly_close(self, targets, num_months=120):
        self.calls.append((tuple(targets), num_months))
        return {code: {"2026-09": 10.0} for code in targets}


@pytest.fixture
def fake_service():
    return FakeMarketService()


@pytest.fixture
def facade(tmp_path, fake_service, monkeypatch):
    """装配一个远端已被替换、库指向临时目录的门面"""
    fake_tencent = FakeTencentClient()
    monkeypatch.setattr(facade_module, "MarketService",
                        lambda *a, **k: fake_service)
    monkeypatch.setattr(facade_module, "TencentMarketClient",
                        lambda *a, **k: fake_tencent)
    db_path = str(tmp_path / "facade.duckdb")
    instance = MarketDataFacade(priority="local_first", db_path=db_path)
    instance.fake_service = fake_service
    instance.fake_tencent = fake_tencent
    yield instance
    instance.close()


# ---------------------------------------------------------------------------
# 一、优先级解析
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["local_first", "remote_first"])
def test_priority_accepted(tmp_path, monkeypatch, value):
    monkeypatch.setattr(facade_module, "MarketService", lambda *a, **k: FakeMarketService())
    monkeypatch.setattr(facade_module, "TencentMarketClient", lambda *a, **k: FakeTencentClient())
    instance = MarketDataFacade(priority=value, db_path=str(tmp_path / "p.duckdb"))
    assert instance.priority == value
    instance.close()


def test_priority_invalid_falls_back_to_default(tmp_path, monkeypatch):
    """非法取值必须退回默认并继续工作，不能抛异常挡住启动"""
    monkeypatch.setattr(facade_module, "MarketService", lambda *a, **k: FakeMarketService())
    monkeypatch.setattr(facade_module, "TencentMarketClient", lambda *a, **k: FakeTencentClient())
    instance = MarketDataFacade(priority="nope", db_path=str(tmp_path / "p.duckdb"))
    assert instance.priority in ("local_first", "remote_first")
    instance.close()


def test_priority_none_reads_config(tmp_path, monkeypatch):
    monkeypatch.setattr(facade_module, "MarketService", lambda *a, **k: FakeMarketService())
    monkeypatch.setattr(facade_module, "TencentMarketClient", lambda *a, **k: FakeTencentClient())
    monkeypatch.setattr(facade_module, "load_data_source_priority",
                        lambda: "remote_first")
    instance = MarketDataFacade(priority=None, db_path=str(tmp_path / "p.duckdb"))
    assert instance.priority == "remote_first"
    instance.close()


def test_context_manager_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(facade_module, "MarketService", lambda *a, **k: FakeMarketService())
    monkeypatch.setattr(facade_module, "TencentMarketClient", lambda *a, **k: FakeTencentClient())
    with MarketDataFacade(priority="local_first",
                          db_path=str(tmp_path / "p.duckdb")) as instance:
        assert instance.priority == "local_first"


# ---------------------------------------------------------------------------
# 二、证券基础信息：两个方向
# ---------------------------------------------------------------------------
def test_local_first_securities_hits_local(facade):
    """先落库再取：第二次应命中本地，完全不碰远端"""
    facade.fetch_securities()
    facade.fake_service.calls.clear()
    again = facade.fetch_securities()
    assert facade.fake_service.calls == [], "命中本地时不得再请求远端"
    assert len(again) == 1


def test_local_first_securities_falls_back_to_remote(facade):
    """本地空 -> 走远端，并顺手回写本地（cache-aside）"""
    assert facade.fake_service.calls == []          # 构造时不应已取过
    data = facade.fetch_securities()
    assert facade.fake_service.calls == ["securities"]
    assert len(data) == 1
    # 回写生效：直接查库应已有数据
    stored = facade._security_repo.find_all()
    assert len(stored) == 1, "远端取到的数据必须回写本地"


def test_remote_first_securities_hits_remote(facade):
    """remote_first：即使本地有数据也优先远端，并刷新本地"""
    facade.fetch_securities()                        # 先把本地灌上
    facade._priority = "remote_first"
    facade.fake_service.calls.clear()
    facade.fetch_securities()
    assert facade.fake_service.calls == ["securities"], "remote_first 必须先问远端"


def test_remote_first_securities_falls_back_to_local(facade):
    """远端失败 -> 回退本地，而不是把空表交给上层"""
    facade.fetch_securities()                        # 本地有数据
    facade._priority = "remote_first"
    facade.fake_service.behaviour["securities"] = pd.DataFrame()
    data = facade.fetch_securities()
    assert len(data) == 1, "远端空时必须回退本地"


def test_securities_all_empty_returns_empty(facade):
    """本地与远端都空时返回空表，而不是抛异常"""
    facade.fake_service.behaviour["securities"] = pd.DataFrame()
    assert facade.fetch_securities().empty


def test_write_back_skips_empty(facade):
    """空数据不回写（否则会在库里插 0 行并打误导日志）"""
    facade._write_back(facade._security_repo, pd.DataFrame(), "securities")
    assert facade._security_repo.find_all().empty


def test_write_back_tolerates_none(facade):
    facade._write_back(facade._security_repo, None, "securities")


# ---------------------------------------------------------------------------
# 三、日 K：两个方向
# ---------------------------------------------------------------------------
def test_local_first_prices_hits_local(facade):
    facade.fetch_daily_prices("600519.SH", "2026-09-01", "2026-09-30")
    facade.fake_service.calls.clear()
    facade.fetch_daily_prices("600519.SH", "2026-09-01", "2026-09-30")
    assert facade.fake_service.calls == [], "命中本地时不得再请求远端"


def test_local_first_prices_falls_back_to_remote(facade):
    data = facade.fetch_daily_prices("600519.SH", "2026-09-01", "2026-09-30")
    assert len(data) == 1
    assert facade._price_repo.find_by_code("600519.SH") is not None, "应已回写"


def test_remote_first_prices_hits_remote(facade):
    facade.fetch_daily_prices("600519.SH", "2026-09-01", "2026-09-30")
    facade._priority = "remote_first"
    facade.fake_service.calls.clear()
    facade.fetch_daily_prices("600519.SH", "2026-09-01", "2026-09-30")
    assert facade.fake_service.calls, "remote_first 必须先问远端"


def test_remote_first_prices_falls_back_to_local(facade):
    facade.fetch_daily_prices("600519.SH", "2026-09-01", "2026-09-30")
    facade._priority = "remote_first"
    facade.fake_service.behaviour["prices"] = pd.DataFrame()
    data = facade.fetch_daily_prices("600519.SH", "2026-09-01", "2026-09-30")
    assert len(data) == 1, "远端空时必须回退本地"


def test_prices_dates_are_compacted_for_remote(facade):
    """远端接口要 YYYYMMDD，门面负责压缩 —— 传错格式源站会静默返回空"""
    facade.fetch_daily_prices("600519.SH", "2026-09-01", "2026-09-30")
    call = [c for c in facade.fake_service.calls if c[0] == "prices"][0]
    assert call[2] == "20260901", "起始日应压缩为 YYYYMMDD"
    assert call[3] == "20260930", "结束日应压缩为 YYYYMMDD"


def test_compact_date_static():
    assert MarketDataFacade._compact_date("2026-09-30") == "20260930"
    assert MarketDataFacade._compact_date("20260930") == "20260930"


# ---------------------------------------------------------------------------
# 四、估值快照
# ---------------------------------------------------------------------------
def test_local_first_valuations_falls_back_and_writes_back(facade):
    data = facade.fetch_valuations("2026-09-30")
    assert len(data) == 1
    assert facade.fake_service.calls == ["valuations"]
    again = facade.fetch_valuations("2026-09-30")
    assert again is not None and not again.empty, "回写后应可从本地读到"


def test_local_first_valuations_hits_local(facade):
    facade.fetch_valuations("2026-09-30")
    facade.fake_service.calls.clear()
    facade.fetch_valuations("2026-09-30")
    assert facade.fake_service.calls == [], "命中本地时不得再请求远端"


def test_remote_first_valuations_falls_back_to_local(facade):
    facade.fetch_valuations("2026-09-30")
    facade._priority = "remote_first"
    facade.fake_service.behaviour["valuations"] = pd.DataFrame()
    data = facade.fetch_valuations("2026-09-30")
    assert data is not None and not data.empty, "远端空时必须回退本地"


def test_valuations_without_trade_date_uses_latest_local_day(facade):
    """不指定日期时取本地最新交易日；本地无日 K 则无法定位，返回空"""
    facade.fetch_daily_prices("600519.SH", "2026-09-01", "2026-09-30")
    facade.fake_service.calls.clear()
    facade.fetch_valuations()
    # 本地没有估值数据 -> 仍走远端；重点是不要抛异常
    assert facade.fake_service.calls == ["valuations"]


def test_read_local_valuations_without_prices_returns_empty(facade):
    """本地连日 K 都没有时无从得知最新交易日，应返回空表"""
    assert facade._read_local_valuations(None).empty


# ---------------------------------------------------------------------------
# 五、历史估值序列
# ---------------------------------------------------------------------------
def test_local_first_history_falls_back_and_writes_back(facade):
    data = facade.fetch_valuation_history("600519.SH")
    assert len(data) == 1
    call = [c for c in facade.fake_service.calls if c[0] == "history"][0]
    assert call[2] == "全部", "period 应透传给远端"


def test_remote_first_history_falls_back_to_local(facade):
    facade.fetch_valuation_history("600519.SH")
    facade._priority = "remote_first"
    facade.fake_service.behaviour["history"] = pd.DataFrame()
    data = facade.fetch_valuation_history("600519.SH")
    assert len(data) == 1, "远端空时必须回退本地"


# ---------------------------------------------------------------------------
# 六、批量月线：绕过优先级（明确只走远端）
# ---------------------------------------------------------------------------
def test_multi_monthly_close_uses_tencent_directly(facade):
    """批量月线只走腾讯直连，不查本地库 —— 文档里已写明这一差异"""
    result = facade.fetch_multi_monthly_close(["600519.SH", "sh000001"], num_months=12)
    assert set(result) == {"600519.SH", "sh000001"}
    assert facade.fake_service.calls == [], "不应经过 MarketService"
    assert facade.fake_tencent.calls == [(("600519.SH", "sh000001"), 12)]


def test_multi_monthly_close_forwards_targets_untouched(facade):
    """门面原样透传 targets：`.code` 归一化是 TencentMarketClient 的职责

    门面文档里已写明「与 datasource 层的区别」，所以这里只断言「不加工」——
    归一化行为由 test_data_interfaces 里的客户端用例覆盖。
    """
    class Target:
        def __init__(self, code):
            self.code = code

    target = Target("600519.SH")
    facade.fetch_multi_monthly_close([target], num_months=3)
    assert facade.fake_tencent.calls[0][0] == (target,), "门面不应改写 targets"


# ---------------------------------------------------------------------------
# 七、远端抛异常时的行为
# ---------------------------------------------------------------------------
def test_remote_exception_propagates_by_design(facade):
    """【契约】门面不捕获远端异常 —— 吞异常是数据源层的职责

    「五级降级」的失败处理发生在各 source 内部：某个通道抓不到就返回空表，
    由上一级通道接手。门面这一层只负责「按优先级选本地还是远端」，
    因此远端真的抛出（例如配置错误、代码 bug）时向上传播才是正确行为 ——
    静默吞掉只会让整条流水线拿着空表继续跑，算出一份「看起来正常」的错结论。

    反过来，source 层返回空表时门面必须回退，见前面几个用例。
    """
    facade.fake_service.behaviour["securities"] = RuntimeError("网络中断")
    with pytest.raises(RuntimeError, match="网络中断"):
        facade.fetch_securities()