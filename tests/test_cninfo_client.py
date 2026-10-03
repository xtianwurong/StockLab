#!/usr/bin/env python3
"""
==============================================================================
StockLab - 巨潮公告客户端测试 (tests/test_cninfo_client.py)
==============================================================================

【功能用途】
  覆盖免费数据源扩展需求 §44 的 CNINFO 断言：
    1. 公告 JSON 解析（契约列序、必填字段缺失行被丢弃）
    2. 分页（pageSize 服务端硬限 30、hasMore 终止、页码透传）
    3. orgId（全市场映射缓存 / topSearch 精确匹配 / 模糊命中被拒绝 / 缺失跳过）
    4. 日期换算（announcementTime 毫秒时间戳按 UTC+8 换算）
    5. title 清洗（<em> 标签与 HTML 实体）
    6. PDF URL 拼接（相对路径补 static.cninfo.com.cn 前缀）
    7. 去重（announcement_id 优先 + ts_code/日期/标题兜底）
    8. 网络异常（fixture 注入，不依赖真实断网）

【运行方式】
  ./venv/bin/python tests/test_cninfo_client.py
  （纯离线：注入 fake transport，不发起任何网络请求）
==============================================================================
"""

import json
import os
import sys
from datetime import date


import requests

# 允许直接执行本文件（./venv/bin/python tests/xxx.py）；走 pytest 时由
# pytest.ini 的 `pythonpath = .` 统一负责，不会重复插入。
if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.domain import ANNOUNCEMENT_COLUMNS
import pytest
from stocklab.datasource.cninfo_client import CninfoClient

# 贵州茅台 2026-08-15 半年报公告（2026-10-03 实测返回的真实字段形态）
MS_20260815 = 1786723200000

class FakeResponse:
    """最小 HTTP 响应桩"""

    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

class FakeTransport:
    """
    按 URL 分发 fixture 的传输层桩

    Args:
        query_pages: {页号: (公告列表, hasMore)} -> hisAnnouncement/query
        stock_map: dict -> szse_stock.json 的 stockList
        top_search: list -> topSearch 命中列表
        fail: True -> 所有请求抛网络异常
    """

    def __init__(self, query_pages=None, stock_map=None, top_search=None, fail=False):
        self.query_pages = query_pages or {}
        # 缺省给两只常用股票的映射（显式传 {} 可覆盖为空，用于测试解析失败路径）
        self.stock_map = stock_map if stock_map is not None else {
            "600519": "gssh0600519",
            "000001": "gssz0000001",
        }
        self.top_search = top_search if top_search is not None else []
        self.fail = fail
        self.posts = []
        self.gets = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.posts.append((url, dict(data or {})))
        if self.fail:
            raise requests.ConnectionError("fixture injected failure")
        if "topSearch" in url:
            return FakeResponse(json.dumps(self.top_search))
        if "hisAnnouncement/query" in url:
            page = int(data.get("pageNum", "1"))
            rows, has_more = self.query_pages.get(page, ([], False))
            payload = {
                "totalAnnouncement": sum(len(v[0]) for v in self.query_pages.values()),
                "hasMore": has_more,
                "announcements": rows,
            }
            return FakeResponse(json.dumps(payload))
        return FakeResponse("")

    def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        if self.fail:
            raise requests.ConnectionError("fixture injected failure")
        if "szse_stock.json" in url:
            rows = [{"code": code, "orgId": org_id}
                    for code, org_id in self.stock_map.items()]
            return FakeResponse(json.dumps({"stockList": rows}))
        return FakeResponse("")

def _announcement(sec_code, title, ms, announcement_id, adjunct_url, category=None):
    """构造一条原始公告 JSON（字段与实测返回一致）"""
    return {
        "secCode": sec_code,
        "secName": "示例",
        "announcementId": announcement_id,
        "announcementTitle": title,
        "announcementTime": ms,
        "adjunctUrl": adjunct_url,
        "announcementTypeName": category,
    }

def _client(**kwargs):
    """构造注入了 fake transport 的客户端，并清空模块级缓存保证测试隔离"""
    kwargs.setdefault("sleep_seconds", 0)
    transport = kwargs.pop("transport")
    client = CninfoClient(transport=transport, **kwargs)
    client.clear_cache()
    return client

def test_field_parsing():
    """测试单条公告解析：契约列、时间换算、标题清洗、PDF URL、category"""

    transport = FakeTransport(
        query_pages={
            1: ([_announcement(
                "600519",
                "<em>贵州茅台2026年半年度报告</em>",
                MS_20260815,
                "1225475868",
                "finalpage/2026-08-15/1225475868.PDF",
            )], False)
        }
    )
    client = _client(transport=transport)
    frame = client.get_announcements("600519.SH", "2026-08-01", "2026-08-31", page=1)

    assert len(frame) == 1, frame
    # 列序必须是 announcement 契约的前 9 列（其余两列由同步阶段补齐）
    assert list(frame.columns) == list(ANNOUNCEMENT_COLUMNS[:9]), list(frame.columns)

    row = frame.iloc[0]
    assert row["announcement_id"] == "1225475868"
    assert row["ts_code"] == "600519.SH"
    # 毫秒时间戳按 UTC+8 换算 -> 2026-08-15（与 PDF URL 日期一致）
    assert row["announcement_date"] == date(2026, 8, 15), row["announcement_date"]
    assert row["publish_time"] == MS_20260815
    # <em> 高亮标签必须清洗掉
    assert row["title"] == "贵州茅台2026年半年度报告", row["title"]
    # 相对路径补 static 前缀
    assert row["pdf_url"] == "http://static.cninfo.com.cn/finalpage/2026-08-15/1225475868.PDF"
    assert row["source"] == "cninfo"
    # announcementTypeName 缺失 -> 允许 NULL（不是空串）
    assert row["category"] is None, row["category"]
    print("  -> 契约列序 / 时间换算 / 标题清洗 / PDF URL / NULL category 全部正确")

    # 绝对 URL 不重复拼前缀；category 有值时保留；HTML 实体反转义
    transport = FakeTransport(
        query_pages={
            1: ([_announcement(
                "000001",
                "关于召开股东大会 &amp; 议案的通知&#40;公告&#41;",
                MS_20260815,
                "999001",
                "http://other.cdn/x.PDF",
                category="临时公告",
            )], False)
        }
    )
    client = _client(transport=transport)
    frame = client.get_announcements("000001.SZ", "2026-08-01", "2026-08-31")
    row = frame.iloc[0]
    assert row["pdf_url"] == "http://other.cdn/x.PDF", row["pdf_url"]
    assert row["category"] == "临时公告"
    assert row["title"] == "关于召开股东大会 & 议案的通知(公告)", row["title"]
    assert row["ts_code"] == "000001.SZ"

    # 关键字段缺失的脏数据整行丢弃，不能写进库里
    transport = FakeTransport(
        query_pages={
            1: ([
                _announcement("", "无代码公告", MS_20260815, "1", "a.PDF"),
                _announcement("600519", "  ", MS_20260815, "2", "b.PDF"),
                _announcement("600519", "无时间", None, "3", "c.PDF"),
                _announcement("600519", "无 ID", MS_20260815, "", "d.PDF"),
                _announcement("600519", "正常公告", MS_20260815, "4", "e.PDF"),
            ], False)
        }
    )
    client = _client(transport=transport)
    frame = client.get_announcements("600519.SH", "2026-08-01", "2026-08-31")
    assert len(frame) == 1, frame
    assert frame.iloc[0]["title"] == "正常公告"
    print("  -> 脏数据（缺代码/空标题/缺时间/缺 ID）5 取 1，全部被丢弃")

def test_pagination():
    """测试分页：页码透传、hasMore 终止、满 30 条才可能有下一页"""

    page1 = [
        _announcement("600519", "公告-%02d" % i, MS_20260815, "id-%02d" % i,
                      "f/%02d.PDF" % i)
        for i in range(30)
    ]
    page2 = [
        _announcement("600519", "公告-b%02d" % i, MS_20260815, "id-b%02d" % i,
                      "f/b%02d.PDF" % i)
        for i in range(7)
    ]
    transport = FakeTransport(query_pages={1: (page1, True), 2: (page2, False)})
    client = _client(transport=transport)

    frame = client.fetch_announcements("600519.SH", "2026-01-01", "2026-10-03")
    assert len(frame) == 37, len(frame)

    query_posts = [post for post in transport.posts if "hisAnnouncement/query" in post[0]]
    assert len(query_posts) == 2, len(query_posts)
    assert query_posts[0][1]["pageNum"] == "1"
    assert query_posts[1][1]["pageNum"] == "2"
    # pageSize 服务端硬限 30
    assert query_posts[0][1]["pageSize"] == "30"
    assert query_posts[0][1]["seDate"] == "2026-01-01~2026-10-03"
    assert query_posts[0][1]["stock"] == "600519,gssh0600519"
    print("  -> 2 页翻页、pageNum 透传、pageSize=30、seDate/stock 组合全部正确")

    # 单页不满 30 条：即使服务端 hasMore=true 也必须停止（防死循环）
    transport = FakeTransport(query_pages={1: (page2, True)})
    client = _client(transport=transport)
    frame = client.fetch_announcements("600519.SH", "2026-01-01", "2026-10-03")
    assert len(frame) == 7, len(frame)
    assert len([p for p in transport.posts if "hisAnnouncement/query" in p[0]]) == 1
    print("  -> 本页 < 30 条即终止（hasMore 不可信时也不会死循环）")

    # 全市场模式（不传 symbol）：stock 参数留空、column 走 szse
    transport = FakeTransport(query_pages={1: (page2, False)})
    client = _client(transport=transport)
    frame = client.fetch_announcements(None, "2026-09-28", "2026-10-03")
    assert len(frame) == 7
    query_post = [p for p in transport.posts if "hisAnnouncement/query" in p[0]][0]
    assert query_post[1]["stock"] == ""
    assert query_post[1]["column"] == "szse"
    print("  -> 全市场模式：不带 stock 参数、column=szse")

    # 沪市单只查询走 column=sse
    transport = FakeTransport(query_pages={1: (page2, False)})
    client = _client(transport=transport)
    client.get_announcements("600519.SH", "2026-01-01", "2026-10-03")
    assert [p for p in transport.posts if "hisAnnouncement/query" in p[0]][0][1]["column"] == "sse"
    print("  -> 沪市 column=sse / 深市与全市场 column=szse 路由正确")

def test_org_id():
    """测试 orgId 解析：映射缓存 / topSearch 精确匹配 / 模糊命中拒绝 / 失败降级"""

    # 1) 全市场映射命中（szse_stock.json）
    transport = FakeTransport(
        stock_map={"600519": "gssh0600519", "000001": "gssz0000001"}
    )
    client = _client(transport=transport)
    assert client.resolve_org_id("600519.SH") == "gssh0600519"
    assert client.resolve_org_id("000001") == "gssz0000001"
    map_gets = [url for url in transport.gets if "szse_stock.json" in url]
    assert len(map_gets) == 1, "全市场映射必须只拉一次（模块级缓存）"
    # 二次查询直接命中缓存，不再请求
    assert client.resolve_org_id("600519") == "gssh0600519"
    assert len([url for url in transport.gets if "szse_stock.json" in url]) == 1
    print("  -> 全市场映射只拉一次，二次命中缓存")

    # 2) 映射缺失 -> topSearch 精确匹配（920000 模糊命中 832000 必须被拒绝）
    transport = FakeTransport(
        stock_map={},
        top_search=[{"code": "832000", "orgId": "gfbj0832000", "zwjc": "安徽凤凰"}],
    )
    client = _client(transport=transport)
    assert client.resolve_org_id("920000") == "", "模糊命中不得接受"
    top_posts = [p for p in transport.posts if "topSearch" in p[0]]
    assert len(top_posts) == 1 and top_posts[0][1]["keyWord"] == "920000"
    print("  -> topSearch 模糊命中（920000 -> 832000）被拒绝")

    transport = FakeTransport(
        stock_map={},
        top_search=[{"code": "920819", "orgId": "gfbj0920819", "zwjc": "示例"}],
    )
    client = _client(transport=transport)
    assert client.resolve_org_id("920819") == "gfbj0920819"
    print("  -> topSearch 精确匹配命中")

    # 3) 单只查询但 orgId 解析失败：必须跳过查询（否则会拉回全市场脏数据）
    transport = FakeTransport(stock_map={}, top_search=[])
    client = _client(transport=transport)
    assert client.resolve_org_id("600519") == ""
    frame = client.fetch_announcements("600519.SH", "2026-01-01", "2026-10-03")
    assert frame.empty
    assert not [p for p in transport.posts if "hisAnnouncement/query" in p[0]], \
        "orgId 缺失时禁止发起公告查询"
    print("  -> orgId 缺失时跳过该股查询，不拉回全市场数据")

    # 4) 解析失败结果被缓存（不再重复请求 topSearch）
    client.resolve_org_id("600519")
    assert len([p for p in transport.posts if "topSearch" in p[0]]) == 1
    print("  -> 失败结果同样缓存，不重复打接口")

def test_dedup():
    """测试去重：同 announcementId 跨页 / 不同 id 同标题同日期"""

    # 同一 announcementId 出现在两页 -> 只留 1 条
    dup = _announcement("600519", "重复公告", MS_20260815, "same-id", "a.PDF")
    other = _announcement("600519", "另一页公告", MS_20260815, "other-id", "b.PDF")
    transport = FakeTransport(query_pages={1: ([dup] * 30, True), 2: ([dup, other], False)})
    client = _client(transport=transport)
    frame = client.fetch_announcements("600519.SH", "2026-01-01", "2026-10-03")
    assert len(frame) == 2, len(frame)
    assert sorted(frame["announcement_id"].tolist()) == ["other-id", "same-id"]
    print("  -> 跨页相同 announcementId 只保留 1 条")

    # 不同 announcementId 但 (ts_code, 日期, 标题) 相同 -> 去重兜底生效
    twin_a = _announcement("600519", "同内容公告", MS_20260815, "id-a", "x.PDF")
    twin_b = _announcement("600519", "同内容公告", MS_20260815, "id-b", "y.PDF")
    transport = FakeTransport(
        query_pages={1: ([twin_a] + [twin_b] + [_announcement(
            "600519", "第%d条" % i, MS_20260815, "pad-%d" % i, "p.PDF"
        ) for i in range(28)], False)}
    )
    client = _client(transport=transport)
    frame = client.fetch_announcements("600519.SH", "2026-01-01", "2026-10-03")
    titles = frame["title"].tolist()
    assert titles.count("同内容公告") == 1, titles
    assert len(frame) == 29, len(frame)
    print("  -> 不同 announcementId 的同内容公告按 (ts_code, 日期, 标题) 去重")

def test_failure():
    """测试网络异常与错误响应（fixture 注入，不依赖真实断网）"""

    # 连接异常 -> 空表，不抛出
    transport = FakeTransport(fail=True)
    client = _client(transport=transport)
    assert client.fetch_announcements("600519.SH", "2026-01-01", "2026-10-03").empty
    assert client.get_announcements("600519.SH", "2026-01-01", "2026-10-03").empty
    assert client.resolve_org_id("600519") == ""

    # HTTP 5xx -> 空表
    class ErrorTransport(FakeTransport):
        def post(self, url, headers=None, data=None, timeout=None):
            return FakeResponse("server error", status_code=500)

        def get(self, url, headers=None, timeout=None):
            return FakeResponse("server error", status_code=500)

    client = _client(transport=ErrorTransport())
    assert client.fetch_announcements("600519.SH", "2026-01-01", "2026-10-03").empty

    # 非 JSON 响应 -> 空表
    class HtmlTransport(FakeTransport):
        def post(self, url, headers=None, data=None, timeout=None):
            return FakeResponse("<html>blocked</html>")

        def get(self, url, headers=None, timeout=None):
            return FakeResponse("<html>blocked</html>")

    client = _client(transport=HtmlTransport())
    assert client.fetch_announcements("600519.SH", "2026-01-01", "2026-10-03").empty
    assert client.resolve_org_id("600519") == ""
    print("  -> 连接异常 / HTTP 5xx / 非 JSON 响应 全部安全降级为空表")

    # 接口返回错误报文
    class ErrorPayload(FakeTransport):
        def post(self, url, headers=None, data=None, timeout=None):
            if "hisAnnouncement/query" in url:
                return FakeResponse(json.dumps({"error": "bad request"}))
            return super().post(url, headers=headers, data=data, timeout=timeout)

    client = _client(transport=ErrorPayload(
        stock_map={"600519": "gssh0600519"}
    ))
    assert client.fetch_announcements("600519.SH", "2026-01-01", "2026-10-03").empty
    print("  -> 接口错误报文（无 announcements 字段）安全降级")

    # 缓存清理：clear_cache 后必须重新拉取映射
    transport = FakeTransport(stock_map={"600519": "gssh0600519"})
    client = _client(transport=transport)
    client.resolve_org_id("600519")
    assert len([u for u in transport.gets if "szse_stock.json" in u]) == 1
    client.clear_cache()
    client.resolve_org_id("600519")
    assert len([u for u in transport.gets if "szse_stock.json" in u]) == 2
    print("  -> clear_cache() 可清空模块级缓存（测试隔离）")


if __name__ == "__main__":
    # 保住旧的直接执行入口：委托给 pytest，退出码语义一致
    raise SystemExit(pytest.main([__file__, "-v"]))
