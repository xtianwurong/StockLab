#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金/基本面 Web 接口契约测试 (tests/test_fund_web_apis.py)
==============================================================================

【功能用途】
  用 Flask test_client 把以下接口的**参数校验分支**与**业务分支**各跑一遍：
    - fund_analysis_api：/api/fund/list|info|nav、/api/sw/indices|mapping、
      /api/sector/indices、/api/capital/flow、/api/fund/allocation(/history)
    - fundamental_api：/api/fundamental/income|balance|cashflow|indicators|latest
    - fund_holding_api：/api/fund/holding(/latest)、/api/stock/industry/mapping

【断言口径】
  按项目既定原则只断言**状态码**与 **error 字段的存在性**：
    - 参数非法必须 400 且带 error（这是契约，钉死）；
    - 业务分支允许 200（有数据）/ 400（业务拒绝，如调仓分析算不出）/ 404（查无此基金），
      但非 200 必须带 error —— 绝不允许「500 且无 error」这种裸栈泄漏出去。
  不断言具体业务数值：真实库数据会随同步演进，钉数值等于钉一份会腐烂的快照。

【运行方式】
  ./venv/bin/python -m pytest tests/test_fund_web_apis.py -v
  # 依赖真实库，须先 pkill -f serve_web.py（conftest 会自动检测并给出提示）
"""

import os
import sys

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ---------------------------------------------------------------------------
# 1. 参数非法 -> 必须 400 + error
# ---------------------------------------------------------------------------
INVALID_CASES = [
    # --- fund_analysis_api ---
    "/api/fund/info",                                  # 缺 code
    "/api/fund/nav",                                   # 缺 code
    "/api/fund/nav?code=000001.OF&start=bad-date",     # start 日期格式
    "/api/fund/nav?code=000001.OF&end=2026-13-40",     # end 日期格式
    "/api/sw/indices?level=9",                         # 层级越界
    "/api/sector/indices",                             # 缺 codes
    "/api/sector/indices?codes=%20,%20",               # 全是分隔符
    "/api/sector/indices?codes=801010.SI&start=bad",   # start 日期格式
    "/api/capital/flow?type=unknown",                  # 板块类型不在枚举内
    "/api/fund/allocation",                            # 缺 code
    "/api/fund/allocation?code=000001.OF&date=bad",    # 日期格式
    "/api/fund/allocation?code=000001.OF&window=5",    # 窗口下界
    "/api/fund/allocation?code=000001.OF&window=9999", # 窗口上界
    "/api/fund/allocation?code=000001.OF&level=3",     # 层级越界
    "/api/fund/allocation/history",                    # 缺 date
    "/api/fund/allocation/history?code=000001.OF&date=bad",
    "/api/fund/allocation/history?code=000001.OF&date=2026-09-30&window=abc",

    # --- fundamental_api ---
    "/api/fundamental/income",                         # 缺 code
    "/api/fundamental/income?code=600519.SH&as_of=bad",
    "/api/fundamental/income?code=600519.SH&as_of=1900-01-01",
    "/api/fundamental/balance",                        # 缺 code
    "/api/fundamental/balance?code=600519.SH&as_of=bad",
    "/api/fundamental/cashflow",                       # 缺 code
    "/api/fundamental/cashflow?code=600519.SH&as_of=bad",
    "/api/fundamental/indicators",                     # 缺 code
    "/api/fundamental/indicators?code=600519.SH&as_of=bad",
    "/api/fundamental/latest",                         # 缺 code

    # --- fund_holding_api ---
    "/api/fund/holding",                               # 缺 code
    "/api/fund/holding?code=000001.OF&start=bad",
    "/api/fund/holding?code=000001.OF&end=bad",
    "/api/fund/holding/latest",                        # 缺 code
    "/api/stock/industry/mapping?level=9",             # 层级越界
]


@pytest.mark.parametrize("path", INVALID_CASES)
def test_invalid_parameter_is_rejected(client, path):
    """参数非法必须 400 且带 error —— 这是前端能否显示「哪里填错了」的依据"""
    response = client.get(path)

    assert response.status_code == 400, f"{path} -> {response.status_code}"
    payload = response.get_json()
    assert isinstance(payload, dict)
    assert payload.get("error"), f"{path} 的 400 响应必须带 error 字段"


# ---------------------------------------------------------------------------
# 2. 业务分支：200 有数据 / 400 业务拒绝 / 404 查无此基金
# ---------------------------------------------------------------------------
BUSINESS_CASES = [
    # --- fund_analysis_api ---
    "/api/fund/list",
    "/api/fund/list?type=%E8%82%A1%E7%A5%A8%E5%9E%8B",   # 中文类型筛选（须百分号编码）
    "/api/fund/info?code=000001.OF",
    "/api/fund/info?code=999999.OF",                     # 查无此基金 -> 404
    "/api/fund/nav?code=000001.OF&start=2026-01-01&end=2026-09-30",
    "/api/sw/indices?level=1",
    "/api/sw/mapping",
    "/api/sector/indices?codes=801010.SI&start=2026-07-01",
    "/api/capital/flow?type=sw_level1",
    "/api/fund/allocation?code=000001.OF&date=2026-09-30&window=60&level=1",
    "/api/fund/allocation?code=999999.OF&date=2026-09-30",  # 无净值历史 -> 业务拒绝
    "/api/fund/allocation/history?code=000001.OF&date=2026-09-30&window=60&level=1",

    # --- fundamental_api ---
    "/api/fundamental/income?code=600519.SH",
    "/api/fundamental/income?code=600519.SH&as_of=2024-03-31",
    "/api/fundamental/balance?code=600519.SH",
    "/api/fundamental/cashflow?code=600519.SH",
    "/api/fundamental/indicators?code=600519.SH",
    "/api/fundamental/latest?code=600519.SH",

    # --- fund_holding_api ---
    "/api/fund/holding?code=000001.OF",
    "/api/fund/holding?code=000001.OF&start=2026-01-01&end=2026-12-31",
    "/api/fund/holding/latest?code=000001.OF",
    "/api/fund/holding/latest?code=999999.OF",           # 查无持仓 -> 404
    "/api/stock/industry/mapping?level=1",
    "/api/stock/industry/mapping?level=2",
    "/api/stock/industry/mapping?level=3",
]


@pytest.mark.parametrize("path", BUSINESS_CASES)
def test_business_endpoint_never_leaks_server_error(client, path):
    """业务分支：只允许 200 / 400 / 404，且非 200 必须带 error"""
    response = client.get(path)

    assert response.status_code in (200, 400, 404), (
        f"{path} -> {response.status_code}: {response.get_data(as_text=True)[:300]}"
    )
    payload = response.get_json()
    assert isinstance(payload, dict), f"{path} 必须返回 JSON"

    if response.status_code == 200:
        assert "error" not in payload, f"{path} 成功响应不应带 error"
    else:
        assert payload.get("error"), f"{path} 非 200 响应必须带 error"


def test_fund_list_carries_paging_free_payload(client):
    """基金列表给 items + count，前端据此渲染空态而不是转圈"""
    payload = client.get("/api/fund/list").get_json()

    assert "items" in payload and "count" in payload
    assert isinstance(payload["items"], list)
    assert payload["count"] == len(payload["items"])


def test_latest_holding_reports_report_date(client):
    """最新持仓要把报告期显式带出来 —— 页面据此标注「2026-06-30 报告」"""
    response = client.get("/api/fund/holding/latest?code=000001.OF")

    if response.status_code != 200:
        pytest.skip("真实库暂无该基金持仓（同步演进后应为 200）")
    payload = response.get_json()
    assert payload["count"] > 0
    assert payload["report_date"]
    assert payload["items"][0]["weight"] > 0


def test_industry_mapping_level_routes_differently(client):
    """level=1/2 返回 code->name 映射表，level=3 返回明细行 —— 三种形状不能串"""
    for level in (1, 2, 3):
        payload = client.get(f"/api/stock/industry/mapping?level={level}").get_json()
        assert "count" in payload
        if level in (1, 2):
            assert "mapping" in payload, f"level={level} 应返回 mapping 字典"
        else:
            assert "items" in payload, "level=3 应返回明细行 items"
