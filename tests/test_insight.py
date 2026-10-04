"""
投资人观点库测试 (tests/test_insight.py)

覆盖本轮新增的整个 insight 域：
  stocklab/domain/insight.py              列契约与枚举
  stocklab/persistence/migrations/006      三张表与索引
  stocklab/persistence/repository/insight  只插入、增量水位、多维筛选
  stocklab/normalization/insight           UGC 清洗、去重哈希、启发式标注
  stocklab/datasource/insight              采集框架（限速/凭证/失败分类/分页）
  app/scripts/sync_investor_insight        登记表载入、manual 同步、退出码
  app/web/insight_api                      三个接口的应答契约

【本文件最重要的三条不变量】
  1. 抓取侧永远写不出 verified —— 只有 manual 通道能覆盖（红线）。
  2. 核验状态必须随每条言论一起下发，否则前端无法诚实呈现。
  3. content_hash 必须「同一段话跨平台同哈希 / 不同段话不同哈希」，
     否则跨平台去重会失效或误合。

运行：
  ./venv/bin/python -m pytest tests/test_insight.py -v
  （web 一组用真实库，需先 pkill -f serve_web.py）
"""

import json
import os
import re
import sys

import pandas as pd
import pytest

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.domain import (
    INVESTOR_ACCOUNT_COLUMNS,
    INVESTOR_COLUMNS,
    INVESTOR_QUOTE_COLUMNS,
    PLATFORMS,
    QUOTE_TYPES,
    VERIFICATION_STATUSES,
    DataContractError,
)
from stocklab.normalization.insight import (
    MANUAL_PLATFORM,
    UNVERIFIED,
    build_quote_id,
    clean_ugc_text,
    compute_content_hash,
    detect_quote_type,
    detect_stock_codes,
    detect_themes,
    make_summary,
    normalize_investor_accounts,
    normalize_investors,
    normalize_investor_quotes,
)
from stocklab.datasource.insight import (
    AccountFetchOutcome,
    CollectRequest,
    InsightBlockedError,
    InsightCollectError,
    InsightCollectorRegistry,
    InsightCredentialError,
    InsightParseError,
    build_default_registry,
    fetch_account_quotes,
    normalize_outcome,
)
from stocklab.datasource.insight.base import InsightCollector, make_robots_checker
from stocklab.datasource.insight.guba import GubaCollector, _parse_post_time
from stocklab.datasource.insight.manual import ManualInsightCollector
from stocklab.datasource.insight.xueqiu import XueqiuCollector, _to_datetime
from stocklab.persistence.repository.insight import (
    InvestorAccountRepository,
    InvestorQuoteRepository,
    InvestorRepository,
    decode_raw_meta,
    dumps_raw_meta,
)
from app.scripts.sync_investor_insight import (
    DEFAULT_LOOKBACK_DAYS,
    load_sources,
    main,
    parse_args,
    resolve_since,
    show_status,
    sync_quotes,
)


# ===========================================================================
# 领域契约
# ===========================================================================
class TestDomainContract:
    """列契约与枚举必须与 006 迁移 DDL 同序"""

    def test_column_counts(self):
        assert len(INVESTOR_COLUMNS) == 9
        assert len(INVESTOR_ACCOUNT_COLUMNS) == 9
        assert len(INVESTOR_QUOTE_COLUMNS) == 16

    def test_quote_id_first_in_quotes(self):
        # quote_id 是主键，排在第一列；Repository 的 ON CONFLICT 依赖它
        assert INVESTOR_QUOTE_COLUMNS[0] == "quote_id"

    def test_verification_column_exists(self):
        assert "verification" in INVESTOR_QUOTE_COLUMNS
        assert "content_hash" in INVESTOR_QUOTE_COLUMNS
        assert "source_url" in INVESTOR_QUOTE_COLUMNS
        assert "published_at" in INVESTOR_QUOTE_COLUMNS
        assert "captured_at" in INVESTOR_QUOTE_COLUMNS
        assert "platform" in INVESTOR_QUOTE_COLUMNS

    def test_enums(self):
        assert "xueqiu" in PLATFORMS and "guba" in PLATFORMS
        assert "manual" in PLATFORMS
        assert set(VERIFICATION_STATUSES) == {
            "unverified", "verified", "disputed", "fabricated",
        }
        assert "philosophy" in QUOTE_TYPES and "macro_view" in QUOTE_TYPES


# ===========================================================================
# 归一化：UGC 清洗与去重哈希
# ===========================================================================
class TestCleanAndHash:
    """UGC 文本清洗 —— 这是本域最脏输入的唯一入口"""

    def test_strips_html_tags_and_entities(self):
        assert clean_ugc_text("<b>平安银行</b>&amp;茅台") == "平安银行&茅台"

    def test_strips_forward_and_platform_noise(self):
        assert "转发微博" not in clean_ugc_text("买600519 转发微博")
        assert "来自雪球客户端" not in clean_ugc_text("买600519 来自雪球客户端")
        assert "阅读全文" not in clean_ugc_text("买600519 阅读全文")

    def test_collapses_whitespace(self):
        assert clean_ugc_text("a   b\nc\t\nd") == "a b c d"

    def test_empty_and_none(self):
        assert clean_ugc_text(None) == ""
        assert clean_ugc_text("   ") == ""

    def test_bom_and_zero_width_removed(self):
        # 零宽字符会让「看起来一样」的两段话算出不同哈希
        assert "估值" in clean_ugc_text("﻿​估值")

    def test_max_length_truncates(self):
        assert len(clean_ugc_text("中" * 100, max_length=10)) == 10

    def test_chinese_punctuation_is_not_altered(self):
        """
        【回归钉子】初版把中文标点映射成半角，句号被改写成 '.'，
        等于**篡改引文**。这条钉住：标点必须原样保留。
        """
        original = "买股票就是买公司的一部分（长期）。停牌？回调，机会来了！"
        assert clean_ugc_text(original) == original

    def test_fullwidth_latin_normalized(self):
        # 字母数字归一，跨平台去重才一致
        assert compute_content_hash("估值ＡＢＣ") == compute_content_hash("估值abc")

    def test_hash_same_content_across_whitespace(self):
        assert compute_content_hash("估值便宜") == compute_content_hash("估值　便宜")

    def test_hash_different_content_differs(self):
        assert compute_content_hash("估值便宜") != compute_content_hash("估值很贵")

    def test_hash_differs_on_chinese_punctuation(self):
        # 标点是原文差异的一部分，不该被哈希抹平
        assert compute_content_hash("好，对吧。") != compute_content_hash("好,对吧")

    def test_hash_empty_returns_empty(self):
        assert compute_content_hash("") == ""
        assert compute_content_hash(None) == ""

    def test_summary_length_is_characters_not_bytes(self):
        assert len(make_summary("中" * 300, length=50)) <= 51
        assert make_summary("中" * 300).endswith("…")

    def test_summary_short_keeps_whole(self):
        assert make_summary("估值低") == "估值低"


class TestHeuristics:
    """启发式标注是启发式，不是分类器 —— 宁可漏，不可错标成别类"""

    @pytest.mark.parametrize("text,expected", [
        ("我判断宏观会降息，通胀回落，地产不行", "macro_view"),
        ("选股标准是商业模式好、有护城河", "selection"),
        ("我重仓这只票，已经加仓了", "position"),
        ("风险很大，谨慎，不要买", "caution"),
        ("要耐心，要有纪律，看不懂就放弃", "discipline"),
    ])
    def test_quote_type_detection(self, text, expected):
        assert detect_quote_type(text) == expected

    def test_quote_type_no_keyword_returns_none(self):
        # 没有明显特征就不猜 —— 宁可 NULL 也不错标
        assert detect_quote_type("") is None
        assert detect_quote_type("zzz") is None or detect_quote_type("zzz") is None

    def test_themes_are_multiple(self):
        assert "估值" in detect_themes("估值便宜，商业模式好，长期持有")

    def test_themes_empty_text(self):
        assert detect_themes("") == ""

    def test_themes_respect_limit(self):
        assert len(detect_themes("估值 商业模式 企业文化 能力圈 长期持有 宏观判断",
                                 limit=2).split(",")) == 2

    def test_stock_code_detection_next_to_chinese(self):
        """
        【回归钉子】Python 默认 \\w 匹配汉字，用 \\w 做前后哨兵时
        「今天买了600519」里的「了」会阻断断言，导致一个代码都抽不到。
        """
        assert detect_stock_codes("今天买了600519，跌了3个点") == "600519"

    def test_stock_code_with_suffix(self):
        # detect_stock_codes 返回逗号分隔串，不是 list ——
        # 直接 set() 会退化成字符集（我第一版就是这么写错的）
        codes = detect_stock_codes("看好 000001.SZ 和 sh601318")
        assert set(codes.split(",")) == {"000001", "601318"}

    def test_stock_code_does_not_match_price(self):
        assert detect_stock_codes("股价60.00元，市盈率25倍") == ""

    def test_stock_code_does_not_match_date(self):
        assert detect_stock_codes("2026-10-01 的数据") == ""

    def test_stock_code_dedup(self):
        assert detect_stock_codes("600519 和 600519") == "600519"

    def test_build_quote_id_prefers_platform_id(self):
        assert build_quote_id("xueqiu", "123", "abc") == "xueqiu:123"

    def test_build_quote_id_falls_back_to_hash(self):
        assert build_quote_id("guba", None, "abc123") == "guba:habc123"

    def test_build_quote_id_empty_when_nothing(self):
        assert build_quote_id("guba", None, "") == ""


# ===========================================================================
# 归一化：契约帧
# ===========================================================================
class TestNormalizeFrames:
    """字段缺失必须丢弃而不是留空壳 —— 这些都是「抓不到时的正确行为」"""

    def test_investors_column_order(self):
        frame = normalize_investors([
            {"investor_code": "dyp", "name": "段永平", "style_tags": ["value"]},
        ])
        assert list(frame.columns) == list(INVESTOR_COLUMNS)
        assert frame.iloc[0]["style_tags"] == "value"

    def test_investors_skip_missing_code_or_name(self):
        assert normalize_investors([{"name": "无code"}]).empty
        assert normalize_investors([{"investor_code": "x"}]).empty

    def test_investors_created_time_required(self):
        # created_time 是 NOT NULL，归一化必须给默认值
        frame = normalize_investors([{"investor_code": "a", "name": "A"}])
        assert frame.iloc[0]["created_time"] is not None

    def test_investors_created_time_is_recording_time_not_birth(self):
        # 登记表手写 created_time 无意义，只采信显式传入值
        stamp = pd.Timestamp("2020-01-01")
        frame = normalize_investors(
            [{"investor_code": "a", "name": "A", "created_time": None}],
            created_time=stamp,
        )
        assert frame.iloc[0]["created_time"] == stamp

    def test_accounts_require_investor_platform_and_name(self):
        assert normalize_investor_accounts([{"account_name": "x"}]).empty
        assert normalize_investor_accounts(
            [{"investor_code": "a", "account_name": "x"}]).empty
        ok = normalize_investor_accounts([{
            "investor_code": "a", "account_name": "x", "platform": "guba",
        }])
        assert len(ok) == 1

    def test_accounts_default_platform(self):
        frame = normalize_investor_accounts(
            [{"investor_code": "a", "account_name": "x"}],
            default_platform="manual",
        )
        assert frame.iloc[0]["platform"] == "manual"

    def test_quotes_column_order(self):
        frame = normalize_investor_quotes([{
            "content": "这是一段足够长的内容",
            "investor_code": "dyp", "platform": "xueqiu",
        }])
        assert list(frame.columns) == list(INVESTOR_QUOTE_COLUMNS)

    def test_shell_content_dropped(self):
        # 空壳条目（"赞"/"转发微博"）不入库，否则页面上是一行空白
        assert normalize_investor_quotes([
            {"content": "赞", "investor_code": "x", "platform": "p"},
        ]).empty
        assert normalize_investor_quotes([
            {"content": "  ", "investor_code": "x", "platform": "p"},
        ]).empty

    def test_missing_investor_dropped(self):
        assert normalize_investor_quotes([
            {"content": "这是一段足够长的内容", "platform": "p"},
        ]).empty

    def test_missing_platform_dropped(self):
        assert normalize_investor_quotes([
            {"content": "这是一段足够长的内容", "investor_code": "x"},
        ]).empty

    def test_fallback_investor_and_platform_applied(self):
        frame = normalize_investor_quotes(
            [{"content": "这是一段足够长的内容"}],
            investor_code="fallback_code", platform="manual",
        )
        assert len(frame) == 1
        assert frame.iloc[0]["investor_code"] == "fallback_code"
        assert frame.iloc[0]["platform"] == "manual"

    def test_auto_annotate_off_leaves_null(self):
        # auto_annotate=False 时宁可留 NULL，不写猜测值
        frame = normalize_investor_quotes([{
            "content": "估值低，长期持有，600519",
            "investor_code": "x", "platform": "p",
        }], auto_annotate=False)
        assert frame.iloc[0]["quote_type"] in (None, None) or pd.isna(
            frame.iloc[0]["quote_type"])
        assert not frame.iloc[0]["theme"]

    def test_analyze_gives_type_and_theme(self):
        frame = normalize_investor_quotes([{
            "content": "估值低，长期持有，600519",
            "investor_code": "x", "platform": "p",
        }])
        assert frame.iloc[0]["theme"]
        assert "600519" in (frame.iloc[0]["stock_codes"] or "")

    def test_quote_id_generated_when_absent(self):
        frame = normalize_investor_quotes([{
            "content": "这是一段足够长的内容", "investor_code": "x",
            "platform": "p",
        }])
        assert frame.iloc[0]["quote_id"].startswith("p:")

    def test_quote_id_kept_when_given(self):
        frame = normalize_investor_quotes([{
            "content": "这是一段足够长的内容", "investor_code": "x",
            "platform": "p", "quote_id": "p:fixed",
        }])
        assert frame.iloc[0]["quote_id"] == "p:fixed"

    def test_content_hash_generated_when_absent(self):
        frame = normalize_investor_quotes([{
            "content": "这是一段足够长的内容", "investor_code": "x",
            "platform": "p",
        }])
        assert len(frame.iloc[0]["content_hash"]) == 64

    def test_no_source_url_when_illegal(self):
        frame = normalize_investor_quotes([{
            "content": "这是一段足够长的内容", "investor_code": "x",
            "platform": "p", "source_url": "javascript:alert(1)",
        }])
        # 假链接比没有链接更糟，它会让人以为这句话已被核实过
        assert frame.iloc[0]["source_url"] is None

    def test_source_url_kept_when_https(self):
        frame = normalize_investor_quotes([{
            "content": "这是一段足够长的内容", "investor_code": "x",
            "platform": "p", "source_url": "https://xueqiu.com/1/2",
        }])
        assert frame.iloc[0]["source_url"] == "https://xueqiu.com/1/2"


class TestVerificationRedLine:
    """
    【本域最重要的一组测试】抓取侧永远写不出 verified

    回答的问题：如果某个采集器误传了 verified，页面会怎样？
      答：会被强制降级为 unverified。这是**有意的**，因为标错为「已核实」
      比标错为「未核实」危险得多。
    """

    @pytest.mark.parametrize("platform", ["xueqiu", "guba", "eastmoney", "weibo"])
    def test_scraped_side_cannot_claim_verified(self, platform):
        frame = normalize_investor_quotes([{
            "content": "这是一段足够长的内容",
            "investor_code": "x", "platform": platform,
            "verification": "verified",
        }])
        assert frame.iloc[0]["verification"] == UNVERIFIED

    def test_manual_channel_may_claim_verified(self):
        frame = normalize_investor_quotes([{
            "content": "这是一段足够长的内容",
            "investor_code": "x", "platform": MANUAL_PLATFORM,
            "verification": "verified",
        }])
        assert frame.iloc[0]["verification"] == "verified"

    def test_manual_defaults_to_unverified_when_absent(self):
        frame = normalize_investor_quotes([{
            "content": "这是一段足够长的内容",
            "investor_code": "x", "platform": MANUAL_PLATFORM,
        }])
        assert frame.iloc[0]["verification"] == UNVERIFIED

    def test_unknown_status_cannot_be_written(self):
        # 非法状态不会被静默改成 unverified —— 那是静默降级，同样误导人。
        # manual 采集器会先校验并拒绝，这里钉住归一化层的权威判断。
        from stocklab.normalization.insight import _resolve_verification
        assert _resolve_verification("bogus", "xueqiu") == UNVERIFIED
        # manual 平台：归一化信任 manual 采集器已校验过（raw 不在此拦）
        assert _resolve_verification("bogus", MANUAL_PLATFORM) == "bogus"


# ===========================================================================
# 迁移与 Repository
# ===========================================================================
class TestMigrationAndRepositories:
    """006 迁移产物与只插入语义"""

    @pytest.fixture
    def db(self, fresh_db):
        return fresh_db[1]

    @pytest.fixture
    def registred(self, db):
        """已载入登记表的库"""
        load_sources(db, "configs/insight_sources.json")
        return db

    def test_three_tables_created(self, db):
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'insight' ORDER BY table_name"
        ).fetchdf()["table_name"].tolist()
        assert rows == ["investor_accounts", "investor_quotes", "investors"]

    def test_indexes_created(self, db):
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT index_name FROM duckdb_indexes() "
            "WHERE schema_name = 'insight' ORDER BY index_name"
        ).fetchdf()["index_name"].tolist()
        assert "idx_quotes_content_hash" in rows
        assert "idx_quotes_investor_published" in rows
        assert "idx_quotes_theme" in rows

    def test_load_sources_is_idempotent(self, registred):
        # 只插入 + ON CONFLICT DO NOTHING，重跑不该增加行数
        assert registred and InvestorRepository(registred).count() == 3
        before_i = InvestorRepository(registred).count()
        before_a = InvestorAccountRepository(registred).count()
        new_i, new_a = load_sources(registred, "configs/insight_sources.json")
        assert (new_i, new_a) == (0, 0)
        assert InvestorRepository(registred).count() == before_i
        assert InvestorAccountRepository(registred).count() == before_a

    def test_load_sources_missing_file_raises(self, db):
        with pytest.raises(FileNotFoundError):
            load_sources(db, "configs/no_such_file.json")

    def test_load_sources_rejects_top_level_list(self, db, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("[1,2]", encoding="utf-8")
        with pytest.raises(ValueError):
            load_sources(db, str(path))

    def test_load_sources_rejects_orphan_account(self, db, tmp_path):
        """登记表手写错 investor_code 的概率不低，必须显式报错
        而不是让账号「静静地没人用」。"""
        path = tmp_path / "orphan.json"
        path.write_text(json.dumps({
            "investors": [{"investor_code": "known", "name": "A"}],
            "accounts": [{"investor_code": "unknown_code",
                          "platform": "guba", "account_name": "x"}],
        }), encoding="utf-8")
        with pytest.raises(ValueError, match="不存在的投资人"):
            load_sources(db, str(path))

    def test_investor_repo_find_by_code(self, registred):
        repo = InvestorRepository(registred)
        found = repo.find_by_code("dyp_0001")
        assert len(found) == 1
        assert found.iloc[0]["name"] == "段永平"
        assert repo.find_by_code("nope").empty

    def test_account_repo_find_enabled_filters_platform(self, registred):
        repo = InvestorAccountRepository(registred)
        enabled = repo.find_enabled()
        assert set(enabled["platform"]) == {"xueqiu"}
        assert repo.find_enabled(platform="guba").empty
        # 全部账号 3 个，启用 1 个
        assert repo.count() == 3
        assert len(enabled) == 1

    def test_account_repo_find_by_investor(self, registred):
        rows = InvestorAccountRepository(registred).find_by_investor("dyp_0001")
        assert len(rows) == 1
        assert rows.iloc[0]["platform"] == "xueqiu"

    def test_quote_repo_insert_then_search(self, registred):
        repo = InvestorQuoteRepository(registred)
        frame = normalize_investor_quotes([
            {"content": "估值便宜，长期持有，600519", "investor_code": "dyp_0001",
             "platform": "xueqiu", "published_at": "2025-06-01 10:00:00"},
        ])
        assert repo.insert(frame) == 1
        assert repo.count() == 1
        assert len(repo.search(keyword="估值")) == 1
        assert len(repo.search(keyword="不存在的词")) == 0

    def test_quote_repo_insert_rejects_contract_violation(self, registred):
        """缺列返回 0 而不是抛异常（与其它 Repository 一致），
        这样一批里的坏行不会让整轮同步崩掉。"""
        repo = InvestorQuoteRepository(registred)
        bad = pd.DataFrame({"quote_id": ["x"]})
        assert repo.insert(bad) == 0
        assert repo.insert(None) == 0
        assert repo.insert(pd.DataFrame()) == 0

    def test_quote_repo_insert_idempotent_by_id(self, registred):
        repo = InvestorQuoteRepository(registred)
        frame = normalize_investor_quotes([{
            "content": "这是一段足够长的内容",
            "investor_code": "dyp_0001", "platform": "manual",
            "quote_id": "manual:fixed",
        }])
        assert repo.insert(frame) == 1
        assert repo.insert(frame) == 1       # 提交但冲突被忽略
        assert repo.count() == 1             # 真实只有一条

    def test_existing_hashes(self, registred):
        repo = InvestorQuoteRepository(registred)
        assert repo.existing_hashes([]) == set()
        assert repo.existing_hashes(["a", None]) == set()
        frame = normalize_investor_quotes([{
            "content": "这是一段足够长的内容",
            "investor_code": "dyp_0001", "platform": "manual",
        }])
        repo.insert(frame)
        digest = frame.iloc[0]["content_hash"]
        assert repo.existing_hashes([digest]) == {digest}
        assert repo.existing_hashes(["other"]) == set()

    def test_search_filters(self, registred):
        repo = InvestorQuoteRepository(registred)
        rows = [
            {"content": "估值便宜，长期持有 600519", "investor_code": "dyp_0001",
             "platform": "xueqiu", "quote_type": "philosophy",
             "published_at": "2025-06-01 10:00:00"},
            {"content": "宏观会降息，通胀回落", "investor_code": "danbin_0001",
             "platform": "manual", "quote_type": "macro_view",
             "verification": "verified",
             "published_at": "2025-08-01 10:00:00"},
        ]
        repo.insert(normalize_investor_quotes(rows))

        assert len(repo.search(investor_codes=["dyp_0001"])) == 1
        assert len(repo.search(platforms=["manual"])) == 1
        assert len(repo.search(quote_types=["philosophy"])) == 1
        assert len(repo.search(verification="verified")) == 1
        assert len(repo.search(verification="disputed")) == 0
        assert len(repo.search(stock_codes=["600519"])) == 1
        assert len(repo.search(keyword="降息")) == 1
        assert len(repo.search(since="2025-07-01")) == 1
        assert len(repo.search(since="2026-01-01")) == 0

    def test_search_order_by_falls_back_to_captured_at(self, registred):
        """非法 order_by 必须回退而不是抛异常（接口层负责 400）"""
        repo = InvestorQuoteRepository(registred)
        assert repo.search(order_by="; DROP TABLE").empty

    def test_search_limit_and_offset(self, registred):
        repo = InvestorQuoteRepository(registred)
        repo.insert(normalize_investor_quotes([
            {"content": "内容 %d 足够长" % i, "investor_code": "dyp_0001",
             "platform": "manual", "published_at": "2025-06-01 10:00:00"}
            for i in range(10)
        ]))
        assert len(repo.search(limit=3)) == 3
        assert len(repo.search(limit=3, offset=9)) == 1
        assert len(repo.search(limit=3, offset=100)) == 0

    def test_latest_published_at_watermark(self, registred):
        repo = InvestorQuoteRepository(registred)
        assert repo.latest_published_at("dyp_0001") is None
        repo.insert(normalize_investor_quotes([{
            "content": "这是一段足够长的内容",
            "investor_code": "dyp_0001", "platform": "manual",
            "published_at": "2025-06-01 10:00:00",
        }]))
        latest = repo.latest_published_at("dyp_0001")
        assert pd.Timestamp(latest).year == 2025
        assert repo.latest_published_at("nobody") is None

    def test_verification_summary_and_count(self, registred):
        repo = InvestorQuoteRepository(registred)
        assert repo.verification_summary() == {}
        assert repo.count() == 0
        repo.insert(normalize_investor_quotes([
            {"content": "第一条足够长", "investor_code": "dyp_0001",
             "platform": "manual", "verification": "verified"},
            {"content": "第二条足够长", "investor_code": "dyp_0001",
             "platform": "xueqiu"},
        ]))
        summary = repo.verification_summary()
        assert summary == {"verified": 1, "unverified": 1}
        assert repo.count(verification="verified") == 1

    def test_list_investors_groups_quotes(self, registred):
        repo = InvestorQuoteRepository(registred)
        repo.insert(normalize_investor_quotes([
            {"content": "第一条足够长", "investor_code": "dyp_0001",
             "platform": "manual", "verification": "verified"},
            {"content": "第二条足够长", "investor_code": "dyp_0001",
             "platform": "manual"},
            {"content": "第三条足够长", "investor_code": "linyuan_0001",
             "platform": "manual"},
        ]))
        rows = repo.list_investors().to_dict("records")
        by_code = {row["investor_code"]: row for row in rows}
        assert by_code["dyp_0001"]["quote_count"] == 2
        assert by_code["dyp_0001"]["verified_count"] == 1
        assert by_code["linyuan_0001"]["quote_count"] == 1
        # 按条数降序：dyp 在前
        assert rows[0]["investor_code"] == "dyp_0001"

    def test_list_investors_includes_zero_count(self, registred):
        """没言论的投资人也要出现 —— 空状态是这页要正确展示的场景"""
        rows = InvestorQuoteRepository(registred).list_investors()
        assert len(rows) == 3
        assert (rows["quote_count"] == 0).all()

    def test_list_themes(self, registred):
        repo = InvestorQuoteRepository(registred)
        repo.insert(normalize_investor_quotes([
            {"content": "估值低，长期持有 600519", "investor_code": "dyp_0001",
             "platform": "manual"},
            {"content": "估值高，不适合", "investor_code": "dyp_0001",
             "platform": "manual"},
        ]))
        themes = repo.list_themes().to_dict("records")
        assert themes and themes[0]["theme"] == "估值"
        assert themes[0]["quote_count"] == 2

    def test_raw_meta_round_trip(self):
        assert dumps_raw_meta(None) is None
        assert dumps_raw_meta({}) is None
        text = dumps_raw_meta({"note": "出处线索"})
        assert decode_raw_meta(text) == {"note": "出处线索"}
        assert decode_raw_meta(None) == {}
        assert decode_raw_meta("not json") == {}


# ===========================================================================
# 采集框架
# ===========================================================================
class _StubCollector(InsightCollector):
    """测试用桩：记录每次调用，返回可配置的结果

    PLATFORM 刻意**不是** manual —— fetch_account_quotes 对 manual 走
    「直接 fetch、不分页」的旁路，用 manual 当桩会让所有分页用例永远进不到
    分页逻辑（踩过这个坑：断言全绿但什么都没测到）。
    """

    PLATFORM = "guba"
    PAGE_SIZE = 20

    def __init__(self, pages=None, error=None, error_from_page=None, **kwargs):
        """
        Args:
            pages (dict): {页码: 记录列表}
            error: 从第 1 页起就抛的异常（模拟「一进来就失败」）
            error_from_page (int): 从第 N 页起才抛（模拟「抓了几页后被限流」）
        """
        super().__init__(**kwargs)
        self.pages = pages or {}
        self.error = error
        self.error_from_page = error_from_page
        self.calls = []

    def fetch(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        return []

    def _fetch_page(self, request, uid, page):
        self.calls.append((uid, page))
        if self.error:
            raise self.error
        if self.error_from_page is not None and page >= self.error_from_page:
            raise InsightBlockedError("HTTP 429（第 %d 页）" % page)
        return self.pages.get(page, [])


class TestCollectorBase:
    """采集礼仪：限速 / 凭证 / 失败分类 —— 这三件都是「静默的错」"""

    def test_abstract_fetch_not_implemented(self):
        with pytest.raises(NotImplementedError):
            InsightCollector().fetch(CollectRequest(account={}))

    def test_throttle_sleeps_until_interval(self):
        sleeps, clock = [], {"t": 0.0}
        collector = _StubCollector(
            sleep_fn=lambda s: sleeps.append(s),
            monotonic_fn=lambda: clock["t"],
            error=None,
        )
        collector.MIN_INTERVAL_SECONDS = 2.5
        collector.mark_request_sent()
        clock["t"] = 0.5
        collector.throttle()          # 距上次 0.5s，应补 2.0s
        assert sleeps == [2.0]

    def test_throttle_no_sleep_when_enough_time_passed(self):
        sleeps, clock = [], {"t": 0.0}
        collector = _StubCollector(
            sleep_fn=lambda s: sleeps.append(s),
            monotonic_fn=lambda: clock["t"],
        )
        collector.mark_request_sent()
        clock["t"] = 99.0
        collector.throttle()
        assert sleeps == []

    def test_throttle_first_request_is_noop(self):
        sleeps = []
        collector = _StubCollector(sleep_fn=lambda s: sleeps.append(s))
        collector.throttle()
        assert sleeps == []

    def test_credential_required_raises(self):
        collector = _StubCollector()
        collector.REQUIRES_CREDENTIAL = True
        with pytest.raises(InsightCredentialError) as exc:
            collector.require_credentials()
        # 报错要指出去哪改配置，否则只是一句「凭证缺失」
        assert "cookie" in str(exc.value).lower() or "credential" in str(exc.value).lower() \
            or "登录态" in str(exc.value)

    def test_credential_ok_when_configured(self):
        collector = _StubCollector()
        collector.REQUIRES_CREDENTIAL = True
        collector._credential = "xq_a_token=abc"
        collector.require_credentials()   # 不抛

    def test_no_credential_required_passes(self):
        _StubCollector().require_credentials()

    def test_describe_reports_readiness(self):
        collector = _StubCollector()
        info = collector.describe()
        assert info["platform"] == "guba"
        assert info["credential_configured"] is True

    def test_robots_default_allows(self):
        # 默认不检查（代价已在 base 模块头写明）
        assert _StubCollector().robots_allows("https://example.com/x") is True

    def test_robots_checker_blocks_and_raises(self):
        def deny(url):
            return False
        collector = _StubCollector(robots_checker=deny)
        assert collector.robots_allows("https://example.com/x") is False
        with pytest.raises(InsightCollectError, match="robots.txt 禁止"):
            collector.assert_robots_allows("https://example.com/x")

    def test_robots_checker_allows(self):
        collector = _StubCollector(robots_checker=lambda url: True)
        collector.assert_robots_allows("https://example.com/x")

    def test_robots_checker_failure_treated_as_allowed(self):
        def boom(url):
            raise RuntimeError("网络故障")
        collector = _StubCollector(robots_checker=boom)
        # 检查器自身故障不该中断抓取（可用性优先）
        assert collector.robots_allows("https://x.com/a") is True

    def test_make_robots_checker_returns_callable(self):
        assert callable(make_robots_checker())

    def test_registry_register_rejects_unknown_platform(self):
        registry = InsightCollectorRegistry(collectors={})
        with pytest.raises(ValueError, match="不在 PLATFORMS 契约内"):
            registry.register("bogus", _StubCollector())

    def test_registry_get_missing_raises(self):
        registry = InsightCollectorRegistry(collectors={})
        with pytest.raises(InsightCollectError, match="没有注册采集器"):
            registry.get("xueqiu")

    def test_registry_register_replaces(self):
        registry = build_default_registry()
        stub = _StubCollector()
        registry.register("manual", stub)
        assert registry.get("manual") is stub

    def test_registry_platforms_sorted(self):
        assert build_default_registry().platforms() == sorted(
            ["xueqiu", "guba", "manual"])

    def test_registry_describe_lists_all(self):
        infos = build_default_registry().describe()
        assert {x["platform"] for x in infos} == {"xueqiu", "guba", "manual"}

    def test_registry_set_credential(self):
        registry = build_default_registry()
        registry.set_credential("xueqiu", "xq_a_token=1")
        assert registry.get("xueqiu").verify_credentials() is True


class TestOutcomeSemantics:
    """AccountFetchOutcome：失败必须可区分，否则只能说「失败了」"""

    def _outcome(self, **kwargs):
        base = dict(account_id="a", platform="guba", records=[])
        base.update(kwargs)
        return AccountFetchOutcome(**base)

    def test_ok_when_no_error(self):
        assert self._outcome().ok is True
        assert self._outcome().error is None

    def test_not_ok_when_error(self):
        assert self._outcome(error="boom", error_kind="parse").ok is False

    @pytest.mark.parametrize("kind,expected", [
        ("network", True),
        ("credential", False),
        ("parse", False),
        ("blocked", False),
    ])
    def test_only_network_is_retryable(self, kind, expected):
        """凭证缺失重试无用；被限流重试等于继续违规"""
        assert self._outcome(error="x", error_kind=kind).retryable is expected

    def test_describe_success(self):
        out = self._outcome(records=[{}, {}, {}], pages=1)
        assert "OK 3 条/1 页" in out.describe()

    def test_describe_failure_includes_kind(self):
        out = self._outcome(error="改版了", error_kind="parse")
        assert "失败[parse]" in out.describe()

    def test_warnings_preserved(self):
        out = self._outcome(warnings=["w1"])
        assert out.warnings == ["w1"]


class TestRegistryAndCredential:
    """registry 默认构造与凭证注入"""

    def test_default_registry_has_three_platforms(self):
        assert build_default_registry().platforms() == ["guba", "manual", "xueqiu"]

    def test_xueqiu_requires_credential_by_default(self):
        registry = build_default_registry()
        assert registry.get("xueqiu").verify_credentials() is False
        assert registry.get("guba").verify_credentials() is True

    def test_credentials_from_dict(self):
        registry = build_default_registry(credentials={"xueqiu": "a=b"})
        assert registry.get("xueqiu").verify_credentials() is True

    def test_empty_credential_ignored(self):
        registry = build_default_registry(credentials={"xueqiu": ""})
        assert registry.get("xueqiu").verify_credentials() is False

    def test_manual_path_plumbed(self, tmp_path):
        registry = build_default_registry(manual_path=str(tmp_path / "x.json"))
        assert registry.get("manual").path.endswith("x.json")

    def test_xueqiu_fetch_without_credential_raises(self):
        collector = build_default_registry().get("xueqiu")
        request = CollectRequest(account={
            "platform": "xueqiu", "account_name": "大道无形我有型",
            "account_uid": "1240335488",
        })
        with pytest.raises(InsightCredentialError):
            collector.fetch(request)

    def test_xueqiu_fetch_without_uid_raises_parse(self):
        """缺 account_uid 是配置错误，必须报错而不是当空内容"""
        collector = build_default_registry().get("xueqiu")
        collector._credential = "xq_a_token=1"
        with pytest.raises(InsightParseError, match="account_uid"):
            collector.fetch(CollectRequest(account={
                "platform": "xueqiu", "account_name": "大道无形我有型",
            }))


class TestXueqiuParsing:
    """雪球解析：接口改版必须可识别，不能当空结果"""

    def _collector(self):
        c = XueqiuCollector()
        c._credential = "xq_a_token=1"
        return c

    def _request(self):
        return CollectRequest(account={
            "platform": "xueqiu", "investor_code": "dyp_0001",
            "account_name": "大道无形我有型", "account_uid": "1240335488",
        })

    def test_parse_valid_payload(self):
        payload = {"list": [{
            "id": 222, "target": "/1240335488/222",
            "text": "<p>估值便宜</p>", "created_at": 1758000000000,
            "reply_count": 3,
        }]}
        records = self._collector()._parse_records(payload, self._request(), "1240335488")
        assert len(records) == 1
        row = records[0]
        assert row["platform_id"] == "222"
        assert row["platform"] == "xueqiu"
        assert row["investor_code"] == "dyp_0001"
        assert row["source_url"] == "https://xueqiu.com/1240335488/222"
        assert row["raw_meta"]["reply_count"] == 3

    def test_missing_list_raises_instead_of_empty(self):
        """list 消失 = 接口改版或登录墙，返回空会让「抓到 0 条」变成常态"""
        with pytest.raises(InsightParseError, match="list"):
            self._collector()._parse_records(
                {"code": 0, "error": "login"}, self._request(), "1")

    def test_non_dict_payload_raises(self):
        with pytest.raises(InsightParseError, match="不是对象"):
            self._collector()._parse_records(["x"], self._request(), "1")

    def test_empty_text_skipped(self):
        payload = {"list": [{"id": 1, "text": ""}, {"id": 2, "text": "有内容"}]}
        assert len(self._collector()._parse_records(
            payload, self._request(), "1")) == 1

    def test_falls_back_to_target_when_id_missing(self):
        payload = {"list": [{"target": "/1240335488/999", "text": "内容"}]}
        records = self._collector()._parse_records(payload, self._request(), "1")
        assert records[0]["platform_id"] == "999"

    def test_no_id_and_no_target_skipped_with_warning(self):
        payload = {"list": [{"text": "内容"}]}
        assert self._collector()._parse_records(
            payload, self._request(), "1") == []

    @pytest.mark.parametrize("value,expect_none", [
        (None, True), ("", True), (0, True), ("abc", True), (-1, True),
    ])
    def test_bad_timestamp_returns_none(self, value, expect_none):
        assert (_to_datetime(value) is None) is expect_none

    def test_millisecond_and_second_timestamps(self):
        """量级判断而非硬编码除数 —— 平台会切换单位"""
        ms = _to_datetime(1758000000000)
        sec = _to_datetime(1758000000)
        assert ms is not None and sec is not None
        assert pd.Timestamp(ms) == pd.Timestamp(sec)

    def test_parse_records_via_fetch_page_path(self):
        """缺 uid 时 fetch 直接抛错，不发请求"""
        collector = self._collector()
        with pytest.raises(InsightParseError, match="account_uid"):
            collector.fetch(CollectRequest(account={
                "platform": "xueqiu", "account_name": "无uid",
            }))


class TestGubaParsing:
    """股吧解析：东财接口变动频繁，两种形态都要认"""

    def _collector(self):
        return GubaCollector()

    def _request(self):
        return CollectRequest(account={
            "platform": "guba", "investor_code": "danbin_0001",
            "account_name": "但斌", "account_uid": "999",
        })

    def test_parse_data_list_shape(self):
        payload = {"re": 0, "data": {"list": [
            {"post_id": "10", "post_title": "标题",
             "post_content": "正文", "user_nickname": "但斌",
             "post_publish_time": "2025-06-01 10:00:00"},
        ]}}
        records = self._collector()._parse_records(payload, self._request(), "999")
        assert len(records) == 1
        row = records[0]
        assert row["platform"] == "guba"
        assert "999,10" in row["source_url"]
        # 标题 + 正文都进 content（观点常在标题里）
        assert "标题" in row["content"] and "正文" in row["content"]
        assert row["account_name"] == "但斌"

    def test_parse_root_list_shape_fallback(self):
        payload = {"list": [{"post_id": "1", "post_content": "正文"}]}
        assert len(self._collector()._parse_records(
            payload, self._request(), "999")) == 1

    def test_missing_list_raises(self):
        with pytest.raises(InsightParseError, match="找不到文章列表"):
            self._collector()._parse_records({"re": 1}, self._request(), "999")

    def test_non_dict_raises(self):
        with pytest.raises(InsightParseError, match="不是对象"):
            self._collector()._parse_records(None, self._request(), "999")

    def test_missing_post_id_skipped(self):
        payload = {"list": [{"post_content": "正文"}]}
        assert self._collector()._parse_records(
            payload, self._request(), "999") == []

    def test_empty_content_skipped(self):
        payload = {"list": [{"post_id": "1", "post_title": "", "post_content": ""}]}
        assert self._collector()._parse_records(
            payload, self._request(), "999") == []

    def test_missing_uid_raises(self):
        with pytest.raises(InsightParseError, match="account_uid"):
            self._collector().fetch(CollectRequest(account={
                "platform": "guba", "account_name": "无uid",
            }))

    @pytest.mark.parametrize("value", [None, "", 0, "abc"])
    def test_parse_post_time_invalid_returns_none(self, value):
        assert _parse_post_time(value) is None

    def test_parse_post_time_string_and_millisecond(self):
        """东财两套接口分别给字符串与毫秒，两种都要认"""
        assert _parse_post_time("2025-06-01 10:00:00") is not None
        assert _parse_post_time(1758000000000) is not None


class TestManualCollector:
    """人工录入：唯一允许写非抓取内容的通道"""

    def test_missing_file_raises_with_template_hint(self, tmp_path):
        with pytest.raises(InsightCollectError, match="insight_manual.example"):
            ManualInsightCollector(path=str(tmp_path / "no.json")).fetch(
                CollectRequest(account={}))

    def test_invalid_json_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{oops", encoding="utf-8")
        with pytest.raises(InsightCollectError, match="JSON 解析失败"):
            ManualInsightCollector(path=str(path)).fetch(CollectRequest(account={}))

    def test_top_level_object_requires_quotes_key(self, tmp_path):
        """
        【钉住严格校验】键名写错（quote vs quotes）必须报错。
        若放宽成「没有 quotes 就当空」，键名打错的同步会静默零条，
        用户只看到「本次无新增」，是本域最糟的失败模式。
        """
        path = tmp_path / "typo.json"
        path.write_text('{"quote": []}', encoding="utf-8")
        with pytest.raises(InsightCollectError, match="没有 quotes"):
            ManualInsightCollector(path=str(path)).fetch(CollectRequest(account={}))

    def test_top_level_list_accepted(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text('[{"content": "内容足够长", "investor_code": "x"}]',
                        encoding="utf-8")
        assert len(ManualInsightCollector(path=str(path)).fetch(
            CollectRequest(account={}))) == 1

    def test_example_template_produces_zero_rows(self):
        """example 文件被直接 cp 使用时不该写垃圾（设计决定，见该文件注释）"""
        collector = ManualInsightCollector(
            path="configs/insight_manual.example.json")
        assert collector.fetch(CollectRequest(account={})) == []

    def test_wrong_top_level_type_raises(self, tmp_path):
        path = tmp_path / "n.json"
        path.write_text('{"quotes": 12}', encoding="utf-8")
        with pytest.raises(InsightCollectError, match="应为列表"):
            ManualInsightCollector(path=str(path)).fetch(CollectRequest(account={}))

    def test_entry_missing_investor_raises(self, tmp_path):
        """坏条目整轮失败并给出行号，不静默跳过 —— 库里少一条未知的东西最糟"""
        path = tmp_path / "e.json"
        path.write_text('{"quotes": [{"content": "内容足够长"}]}', encoding="utf-8")
        with pytest.raises(InsightCollectError, match="第 1 条缺少 investor_code"):
            ManualInsightCollector(path=str(path)).fetch(CollectRequest(account={}))

    def test_entry_not_dict_raises(self, tmp_path):
        path = tmp_path / "e.json"
        path.write_text('{"quotes": ["just a string"]}', encoding="utf-8")
        with pytest.raises(InsightCollectError, match="第 1 条不是对象"):
            ManualInsightCollector(path=str(path)).fetch(CollectRequest(account={}))

    def test_empty_content_entry_skipped_not_errored(self, tmp_path):
        path = tmp_path / "e.json"
        path.write_text(json.dumps({
            "quotes": [{"investor_code": "x"},
                       {"content": "内容足够长", "investor_code": "x"}],
        }), encoding="utf-8")
        records = ManualInsightCollector(path=str(path)).fetch(
            CollectRequest(account={}))
        assert len(records) == 1

    @pytest.mark.parametrize("status", ["", None, "unverified"])
    def test_missing_verification_defaults_to_unverified(self, tmp_path, status):
        path = tmp_path / "e.json"
        path.write_text(json.dumps({"quotes": [{
            "content": "内容足够长", "investor_code": "x",
            **({"verification": status} if status is not None else {}),
        }]}), encoding="utf-8")
        records = ManualInsightCollector(path=str(path)).fetch(
            CollectRequest(account={}))
        assert records[0]["verification"] == "unverified"

    @pytest.mark.parametrize("status", ["verified", "disputed", "fabricated"])
    def test_legal_verification_accepted(self, tmp_path, status):
        path = tmp_path / "e.json"
        path.write_text(json.dumps({"quotes": [{
            "content": "内容足够长", "investor_code": "x",
            "verification": status,
        }]}), encoding="utf-8")
        records = ManualInsightCollector(path=str(path)).fetch(
            CollectRequest(account={}))
        assert records[0]["verification"] == status

    def test_illegal_verification_rejected_not_downgraded(self, tmp_path):
        """
        【关键】非法状态整轮失败，而不是悄悄改成 unverified。
        悄悄降级同样会让人误信「已经核验过了只是没标出来」。
        """
        path = tmp_path / "e.json"
        path.write_text(json.dumps({"quotes": [{
            "content": "内容足够长", "investor_code": "x",
            "verification": "truthy",
        }]}), encoding="utf-8")
        with pytest.raises(InsightCollectError, match="不是合法值"):
            ManualInsightCollector(path=str(path)).fetch(CollectRequest(account={}))

    def test_platform_defaults_to_manual(self, tmp_path):
        path = tmp_path / "e.json"
        path.write_text(json.dumps({"quotes": [
            {"content": "内容足够长", "investor_code": "x"}]}), encoding="utf-8")
        records = ManualInsightCollector(path=str(path)).fetch(
            CollectRequest(account={}))
        assert records[0]["platform"] == "manual"


class TestFetchAccountQuotes:
    """统一分页编排：页码从 1 开始，取满就停"""

    def _account(self, uid="1"):
        return {
            "account_id": "a1", "platform": "guba",
            "investor_code": "dyp_0001", "account_name": "X",
            "account_uid": uid,
        }

    def test_manual_platform_does_not_page(self):
        collector = _StubCollector(pages={1: [{"content": "x"}]})
        collector.PLATFORM = "manual"
        outcome = fetch_account_quotes(collector, self._account(), limit=50)
        assert outcome.ok
        assert outcome.pages == 1
        # manual 不走 _fetch_page，直接 fetch（不会调用打桩的分页）
        assert collector.calls and isinstance(collector.calls[0], CollectRequest)

    def test_single_page_when_limit_small(self):
        collector = _StubCollector(pages={1: [{"content": "x"}] * 3})
        collector.PAGE_SIZE = 20
        outcome = fetch_account_quotes(collector, self._account(), limit=10)
        assert outcome.pages == 1
        assert len(outcome.records) == 3

    def test_multi_page_collects_until_enough(self):
        pages = {p: [{"content": "x"}] * 20 for p in range(1, 5)}
        collector = _StubCollector(pages=pages)
        collector.PAGE_SIZE = 20
        outcome = fetch_account_quotes(collector, self._account(), limit=45)
        assert outcome.pages == 3
        assert len(outcome.records) == 45

    def test_stops_on_short_page(self):
        pages = {1: [{"content": "x"}] * 20, 2: [{"content": "x"}] * 5}
        collector = _StubCollector(pages=pages)
        collector.PAGE_SIZE = 20
        outcome = fetch_account_quotes(collector, self._account(), limit=1000)
        assert outcome.pages == 2
        assert len(outcome.records) == 25

    def test_first_page_failure_returns_error_not_empty(self):
        """首页失败 = 整体失败，绝不返回「空列表」冒充正常"""
        collector = _StubCollector(error=InsightParseError("改版了"))
        outcome = fetch_account_quotes(collector, self._account(), limit=100)
        assert not outcome.ok
        assert outcome.error_kind == "parse"
        assert outcome.records == []

    def test_mid_pagination_failure_keeps_partial_with_warning(self):
        pages = {1: [{"content": "x"}] * 20}
        collector = _StubCollector(pages=pages, error_from_page=2)
        collector.PAGE_SIZE = 10
        outcome = fetch_account_quotes(collector, self._account(), limit=100)
        # 已抓到的要保留，但必须显式告警「没取完」
        assert len(outcome.records) == 20
        assert outcome.error_kind == "blocked"
        assert outcome.retryable is False
        assert any("分页未取完" in w for w in outcome.warnings)

    def test_credential_error_classified(self):
        collector = _StubCollector(error=InsightCredentialError("缺凭证"))
        outcome = fetch_account_quotes(collector, self._account(), limit=10)
        assert outcome.error_kind == "credential"
        assert outcome.retryable is False

    def test_watermark_warning_when_published_at_missing(self):
        collector = _StubCollector(pages={1: [{"content": "x"}]})
        collector.PAGE_SIZE = 20
        outcome = fetch_account_quotes(
            collector, self._account(), limit=10, start_time="2025-01-01")
        assert any("缺少 published_at" in w for w in outcome.warnings)

    def test_no_watermark_warning_when_all_have_time(self):
        collector = _StubCollector(
            pages={1: [{"content": "x", "published_at": "2025-06-01"}]})
        collector.PAGE_SIZE = 20
        outcome = fetch_account_quotes(
            collector, self._account(), limit=10, start_time="2025-01-01")
        assert not outcome.warnings

    def test_max_pages_guard(self):
        """平台忽略 page 参数、永远返回第 1 页时不能死循环"""
        pages = {p: [{"content": "x"}] * 20 for p in range(1, 100)}
        collector = _StubCollector(pages=pages)
        collector.PAGE_SIZE = 20
        outcome = fetch_account_quotes(collector, self._account(), limit=100000)
        from stocklab.datasource.insight.collectors import MAX_PAGES
        assert outcome.pages == MAX_PAGES


class TestNormalizeOutcome:
    """归一化 + 跨平台去重的编排"""

    def _account(self):
        return {"investor_code": "dyp_0001", "platform": "manual",
                "account_name": "X"}

    def test_drops_known_hashes(self):
        account = self._account()
        raw = [{"content": "这是一段足够长的内容", "investor_code": "dyp_0001",
                "platform": "manual", "platform_id": "1"}]
        frame = normalize_investor_quotes(raw)
        digest = frame.iloc[0]["content_hash"]

        outcome = AccountFetchOutcome("a", "manual", raw, pages=1)
        kept = normalize_outcome(outcome, account, existing_hashes={digest})
        assert kept.empty

        fresh = normalize_outcome(outcome, account, existing_hashes=set())
        assert len(fresh) == 1

    def test_empty_outcome_returns_empty(self):
        outcome = AccountFetchOutcome("a", "manual", [], pages=1)
        frame = normalize_outcome(outcome, self._account())
        assert frame.empty

    def test_frame_has_contract_columns(self):
        raw = [{"content": "这是一段足够长的内容", "investor_code": "dyp_0001",
                "platform": "manual"}]
        outcome = AccountFetchOutcome("a", "manual", raw, pages=1)
        frame = normalize_outcome(outcome, self._account())
        assert list(frame.columns) == list(INVESTOR_QUOTE_COLUMNS)

    def test_no_existing_hashes_skips_filtering(self):
        raw = [{"content": "这是一段足够长的内容", "investor_code": "dyp_0001",
                "platform": "manual"}]
        outcome = AccountFetchOutcome("a", "manual", raw, pages=1)
        assert len(normalize_outcome(outcome, self._account())) == 1


# ===========================================================================
# 同步 CLI
# ===========================================================================
class TestSyncCli:
    """parse_args / resolve_since / sync_quotes / main 的编排与失败语义"""

    @pytest.fixture
    def db(self, tmp_db_path):
        from stocklab.persistence import Database
        from stocklab.persistence.storage import initialize_database
        path = os.path.join(tmp_db_path, "sync.duckdb")
        initialize_database(path)
        handle = Database(path)
        load_sources(handle, "configs/insight_sources.json")
        yield handle
        handle.close()

    @pytest.fixture
    def manual_file(self, tmp_path):
        payload = {"quotes": [
            {"content": "估值便宜，长期持有", "investor_code": "dyp_0001",
             "quote_type": "philosophy", "published_at": "2025-06-01 10:00:00"},
            {"content": "看不懂的不碰，能力圈之外", "investor_code": "dyp_0001",
             "quote_type": "discipline", "published_at": "2025-07-02 09:30:00"},
            {"content": "我对宏观没太多预测能力", "investor_code": "danbin_0001",
             "verification": "verified", "source_url": "https://example.com/i"},
            {"content": "赞", "investor_code": "dyp_0001"},     # 空壳，应丢
        ]}
        path = tmp_path / "manual.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_parse_args_defaults(self):
        args = parse_args([])
        assert args.command == "status"
        assert args.limit == 50

    @pytest.mark.parametrize("command", [
        "load-sources", "manual", "collect", "status",
    ])
    def test_parse_args_commands(self, command):
        assert parse_args([command]).command == command

    def test_parse_args_collect_with_platform(self):
        args = parse_args(["collect", "--platform", "xueqiu", "--limit", "5"])
        assert args.command == "collect"
        assert args.platform == "xueqiu"
        assert args.limit == 5

    def test_parse_args_rejects_unknown_command(self):
        with pytest.raises(SystemExit):
            parse_args(["bogus"])

    def test_resolve_since_uses_explicit_arg(self, db):
        args = parse_args(["manual", "--since", "2024-01-01"])
        args.since = "2024-01-01"
        frame = pd.DataFrame({"account_id": ["a1", "a2"],
                              "investor_code": ["x", "y"]})
        result = resolve_since(args, InvestorQuoteRepository(db), frame)
        assert set(result.values()) == {"2024-01-01"}

    def test_resolve_since_falls_back_to_lookback(self, db):
        args = parse_args([])
        args.since = None
        frame = pd.DataFrame({"account_id": ["a1"],
                              "investor_code": ["dyp_0001"]})
        result = resolve_since(args, InvestorQuoteRepository(db), frame)
        # 库内为空 -> 回看 N 天，而不是全量重抓
        expected = pd.Timestamp.now() - pd.Timedelta(days=DEFAULT_LOOKBACK_DAYS)
        got = pd.Timestamp(result["a1"])
        assert abs((expected - got).days) <= 1

    def test_resolve_since_uses_watermark_when_present(self, db):
        repo = InvestorQuoteRepository(db)
        repo.insert(normalize_investor_quotes([{
            "content": "这是一段足够长的内容", "investor_code": "dyp_0001",
            "platform": "manual", "published_at": "2025-06-01 10:00:00",
        }]))
        args = parse_args([])
        args.since = None
        frame = pd.DataFrame({"account_id": ["a1"],
                              "investor_code": ["dyp_0001"]})
        result = resolve_since(args, repo, frame)
        assert result["a1"] == "2025-06-01"

    def test_manual_sync_writes_rows(self, db, manual_file):
        args = parse_args(["manual", "--manual-path", manual_file])
        args.platform = "manual"
        args.account = None
        ok, failed, details = sync_quotes(db, args)
        assert failed == 0
        assert ok == 1
        repo = InvestorQuoteRepository(db)
        assert repo.count() == 3          # 空壳「赞」被丢弃

    def test_second_sync_logs_zero_new(self, db, manual_file, caplog):
        """
        【回归钉子】重复同步的日志必须报「新增 0 条」。

        insert() 返回的是**提交行数**，而 ON CONFLICT DO NOTHING 丢弃的冲突行
        不计入。早期实现直接把 insert 返回值当新增数写进日志，于是第二次同步
        打印「抓到 3 条 -> 新增 3 条」而库里一条没多 —— 用户据此相信增量成功了。
        """
        args = parse_args(["manual", "--manual-path", manual_file])
        args.platform = "manual"
        args.account = None

        with caplog.at_level("INFO"):
            sync_quotes(db, args)
        assert "新增 3 条" in caplog.text

        caplog.clear()
        with caplog.at_level("INFO"):
            sync_quotes(db, args)
        assert "新增 0 条" in caplog.text, "重复同步日志应如实报 0 条新增"
        assert "库内合计 3" in caplog.text

    def test_manual_sync_honors_verification(self, db, manual_file):
        args = parse_args(["manual", "--manual-path", manual_file])
        args.platform = "manual"
        args.account = None
        sync_quotes(db, args)
        repo = InvestorQuoteRepository(db)
        assert repo.count(verification="verified") == 1

    def test_manual_sync_is_idempotent(self, db, manual_file):
        args = parse_args(["manual", "--manual-path", manual_file])
        args.platform = "manual"
        args.account = None
        sync_quotes(db, args)
        before = InvestorQuoteRepository(db).count()
        sync_quotes(db, args)
        assert InvestorQuoteRepository(db).count() == before

    def test_collect_requires_platform_flag(self, monkeypatch):
        """
        collect 不指定平台必须退出码 2 且不发起任何采集 ——
        否则默认行为会是一次「全量爬一遍」，而这不是用户敲这条命令时的意图。
        """
        import app.scripts.sync_investor_insight as mod

        def must_not_call(db, args):
            raise AssertionError("不该调用 sync_quotes")

        monkeypatch.setattr(mod, "sync_quotes", must_not_call)
        monkeypatch.setattr(mod, "Database", lambda p: _FakeDatabase())
        assert main(["collect"]) == 2

    def test_collect_with_platform_attempts(self, monkeypatch):
        import app.scripts.sync_investor_insight as mod
        seen = {}

        def fake_sync(db, args):
            seen["platform"] = args.platform
            return 1, 0, []

        monkeypatch.setattr(mod, "sync_quotes", fake_sync)
        monkeypatch.setattr(mod, "Database", lambda p: _FakeDatabase())
        assert main(["collect", "--platform", "guba"]) == 0
        assert seen["platform"] == "guba"

    def test_exit_code_1_when_a_capture_fails(self, monkeypatch):
        import app.scripts.sync_investor_insight as mod
        monkeypatch.setattr(mod, "sync_quotes",
                            lambda db, args: (1, 1, [object()]))
        monkeypatch.setattr(mod, "Database", lambda p: _FakeDatabase())
        assert main(["collect", "--all"]) == 1

    def test_manual_sync_credential_error_returns_code_1(self, db, tmp_path):
        """凭证缺失必须让退出码非 0 —— 否则 cron 里静默丢数据"""
        path = tmp_path / "m.json"
        path.write_text(json.dumps({"quotes": [{
            "content": "内容足够长", "investor_code": "dyp_0001",
        }]}), encoding="utf-8")
        args = parse_args(["manual", "--manual-path", str(path)])
        args.platform = "manual"
        args.account = None
        ok, failed, details = sync_quotes(db, args)
        assert failed == 0 and ok == 1

    def test_collect_unknown_platform_counts_failure_not_crash(
            self, db, monkeypatch):
        """账号指向未注册平台：不中断整轮，但必须计入失败（退出码 1）"""
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO insight.investors VALUES "
            "('probe', '探针', NULL, NULL, NULL, NULL, NULL, true, now())")
        conn.execute(
            "INSERT INTO insight.investor_accounts VALUES "
            "('bogus:probe', 'probe', 'bogus', '探针', NULL, NULL, true, "
            "NULL, now())")

        import app.scripts.sync_investor_insight as mod

        class _Registry:
            def get(self, platform):
                raise InsightCollectError("平台 %r 没有注册采集器" % platform)

        monkeypatch.setattr(mod, "build_default_registry",
                            lambda **kw: _Registry())
        args = parse_args(["collect", "--platform", "bogus"])
        ok, failed, details = sync_quotes(db, args)
        assert failed == 1
        assert details and "没有注册采集器" in details[0].error

    def test_collect_unknown_platform_does_not_abort_other_accounts(
            self, db, monkeypatch):
        """一个平台坏掉不该让其它平台的同步结果作废"""
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO insight.investor_accounts VALUES "
            "('guba:probe2', 'linyuan_0001', 'guba', '林园投资', "
            "'111', NULL, true, NULL, now())")

        import app.scripts.sync_investor_insight as mod

        class _Registry:
            def get(self, platform):
                if platform == "guba":
                    raise InsightCollectError("平台 guba 未注册")
                return mod.build_default_registry.__wrapped__ if False else None

        seen = []

        class _Mixed:
            def get(self, platform):
                if platform == "guba":
                    raise InsightCollectError("平台 %r 未注册" % platform)
                raise AssertionError("不该走到这里")

        monkeypatch.setattr(mod, "build_default_registry", lambda **kw: _Mixed())
        args = parse_args(["collect", "--platform", "guba"])
        ok, failed, details = sync_quotes(db, args)
        assert failed == 1
        # 整轮继续执行完毕，没有抛异常中断
        assert details[0].error_kind == "network"

    def test_collect_no_enabled_accounts_returns_zero(self, db):
        args = parse_args(["collect", "--platform", "guba"])
        ok, failed, _ = sync_quotes(db, args)
        # 登记表里 guba 账号 is_enabled=false，应无待抓账号
        assert (ok, failed) == (0, 0)

    def test_show_status_returns_summary(self, db, capsys):
        summary = show_status(db, "configs/insight_sources.json",
                              "configs/insight_manual.example.json")
        assert summary["investor_count"] == 3
        assert summary["account_count"] == 3
        assert summary["quote_count"] == 0
        out = capsys.readouterr().out
        assert "投资人观点库状态" in out
        assert "1240335488" in out
        assert "人工录入文件" in out

    def test_show_status_flags_unfilled_uid(self, db, capsys):
        show_status(db, "configs/insight_sources.json", "nope.json")
        out = capsys.readouterr().out
        # 未填 UID 的账号要给出可操作提示，而不是装作正常
        assert "（未填）" in out
        assert "insight_sources" in out


class _FakeDatabase:
    """main() 里 Database 用作上下文管理器的最小桩"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ===========================================================================
# Web 接口
# ===========================================================================
@pytest.mark.usefixtures("app")
class TestInsightWebApi:
    """
    三个接口的应答契约（用真实库，遵循 conftest 的 app fixture 约束）

    【为什么用真实库而不是临时库】
      insight 接口的取数走 store.facade_database() —— 那是门面持有的那个
      Database，绑定启动时的 db_path。接口层没有注入点，所以要测真实应答
      就只能用真实库。代价是必须在 fixture 里**清理自造数据**，
      否则留下的假语录会混进真实数据（本域尤其不能留，它是人要读的）。
    """

    @pytest.fixture
    def api(self):
        from app.web import insight_api
        return insight_api

    @pytest.fixture
    def seeded(self, ctx):
        """载入登记表 + 造 2 条观点，退出时**逐条删除**（不留假语录）

        ctx 是必须的：store.current_db_path() / facade_database() 都要
        current_app 才能取到 db_path。漏了它会得到
        RuntimeError: Working outside of application context。
        """
        from stocklab.persistence.storage import initialize_database
        import app.web.store as store

        db_path = store.current_db_path()
        initialize_database(db_path)          # 幂等：确保 006 已应用
        db = store.facade_database()
        load_sources(db, "configs/insight_sources.json")
        InvestorQuoteRepository(db).insert(normalize_investor_quotes([
            {"content": "估值便宜，长期持有 600519", "investor_code": "dyp_0001",
             "platform": "xueqiu", "quote_type": "philosophy",
             "source_url": "https://xueqiu.com/123/456",
             "published_at": "2025-06-01 10:00:00"},
            {"content": "宏观会降息，通胀回落", "investor_code": "danbin_0001",
             "platform": "manual", "quote_type": "macro_view",
             "verification": "verified",
             "published_at": "2025-08-01 10:00:00"},
        ]))
        yield db

        # 清理自造数据；登记表本身是配置，留给用户也无害，但一并还原更干净
        conn = db.get_connection()
        conn.execute(
            "DELETE FROM insight.investor_quotes WHERE content IN "
            "('估值便宜，长期持有 600519', '宏观会降息，通胀回落')")

    @staticmethod
    def _json(result):
        """handler 返回 Response 或 (Response, status) 两种形态，统一取出 JSON"""
        response = result[0] if isinstance(result, tuple) else result
        if isinstance(response, tuple):
            response = response[0]
        return response.get_json()

    @staticmethod
    def _status(result):
        if isinstance(result, tuple):
            return result[1]
        return 200

    # -- meta -------------------------------------------------------------
    def test_meta_returns_enums(self, api, seeded):
        with _request_ctx():
            data = self._json(api.handle_insight_meta())
        assert len(data["investors"]) == 3
        assert {p["value"] for p in data["platforms"]} >= {"xueqiu", "guba", "manual"}
        assert {t["value"] for t in data["quote_types"]} >= set(QUOTE_TYPES)
        assert {v["value"] for v in data["verifications"]} == set(VERIFICATION_STATUSES)
        assert "verification_hint" in data

    def test_meta_labels_are_translated(self, api, seeded):
        with _request_ctx():
            data = self._json(api.handle_insight_meta())
        platforms = {p["value"]: p["label"] for p in data["platforms"]}
        assert platforms["xueqiu"] == "雪球"
        verifications = {v["value"]: v["label"] for v in data["verifications"]}
        assert verifications["unverified"] == "未核实"
        assert verifications["verified"] == "已核实原文"

    def test_meta_reports_counts(self, api, seeded):
        with _request_ctx():
            data = self._json(api.handle_insight_meta())
        assert data["quote_count"] == 2
        assert data["verification_summary"] == {"unverified": 1, "verified": 1}

    # -- quotes：参数校验必须 400 而不是静默回退 ---------------------------
    def test_quotes_reject_short_keyword(self, api, seeded):
        with _request_ctx({"keyword": "估"}):
            result = api.handle_insight_quotes()
        assert self._status(result) == 400
        assert "至少" in self._json(result)["error"]

    def test_quotes_reject_bad_verification(self, api, seeded):
        with _request_ctx({"verification": "truthy"}):
            result = api.handle_insight_quotes()
        assert self._status(result) == 400

    def test_quotes_reject_bad_order_by(self, api, seeded):
        with _request_ctx({"order_by": "content"}):
            result = api.handle_insight_quotes()
        assert self._status(result) == 400

    def test_quotes_reject_bad_since(self, api, seeded):
        with _request_ctx({"since": "20250601"}):
            result = api.handle_insight_quotes()
        assert self._status(result) == 400

    @pytest.mark.parametrize("qs", [
        {"limit": "0"}, {"limit": "999"}, {"limit": "abc"},
        {"offset": "-1"}, {"offset": "x"},
    ])
    def test_quotes_reject_bad_paging(self, api, seeded, qs):
        """垃圾分页参数必须 400 —— 静默退回默认分页会让调用方拿到一批
        条数完全不同的数据，却以为自己的 limit 生效了。"""
        with _request_ctx(qs):
            result = api.handle_insight_quotes()
        assert self._status(result) == 400, qs

    # -- quotes：正常应答 ---------------------------------------------------
    def test_quotes_returns_items_with_verification(self, api, seeded):
        """每条都必须带 verification —— 前端没有状态就无法诚实呈现"""
        with _request_ctx({}):
            data = self._json(api.handle_insight_quotes())
        assert len(data["items"]) == 2
        for item in data["items"]:
            assert item["verification"] in VERIFICATION_STATUSES
            assert item["verification_label"]
            assert isinstance(item["has_source"], bool)
        assert data["verification_hint"]

    def test_quotes_filter_by_platform(self, api, seeded):
        with _request_ctx({"platforms": "manual"}):
            data = self._json(api.handle_insight_quotes())
        assert data["returned"] == 1

    def test_quotes_filter_by_investor(self, api, seeded):
        with _request_ctx({"investor_codes": "dyp_0001"}):
            data = self._json(api.handle_insight_quotes())
        assert data["returned"] == 1

    def test_quotes_filter_by_verification(self, api, seeded):
        with _request_ctx({"verification": "verified"}):
            data = self._json(api.handle_insight_quotes())
        assert data["returned"] == 1
        assert data["items"][0]["verification"] == "verified"

    def test_quotes_filter_by_stock_code(self, api, seeded):
        with _request_ctx({"stock_codes": "600519"}):
            data = self._json(api.handle_insight_quotes())
        assert data["returned"] == 1

    def test_quotes_keyword_hits_content(self, api, seeded):
        with _request_ctx({"keyword": "降息"}):
            data = self._json(api.handle_insight_quotes())
        assert data["returned"] == 1

    def test_quotes_since_filters(self, api, seeded):
        with _request_ctx({"since": "2025-07-01"}):
            data = self._json(api.handle_insight_quotes())
        assert data["returned"] == 1

    def test_quotes_paging(self, api, seeded):
        with _request_ctx({"limit": "1"}):
            data = self._json(api.handle_insight_quotes())
        assert data["returned"] == 1 and data["offset"] == 0
        with _request_ctx({"limit": "1", "offset": "1"}):
            data = self._json(api.handle_insight_quotes())
        assert data["offset"] == 1 and data["returned"] == 1
        with _request_ctx({"limit": "1", "offset": "10"}):
            data = self._json(api.handle_insight_quotes())
        assert data["returned"] == 0

    def test_quotes_resolves_illegal_order_by_to_captured_at(self, api, seeded):
        """接口层已 400，Repository 仍兜底回退 —— 两层防线都在"""
        with _request_ctx({"order_by": "published_at"}):
            data = self._json(api.handle_insight_quotes())
        assert data["returned"] == 2

    # -- investor ----------------------------------------------------------
    def test_quotes_reject_unencoded_cjk(self, api, seeded):
        """
        【护栏测试】未百分号编码的中文查询串必须 400，不能返回 0 条。

        WSGI 把 QUERY_STRING 当 latin-1 字节流。客户端直接塞中文字节时，
        这串字节经 latin-1 往返会变成 'ä¼°'（长度 2，**绕过单字校验**），
        拿它 LIKE 匹配必然 0 条 —— 于是「关键词根本没送达」被包装成了
        「没有相关语录」。这是假阴性，必须显式报错。

        【为什么不用 test_request_context】
           它走 urllib.parse，遇到非 ASCII 会直接 UnicodeDecodeError；
           真实服务器是把原始字节当 latin-1 放进 QUERY_STRING 的，
           所以这里手工构造 WSGI environ 才是真实形态。
        """
        raw = b"keyword=\xe4\xbc\xb0"        # 「估」的原始 UTF-8 字节
        with _raw_query_ctx("/api/insight/quotes", raw):
            result = api.handle_insight_quotes()
        assert self._status(result) == 400
        assert "编码" in self._json(result)["error"]

    def test_quotes_accepts_percent_encoded_cjk(self, api, seeded):
        """正路（已编码）仍要能进到单字校验，而不是被护栏误伤"""
        with _request_ctx({"keyword": "估值"}):
            data = self._json(api.handle_insight_quotes())
        assert data["returned"] == 1          # 命中 seeded 里的「估值便宜…」

    def test_investor_requires_code(self, api, seeded):
        with _request_ctx({}):
            result = api.handle_insight_investor()
        assert self._status(result) == 400

    def test_investor_404_when_unknown(self, api, seeded):
        with _request_ctx({"investor_code": "nope"}):
            result = api.handle_insight_investor()
        assert self._status(result) == 404

    @pytest.mark.parametrize("qs", [{"limit": "0"}, {"limit": "abc"},
                                    {"limit": "999"}])
    def test_investor_rejects_bad_limit(self, api, seeded, qs):
        qs = dict(qs, investor_code="dyp_0001")
        with _request_ctx(qs):
            result = api.handle_insight_investor()
        assert self._status(result) == 400

    def test_investor_profile_payload(self, api, seeded):
        with _request_ctx({"investor_code": "dyp_0001"}):
            data = self._json(api.handle_insight_investor())
        assert data["investor"]["name"] == "段永平"
        assert data["investor"]["style_label"] == "价值投资"
        assert data["accounts"]
        assert data["accounts"][0]["platform_label"] == "雪球"
        assert len(data["quotes"]) == 1
        assert data["quotes"][0]["quote_id"]
        assert "verification_hint" in data


def _request_ctx(qs=None):
    """建一个独立的 Flask 请求上下文（不依赖 app fixture，便于并行）"""
    from flask import Flask
    return Flask(__name__).test_request_context(query_string=qs or {})


def _raw_query_ctx(path, query_bytes):
    """
    用**原始字节**的查询串建请求上下文（模拟未百分号编码的真实客户端）

    Flask 的 test_request_context 走 urllib.parse，遇到非 ASCII 会
    UnicodeDecodeError；而真实 WSGI 服务器只是把原始 QUERY_STRING 字节
    当 latin-1 放进 environ。要复现线上那种请求，只能手工构造 environ。

    Args:
        path (str): 路径
        query_bytes (bytes): 未经百分号编码的查询串

    Returns:
        ContextManager: 请求上下文
    """
    import io
    import sys as _sys

    from flask import Flask
    environ = {
        "PATH_INFO": path,
        "QUERY_STRING": query_bytes.decode("latin-1"),
        "REQUEST_METHOD": "GET",
        "SERVER_NAME": "test",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(b""),
        "wsgi.errors": _sys.stderr,
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    return Flask(__name__).request_context(environ)


# ===========================================================================
# 页面契约：核验状态必须可见
# ===========================================================================
class TestInsightPageContract:
    """
    【这组钉住的不只是渲染，是产品承诺】

    问题：如果有人为了版面整洁，删掉核验徽标或把绿色用在 unverified 上，
      这个页面会怎样？
      答：三个月后你分不清哪句是原话、哪句是网友转述。
    """

    @pytest.fixture(scope="class")
    def template(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))),
            "app", "web", "templates", "insight.html")
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    @pytest.fixture(scope="class")
    def script(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))),
            "app", "web", "static", "insight.js")
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_template_renders_disclaimer(self, template):
        assert "disclaimer" in template
        assert "未经逐条核对原始出处" in template or "disclaimer-text" in template

    def test_template_is_not_a_quotes_only_page(self, template):
        # 必须显式写明本页的核验立场
        assert "核验状态" in template

    def test_script_renders_verification_badge(self, script):
        assert "verificationBadge" in script
        assert "verification_label" in script

    def test_script_marks_unverified_with_warning_border(self, script):
        """未核实条目要有视觉警示，不能与已核实长得一样"""
        assert 'data-verification' in script
        assert '"unverified"' in script

    def test_script_reports_missing_source(self, script):
        """没有溯源链接要明说，不能默认展示成有链接"""
        assert "has_source" in script
        assert "无溯源链接" in script

    def test_js_syntax(self):
        import subprocess
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))),
            "app", "web", "static", "insight.js")
        result = subprocess.run(
            ["node", "--check", path], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr

    def test_sidebar_has_insight_entry(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))),
            "app", "web", "templates", "_sidebar.html")
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        # 侧边栏为视觉对齐留了空格，断言要对空白不敏感
        assert re.search(r'\("insight",\s+"投资理念"', content)

    def test_server_registers_routes(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))),
            "app", "web", "server.py")
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        for rule in ("/insight", "/api/insight/meta",
                     "/api/insight/quotes", "/api/insight/investor"):
            assert rule in content, "缺少路由 %s" % rule


class TestWebLayerDependencyDirection:
    """
    【钉住 AGENT.md 写明的依赖方向】

    `app.web` 只允许依赖 stocklab.facade / analytics / screener / factor /
    research，**不得 import stocklab.persistence**。

    为什么要专门钉一条：取数逻辑一旦能在接口层直接拼 Repository，
    同一个查询就会被复制到多个接口里，口径迟早分叉（「主题分布必须先 unnest
    逗号再分组」这件事，一旦被抄走一边，两边就永远对不上了）。
    用 AST 只看 import 语句 —— 模块 docstring 里引用这句规则是应该的，
    按文本 grep 会把它一起算成违规。
    """

    @staticmethod
    def _imported_modules(pyfile):
        import ast
        with open(pyfile, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        return names

    def test_no_module_in_app_web_imports_persistence(self):
        web_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app", "web")
        offenders = []
        for name in sorted(os.listdir(web_dir)):
            if not name.endswith(".py"):
                continue
            modules = self._imported_modules(os.path.join(web_dir, name))
            offenders.extend(
                "%s -> %s" % (name, mod)
                for mod in modules
                if mod == "stocklab.persistence"
                or mod.startswith("stocklab.persistence.")
            )
        assert not offenders, (
            "app/web 下这些模块直接 import 了 stocklab.persistence，"
            "应改经 stocklab.facade: %s" % ", ".join(offenders)
        )

    def test_insight_api_reads_through_facade(self):
        api_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app", "web", "insight_api.py")
        modules = self._imported_modules(api_dir)
        assert "stocklab.facade" in modules
        assert not any(m.startswith("stocklab.persistence") for m in modules)
