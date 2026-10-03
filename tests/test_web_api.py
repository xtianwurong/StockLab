#!/usr/bin/env python3
"""
==============================================================================
StockLab - Web 层测试 (tests/test_web_api.py)
==============================================================================

【功能用途】
  用 Flask test_client 覆盖 app/web 层的接口契约与关键口径：
    1. 页面与图标路由可达（200）
    2. 参数校验分支（400）与未知路径（404）
    3. 数据缺失分支（200 + 空 results + message，而非 500）
    4. 证券联想：大小写不敏感、排序稳定、多命中提示
    5. 中文名 -> 代码解析（含多匹配与未命中）
    6. 七档评级：后端 percentile_level 与前端 common.js 的 LEVEL7 完全一致
    7. 全市场分位 SQL 与 ValuationPercentileAnalyzer 抽样口径一致
    8. 全市场排行的排序 / 过滤 / 分页 / null 恒排最后
    9. 指数列表与单指数详情
   10. 多窗口分位（全部 / 近十年 / 近五年 / 近三年）

【运行方式】
  # 必须先停掉 serve_web.py —— DuckDB 同文件跨进程单写者（见 AGENT.md）
  ./venv/bin/python tests/test_web_api.py
==============================================================================
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys

# 将项目根目录加入模块搜索路径，保证直接运行本脚本时能 import stocklab
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.web import create_app
from app.web import store
from stocklab.analytics import ValuationPercentileAnalyzer
from stocklab.facade import MarketDataFacade

import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMON_JS = os.path.join(BASE_DIR, "app", "web", "static", "common.js")

# 抽样比对口径时使用的标的数
CONSENSUS_SAMPLE_SIZE = 20

# 比对口径用的指标
CONSENSUS_INDICATOR = "pe_ttm"

# 整个测试过程共用一个应用实例（store 的查询需要 app context）
_APP = None


def _client():
    """创建测试客户端（复用全局应用，避免重复装配路由）"""
    return _APP.test_client()


def _json(response):
    """读取 JSON 应答体"""
    return response.get_json()


def run_route_tests():
    """阶段一：页面与图标路由可达"""
    print("\n" + "=" * 65)
    print("【阶段一：页面与图标路由】")
    print("=" * 65)

    client = _client()
    for path in ("/", "/market", "/indices", "/favicon.ico"):
        response = client.get(path)
        assert response.status_code == 200, "%s 返回 %s" % (path, response.status_code)
        print("  -> %-14s 200, %d bytes" % (path, len(response.data)))

    health = _json(client.get("/api/health"))
    assert health["status"] == "ok"
    assert health["db_path"]
    print("  -> /api/health  status=%s db=%s" % (health["status"], health["db_path"]))


def run_validation_tests():
    """阶段二：参数校验（400）与未知路径（404）"""
    print("\n" + "=" * 65)
    print("【阶段二：参数校验与 404】")
    print("=" * 65)

    client = _client()
    bad_requests = [
        "/api/percentile?code=",
        "/api/percentile?code=600519.SH&period=近三年",
        "/api/percentile?code=600519.SH&start_date=不是日期",
        "/api/percentile?code=600519.SH&pb=abc",
        "/api/percentile?code=600519.SH&start_date=2026-01-01&end_date=2020-01-01",
        "/api/market/ranking?indicator=市值",
        "/api/market/ranking?limit=0",
        "/api/market/ranking?limit=9999",
        "/api/market/ranking?sort=乱写",
        "/api/market/ranking?order=up",
        "/api/index/detail?code=",
        "/api/index/detail?code=x&indicator=市值",
    ]
    for path in bad_requests:
        response = client.get(path)
        body = _json(response)
        assert response.status_code == 400, "%s 返回 %s" % (path, response.status_code)
        assert "error" in body, "%s 缺少 error 字段" % path
        print("  -> 400 %-52s %s" % (path[:52], body["error"][:34]))

    not_found = client.get("/api/definitely-not-here")
    assert not_found.status_code == 404
    assert "error" in _json(not_found)
    page_not_found = client.get("/definitely-not-here")
    assert page_not_found.status_code == 404
    print("  -> 404 /api/* 与 / 页面路径均正确兜底")


def run_missing_data_tests():
    """阶段三：数据缺失分支必须是 200 + 空结果，而不是 500"""
    print("\n" + "=" * 65)
    print("【阶段三：数据缺失分支】")
    print("=" * 65)

    client = _client()

    unknown = _json(client.get("/api/percentile?code=4048"))
    assert unknown["results"] == [], "未知代码应返回空 results"
    assert unknown["message"], "未知代码应带提示文案"
    assert unknown["security"]["name"] == "", "未知代码不应有名称"
    print("  -> 未知代码 200 + 空 results: %s" % unknown["message"][:34])

    no_member = _json(client.get("/api/index/detail?code=999999"))
    assert no_member["results"] == []
    assert no_member["message"]
    print("  -> 未知指数 200 + 空 results: %s" % no_member["message"][:34])

    empty_query = _json(client.get("/api/securities?q="))
    assert empty_query["items"] == []
    print("  -> 空联想关键词返回空列表")


def run_suggest_tests():
    """阶段四：证券联想的大小写、排序与多命中提示"""
    print("\n" + "=" * 65)
    print("【阶段四：证券联想】")
    print("=" * 65)

    client = _client()

    lower = _json(client.get("/api/securities?q=tcl"))["items"]
    upper = _json(client.get("/api/securities?q=TCL"))["items"]
    assert lower, "小写 tcl 应能命中（初版 bug：大小写不一致返回 0 条）"
    assert len(lower) == len(upper), "大小写结果数应一致"
    assert lower[0]["code"] == "000100.SZ", "代码前缀命中应排在最前"
    print("  -> tcl / TCL 命中数一致 (%d)，首条 %s" % (len(lower), lower[0]["code"]))

    # 代码前缀必须排在名称包含之前
    items = _json(client.get("/api/securities?q=600"))["items"]
    assert items, "代码前缀 600 应命中"
    assert all(item["code"].startswith("600") for item in items), \
        "代码前缀命中的应全部以 600 开头，且排在最前"
    print("  -> 关键词 600 的前 %d 条全部为代码前缀命中" % len(items))

    chinese = _json(client.get("/api/securities?q=%E8%8C%85%E5%8F%B0"))["items"]
    assert len(chinese) == 1 and chinese[0]["code"] == "600519.SH"
    print("  -> 中文联想 茅台 -> %s %s" % (chinese[0]["code"], chinese[0]["name"]))


def run_name_resolve_tests():
    """阶段五：中文名 -> 代码解析"""
    print("\n" + "=" * 65)
    print("【阶段五：中文名解析】")
    print("=" * 65)

    client = _client()

    maotai = _json(client.get("/api/percentile?code=%E8%8C%85%E5%8F%B0"))
    assert maotai["code"] == "600519.SH", "茅台应解析为 600519.SH"
    assert maotai["security"]["name"] == "贵州茅台"
    assert maotai["results"], "解析成功后应有分位结果"
    print("  -> 茅台 -> %s %s，PE-TTM 分位 %s" % (
        maotai["code"], maotai["security"]["name"], maotai["results"][0]["percentile"]))

    ambiguous = client.get("/api/percentile?code=%E5%B9%B3%E5%AE%89")
    assert ambiguous.status_code == 400
    body = _json(ambiguous)
    assert "匹配到多只标的" in body["error"], "多命中应给出可操作的提示"
    print("  -> 平安（多命中）400: %s" % body["error"][:56])

    missing = client.get("/api/percentile?code=%E4%B8%8D%E5%AD%98%E5%9C%A8%E5%85%AC%E5%8F%B8")
    assert missing.status_code == 400
    print("  -> 未命中名称 400: %s" % _json(missing)["error"][:40])


def run_level_consistency_tests():
    """阶段六：七档评级后端与前端口径一致"""
    print("\n" + "=" * 65)
    print("【阶段六：七档评级前后端一致】")
    print("=" * 65)

    source = open(COMMON_JS, encoding="utf-8").read()
    matches = re.findall(r'name:\s*"([^"]+)",\s*max:\s*(\d+)', source)
    assert len(matches) == 7, "前端 LEVEL7 应恰有 7 档，实际 %d" % len(matches)
    front_levels = [(name, int(maximum)) for name, maximum in matches]

    # 在每个档位边界两侧取样，逐一比对
    probes = [0.0, 9.99, 10.0, 19.99, 20.0, 39.99, 40.0, 59.99,
              60.0, 79.99, 80.0, 89.99, 90.0, 99.99, 100.0]
    for value in probes:
        expected = front_levels[-1][0]
        for name, maximum in front_levels:
            if value < maximum:
                expected = name
                break
        actual = store.percentile_level(value)
        assert actual == expected, "分位 %s 后端=%s 前端=%s" % (value, actual, expected)

    # 末档是闭区间：分位恰为 100 必须落到最后一档而不是「不可用」
    assert store.percentile_level(100.0) == front_levels[-1][0], \
        "分位 100 应落入末档 %s" % front_levels[-1][0]
    assert store.percentile_level(None) == "-"
    print("  -> %d 个边界探针全部一致，档位顺序: %s" % (
        len(probes), " / ".join(name for name, _ in front_levels)))


def run_frontend_level_tests():
    """阶段六之二：用 node 真实执行前端 levelOf，与后端逐点比对"""
    print("\n" + "=" * 65)
    print("【阶段六之二：前端 levelOf 实际执行比对】")
    print("=" * 65)

    node = shutil.which("node")
    if not node:
        print("  -> 跳过：未找到 node")
        return

    probes = [0.0, 9.99, 10.0, 19.99, 20.0, 39.99, 40.0, 59.99,
              60.0, 79.99, 80.0, 89.99, 90.0, 99.99, 100.0, None]

    # 在 Node 里给 common.js 一个最小的 window 环境，真实调用 SL.levelOf
    # （用 json.dumps 生成字面量：JSON 与 JS 语法兼容，且 None 会变成 null）
    script = (
        "global.window = {};\n"
        "require(%s);\n"
        "var probes = %s;\n"
        "var out = probes.map(function (p) {\n"
        "  var level = global.window.SL.levelOf(p);\n"
        "  return level ? level.name : null;\n"
        "});\n"
        "console.log(JSON.stringify(out));\n"
    ) % (json.dumps(COMMON_JS), json.dumps(probes))

    completed = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, "node 执行失败: %s" % completed.stderr[:300]

    front_results = json.loads(completed.stdout.strip().splitlines()[-1])
    assert len(front_results) == len(probes), "node 返回结果数不符"

    for value, front_name in zip(probes, front_results):
        back_name = store.percentile_level(value)
        expected = None if back_name == "-" else back_name
        assert front_name == expected, \
            "分位 %s 前端=%s 后端=%s" % (value, front_name, expected)

    print("  -> node 实际执行 %d 个探针，前后端档位完全一致" % len(probes))
    print("  -> 分位 100 前端=%s（应为末档，不可为 null）" % front_results[-2])


def _fetch_history(code):
    """读取单只股票估值历史并做 analyzer 要求的日期类型转换"""
    with MarketDataFacade() as facade:
        frame = facade.fetch_valuation_history(code)
    if frame.empty:
        return None
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame


def run_consensus_tests():
    """阶段七：全市场 SQL 分位与 analyzer 抽样口径一致"""
    print("\n" + "=" * 65)
    print("【阶段七：全市场分位口径抽样比对 (%d 只)】" % CONSENSUS_SAMPLE_SIZE)
    print("=" * 65)

    rows = [
        row for row in store.load_market_percentile(CONSENSUS_INDICATOR)
        if row["percentile"] is not None
    ]
    assert rows, "全市场分位结果不应为空"

    # 首、中、尾 + 确定性抽样，保证可复现
    stride = max(1, len(rows) // CONSENSUS_SAMPLE_SIZE)
    picks = [rows[index] for index in range(0, len(rows), stride)][:CONSENSUS_SAMPLE_SIZE]

    matched = 0
    for pick in picks:
        frame = _fetch_history(pick["ts_code"])
        assert frame is not None, "%s 应有估值历史" % pick["ts_code"]
        results = ValuationPercentileAnalyzer().analyze(frame)
        target = next(item for item in results if item.indicator == CONSENSUS_INDICATOR)
        assert target.percentile is not None, "%s analyzer 分位不应为 None" % pick["ts_code"]
        # store.load_market_percentile 对 SQL 结果做 round(x, 2)，
        # 因此精确比对方式是：analyzer 结果轮转两位后与 store 完全相等
        assert round(target.percentile, 2) == pick["percentile"], \
            "%s 口径不一致 analyzer=%s(舍入后 %s) sql=%s" % (
                pick["ts_code"], target.percentile,
                round(target.percentile, 2), pick["percentile"])
        assert target.sample_count == pick["sample_count"], \
            "%s 样本数不一致" % pick["ts_code"]
        matched += 1

    print("  -> %d/%d 只口径一致（分位与样本数双重校验）" % (matched, len(picks)))


def run_market_ranking_tests():
    """阶段八：全市场排行的排序 / 过滤 / 分页"""
    print("\n" + "=" * 65)
    print("【阶段八：全市场排行】")
    print("=" * 65)

    client = _client()

    first = _json(client.get("/api/market/ranking?limit=20"))
    summary = first["summary"]
    assert first["total"] > 1000, "全市场标的数应远大于 1000"
    assert sum(summary["histogram"]) == summary["available"], \
        "直方图总数应等于可用分位数"
    assert sum(summary["level_counts"].values()) == summary["total"], \
        "七档计数总和应等于统计标的数"
    print("  -> total=%d available=%d 中位分位=%s 直方图合计=%d" % (
        summary["total"], summary["available"],
        summary["median_percentile"], sum(summary["histogram"])))

    # 升序：非空分位必须递增，且 null 恒排最后
    ascending = _json(client.get("/api/market/ranking?limit=100"))["items"]
    values = [item["percentile"] for item in ascending]
    null_positions = [index for index, value in enumerate(values) if value is None]
    if null_positions:
        assert null_positions[0] >= len(values) - len(null_positions), \
            "分位为 null 的记录必须排在末尾"
    finite = [value for value in values if value is not None]
    assert finite == sorted(finite), "升序排序失败"
    print("  -> 升序前 %d 条：%s，null 起始位置 %s" % (
        len(values), "%.2f ~ %.2f" % (finite[0], finite[-1]),
        null_positions[0] if null_positions else "无"))

    descending = _json(client.get(
        "/api/market/ranking?limit=50&order=desc"))["items"]
    desc_values = [item["percentile"] for item in descending if item["percentile"] is not None]
    assert desc_values == sorted(desc_values, reverse=True), "降序排序失败"
    print("  -> 降序前 5 条：%s" % [
        "%s %.1f%%" % (item["name"], item["percentile"]) for item in descending[:5]])

    # 分页：两页不重叠且可拼回
    page_a = _json(client.get("/api/market/ranking?limit=10&offset=0"))["items"]
    page_b = _json(client.get("/api/market/ranking?limit=10&offset=10"))["items"]
    codes_a = [item["ts_code"] for item in page_a]
    codes_b = [item["ts_code"] for item in page_b]
    assert not set(codes_a) & set(codes_b), "分页出现重复标的"
    print("  -> 分页 offset=0 与 offset=10 无重叠")

    # 过滤：市场与关键词
    sh = _json(client.get("/api/market/ranking?market=SH&limit=5"))
    assert all(item["market"] == "SH" for item in sh["items"]), "市场过滤失效"
    assert sh["total"] < first["total"], "SH 标的数应少于全市场"
    search = _json(client.get("/api/market/ranking?q=%E8%8C%85%E5%8F%B0&limit=10"))
    assert search["total"] == 1 and search["items"][0]["ts_code"] == "600519.SH"
    print("  -> market=SH 命中 %d；搜索 茅台 命中 %d" % (sh["total"], search["total"]))

    # 指标切换
    pb = _json(client.get("/api/market/ranking?indicator=pb&limit=3"))
    assert pb["indicator_label"] and pb["indicator_labels"]
    assert len(pb["indicator_labels"]) == 5, "应下发全部 5 个指标的中文名"
    print("  -> indicator=pb 标签=%s，前 3 只 %s" % (
        pb["indicator_label"], [item["name"] for item in pb["items"]]))


def run_index_tests():
    """阶段九：指数列表与单指数详情"""
    print("\n" + "=" * 65)
    print("【阶段九：指数估值】")
    print("=" * 65)

    client = _client()
    listing = _json(client.get("/api/indices"))
    assert listing["items"], "指数列表不应为空"
    codes = [item["index_code"] for item in listing["items"]]
    assert "000300" in codes and "000016" in codes, "应包含沪深300与上证50"
    for item in listing["items"]:
        assert item["member_count"] > 0
        assert item["percentile"] is None or 0 <= item["percentile"] <= 100, \
            "%s 分位越界: %s" % (item["index_code"], item["percentile"])
        assert item["level7"], "%s 应有七档评级" % item["index_code"]
    print("  -> %d 个指数: %s" % (len(listing["items"]), [
        "%s %.1f%%(%s)" % (item["index_name"], item["percentile"], item["level7"])
        for item in listing["items"]]))

    detail = _json(client.get("/api/index/detail?code=000300"))
    assert detail["results"], "沪深300 详情应有结果"
    assert detail["history"]["dates"], "沪深300 详情应有序列"
    assert detail["interval_text"]
    assert len(detail["history"]["series"]["pe_ttm"]) == len(detail["history"]["dates"])
    print("  -> 沪深300 序列 %d 个日期，PE-TTM 当前 %s 分位 %s" % (
        len(detail["history"]["dates"]),
        detail["results"][0]["current_value"],
        detail["results"][0]["percentile"]))

    # 指标切换应仍返回合法分位
    pb = _json(client.get("/api/index/detail?code=000300&indicator=pb"))
    assert pb["results"], "沪深300 PB 序列应有结果"
    print("  -> 沪深300 PB 分位 %s" % pb["results"][0]["percentile"])


def run_percentile_detail_tests():
    """阶段十：个股分位详情、走势图与多窗口"""
    print("\n" + "=" * 65)
    print("【阶段十：个股分位详情与多窗口】")
    print("=" * 65)

    client = _client()
    payload = _json(client.get("/api/percentile?code=600519.SH"))

    assert payload["code"] == "600519.SH"
    assert payload["security"]["name"] == "贵州茅台"
    assert payload["interval_text"] and payload["sample_rows"] > 0
    assert payload["priority"], "应回显取数优先级"

    labels = [item["label"] for item in payload["results"]]
    assert labels == ["PE-TTM", "PE(静)", "市净率 PB", "市销率 PS", "市现率 PCF"], \
        "指标顺序或标签不符: %s" % labels
    print("  -> 指标顺序: %s" % labels)

    for item in payload["results"]:
        assert item["level7"], "%s 应有七档评级" % item["label"]
        if item["percentile"] is not None:
            assert 0 <= item["percentile"] <= 100, "%s 分位越界" % item["label"]
            assert item["percentile"] == round(item["percentile"], 2), \
                "%s 分位应轮转到 2 位小数" % item["label"]
    print("  -> 七档评级与数值轮转检查通过")

    history = payload["history"]
    assert history["dates"], "走势图应有日期轴"
    for indicator, series in history["series"].items():
        assert len(series) == len(history["dates"]), "%s 序列长度与日期轴不一致" % indicator
    print("  -> 走势图 %d 个日期，序列 %s" % (
        len(history["dates"]), {key: len(value) for key, value in history["series"].items()}))

    windows = payload["windows"]
    assert [window["label"] for window in windows] == ["全部", "近十年", "近五年", "近三年"], \
        "窗口标签或顺序不符: %s" % [window["label"] for window in windows]
    rows_seen = [window["sample_rows"] for window in windows]
    assert rows_seen == sorted(rows_seen, reverse=True), "回溯窗口越短样本应越少"
    for window in windows:
        assert "pe_ttm" in window["percentiles"], "%s 缺少 pe_ttm 分位" % window["label"]
    print("  -> 多窗口样本数递减: %s" % rows_seen)
    print("  -> 各窗口 PE-TTM 分位: %s" % [
        "%s=%s%%" % (window["label"], window["percentiles"]["pe_ttm"])
        for window in windows])

    # 区间裁剪必须同时作用于走势图与分位统计
    clipped = _json(client.get(
        "/api/percentile?code=600519.SH&start_date=2020-01-01&end_date=2024-12-31"))
    assert clipped["sample_rows"] < payload["sample_rows"], "区间裁剪未生效"
    # 区间文案由 core 层按「区间内实际首个/末个交易日」生成，2020-01-01 是元旦非交易日，
    # 因此这里只校验文案落在 2020-01 ~ 2024-12 这一裁剪窗口内
    assert clipped["interval_text"].startswith("2020-01"), \
        "区间文案未反映起始日期: %s" % clipped["interval_text"]
    assert "~ 2024-12" in clipped["interval_text"], \
        "区间文案未反映结束日期: %s" % clipped["interval_text"]
    print("  -> 区间裁剪: %d -> %d 行，文案 %s" % (
        payload["sample_rows"], clipped["sample_rows"], clipped["interval_text"]))

    # 覆盖当前值的参数应生效
    override = _json(client.get("/api/percentile?code=600519.SH&pe_ttm=100"))
    pe_item = next(item for item in override["results"] if item["label"] == "PE-TTM")
    assert pe_item["current_value"] == 100, "pe_ttm 覆盖未生效"
    print("  -> 覆盖当前值 pe_ttm=100 生效，分位 %s" % pe_item["percentile"])


def main():
    """按阶段顺序执行全部用例"""
    global _APP
    logging.basicConfig(level=logging.ERROR)

    _APP = create_app()
    # store 的只读聚合与证券表查询依赖 current_app，必须在应用上下文内跑
    with _APP.app_context():
        run_route_tests()
        run_validation_tests()
        run_missing_data_tests()
        run_suggest_tests()
        run_name_resolve_tests()
        run_level_consistency_tests()
        run_frontend_level_tests()
        run_consensus_tests()
        run_market_ranking_tests()
        run_index_tests()
        run_percentile_detail_tests()

    print("\n" + "=" * 65)
    print("全部测试通过")
    print("=" * 65)


if __name__ == "__main__":
    main()
