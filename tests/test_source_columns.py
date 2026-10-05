#!/usr/bin/env python3
"""
==============================================================================
StockLab - 数据源列名防御性检查测试 (tests/test_source_columns.py)
==============================================================================

【功能用途】
   验证各数据源通道的「列名防御性检查」逻辑生效。
   这些检查是防止上游 API 改版导致静默写错库的最后一道防线。

【测试策略】
   1. 正常列名 -> 应正常通过，产出标准 DataFrame
   2. 缺关键列 / 列名改版 -> 应记警告并返回空 DataFrame，**不得**抛异常
   3. 列名大小写 / 空格变化 -> 应被识别为异常

【为什么不直接测完整列名快照】
   列名快照测试需要 mock 到内部私有方法，耦合太紧。
   这里测的是「防御逻辑是否生效」，这是公共契约，更稳定。

【运行方式】
   ./venv/bin/python -m pytest tests/test_source_columns.py -v
"""

import os
import sys

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

from stocklab.datasource._sources.akshare_source import AkShareDataSource
from stocklab.datasource._sources.sina_source import SinaDataSource
from stocklab.datasource._sources.tencent_source import TencentDataSource
from stocklab.datasource._sources.baostock_source import BaoStockDataSource

_logger = logging.getLogger(__name__)


# =============================================================================
# 测试 Fixtures：构造各种列名场景的原始 DataFrame
# =============================================================================

# AkShare 标准日线列（东财接口）
AK_DAILY_OK = pd.DataFrame({
    "日期": pd.date_range("2026-01-01", periods=3, freq="D"),
    "股票代码": ["600519"] * 3,
    "开盘": [100.0, 101.0, 102.0],
    "收盘": [101.0, 102.0, 103.0],
    "最高": [102.0, 103.0, 104.0],
    "最低": [99.0, 100.0, 101.0],
    "成交量": [1000, 2000, 3000],
    "成交额": [1e5, 2e5, 3e5],
    "振幅": [1.0, 1.0, 1.0],
    "涨跌幅": [1.0, 1.0, 1.0],
    "涨跌额": [1.0, 1.0, 1.0],
    "换手率": [0.5, 0.5, 0.5],
})

# AkShare 缺「收盘」列
AK_DAILY_MISSING_CLOSE = AK_DAILY_OK.drop(columns=["收盘"])

# AkShare 列名改版（英文）
AK_DAILY_ENGLISH = AK_DAILY_OK.rename(columns={
    "日期": "date", "收盘": "close", "开盘": "open",
    "最高": "high", "最低": "low", "成交量": "volume"
})

# AkShare 列名有空格
AK_DAILY_SPACES = AK_DAILY_OK.rename(columns=lambda c: " " + c + " ")


# Sina 实时行情字段（按 _fetch_quote_fields 解析后的顺序）
SINA_QUOTE_OK = [
    "贵州茅台", "1800.00", "1780.00", "1795.00", "1810.00", "1770.00",
    "1795.00", "1796.00", "1000000", "1795000000",
    "500", "1795.00", "300", "1794.00", "200", "1793.00",
    "100", "1792.00", "50", "1791.00",
    "600", "1796.00", "400", "1797.00", "300", "1798.00",
    "200", "1799.00", "100", "1800.00",
    "2026-09-30", "15:00:00"
]

# Sina 字段不足
SINA_QUOTE_TOO_FEW = ["贵州茅台", "1800.00", "1780.00"]

# Sina 空名称
SINA_QUOTE_EMPTY_NAME = ["", "1800.00", "1780.00", "1795.00"] + ["0"] * 30


# =============================================================================
# AkShare 通道测试
# =============================================================================

@patch("stocklab.datasource._sources.akshare_source.ak.stock_zh_a_hist")
def test_akshare_standard_columns_pass(mock_akshare):
    """标准列名 -> 正常产出标准 DataFrame"""
    mock_akshare.return_value = AK_DAILY_OK
    source = AkShareDataSource()
    result = source.fetch_monthly_close_prices("600519.SH", adjust_type="")
    assert not result.empty
    assert list(result.columns) == ["trade_date", "close_price"]
    assert len(result) == 3


@patch("stocklab.datasource._sources.akshare_source.ak.stock_zh_a_hist")
def test_akshare_missing_close_column_rejected(mock_akshare, caplog):
    """缺「收盘」列 -> 记警告并返回空表"""
    mock_akshare.return_value = AK_DAILY_MISSING_CLOSE
    source = AkShareDataSource()
    with caplog.at_level(logging.WARNING, logger="stocklab.datasource._sources.akshare_source"):
        result = source.fetch_monthly_close_prices("600519.SH", adjust_type="")
    assert result.empty
    assert any("列名有变化" in msg for msg in caplog.messages)


@patch("stocklab.datasource._sources.akshare_source.ak.stock_zh_a_hist")
def test_akshare_english_columns_rejected(mock_akshare, caplog):
    """英文列名 -> 记警告并返回空表"""
    mock_akshare.return_value = AK_DAILY_ENGLISH
    source = AkShareDataSource()
    with caplog.at_level(logging.WARNING, logger="stocklab.datasource._sources.akshare_source"):
        result = source.fetch_monthly_close_prices("600519.SH", adjust_type="")
    assert result.empty
    assert any("列名有变化" in msg for msg in caplog.messages)


@patch("stocklab.datasource._sources.akshare_source.ak.stock_zh_a_hist")
def test_akshare_spaces_in_column_names_rejected(mock_akshare, caplog):
    """列名含空格 -> 记警告并返回空表（精确匹配，不做 strip）"""
    mock_akshare.return_value = AK_DAILY_SPACES
    source = AkShareDataSource()
    with caplog.at_level(logging.WARNING, logger="stocklab.datasource._sources.akshare_source"):
        result = source.fetch_monthly_close_prices("600519.SH", adjust_type="")
    assert result.empty
    assert any("列名有变化" in msg for msg in caplog.messages)


# =============================================================================
# Sina 通道测试
# =============================================================================

def test_sina_standard_fields_parse_ok():
    """标准 34 字段 -> 解析成功，返回 StockRealtimeQuote"""
    source = SinaDataSource()
    source._fetch_quote_fields = lambda code: SINA_QUOTE_OK
    quote = source.fetch_realtime_quote("600519.SH")
    assert quote is not None
    assert quote.stock_code == "600519.SH"
    assert quote.stock_name == "贵州茅台"
    assert quote.current_price == 1795.00
    # 涨跌幅约 0.84%
    assert abs(quote.change_percent - 0.84) < 0.02


def test_sina_too_few_fields_rejected():
    """字段不足 10 个 -> 返回空列表 -> fetch_realtime_quote 返回 None"""
    source = SinaDataSource()
    source._fetch_quote_fields = lambda code: SINA_QUOTE_TOO_FEW
    quote = source.fetch_realtime_quote("600519.SH")
    assert quote is None


def test_sina_empty_name_rejected():
    """名称为空 -> 返回空列表 -> fetch_realtime_quote 返回 None"""
    source = SinaDataSource()
    source._fetch_quote_fields = lambda code: SINA_QUOTE_EMPTY_NAME
    quote = source.fetch_realtime_quote("600519.SH")
    assert quote is None


def test_sina_stock_name_extraction():
    """股票简称提取复用同一解析路径"""
    source = SinaDataSource()
    source._fetch_quote_fields = lambda code: SINA_QUOTE_OK
    name = source.fetch_stock_name("600519.SH")
    assert name == "贵州茅台"


def test_sina_stock_name_empty_on_failure():
    """解析失败时股票简称返回空串"""
    source = SinaDataSource()
    source._fetch_quote_fields = lambda code: SINA_QUOTE_TOO_FEW
    name = source.fetch_stock_name("600519.SH")
    assert name == ""


# =============================================================================
# Tencent 通道测试
# =============================================================================

def test_tencent_standard_data_parse_ok():
    """标准字段 -> 解析成功"""
    mock_client = MagicMock()
    mock_client.fetch_quote_fields.return_value = [
        "600519.SH",  # 0: 代码
        "贵州茅台",    # 1: 名称
        "600519",     # 2: 代码
        "1795.00",    # 3: 现价
        "1780.00",    # 4: 昨收
        "1800.00",    # 5: 今开
        "10000",      # 6: 成交量(手)
        "", "", "", "", "", "", "", "", "",  # 7-15
        "", "", "", "", "", "", "", "", "",  # 16-25
        "", "", "", "", "", "", "", "", "",  # 26-35
        "1810.00",    # 33: 最高
        "1770.00",    # 34: 最低
        "", "", "", "", "", "", "", "", "",  # 35-44
        "", "", "", "",  # 45-48
    ]
    source = TencentDataSource(client=mock_client)
    quote = source.fetch_realtime_quote("600519.SH")
    assert quote is not None
    assert quote.stock_code == "600519.SH"
    assert quote.stock_name == "贵州茅台"
    assert quote.current_price == 1795.00


def test_tencent_missing_key_fields_rejected():
    """缺关键字段 -> 该股票不出现在结果中"""
    mock_client = MagicMock()
    mock_client.fetch_quote_fields.return_value = []  # 空字段
    source = TencentDataSource(client=mock_client)
    quote = source.fetch_realtime_quote("600519.SH")
    assert quote is None


# =============================================================================
# Baostock 通道测试
# =============================================================================

def test_baostock_standard_columns_pass():
    """标准列名 -> 正常产出"""
    source = BaoStockDataSource()
    with patch.object(source, "_query_baostock_kline_rows") as mock_query:
        mock_query.return_value = [
            ["2026-01-01", "101.0"],  # date, close
            ["2026-01-02", "102.0"],
        ]
        result = source.fetch_monthly_close_prices("600519.SH", adjust_type="")
        assert not result.empty
        assert len(result) == 2


# =============================================================================
# 通用：防御性检查不得抛异常，必须返回空表/None
# =============================================================================

@patch("stocklab.datasource._sources.akshare_source.ak.stock_zh_a_hist")
def test_akshare_never_raises_on_bad_columns(mock_akshare):
    """列名异常时必须返回空表，绝不抛异常"""
    for bad_frame in [AK_DAILY_MISSING_CLOSE, AK_DAILY_ENGLISH, AK_DAILY_SPACES]:
        mock_akshare.return_value = bad_frame
        source = AkShareDataSource()
        result = source.fetch_monthly_close_prices("600519.SH", adjust_type="")
        assert result.empty


def test_sina_never_raises_on_bad_fields():
    """字段异常时必须返回 None，绝不抛异常"""
    source = SinaDataSource()
    for bad_fields in [SINA_QUOTE_TOO_FEW, SINA_QUOTE_EMPTY_NAME]:
        source._fetch_quote_fields = lambda code: bad_fields
        quote = source.fetch_realtime_quote("600519.SH")
        assert quote is None


def test_tencent_never_raises_on_bad_data():
    """数据异常时必须返回空结果，绝不抛异常"""
    mock_client = MagicMock()
    mock_client.fetch_quote_fields.return_value = []  # 空字段
    source = TencentDataSource(client=mock_client)
    quote = source.fetch_realtime_quote("600519.SH")
    assert quote is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])