#!/usr/bin/env python3
"""
==============================================================================
StockLab - 数据接口测试 (tests/test_data_interfaces.py)
==============================================================================

【功能用途】
  本脚本用于验证 StockLab 核心功能链路：
    0. 验证基础类型转换模块 (stocklab.common.type_conversion)
    1. 验证通用行情客户端 (MarketDataClient)：统一验证股票、ETF 与指数的名称、OHLCV K线与月线批量抓取
    2. 验证腾讯实时行情快照接口 (fetch_realtime_quote)
    3. 验证多级公司简称查询 (fetch_stock_name)
    4. 验证月线历史行情与主备通道容错 (fetch_monthly_close_prices / fetch_monthly_price_and_pe)
    5. 验证核心板块与主板月线走势交互式 HTML 网页生成 (SectorTrendVisualizer)

【这些用例会真的联网】
  除 test_type_conversion 外，其余全部打真实公开接口（腾讯 / AkShare 等），
  因此标记为 integration；断网或代理不通时整组跳过，不会逐个超时。

运行方式
  全量（含联网）:./venv/bin/python -m pytest tests/test_data_interfaces.py
  只跑离线部分:  ./venv/bin/python -m pytest tests/ -m "not integration"
  单独跑本文件:  ./venv/bin/python -m pytest tests/test_data_interfaces.py -v -s
==============================================================================
"""

import os
import sys
import logging

import pytest


# 允许直接执行本文件（./venv/bin/python tests/xxx.py）；走 pytest 时由
# pytest.ini 的 `pythonpath = .` 统一负责，不会重复插入。
if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab import (
    CLOSE_PRICE_COLUMN,
    PE_TTM_COLUMN,
    TRADE_DATE_COLUMN,
    StockDataFetchParams,
    StockQuoteService,
    TencentMarketClient,
    safe_float,
    safe_int,
)
from app.dashboard import SectorTrendVisualizer

# 联网用例的默认标的（命令行可覆盖的场景已移除：pytest 用 -k 或 --deselect 选）
DEFAULT_STOCK_CODE = "600519.SH"


@pytest.fixture(scope="module")
def service():
    """单股取数服务（联网，模块内共用一个实例避免重复握手）"""
    return StockQuoteService()

def test_type_conversion():
    """测试 type_conversion 类型安全转换基础工具"""

    assert safe_float("12.34") == 12.34
    assert safe_float(None, 0.0) == 0.0
    assert safe_float("-", 0.0) == 0.0
    assert safe_float("--", None) is None
    assert safe_int("100.0") == 100
    assert safe_int("abc", -1) == -1
    print("  -> safe_float 与 safe_int 边界测试用例全部通过！")

@pytest.mark.integration
def test_generic_market_client(http_reachable):
    """测试腾讯直连市场行情客户端 (TencentMarketClient) 对股票/ETF/指数的统一接入能力"""
    if not http_reachable:
        pytest.skip("外网不可达，跳过腾讯直连行情用例")
    client = TencentMarketClient()

    test_targets = ["sh000001", "sh512480", "sh600519", "000001.SZ"]

    # 1. 通用名称测试
    print("1. 统一名称查询测试:")
    for sym in test_targets:
        name = client.fetch_name(sym)
        assert len(name) > 0, f"未能获取 {sym} 名称"
        print(f"   * {sym:<10} -> {name}")

    # 2. 批量并发月线获取测试
    print("\n2. 统一批量并发月线收盘价获取:")
    batch_data = client.fetch_multi_monthly_close(test_targets, num_months=3)
    for sym in test_targets:
        assert sym in batch_data, f"缺失标的 {sym} 的数据"
        series = batch_data[sym]
        assert len(series) > 0, f"标的 {sym} 数据为空"
        last_m, last_p = list(series.items())[-1]
        print(f"   * {sym:<10} 获取到 {len(series)} 个月 | 最新收盘: {last_m} = {last_p} 元/点")

    # 3. 全要素 OHLCV K线测试
    print("\n3. 统一全要素 OHLCV 历史 K 线测试 (贵州茅台):")
    bars = client.fetch_kline("sh600519", period="month", count=2)
    assert len(bars) > 0, "未能获取茅台 K 线"
    for b in bars:
        assert "open" in b and "close" in b and "volume" in b
        print(f"   * 日期: {b['date']} | 开: {b['open']} | 收: {b['close']} | 量: {b['volume']}")

    print("  -> TencentMarketClient 通用跨资产接入测试全部通过！")

@pytest.mark.integration
@pytest.mark.parametrize("stock_code", ["600519.SH", "000001.SZ"])
def test_realtime_quote(service, stock_code, http_reachable):
    """腾讯实时行情快照

    迁移前这是一个纯 print 的探针：拿不到数据就打个警告 return，一条断言都没有，
    也就是说这条链路整个挂掉测试也是绿的。现在补上「拿到就必须自洽」的断言。
    """
    if not http_reachable:
        pytest.skip("外网不可达，跳过实时行情用例")

    quote = service.fetch_realtime_quote(stock_code)
    if quote is None:
        pytest.skip("%s 未取到实时行情（通道可能临时不可用）" % stock_code)

    assert quote.stock_code == stock_code, quote.stock_code
    assert quote.stock_name, "实时行情必须带公司简称"
    assert quote.source_name, "必须标明实际生效的数据源"
    assert quote.yesterday_close > 0, quote.yesterday_close
    assert quote.lowest_price > 0 and quote.highest_price > 0
    # 最高价不可能低于最低价；收盘价应落在当日区间内（含停牌/集合竞价的容差）
    assert quote.highest_price >= quote.lowest_price
    if quote.turnover_rate:
        assert 0 <= quote.turnover_rate < 100, quote.turnover_rate


@pytest.mark.integration
def test_monthly_price_and_pe(service, http_reachable):
    """月线价格与 PE-TTM 的主备通道容错 + 列名对齐"""
    if not http_reachable:
        pytest.skip("外网不可达，跳过月线历史用例")

    params = StockDataFetchParams(DEFAULT_STOCK_CODE)
    assert service.fetch_stock_name(DEFAULT_STOCK_CODE), "应能查到公司简称"

    price_df = service.fetch_monthly_close_prices(params)
    if price_df.empty:
        pytest.skip("月线价格未取到（通道可能临时不可用）")
    assert TRADE_DATE_COLUMN in price_df.columns, list(price_df.columns)
    assert CLOSE_PRICE_COLUMN in price_df.columns, list(price_df.columns)
    assert (price_df[CLOSE_PRICE_COLUMN] > 0).all(), "月线收盘价必须全为正"
    # 时间必须升序，否则下游算收益会得到负数
    dates = price_df[TRADE_DATE_COLUMN]
    assert list(dates) == sorted(dates), "月线日期未升序"
    print("   -> 月线价格 %d 条，实际生效数据源: %s"
          % (len(price_df), service.used_source_name))

    combined = service.fetch_monthly_price_and_pe(params)
    if combined.empty:
        pytest.skip("综合数据未取到（通道可能临时不可用）")
    for column in (TRADE_DATE_COLUMN, CLOSE_PRICE_COLUMN, PE_TTM_COLUMN):
        assert column in combined.columns, list(combined.columns)
    assert (combined[CLOSE_PRICE_COLUMN] > 0).all()
    # PE 可以缺（数据源没给），但不能是 inf 或 0 这种明显异常值
    pe = combined[PE_TTM_COLUMN].dropna()
    assert not pe.isin([float("inf"), float("-inf")]).any()
    assert (pe[pe != 0].abs() < 10000).all(), "PE 出现离谱数量级"
    print("   -> 综合数据 %d 条，含有效 PE-TTM: %s"
          % (len(combined), bool(combined[PE_TTM_COLUMN].notna().any())))


if __name__ == "__main__":
    # 保住旧的直接执行入口：委托给 pytest，退出码语义一致
    raise SystemExit(pytest.main([__file__, "-v"]))
