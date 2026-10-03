#!/usr/bin/env python3
"""
==============================================================================
StockLab - 通达信数据源测试 (tests/test_tdx_source.py)
==============================================================================

【功能用途】
  覆盖免费数据源扩展需求 §43 的 TDX 通道断言：
    1. 实时行情（价格 / 量额换算、成交量 手 -> 股）
    2. 月线（category=6，升序排列）
    3. 标准 DataFrame（trade_date / close_price 列契约）
    4. 连接失败 / 空结果 / 服务端代码重定向垃圾数据
    5. 股票代码与市场编号转换（沪 / 深 / 北交所）
    6. 复权类型拒绝（本通道无复权因子能力，禁止返回错误口径）

【运行方式】
  ./venv/bin/python tests/test_tdx_source.py
  （纯离线：注入 fake client，不连接任何真实 TDX 服务器）
==============================================================================
"""

import os
import sys


# 允许直接执行本文件（./venv/bin/python tests/xxx.py）；走 pytest 时由
# pytest.ini 的 `pythonpath = .` 统一负责，不会重复插入。
if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from stocklab.datasource._sources.tdx_source import (
    TdxDataSource,
    _market_of,
    _pure_code,
)

class FakeTdxClient:
    """可编程的 TDX 客户端桩：记录调用，可注入连接异常与返回数据"""

    def __init__(self, bars=None, quotes=None, connect_error=None):
        self.bars_payload = bars if bars is not None else []
        self.quotes_payload = quotes if quotes else []
        self.connect_error = connect_error
        self.calls = []
        self.closed = False

    def connect(self):
        self.calls.append("connect")
        if self.connect_error is not None:
            raise self.connect_error

    def bars(self, category, code, start=0, count=100, market=None):
        self.calls.append(("bars", category, code, start, count, market))
        return self.bars_payload

    def quotes(self, items):
        self.calls.append(("quotes", items))
        return self.quotes_payload

    def close(self):
        self.closed = True
        self.calls.append("close")

def _make_source(client):
    """用固定桩构造 TdxDataSource（每次取数新建一个同款桩，方便断言调用）"""
    created = []

    def factory():
        created.append(client)
        return client

    return TdxDataSource(client_factory=factory), created

def test_market_and_code():
    """测试市场编号与纯代码提取（沪 / 深 / 北交所 / 非法输入）"""

    assert _market_of("600519.SH") == 1
    assert _market_of("688981.SH") == 1
    assert _market_of("000001.SZ") == 0
    assert _market_of("300750.SZ") == 0
    assert _market_of("920819.BJ") == 2
    assert _market_of("832000") == 2
    assert _market_of("430047") == 2
    assert _market_of("600519") == 1
    assert _market_of("BAD") is None

    assert _pure_code("600519.SH") == "600519"
    assert _pure_code("sz000001") == "000001"
    assert _pure_code("000001.SZ") == "000001"
    print("  -> 沪/深/北交所市场编号与纯代码提取全部正确")

def test_monthly():
    """测试月线：category=6、升序、列契约、市场编号透传"""

    bars = [
        {"datetime": "2026-08-31 00:00", "close": 1299.52},
        {"datetime": "2026-06-30 00:00", "close": 1185.49},
        {"datetime": "2026-07-31 00:00", "close": 1250.10},
    ]
    client = FakeTdxClient(bars=bars)
    source, created = _make_source(client)

    frame = source.fetch_monthly_close_prices("600519.SH", "")
    assert len(frame) == 3, frame
    assert list(frame.columns) == ["trade_date", "close_price"], list(frame.columns)
    # 服务端返回乱序，必须按日期升序
    assert [round(value, 2) for value in frame["close_price"]] == [1185.49, 1250.10, 1299.52]
    assert str(frame["trade_date"].iloc[0])[:10] == "2026-06-30"

    # 调用参数：category 必须是实测的月线编号 6，市场编号 1（沪市）
    bar_call = [call for call in client.calls if isinstance(call, tuple) and call[0] == "bars"][0]
    assert bar_call[1] == 6, bar_call
    assert bar_call[2] == "600519"
    assert bar_call[5] == 1, bar_call
    assert client.closed, "取数完成后必须关闭连接（短连接策略）"
    assert created and created[0] is client
    print("  -> 月线 category=6 / 市场编号 / 升序 / 关闭连接 全部正确")

    # 北交所走 market 2
    client_bj = FakeTdxClient(bars=bars)
    source_bj, _ = _make_source(client_bj)
    source_bj.fetch_monthly_close_prices("920819.BJ", "")
    bj_call = [call for call in client_bj.calls if isinstance(call, tuple) and call[0] == "bars"][0]
    assert bj_call[5] == 2, bj_call
    print("  -> 北交所（920819.BJ）使用 market=2")

def test_realtime():
    """测试实时行情：价格、涨跌、量额（手 -> 股）、代码一致性校验"""

    client = FakeTdxClient(quotes=[{
        "market": 1, "code": "600519",
        "price": 1258.62, "last_close": 1235.58, "open": 1239.53,
        "high": 1268.00, "low": 1236.05,
        "vol": 38330, "cur_vol": 644, "amount": 4797246464.0,
    }])
    source, _ = _make_source(client)

    quote = source.fetch_realtime_quote("600519.SH")
    assert quote is not None, "实时行情解析失败"
    assert quote.current_price == 1258.62
    assert quote.yesterday_close == 1235.58
    assert quote.today_open == 1239.53
    assert quote.highest_price == 1268.00
    assert quote.lowest_price == 1236.05
    # vol 单位为手，必须换算为股（38330 手 = 3833000 股，实测新浪为 3833098 股）
    assert quote.volume_shares == 3833000, quote.volume_shares
    assert quote.amount_yuan == 4797246464.0
    assert quote.change_amount > 0 and quote.change_percent > 0
    assert quote.source_name == "tdx(通达信)"
    # 协议不返回名称与估值字段：诚实用默认值
    assert quote.stock_name == ""
    assert quote.pe_ttm is None
    assert client.closed
    print("  -> 现价/昨收/开高低/量额/涨跌全部正确: %s" % quote)

def test_failure():
    """测试失败路径：连接失败 / 空结果 / 代码重定向 / 价格非法 / 非法输入"""

    # 1) 连接异常 -> None / 空表，且不抛出
    class BoomClient(FakeTdxClient):
        def connect(self):
            raise OSError("connection refused")

    source, _ = _make_source(BoomClient())
    assert source.fetch_realtime_quote("600519.SH") is None
    assert source.fetch_monthly_close_prices("600519.SH", "").empty

    # 2) 空结果
    source, _ = _make_source(FakeTdxClient(bars=[], quotes=[]))
    assert source.fetch_realtime_quote("600519.SH") is None
    assert source.fetch_monthly_close_prices("600519.SH", "").empty

    # 3) 服务端对无效代码返回「重定向到其他代码」的垃圾数据 -> 拒绝
    source, _ = _make_source(FakeTdxClient(quotes=[{
        "market": 1, "code": "600839", "price": 0.0, "last_close": 0.0,
        "open": 0.0, "high": 0.0, "low": 0.0, "vol": 0, "amount": 0.0,
    }]))
    assert source.fetch_realtime_quote("832000.BJ") is None

    # 4) 代码匹配但价格为 0（停牌/无效）-> None
    source, _ = _make_source(FakeTdxClient(quotes=[{
        "market": 1, "code": "600519", "price": 0.0, "last_close": 0.0,
        "open": 0.0, "high": 0.0, "low": 0.0, "vol": 0, "amount": 0.0,
    }]))
    assert source.fetch_realtime_quote("600519.SH") is None

    # 5) 非法代码 -> 不建立连接，直接失败
    client = FakeTdxClient(bars=[{"datetime": "2026-08-31 00:00", "close": 1.0}])
    source, _ = _make_source(client)
    assert source.fetch_realtime_quote("NOT-A-CODE") is None
    assert source.fetch_monthly_close_prices("NOT-A-CODE", "").empty
    assert "connect" not in client.calls, "非法代码不应发起连接"

    # 6) 复权请求 -> 拒绝（无复权因子能力，禁止返回不复权价格冒充）
    client = FakeTdxClient(bars=[{"datetime": "2026-08-31 00:00", "close": 1.0}])
    source, _ = _make_source(client)
    for adjust_type in ("qfq", "hfq"):
        assert source.fetch_monthly_close_prices("600519.SH", adjust_type).empty
    assert "connect" not in client.calls, "复权请求不应发起连接"
    print("  -> 6 组失败场景全部安全降级，且不建立无谓连接")

    # 7) 协议不提供证券简称
    source, _ = _make_source(FakeTdxClient())
    assert source.fetch_stock_name("600519.SH") == ""
    print("  -> fetch_stock_name 返回空串（通达信协议无名称字段）")


if __name__ == "__main__":
    # 保住旧的直接执行入口：委托给 pytest，退出码语义一致
    raise SystemExit(pytest.main([__file__, "-v"]))
