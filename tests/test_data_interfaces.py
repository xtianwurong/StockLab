#!/usr/bin/env python3
"""
==============================================================================
StockLab - 数据接口测试 (tests/test_data_interfaces.py)
==============================================================================

【功能用途】
  本脚本用于验证 StockLab 核心功能链路（直接验证分层包 stocklab 及各模块功能）：
    0. 验证基础类型转换模块 (stocklab.common.type_utils)
    1. 验证腾讯实时行情接口 (fetch_realtime_quote)：
       - 验证最新现价、昨收、今开、涨跌额、涨跌幅、成交量/额、换手率、动态 PE/PB、市值等。
    2. 验证多级公司简称查询 (fetch_stock_name)：
       - 优先走腾讯轻量直连，体验毫秒级响应。
    3. 验证月线历史行情与主备通道容错 (fetch_monthly_close_prices / fetch_monthly_price_and_pe)：
       - AkShare -> BaoStock -> 腾讯 三级降级。
    4. 验证核心板块纯正行业 ETF 与 A 股主板 10 年月线交互式网页生成 (SectorTrendVisualizer)。

【运行方式】
  python3 tests/test_data_interfaces.py [股票代码]
==============================================================================
"""

import logging
import os
import sys

# 将项目根目录加入模块搜索路径，保证直接运行本脚本（python tests/test_data_interfaces.py）时能 import stocklab
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab import (
    CLOSE_PRICE_COLUMN,
    PE_TTM_COLUMN,
    TRADE_DATE_COLUMN,
    SectorTrendVisualizer,
    StockDataFetchParams,
    StockDataFetcher,
    safe_float,
    safe_int,
)


def run_type_utils_test():
    """测试 type_utils 类型安全转换基础工具"""
    print("\n" + "=" * 65)
    print("【阶段零：测试 stocklab.common 基础类型转换】")
    print("=" * 65)

    assert safe_float("12.34") == 12.34
    assert safe_float(None, 0.0) == 0.0
    assert safe_float("-", 0.0) == 0.0
    assert safe_float("--", None) is None
    assert safe_int("100.0") == 100
    assert safe_int("abc", -1) == -1
    print("  -> safe_float 与 safe_int 边界测试用例全部通过！")


def run_realtime_quote_test(fetcher: StockDataFetcher, stock_code: str):
    """测试腾讯实时行情快照接口"""
    print("\n" + "=" * 65)
    print(f"【阶段一：测试腾讯实时行情快照】目标股票：{stock_code}")
    print("=" * 65)

    quote = fetcher.fetch_realtime_quote(stock_code)
    if quote is None:
        print(f"-> [警告] 未能获取 {stock_code} 的实时行情")
        return

    print(f"  * 股票代码: {quote.stock_code}")
    print(f"  * 公司简称: {quote.stock_name}")
    print(f"  * 数据来源: {quote.source_name}")
    print(f"  * 行情时间: {quote.quote_time}")
    print(f"  * 当前现价: {quote.current_price:.2f} 元 (昨收: {quote.yesterday_close:.2f} 元, 今开: {quote.today_open:.2f} 元)")
    print(f"  * 当日区间: 最低 {quote.lowest_price:.2f} 元 ~ 最高 {quote.highest_price:.2f} 元")
    print(f"  * 涨跌幅度: {quote.change_amount:+.2f} 元 ({quote.change_percent:+.2f}%)")
    print(f"  * 成交情况: {quote.volume_shares:,} 股 | {quote.amount_yuan / 1e8:.2f} 亿元 | 换手率: {quote.turnover_rate:.2f}%")

    pe_str = f"{quote.pe_ttm:.2f} 倍" if quote.pe_ttm is not None else "暂无"
    pb_str = f"{quote.pb_ratio:.2f} 倍" if quote.pb_ratio is not None else "暂无"
    print(f"  * 估值指标: 动态 PE-TTM: {pe_str} | 市净率 PB: {pb_str}")

    circ_mv_str = f"{quote.circulating_market_value / 1e8:.2f} 亿元" if quote.circulating_market_value else "暂无"
    total_mv_str = f"{quote.total_market_value / 1e8:.2f} 亿元" if quote.total_market_value else "暂无"
    print(f"  * 市值规模: 流通市值: {circ_mv_str} | 总市值: {total_mv_str}")


def run_historical_data_test(fetcher: StockDataFetcher, stock_code: str):
    """测试历史月线价格与 PE-TTM 对齐获取"""
    print("\n" + "=" * 65)
    print(f"【阶段二：测试月线历史与估值对齐】目标股票：{stock_code}")
    print("=" * 65)

    params = StockDataFetchParams(stock_code)

    # 1. 验证公司名称查询
    stock_name = fetcher.fetch_stock_name(stock_code)
    print(f"1. 公司简称查询结果: {stock_name if stock_name else '(未获取到名称)'}")

    # 2. 验证月线价格三级容错
    print("\n2. 测试月线收盘价获取 (fetch_monthly_close_prices)...")
    price_df = fetcher.fetch_monthly_close_prices(params)
    if not price_df.empty:
        print(f"   -> 成功获取 {len(price_df)} 条月线价格")
        print(f"   -> 实际生效数据源: {fetcher.used_source_name}")
        print("   -> 最新 3 个月样本:")
        for _, row in price_df.tail(3).iterrows():
            print(f"      {row[TRADE_DATE_COLUMN].strftime('%Y-%m-%d')}: {row[CLOSE_PRICE_COLUMN]:.2f} 元")
    else:
        print("   -> [警告] 月线价格获取失败！")

    # 3. 验证「月线价格 + PE-TTM」综合对齐
    print("\n3. 测试月度价格与 PE-TTM 综合对齐 (fetch_monthly_price_and_pe)...")
    combined_df = fetcher.fetch_monthly_price_and_pe(params)
    if not combined_df.empty:
        has_pe = combined_df[PE_TTM_COLUMN].notna().any()
        print(f"   -> 综合数据共 {len(combined_df)} 条，是否包含有效 PE-TTM: {has_pe}")
        print("   -> 最新 3 个月对齐样本:")
        for _, row in combined_df.tail(3).iterrows():
            date_str = row[TRADE_DATE_COLUMN].strftime("%Y-%m-%d")
            price_val = row[CLOSE_PRICE_COLUMN]
            pe_raw = row[PE_TTM_COLUMN]
            pe_val = f"{pe_raw:.2f}" if pe_raw == pe_raw else "NaN"
            print(f"      日期: {date_str} | 收盘价: {price_val:.2f} 元 | PE-TTM: {pe_val}")
    else:
        print("   -> [警告] 综合数据获取失败！")


def run_sector_web_generation_test():
    """测试板块纯正 ETF 与主板 10 年走势交互式网页生成"""
    print("\n" + "=" * 65)
    print("【阶段三：测试核心板块与主板月线走势交互式 HTML 网页生成】")
    print("=" * 65)

    test_html = "test_sector_trend.html"
    app = SectorTrendVisualizer(num_months=12)  # 使用 12 个月快速测试 HTML 组装
    result_path = app.generate(output_filename=test_html)
    if result_path and os.path.exists(result_path):
        size_kb = os.path.getsize(result_path) / 1024
        print(f"-> 交互式 HTML 走势网页生成成功: {result_path} (文件大小: {size_kb:.1f} KB)")
        try:
            os.remove(result_path)
        except OSError:
            pass
    else:
        print("-> [警告] 网页生成失败！")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    stock_code = sys.argv[1] if len(sys.argv) > 1 else "000001.SZ"

    print(f">>> 开始执行 StockLab 全功能自动化验证 (股票代码: {stock_code}) <<<")

    # 0. 验证 type_utils 基础转换
    run_type_utils_test()

    fetcher = StockDataFetcher()

    # 1. 验证腾讯实时行情快照
    run_realtime_quote_test(fetcher, stock_code)

    # 2. 验证历史月线与时序对齐
    run_historical_data_test(fetcher, stock_code)

    # 3. 验证板块 ETF 与主板网页图表生成
    run_sector_web_generation_test()

    print("\n>>> 全部验证执行完毕 <<<")


if __name__ == "__main__":
    main()
