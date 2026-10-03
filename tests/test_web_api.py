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
   11. 选股器：因子覆盖率如实标注、算子表、模板就绪度、漏斗、散点、亏损股剔除
   12. 多股对比：分位与个股页逐字一致、序列按交易日对齐（缺失断线不填充）、区间裁剪
   13. 组合监控：codes 精确过滤与 q 模糊搜索语义区分、自选分位与个股页一致
   14. 个股页横向位置：名次越界/直方图合计自检、与 /market 页同源、与自身分位并存

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

# 七档评级语义色：对比页的走势分类色不得与之重复（红绿会暗示优劣）
SL_LEVEL_COLORS = (
    "#15803d", "#16a34a", "#65a30d", "#ca8a04",
    "#ea580c", "#dc2626", "#991b1b",
)

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
    for path in ("/", "/market", "/indices", "/industries", "/screener", "/favicon.ico"):
        response = client.get(path)
        assert response.status_code == 200, "%s 返回 %s" % (path, response.status_code)
        print("  -> %-14s 200, %d bytes" % (path, len(response.data)))

    health = _json(client.get("/api/health"))
    assert health["status"] == "ok"
    assert health["db_path"]
    assert health["data_as_of"], "健康检查应带数据截止日期"
    print("  -> /api/health  status=%s db=%s as_of=%s" % (
        health["status"], health["db_path"], health["data_as_of"]))


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

    # 评级过滤：只返回该档标的，且 summary 仍按全市场口径统计
    low = _json(client.get("/api/market/ranking?level=%E6%9E%81%E5%BA%A6%E4%BD%8E%E4%BC%B0&limit=200"))
    assert low["total"] > 0, "极度低估档应有标的"
    assert all(item["level"] == "极度低估" for item in low["items"]), "评级过滤失效"
    assert sum(low["summary"]["level_counts"].values()) == low["summary"]["total"], \
        "summary 应按过滤前口径统计"
    print("  -> level=极度低估 命中 %d 只（summary total=%d）" % (
        low["total"], low["summary"]["total"]))

    bad_level = client.get("/api/market/ranking?level=不存在")
    assert bad_level.status_code == 400, "非法评级应返回 400"
    print("  -> 非法评级参数正确拒绝")


def run_industry_tests():
    """阶段九：行业估值横截面（层级切换 / 汇总 / 排序）"""
    print("\n" + "=" * 65)
    print("【阶段九：行业估值】")
    print("=" * 65)

    client = _client()

    body = _json(client.get("/api/industries?level=1"))
    assert body["items"], "一级行业不应为空"
    assert body["stat_date"], "应返回统计日期"
    assert body["summary"]["industry_count"] == len(body["items"]), \
        "汇总行业数应等于条目数"
    for item in body["items"]:
        assert item["industry_name"], "行业名不应为空"
        assert item["pe_median"] is None or item["pe_median"] > 0, \
            "PE 中位数应为正或空: %s" % item["industry_name"]
    # 按 PE 中位数升序（缺失排最后）
    medians = [item["pe_median"] for item in body["items"] if item["pe_median"] is not None]
    assert medians == sorted(medians), "行业应按 PE 中位数升序"
    print("  -> 一级行业 %d 个，统计日 %s，行业中位 PE %s" % (
        len(body["items"]), body["stat_date"], body["summary"]["median_pe"]))

    # 层级切换：二级行业数量应多于一级
    level2 = _json(client.get("/api/industries?level=2"))
    assert len(level2["items"]) > len(body["items"]), "二级行业应多于一级"
    print("  -> 二级行业 %d 个" % len(level2["items"]))

    # 非法层级 400
    bad = client.get("/api/industries?level=9")
    assert bad.status_code == 400, "level=9 应返回 400"
    bad2 = client.get("/api/industries?level=abc")
    assert bad2.status_code == 400, "level=abc 应返回 400"
    print("  -> 非法层级参数正确拒绝")


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


def run_screener_tests():
    """阶段十二：选股器元数据与执行接口"""
    print("\n" + "=" * 65)
    print("【阶段十二：选股器】")
    print("=" * 65)

    client = _client()

    # 元数据：因子覆盖率 / 算子 / 模板 / 时点
    meta = _json(client.get("/api/screener/meta"))
    assert meta["as_of"], "元数据应给出默认研究时点"
    assert meta["factors"], "因子清单不应为空"
    assert meta["operators"], "算子表不应为空"
    assert meta["presets"], "预置模板不应为空"
    assert meta["universe_size"] > 0, "股票池不应为空"

    # 算子表必须与 stocklab.screener 的 9 种算子一致
    assert len(meta["operators"]) == 9, "算子应恰有 9 种，实际 %d" % len(meta["operators"])
    for item in meta["operators"]:
        assert item["label"], "算子 %s 缺中文名" % item["key"]
        assert item["needs_value"] == (item["key"] not in ("isna", "notna")), \
            "算子 %s 的 needs_value 不对" % item["key"]

    # 覆盖率必须如实反映数据现状：pe_ttm / pb 可用，其余多为无数据
    by_name = {item["name"]: item for item in meta["factors"]}
    assert by_name["pe_ttm"]["availability"] == "ready", "pe_ttm 应为全市场可用"
    assert by_name["pe_ttm"]["coverage"] > 99, "pe_ttm 覆盖率应接近 100%"
    assert by_name["pb"]["availability"] == "ready", "pb 应为可用"
    # 无数据源的因子必须标 none，且覆盖率不为正
    for name in ("roe", "dividend_yield", "momentum_12m"):
        if name in by_name:
            assert by_name[name]["availability"] in ("none", "sparse"), \
                "%s 应标为无数据/稀疏，实际 %s" % (name, by_name[name]["availability"])

    # 模板就绪度：引用了无数据因子的模板必须标 needs_data
    for preset in meta["presets"]:
        assert preset["readiness"] in ("ready", "needs_data"), preset["key"]
        if preset["readiness"] == "needs_data":
            referenced = [
                rule["factor"]
                for rule in preset["spec"]["rules"]
                if by_name.get(rule["factor"], {}).get("availability") != "ready"
            ]
            assert referenced, "模板 %s 标为 needs_data 却没引用无数据因子" % preset["key"]
    print("  -> meta：%d 个因子 / %d 个算子 / %d 个模板，pe_ttm 覆盖 %.1f%%" % (
        len(meta["factors"]), len(meta["operators"]), len(meta["presets"]),
        by_name["pe_ttm"]["coverage"]))

    # 执行：双因子条件
    spec = {
        "rules": [
            {"factor": "pe_ttm", "operator": "lt", "value": 15},
            {"factor": "pb", "operator": "lt", "value": 1.5},
        ]
    }
    result = _json(client.post("/api/screener/run", json={
        "spec": spec, "as_of": meta["as_of"], "sort": "pe_ttm",
        "order": "asc", "limit": 50,
    }))
    assert result["counts"]["total"] == meta["universe_size"], "总数应等于股票池"
    assert result["counts"]["passed"] > 0, "估值双低条件应能筛出标的"
    assert result["rows"], "应有结果行"
    assert result["rows"][0]["pe_ttm"] <= 15, "首行应满足 PE 条件"
    assert len(result["rows"]) <= 50, "limit 应生效"
    # 默认剔除亏损股：spec 回显里应多出 pe_ttm > 0
    assert result["positive_only"] is True
    assert any(
        rule["factor"] == "pe_ttm" and rule["operator"] == "gt" and rule["value"] == 0
        for rule in result["spec"]["rules"]
    ), "默认应注入 pe_ttm > 0 规则"
    assert "pe_ttm" in result["condition"] and "AND" in result["condition"]
    print("  -> run：%s，条件 %s" % (result["counts"], result["condition"]))

    # 漏斗：逐条规则的通过数 + 是否在帧内
    funnel = result["funnel"]
    assert len(funnel) == 3, "漏斗应含注入的 1 条 + 用户的 2 条，实际 %d" % len(funnel)
    for item in funnel:
        assert item["in_frame"] is True
        assert item["passed"] <= result["counts"]["total"]
    # 亏损股剔除：只影响总通过数，不影响单条规则的独立漏斗
    # （漏斗口径是「每条规则各自在全池上判定」，与 AND 组合结果本就不是同一个数）
    keep_loss = _json(client.post("/api/screener/run", json={
        "spec": spec, "as_of": meta["as_of"], "positive_only": False, "limit": 1,
    }))
    assert keep_loss["positive_only"] is False
    # 不剔除时不应注入 pe_ttm > 0，漏斗也少一条
    assert len(keep_loss["funnel"]) == 2, "不剔除时漏斗应为用户自己的 2 条"
    assert not any(
        rule["factor"] == "pe_ttm" and rule["value"] == 0
        for rule in keep_loss["spec"]["rules"]
    ), "不剔除时不应注入 pe_ttm > 0"
    # 亏损股（PE≤0）会被「PE<15」误纳，所以含亏损股的通过数必然更多
    assert keep_loss["counts"]["passed"] > result["counts"]["passed"], \
        "含亏损股时通过数应更多（%s vs %s）" % (
            keep_loss["counts"]["passed"], result["counts"]["passed"])

    # 漏斗语义自证：pe_ttm>0 通过数 = 剔除亏损股后的候选池，应大于总通过数
    positive_row = [f for f in funnel if f["factor"] == "pe_ttm" and f["operator"] == "gt"][0]
    assert positive_row["passed"] > result["counts"]["passed"], \
        "PE>0 的通过数应大于 AND 组合后的通过数"
    print("  -> 漏斗 3 条（含注入的 PE>0：%d 只）；含亏损股通过 %d 只 vs 剔除后 %d 只" % (
        positive_row["passed"], keep_loss["counts"]["passed"], result["counts"]["passed"]))

    # 散点：两个有数据的因子才出图
    scatter = result["scatter"]
    assert scatter["points"], "双因子条件应产出散点"
    assert scatter["x"] and scatter["y"] and scatter["x"] != scatter["y"]
    for point in scatter["points"]:
        assert len(point) == 5, "散点应为 [x, y, passed, code, name]"
        assert isinstance(point[2], bool)
    # 单因子条件没有第二个可用轴 -> 散点为空（前端隐藏该卡片）
    single = _json(client.post("/api/screener/run", json={
        "spec": {"rules": [{"factor": "pe_ttm", "operator": "lt", "value": 15}]},
        "as_of": meta["as_of"], "limit": 5,
    }))
    assert single["scatter"]["points"] == [], "单因子不应产出散点"
    print("  -> 散点：%s vs %s 共 %d 点；单因子时正确为空" % (
        scatter["x"], scatter["y"], len(scatter["points"])))

    # 参数校验
    for payload, label in (
        ({}, "空体"),
        ({"spec": {}}, "spec 非对象"),
        ({"spec": {"rules": []}}, "rules 为空"),
        ({"spec": spec, "order": "x"}, "order 非法"),
        ({"spec": spec, "limit": 0}, "limit 非法"),
    ):
        response = client.post("/api/screener/run", json=payload)
        assert response.status_code == 400, "%s 应返回 400，实际 %s" % (
            label, response.status_code)
        assert "error" in _json(response), "%s 应带 error 字段" % label
    print("  -> 5 组参数校验全部 400")

    # 未登记因子 / 规则超限
    unknown = client.post("/api/screener/run", json={
        "spec": {"rules": [{"factor": "no_such_factor", "operator": "lt", "value": 1}]},
    })
    assert unknown.status_code == 400, "未登记因子应 400"
    too_many = client.post("/api/screener/run", json={
        "spec": {"rules": [{"factor": "pe_ttm", "operator": "lt", "value": i} for i in range(13)]},
    })
    assert too_many.status_code == 400, "超过 12 条规则应 400"
    print("  -> 未登记因子 / 规则超限 均 400")

    # 页面要素齐备
    page = client.get("/screener").get_data(as_text=True)
    for element_id in ("rule-list", "preset-select", "chk-positive", "as-of-select",
                       "funnel-list", "scatter-chart", "industry-list", "btn-run"):
        assert 'id="%s"' % element_id in page, "选股器页面缺少 %s" % element_id
    assert "/static/screener.js" in page, "选股器页面未引入脚本"
    assert 'class="nav-link active"' in page, "选股器页面导航未高亮"
    print("  -> 页面要素与导航高亮齐备")


def run_compare_tests():
    """阶段十三：多股对比接口"""
    print("\n" + "=" * 65)
    print("【阶段十三：多股对比】")
    print("=" * 65)

    client = _client()

    # 三只标的 + 近十年
    codes = "600519.SH,000001.SZ,300750.SZ"
    data = _json(client.get("/api/compare?codes=%s&period=近十年" % codes))
    assert data["codes"] == ["600519.SH", "000001.SZ", "300750.SZ"], data["codes"]
    assert data["rows"], "应返回标的行"
    assert len(data["rows"]) == 3
    assert data["interval_text"] == "近十年"
    assert data["sample_rows"] > 0, "应对齐出交易日"
    assert data["indicator_labels"], "应下发指标中文名"

    # 每只标的都要有名称与市场
    for row in data["rows"]:
        assert row["ts_code"] in data["codes"]
        assert row["metrics"], "%s 应有指标结论" % row["ts_code"]
        assert row["sample_rows"] > 0, "%s 应有样本" % row["ts_code"]

    # 分类色：逐只不同，且与七档语义色无关（不能用红绿暗示优劣）
    colors = [entry["color"] for entry in data["names"]]
    assert len(set(colors)) == len(colors), "分类色应互不相同"
    assert not any(color in SL_LEVEL_COLORS for color in colors), \
        "走势分类色不应复用七档语义色"
    print("  -> 3 只标的 / 区间 %s / 交易日 %d，分类色 %s" % (
        data["interval_text"], data["sample_rows"], colors))

    # 分位口径必须与个股页一致：同一 analyzer + 同一计算窗口
    # 注意「同窗口」是前提 —— 个股页的 period 只限制向远端取数的区间，
    # 计算窗口由 start_date/end_date 决定；对比页的 period 直接决定计算窗口。
    # 因此这里用「全部」两侧对齐地比。
    full = _json(client.get("/api/compare?codes=%s&period=全部" % codes))
    for row in full["rows"]:
        pe = row["metrics"].get("pe_ttm")
        if not pe:
            continue
        single = _json(client.get(
            "/api/percentile?code=%s&period=全部" % row["ts_code"]))
        match = [item for item in single["results"] if item["indicator"] == "pe_ttm"]
        assert match, "%s 个股页应有 pe_ttm 结果" % row["ts_code"]
        assert match[0]["percentile"] == pe["percentile"], \
            "%s 分位与个股页不一致：对比 %s vs 个股 %s" % (
                row["ts_code"], pe["percentile"], match[0]["percentile"])
        assert match[0]["level7"] == pe["level7"], \
            "%s 评级与个股页不一致：%s vs %s" % (
                row["ts_code"], pe["level7"], match[0]["level7"])
        print("  -> %s 分位 %.2f%%（%s）与个股页逐字一致" % (
            row["ts_code"], pe["percentile"], pe["level7"]))

    # 窗口不同 -> 分位必然不同（证明「口径一致」不是巧合，而是同 analyzer 的结果）
    maotai_10y = [r for r in data["rows"] if r["ts_code"] == "600519.SH"][0]
    maotai_full = [r for r in full["rows"] if r["ts_code"] == "600519.SH"][0]
    assert maotai_10y["metrics"]["pe_ttm"]["percentile"] != \
        maotai_full["metrics"]["pe_ttm"]["percentile"], \
        "近十年与全部分位不应相同（否则说明窗口未生效）"

    # 序列对齐：每只长度 == 交易日数，缺失必须为 None（断线），不得前向填充
    dates = data["history"]["dates"]
    assert len(dates) == data["sample_rows"]
    for code, series in data["history"]["series"].items():
        assert len(series) == len(dates), "%s 序列长度应等于交易日数" % code
        assert all(value is None or isinstance(value, float) for value in series), \
            "%s 序列只应含数值或 None" % code
    # 上市晚的标的必然有一段前置缺失
    gaps = sum(1 for series in data["history"]["series"].values()
               for value in series if value is None)
    assert gaps > 0, "上市较晚的标的应有前置空档（未做前向填充）"
    print("  -> 序列对齐 %d 个交易日，缺失点 %d 处（断线而非填充）" % (len(dates), gaps))

    # 区间裁剪生效：近五年样本数必须少于全部
    assert full["sample_rows"] > data["sample_rows"], "全部区间交易日应更多"
    assert maotai_full["sample_rows"] > maotai_10y["sample_rows"], \
        "茅台全部区间样本数应多于近十年"
    print("  -> 区间裁剪生效：茅台样本 %d（全部） > %d（近十年）" % (
        maotai_full["sample_rows"], maotai_10y["sample_rows"]))

    # 指定单一指标
    only_pb = _json(client.get(
        "/api/compare?codes=%s&indicators=pb" % codes))
    assert only_pb["indicators"] == ["pb"], only_pb["indicators"]
    assert only_pb["primary_indicator"] == "pb"
    for row in only_pb["rows"]:
        assert set(row["metrics"].keys()) <= {"pb"}, "只应返回 pb"
    print("  -> 单指标模式：indicators=%s" % only_pb["indicators"])

    # 参数校验
    for query, label in (
        ("", "缺 codes"),
        ("codes=600519.SH", "仅 1 只"),
        ("codes=%s&indicators=nope" % codes, "非法指标"),
        ("codes=%s&period=xxx" % codes, "非法区间"),
    ):
        response = client.get("/api/compare?" + query)
        assert response.status_code == 400, "%s 应 400，实际 %s" % (
            label, response.status_code)
        assert "error" in _json(response)
    # 去重与上限
    dup = _json(client.get("/api/compare?codes=600519.SH,600519.SH,000001.SZ"))
    assert dup["codes"] == ["600519.SH", "000001.SZ"], "重复代码应去重"
    too_many = ",".join("%06d.SZ" % (300000 + i) for i in range(11))
    assert client.get("/api/compare?codes=" + too_many).status_code == 400, "超 10 只应 400"
    print("  -> 5 组参数校验 + 代码去重 + 10 只上限 均正确")

    # 页面要素
    page = client.get("/compare").get_data(as_text=True)
    for element_id in ("cmp-input", "chip-row", "cmp-chart", "radar-chart", "cmp-body"):
        assert 'id="%s"' % element_id in page, "对比页缺少 %s" % element_id
    assert "/static/compare.js" in page
    assert 'class="nav-link active"' in page, "对比页导航未高亮"
    print("  -> 页面要素与导航高亮齐备")


def run_portfolio_tests():
    """阶段十四：组合监控（自选股 codes 精确过滤 + 分位口径一致性）"""
    print("\n" + "=" * 65)
    print("【阶段十四：组合监控】")
    print("=" * 65)

    client = _client()
    codes = "600519.SH,000001.SZ,300750.SZ"

    # codes 精确过滤：只返回点名的三只
    data = _json(client.get(
        "/api/market/ranking?codes=%s&indicator=pe_ttm&limit=50" % codes))
    assert data["total"] == 3, "codes 过滤应只返回 3 只，实际 %s" % data["total"]
    assert sorted(item["ts_code"] for item in data["items"]) == \
        sorted(["600519.SH", "000001.SZ", "300750.SZ"])
    assert data["summary"]["total"] == 3, "summary 应按过滤后口径统计"

    # 过滤确实生效：远小于全市场
    full = _json(client.get("/api/market/ranking?indicator=pe_ttm&limit=1"))
    assert full["total"] > 1000, "全市场标的数应远大于 1000"
    assert data["total"] < full["total"], "codes 过滤后应显著少于全市场"
    print("  -> codes 精确过滤：%d 只（全市场 %d 只）" % (data["total"], full["total"]))

    # q 是「代码或名称的子串匹配」，codes 是「ts_code 精确集合」，两者语义不同：
# 用一个会命中多只的名称关键字证明 codes 不能被 q 替代
    fuzzy = _json(client.get("/api/market/ranking?q=银行&indicator=pe_ttm"))
    exact_code = _json(client.get("/api/market/ranking?codes=000001.SZ&indicator=pe_ttm"))
    assert fuzzy["total"] > 1, "「银行」应命中多只银行股，实际 %d" % fuzzy["total"]
    assert exact_code["total"] == 1, "codes 精确过滤应只 1 条"
    assert exact_code["items"][0]["name"] == "平安银行"
    # codes 里的票必然能在 q 的结果里按名称找到，反之不成立
    names_in_fuzzy = set(item["name"] for item in fuzzy["items"])
    assert "平安银行" in names_in_fuzzy
    assert len(names_in_fuzzy) > 1, "q 的结果应包含多只，证明它不是精确集合"
    print("  -> q 名称模糊命中 %d 只（多只），codes 精确命中 %d 只（唯一）" % (
        fuzzy["total"], exact_code["total"]))

    # 不存在的代码：返回 0 条而不是 500
    missing = _json(client.get(
        "/api/market/ranking?codes=999999.SH&indicator=pe_ttm"))
    assert missing["total"] == 0
    assert missing["items"] == []
    print("  -> 不存在的代码返回 0 条（不报错）")

    # 自选股的分位必须与个股页一致（同一 analyzer）
    for row in data["items"]:
        if row["percentile"] is None:
            continue
        single = _json(client.get(
            "/api/percentile?code=%s&period=全部" % row["ts_code"]))
        match = [item for item in single["results"] if item["indicator"] == "pe_ttm"]
        assert match, "%s 个股页应有 pe_ttm" % row["ts_code"]
        assert match[0]["percentile"] == row["percentile"], \
            "%s 自选分位(%s) 与个股页(%s) 不一致" % (
                row["ts_code"], row["percentile"], match[0]["percentile"])
        assert match[0]["level7"] == row["level"], \
            "%s 评级(%s) 与个股页(%s) 不一致" % (
                row["ts_code"], row["level"], match[0]["level7"])
    print("  -> 自选分位与评级与个股页逐字一致")

    # 两项指标都能按 codes 取（页面一次拉 PE + PB）
    pb = _json(client.get(
        "/api/market/ranking?codes=%s&indicator=pb&limit=50" % codes))
    assert pb["total"] == 3, "PB 也应支持 codes 过滤"
    assert pb["indicator"] == "pb"
    print("  -> PE / PB 两项指标均可按 codes 取数")

    # 组合 codes + market 过滤（两个条件同时生效）
    sh = _json(client.get(
        "/api/market/ranking?codes=%s&indicator=pe_ttm&market=SH" % codes))
    assert all(item["market"] == "SH" for item in sh["items"]), "codes 与 market 应叠加生效"
    print("  -> codes 与 market 过滤可叠加")

    # 页面要素
    page = client.get("/portfolio").get_data(as_text=True)
    for element_id in ("add-input", "add-suggest", "alert-indicator", "stat-grid",
                       "watch-grid", "watch-table-card", "watch-head"):
        assert 'id="%s"' % element_id in page, "组合监控页缺少 %s" % element_id
    assert "/static/portfolio.js" in page
    assert 'class="nav-link active"' in page, "组合监控页导航未高亮"
    # 自选列表纯客户端存储：存储逻辑必须在 portfolio.js 里。
# 内联脚本只有布局的防闪烁主题脚本（读 sl-theme），不得出现自选列表的存储键。
    inline_scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page, re.S)
    assert inline_scripts, "页面应有内联的首屏主题脚本"
    for script_body in inline_scripts:
        assert "sl-watchlist" not in script_body, \
            "自选列表的存储逻辑不应内联在页面脚本里"
        assert "sl-theme" in script_body or "localStorage" not in script_body, \
            "内联脚本若读 localStorage，只应是布局的主题脚本"
    assert "localStorage" in page, "页面应说明自选列表存在浏览器本地"
    print("  -> 页面要素齐备；自选存储逻辑仅在 portfolio.js（内联脚本只有主题防闪烁）")


def run_market_context_tests():
    """阶段十五：个股页全市场横向位置"""
    print("\n" + "=" * 65)
    print("【阶段十五：个股页横向位置】")
    print("=" * 65)

    client = _client()
    data = _json(client.get("/api/percentile?code=600519.SH"))
    context = data.get("market_context")
    assert context, "应答应带 market_context"
    assert set(context) == {item["indicator"] for item in data["results"]}, \
        "横向定位应覆盖与 results 相同的指标集合"

    # 有数据的指标必须给出名次；无数据的显式标不可用
    usable = 0
    for indicator, info in context.items():
        assert "available" in info, "%s 缺 available" % indicator
        if not info["available"]:
            assert info["rank"] is None, "%s 不可用时不应有名次" % indicator
            continue
        usable += 1
        assert info["rank"] and info["total"], "%s 缺名次或样本数" % indicator
        assert 1 <= info["rank"] <= info["total"], \
            "%s 名次 %s 越界（total=%s）" % (indicator, info["rank"], info["total"])
        assert len(info["histogram"]) == 10, "%s 直方图应有 10 档" % indicator
        assert sum(info["histogram"]) == info["total"], \
            "%s 直方图合计应等于样本数" % indicator
        assert all(value >= 0 for value in info["histogram"])
    assert usable > 0, "应有指标可给出横向名次"
    print("  -> 5 项指标中 %d 项可定位，名次与直方图自洽（合计 = 样本数）" % usable)

    # 横截面必须与 /market 页同源：同标的同指标同分位同评级
    for indicator, info in context.items():
        if not info["available"]:
            continue
        ranking = _json(client.get(
            "/api/market/ranking?indicator=%s&q=600519" % indicator))
        match = [item for item in ranking["items"] if item["ts_code"] == "600519.SH"]
        assert match, "%s 在 /market 页应能查到 600519" % indicator
        assert match[0]["percentile"] == info["percentile"], \
            "%s 横向分位(%s)与 /market 页(%s)不一致" % (
                indicator, info["percentile"], match[0]["percentile"])
        assert match[0]["level"] == info["level"], \
            "%s 横向评级(%s)与 /market 页(%s)不一致" % (
                indicator, info["level"], match[0]["level"])
    print("  -> 各指标横向分位/评级与 /market 页逐字一致")

    # 名次必须真的落在全市场排序里：按分位升序，茅台名次应与其分位相称
    ranking = _json(client.get(
        "/api/market/ranking?indicator=pe_ttm&limit=200&sort=percentile&order=asc"))
    pe = context["pe_ttm"]
    if pe["available"] and pe["percentile"] is not None:
        # 分位越低越便宜，名次应越靠前：名次占比不应远大于分位占比
        rank_share = pe["rank"] * 100.0 / pe["total"]
        # 名次占比与分位天然接近（同为升序位次），留 2 倍容差
        assert rank_share < max(pe["percentile"] * 2, 2.0), \
            "名次占比 %.1f%% 与分位 %.1f%% 明显不相称" % (rank_share, pe["percentile"])
    print("  -> 名次与分位相称（名次占比 %.1f%% ≈ 分位 %.1f%%）" % (
        pe["rank"] * 100.0 / pe["total"], pe["percentile"]))

    # 与自身历史分位不同：这是本区块存在的意义（两个不同问题）
    own = [item for item in data["results"] if item["indicator"] == "pe_ttm"]
    assert own, "应有 pe_ttm 自身分位"
    if own[0]["percentile"] is not None and pe["available"]:
        # 两者都是升序位次，单标的时可能恰好相等，但字段必须都存在
        assert "percentile" in pe and "percentile" in own[0]
        assert pe["median_percentile"] is None or isinstance(pe["median_percentile"], float)
    print("  -> 自身分位 %s%% 与横向分位 %s%% 同时存在（两个维度）" % (
        own[0]["percentile"], pe["percentile"]))

    # 未知代码不应崩，且横向定位应显式标不可用
    unknown = _json(client.get("/api/percentile?code=999999.SH"))
    assert unknown["results"] == [], "未知代码应返回空 results"
    assert unknown["message"], "未知代码应带提示"
    print("  -> 未知代码安全降级（空 results + 提示文案）")

    # 页面要素齐备
    page = client.get("/").get_data(as_text=True)
    for element_id in ("market-context-card", "mc-rank-value", "mc-rank-sub",
                       "mc-bars", "mc-dist-title", "mc-list"):
        assert 'id="%s"' % element_id in page, "个股页缺少 %s" % element_id
    print("  -> 页面要素齐备")


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
        run_industry_tests()
        run_index_tests()
        run_percentile_detail_tests()
        run_screener_tests()
        run_compare_tests()
        run_portfolio_tests()
        run_market_context_tests()

    print("\n" + "=" * 65)
    print("全部测试通过")
    print("=" * 65)


if __name__ == "__main__":
    main()
