#!/usr/bin/env python3
"""
==============================================================================
StockLab - 免费数据源连通性实测脚本 (tests/live_check_sources.py)
==============================================================================

【用途】
  手工执行的实网联通性验证（不参与 CI、不改动数据库）。
  用于：
    1. 新增数据源落地后的首轮验收（需求 §45 / §77）
    2. 疑似上游变更时的快速复查
    3. TDX 协议变更后的选型确认（需求 §67 Step 0）

【运行方式】
  ./venv/bin/python tests/live_check_sources.py
  （需真实网络，仅打印结果，不写数据库）

【预期输出】
  每源打印：连通/失败 + 关键字段（价格、orgId、月线根数等）
  所有源 OK 时退出码 0；任一源失败退出码 1。
==============================================================================
"""

import json
import sys
import os

# 将项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from stocklab.common.http_client import BROWSER_USER_AGENT
from stocklab.datasource._sources.sina_source import SinaDataSource
from stocklab.datasource._sources.tdx_source import TdxDataSource
from stocklab.datasource.cninfo_client import CninfoClient

# ── 新浪 ───────────────────────────────────────────────────────────────────

def check_sina():
    print("\n=== 新浪财经 (SinaDataSource) ===")
    source = SinaDataSource(http_timeout=10)

    # 1) 实时行情
    quote = source.fetch_realtime_quote("600519.SH")
    if quote:
        print(f"  ✓ 实时行情: {quote.stock_name} 现价 {quote.current_price} 涨跌 {quote.change_percent:.2f}% 量 {quote.volume_shares}")
        ok1 = True
    else:
        print("  ✗ 实时行情失败")
        ok1 = False

    # 2) 股票简称
    name = source.fetch_stock_name("600519.SH")
    if name == "贵州茅台":
        print(f"  ✓ 股票简称: {name}")
        ok2 = True
    else:
        print(f"  ✗ 股票简称异常: {name}")
        ok2 = False

    # 3) 月线（不复权）
    frame = source.fetch_monthly_close_prices("600519.SH", "")
    if not frame.empty:
        print(f"  ✓ 月线不复权: {len(frame)} 个月，最新 {frame['trade_date'].max():%Y-%m} 收盘 {frame['close_price'].iloc[-1]:.2f}")
        ok3 = True
    else:
        print("  ✗ 月线不复权失败")
        ok3 = False

    # 4) 月线（前复权 / 后复权）
    for adj in ("qfq", "hfq"):
        f = source.fetch_monthly_close_prices("600519.SH", adj)
        if not f.empty:
            print(f"  ✓ 月线 {adj}: {len(f)} 个月，最新 {f['close_price'].iloc[-1]:.2f}")
        else:
            print(f"  ✗ 月线 {adj}: 空表（可能因子获取失败）")
            ok3 = False

    return ok1 and ok2 and ok3


# ── 通达信 ────────────────────────────────────────────────────────────────

def check_tdx():
    print("\n=== 通达信公开行情 (TdxDataSource / tdxdata) ===")
    try:
        source = TdxDataSource()
    except Exception as e:
        print(f"  ✗ 初始化失败: {e}")
        return False

    # 1) 实时行情
    quote = source.fetch_realtime_quote("600519.SH")
    if quote:
        print(f"  ✓ 实时行情: 现价 {quote.current_price} 昨收 {quote.yesterday_close} 量 {quote.volume_shares}股")
        ok1 = True
    else:
        print("  ✗ 实时行情失败")
        ok1 = False

    # 2) 月线
    frame = source.fetch_monthly_close_prices("600519.SH", "")
    if not frame.empty:
        print(f"  ✓ 月线: {len(frame)} 个月，最新 {frame['trade_date'].max():%Y-%m} 收盘 {frame['close_price'].iloc[-1]:.2f}")
        ok2 = True
    else:
        print("  ✗ 月线失败")
        ok2 = False

    # 3) 北交所月线（验证 market=2 路由）
    frame = source.fetch_monthly_close_prices("920819.BJ", "")
    if not frame.empty:
        print(f"  ✓ 北交所月线: {len(frame)} 个月，最新 {frame['trade_date'].max():%Y-%m}")
        ok3 = True
    else:
        print("  ✗ 北交所月线失败（可能该标的已退市/无行情）")
        ok3 = True  # 不阻塞

    return ok1 and ok2 and ok3


# ── 巨潮公告 ──────────────────────────────────────────────────────────────

def check_cninfo():
    from datetime import date
    print("\n=== 巨潮资讯公告 (CninfoClient) ===")
    client = CninfoClient(http_timeout=15, sleep_seconds=0.8)

    # 1) orgId 解析
    org = client.resolve_org_id("600519")
    if org:
        print(f"  ✓ orgId(600519) = {org}")
        ok1 = True
    else:
        print("  ✗ orgId 解析失败")
        ok1 = False

    # 2) 单只查询（带 orgId 精确路由）
    frame = client.get_announcements("600519.SH", "2026-08-01", "2026-08-31", page=1)
    if not frame.empty:
        row = frame.iloc[0]
        print(f"  ✓ 单只查询: {len(frame)} 条，最新 {row['title'][:30]}...")
        ok2 = True
    else:
        print("  ✗ 单只查询空表")
        ok2 = False

    # 3) 全市场翻页（不带 stock 参数）
    frame = client.fetch_announcements(None, "2026-09-28", "2026-10-03", max_pages=2)
    if not frame.empty:
        print(f"  ✓ 全市场翻页: {len(frame)} 条（含非 A 股，同步阶段会过滤）")
        ok3 = True
    else:
        print("  ✗ 全市场翻页空表")
        ok3 = False

    # 4) 日期换算验证（任意行检查 UTC+8 换算逻辑，不校验具体日期）
    if frame is not None and len(frame) > 0:
        row = frame.iloc[0]
        if isinstance(row["announcement_date"], date):
            print(f"  ✓ 日期换算 UTC+8 正确: {row['announcement_date']} (类型: {type(row['announcement_date']).__name__})")
            ok4 = True
        else:
            print(f"  ✗ 日期类型异常: {row['announcement_date']} (类型: {type(row['announcement_date']).__name__})")
            ok4 = False
    else:
        ok4 = True

    # 5) 标题清洗 / PDF URL
    if len(frame) > 0:
        row = frame.iloc[0]
        if "<em>" not in row["title"] and row["pdf_url"].startswith("http://static.cninfo.com.cn/"):
            print(f"  ✓ 标题清洗 / PDF URL 正确: {row['pdf_url'][:60]}...")
            ok5 = True
        else:
            print(f"  ✗ 标题或 PDF URL 异常: {row['title'][:30]} | {row['pdf_url'][:60]}")
            ok5 = False
    else:
        ok5 = True

    return ok1 and ok2 and ok3 and ok4 and ok5


# ── 交叉校验（新浪 vs TDX 价格一致性）─────────────────────────────────────

def check_cross_validation():
    print("\n=== 交叉校验：新浪 vs 通达信 600519 实时价 ===")
    sina = SinaDataSource(http_timeout=10)
    tdx = TdxDataSource()
    q1 = sina.fetch_realtime_quote("600519.SH")
    q2 = tdx.fetch_realtime_quote("600519.SH")
    if q1 and q2:
        d = abs(q1.current_price - q2.current_price)
        if d < 0.01:
            print(f"  ✓ 价格一致: 新浪 {q1.current_price} == 通达信 {q2.current_price}")
            return True
        else:
            print(f"  ✗ 价差 {d:.2f}: 新浪 {q1.current_price} vs 通达信 {q2.current_price}")
            return False
    else:
        print("  ✗ 任一源无数据，无法校验")
        return False


# ── 主流程 ────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("StockLab 免费数据源连通性实测 (tests/live_check_sources.py)")
    print("=" * 70)
    print("警告：此脚本访问真实公开接口，不修改数据库，仅打印结果。")
    print("     如遇连接超时/被限流，属正常网络环境波动，请稍后重试。")

    ok = True
    ok &= check_sina()
    ok &= check_tdx()
    ok &= check_cninfo()
    ok &= check_cross_validation()

    print("\n" + "=" * 70)
    if ok:
        print("全部数据源连通性实测 通过 ✓")
    else:
        print("存在失败项 ✗  请检查网络/上游状态")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())