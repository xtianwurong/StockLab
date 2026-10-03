#!/usr/bin/env python3
"""
==============================================================================
StockLab - 新浪数据源测试 (tests/test_sina_source.py)
==============================================================================

【功能用途】
  覆盖免费数据源扩展需求 §42 的新浪通道断言：
    1. 实时行情解析（字段口径、涨跌计算、GBK 报文）
    2. 股票简称
    3. 标准 DataFrame（日线 -> 月末重采样）
    4. 异常返回空表 / None
    5. 无效代码
    6. 复权因子应用（qfq = f(d)/f_latest，hfq = f(d)）
    7. 符号转换（sh/sz/bj 前缀）

【运行方式】
  ./venv/bin/python tests/test_sina_source.py
  （纯离线：原始报文以 fixture 内置，不发起任何网络请求）
==============================================================================
"""

import os
import sys

# 将项目根目录加入模块搜索路径，保证直接运行本脚本时能 import stocklab
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.datasource._sources.sina_source import (
    SinaDataSource,
    _parse_jsonp_array,
    _to_sina_symbol,
)

# 实时行情 fixture（2026-09-30 收盘，字段布局与实测一致）
QUOTE_TEXT = (
    'var hq_str_sh600519="贵州茅台,1239.530,1235.580,1258.620,1268.000,1236.050,'
    "1258.620,1258.650,3833098,4797246636.000,1445,1258.620,100,1258.440,100,"
    "1258.160,200,1258.050,4100,1258.000,200,1258.650,300,1258.660,100,1258.680,"
    '200,1258.690,8000,1258.750,2026-09-30,15:34:59,00,D|3600|4531032.00";\n'
)

# 日线 JSONP fixture：跨两个月，用于验证月末重采样
KLINE_TEXT = (
    "/*<script>location.href='//sina.com';</script>*/\n"
    "var _sh600519=(["
    '{"day":"2026-05-29","open":"99.000","high":"101.000","low":"98.000","close":"100.000","volume":"1000"},'
    '{"day":"2026-06-01","open":"105.000","high":"111.000","low":"104.000","close":"110.000","volume":"2000"},'
    '{"day":"2026-06-30","open":"115.000","high":"121.000","low":"114.000","close":"120.000","volume":"3000"}'
    "])"
)

# 后复权因子 fixture：1900 基准 + 2026-06-15 一次除权（因子翻倍）
FACTOR_TEXT = (
    "var sh600519hfq={\"total\":2,\"data\":["
    "{\"d\":\"2026-06-15\", \"f\":\"2.0000000000000000\"},"
    "{\"d\":\"1900-01-01\", \"f\":\"1.0000000000000000\"}"
    "]}\n"
)


def _stub_source(handler):
    """构造一个不联网的 SinaDataSource：用 handler 假装 HTTP 响应"""
    source = SinaDataSource()
    source._get_text = handler
    return source


def _stub_by_url(kline_text=KLINE_TEXT, factor_text=FACTOR_TEXT, quote_text=QUOTE_TEXT):
    """按 URL 分发 fixture 的桩"""
    def handler(url):
        if "hfq.js" in url:
            return factor_text
        if "getKLineData" in url:
            return kline_text
        if "hq.sinajs.cn" in url:
            return quote_text
        return ""
    return _stub_source(handler)


def run_symbol_test():
    """测试代码符号转换：标准代码 / 纯数字 / 带前缀 / 非法输入"""
    print("\n" + "=" * 65)
    print("【阶段一：测试股票代码符号转换】")
    print("=" * 65)

    assert _to_sina_symbol("600519.SH") == "sh600519"
    assert _to_sina_symbol("000001.SZ") == "sz000001"
    assert _to_sina_symbol("300750.SZ") == "sz300750"
    assert _to_sina_symbol("920819.BJ") == "bj920819"
    assert _to_sina_symbol("600519") == "sh600519"
    assert _to_sina_symbol("430047") == "bj430047"
    assert _to_sina_symbol("sh600519") == "sh600519"
    assert _to_sina_symbol("XX600519") == ""
    assert _to_sina_symbol("") == ""
    print("  -> 10 组符号转换全部正确")


def run_jsonp_test():
    """测试 JSONP 日线响应解析（正常 / 残缺 / 非 JSON）"""
    print("\n" + "=" * 65)
    print("【阶段二：测试 JSONP 解析】")
    print("=" * 65)

    rows = _parse_jsonp_array(KLINE_TEXT)
    assert len(rows) == 3, len(rows)
    assert rows[0]["day"] == "2026-05-29"
    assert _parse_jsonp_array("") == []
    assert _parse_jsonp_array("no json here") == []
    assert _parse_jsonp_array('{"__ERROR":3,"__ERRORMSG":"Service not valid"}') == []
    assert _parse_jsonp_array('var x=(["a","b"])') == ["a", "b"]
    print("  -> JSONP 正常 / 空 / 非法 / 错误报文 全部按预期处理")


def run_realtime_test():
    """测试实时行情解析：价格、涨跌、量额、时间、简称"""
    print("\n" + "=" * 65)
    print("【阶段三：测试实时行情解析】")
    print("=" * 65)

    source = _stub_by_url()
    quote = source.fetch_realtime_quote("600519.SH")
    assert quote is not None, "实时行情解析失败"
    assert quote.stock_name == "贵州茅台", quote.stock_name
    assert quote.current_price == 1258.62, quote.current_price
    assert quote.yesterday_close == 1235.58, quote.yesterday_close
    assert quote.today_open == 1239.53, quote.today_open
    assert quote.highest_price == 1268.00, quote.highest_price
    assert quote.lowest_price == 1236.05, quote.lowest_price
    assert quote.volume_shares == 3833098, quote.volume_shares
    assert quote.amount_yuan == 4797246636.0, quote.amount_yuan
    assert quote.change_amount == round(1258.62 - 1235.58, 4), quote.change_amount
    assert quote.change_percent > 0, quote.change_percent
    assert quote.quote_time == "2026-09-30 15:34:59", quote.quote_time
    assert quote.source_name == "sina(新浪财经)", quote.source_name
    # 新浪该端点不返回换手率 / PE / PB / 市值：保持默认值而非编造
    assert quote.turnover_rate == 0.0
    assert quote.pe_ttm is None and quote.pb_ratio is None
    print("  -> 现价/昨收/开高低/量额/涨跌/时间/简称全部正确: %s" % quote)


def run_stock_name_test():
    """测试股票简称查询（复用实时行情响应）"""
    print("\n" + "=" * 65)
    print("【阶段四：测试股票简称】")
    print("=" * 65)

    source = _stub_by_url()
    assert source.fetch_stock_name("600519.SH") == "贵州茅台"
    # 空响应 -> 空串（不抛异常）
    empty = _stub_source(lambda url: "")
    assert empty.fetch_stock_name("600519.SH") == ""
    print("  -> 简称解析与空响应降级正确")


def run_monthly_test():
    """测试日线 -> 月末重采样（不复权 / 前复权 / 后复权）"""
    print("\n" + "=" * 65)
    print("【阶段五：测试月线收盘价与复权】")
    print("=" * 65)

    # 不复权：每月取最后一个交易日
    frame = _stub_by_url().fetch_monthly_close_prices("600519.SH", "")
    assert len(frame) == 2, frame
    assert list(frame.columns) == ["trade_date", "close_price"], list(frame.columns)
    assert str(frame["trade_date"].iloc[0])[:10] == "2026-05-31"
    assert frame["close_price"].iloc[0] == 100.0
    assert str(frame["trade_date"].iloc[1])[:10] == "2026-06-30"
    assert frame["close_price"].iloc[1] == 120.0
    print("  -> 不复权月线月末取值正确")

    # 后复权：2026-06-15 因子 2.0 生效，6 月月末 = 120 * 2
    frame = _stub_by_url().fetch_monthly_close_prices("600519.SH", "hfq")
    assert frame["close_price"].iloc[0] == 100.0, frame
    assert frame["close_price"].iloc[1] == 240.0, frame["close_price"].iloc[1]
    print("  -> 后复权 hfq = raw × f(d) 正确")

    # 前复权：qfq 因子 = f(d)/f(latest) -> 除权前 0.5、除权后 1.0
    frame = _stub_by_url().fetch_monthly_close_prices("600519.SH", "qfq")
    assert frame["close_price"].iloc[0] == 50.0, frame["close_price"].iloc[0]
    assert frame["close_price"].iloc[1] == 120.0, frame["close_price"].iloc[1]
    print("  -> 前复权 qfq = raw × f(d)/f(latest) 正确")


def run_failure_test():
    """测试异常路径：空响应 / 错误报文 / 无效代码 / 非法复权类型 / 重试"""
    print("\n" + "=" * 65)
    print("【阶段六：测试异常与无效输入】")
    print("=" * 65)

    # 空响应
    source = _stub_source(lambda url: "")
    assert source.fetch_realtime_quote("600519.SH") is None
    assert source.fetch_monthly_close_prices("600519.SH", "").empty

    # 东财式错误报文（新浪失效接口的历史返回）
    bad = _stub_by_url(
        kline_text='{"__ERROR":3,"__ERRORMSG":"Service not valid"}'
    )
    assert bad.fetch_monthly_close_prices("600519.SH", "").empty

    # 价格字段为 0 的非法报文
    zero = _stub_by_url(quote_text='var hq_str_sh600519="贵州茅台,0,0,0,0,0,0,0,0,0,0,0";')
    assert zero.fetch_realtime_quote("600519.SH") is None

    # 无效代码：符号无法识别 -> 不发请求，直接失败
    invalid = _stub_by_url()
    assert invalid.fetch_realtime_quote("NOT-A-CODE") is None
    assert invalid.fetch_monthly_close_prices("NOT-A-CODE", "").empty

    # 非法复权类型 -> 空表（不返回错误口径的数据）
    assert _stub_by_url().fetch_monthly_close_prices("600519.SH", "adjusted").empty

    # 复权因子缺失 -> 空表
    no_factor = _stub_by_url(factor_text="")
    assert no_factor.fetch_monthly_close_prices("600519.SH", "qfq").empty

    # 重试次数耗尽仍返回空表（不抛异常）
    source = SinaDataSource()
    source._get_text = lambda url: ""
    assert source.fetch_monthly_close_prices("600519.SH", "", retry_count=2).empty
    print("  -> 8 组异常场景均安全降级，无异常抛出")


def main():
    """运行全部新浪通道测试"""
    run_symbol_test()
    run_jsonp_test()
    run_realtime_test()
    run_stock_name_test()
    run_monthly_test()
    run_failure_test()
    print("\n" + "=" * 65)
    print("新浪数据源测试全部通过！")
    print("=" * 65)


if __name__ == "__main__":
    main()
