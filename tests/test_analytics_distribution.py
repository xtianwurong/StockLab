"""
市盈率分布统计与三类报告渲染 (tests/test_analytics_distribution.py)

覆盖 stocklab/analytics 下此前零覆盖的四个模块：
  valuation_distribution.py —— 分布分析器 + 4 个结果实体
  profile_reporter.py       —— 纯文本报告
  markdown_reporter.py       —— Markdown 报告
  percentile_reporter.py     —— 单股分位控制台 / Markdown 报告

【为什么这四个模块此前完全没测】
  它们是「本地看板的文案生成器」，日常开发只会改 Web 页面，感觉不到它们；
  但它们的输出直接决定用户看到的口径说明，一旦算错或漏分支，
  页面上会出现「中位数 0.00」「占比 100.00%」这种看起来正常的错数字。

运行：
  ./venv/bin/python -m pytest tests/test_analytics_distribution.py -v
  （纯合成数据，不联网、不碰数据库）
"""

import os
import sys

import pandas as pd
import pytest

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.analytics.valuation_distribution import (
    PE_MIN_REASONABLE_SAMPLE_COUNT,
    DistributionBucket,
    MarketValuationDistribution,
    PeRankEntry,
    ValuationDistributionAnalyzer,
    ValuationDistributionProfile,
)
from stocklab.analytics.profile_reporter import ValuationDistributionReporter
from stocklab.analytics.markdown_reporter import ValuationDistributionMarkdownReporter
from stocklab.analytics.percentile_reporter import ValuationPercentileReporter


# ---------------------------------------------------------------------------
# 合成输入
# ---------------------------------------------------------------------------
@pytest.fixture
def valuation_df():
    """一份含各种边界情形的估值快照：正常 / 亏损 / 缺失 / 超高 / 重复交易日"""
    return pd.DataFrame(
        {
            "ts_code": ["600519.SH", "000001.SZ", "300750.SZ", "601234.SH",
                        "600036.SH", "000002.SZ"],
            "trade_date": pd.to_datetime(
                ["2026-09-30"] * 5 + ["2026-09-29"]),
            "pe_ttm": [28.0, 5.0, 45.0, -8.0, float("nan"), 1200.0],
        }
    )


@pytest.fixture
def securities_df():
    return pd.DataFrame(
        {
            "ts_code": ["600519.SH", "000001.SZ", "300750.SZ", "601234.SH", "600036.SH"],
            "name": ["贵州茅台", "平安银行", "宁德时代", "某某退", "招商银行"],
            "market": ["SH", "SZ", "SZ", "SH", "SZ"],
        }
    )


@pytest.fixture
def profile(valuation_df, securities_df):
    """正常路径的统计结果"""
    return ValuationDistributionAnalyzer().analyze(valuation_df, securities_df)


# ---------------------------------------------------------------------------
# 一、结果实体：纯数据载体 + 派生计算
# ---------------------------------------------------------------------------
def test_bucket_ratio_and_dict():
    bucket = DistributionBucket("20-30", 20.0, 30.0, 3, 10)
    assert bucket.ratio() == 0.3
    assert bucket.to_dict() == {
        "label": "20-30", "lower_bound": 20.0, "upper_bound": 30.0,
        "stock_count": 3, "total_count": 10,
    }


def test_bucket_ratio_zero_denominator():
    """占比分母为 0 时必须返回 0.0 而不是 ZeroDivisionError"""
    assert DistributionBucket("20-30", 20.0, 30.0, 0, 0).ratio() == 0.0


def test_rank_entry_dict():
    assert PeRankEntry("600519.SH", "贵州茅台", "SH", 28.0).to_dict() == {
        "ts_code": "600519.SH", "name": "贵州茅台", "market": "SH", "pe_value": 28.0,
    }


def test_market_distribution_dict():
    item = MarketValuationDistribution("SH", 5, 20.0, 8.0, 60.0)
    assert item.to_dict() == {
        "market": "SH", "valid_pe_count": 5,
        "median_pe": 20.0, "min_pe": 8.0, "max_pe": 60.0,
    }


def test_profile_empty_defaults():
    profile = ValuationDistributionProfile("pe_ttm")
    assert profile.status == "正常"
    assert profile.is_empty() is True
    assert profile.coverage_ratio() == 0.0, "总样本为 0 时覆盖率应为 0"
    assert profile.is_representative() is False
    assert profile.to_dict()["pe_column"] == "pe_ttm"


def test_profile_representative_threshold(profile):
    """代表性判定用的是库层的常量，不允许被调用方绕过"""
    assert profile.is_representative() is (profile.valid_pe_count >= PE_MIN_REASONABLE_SAMPLE_COUNT)
    profile.valid_pe_count = PE_MIN_REASONABLE_SAMPLE_COUNT
    assert profile.is_representative() is True
    profile.valid_pe_count = PE_MIN_REASONABLE_SAMPLE_COUNT - 1
    assert profile.is_representative() is False


# ---------------------------------------------------------------------------
# 二、分析器：样本口径
# ---------------------------------------------------------------------------
def test_sample_breakdown(profile):
    """总样本 = 有效 + 无效 + 非正，三者必须闭合"""
    assert profile.total_stock_count == 6
    assert profile.valid_pe_count == 4, "PE>0 的才是有效样本"
    assert profile.non_positive_pe_count == 1, "PE<0 的亏损样本单列"
    assert profile.invalid_pe_count == 1, "PE 为 NaN 的算无效"
    assert (profile.total_stock_count
            == profile.valid_pe_count + profile.invalid_pe_count
            + profile.non_positive_pe_count)
    assert profile.is_empty() is False


def test_coverage_ratio(profile):
    assert profile.coverage_ratio() == pytest.approx(4 / 6)


def test_trade_date_is_max_not_request_date(profile):
    """数据日期取样本里的最大交易日，不是调用方请求的日期"""
    assert profile.trade_date == "2026-09-30"


def test_trade_date_empty_without_column(valuation_df):
    result = ValuationDistributionAnalyzer().analyze(valuation_df.drop(columns=["trade_date"]))
    assert result.trade_date == "", "无日期列时应为空串而不是 None"


def test_trade_date_all_na(valuation_df):
    frame = valuation_df.copy()
    frame["trade_date"] = pd.to_datetime([None] * len(frame))
    assert ValuationDistributionAnalyzer().analyze(frame).trade_date == ""


# ---------------------------------------------------------------------------
# 三、分析器：集中趋势与截尾均值
# ---------------------------------------------------------------------------
def test_central_tendency(profile):
    assert profile.min_pe == 5.0
    assert profile.max_pe == 1200.0
    assert profile.mean_pe == pytest.approx((28.0 + 5.0 + 45.0 + 1200.0) / 4)
    assert profile.median_pe == pytest.approx((28.0 + 45.0) / 2)


def test_trimmed_mean_excludes_extreme_high(profile):
    """截尾均值必须排除超过阈值的极端高值，否则被 1200 倍一只拉飞"""
    assert profile.extreme_high_count == 1, "1200 倍那只是极端高值"
    assert profile.trimmed_mean_pe == pytest.approx((28.0 + 5.0 + 45.0) / 3)
    assert profile.trimmed_mean_pe != profile.mean_pe


def test_trimmed_mean_none_when_all_below_threshold(valuation_df):
    frame = valuation_df.copy()
    frame["pe_ttm"] = [10.0, 12.0, 11.0, 9.0, 13.0, 14.0]
    result = ValuationDistributionAnalyzer().analyze(frame)
    assert result.extreme_high_count == 0
    assert result.trimmed_mean_pe == pytest.approx(result.mean_pe)


# ---------------------------------------------------------------------------
# 四、分析器：分位与分桶
# ---------------------------------------------------------------------------
def test_quantiles_present(profile):
    assert profile.quantiles, "应给出各分位点"
    labels = [item[0] for item in profile.quantiles]
    values = [item[1] for item in profile.quantiles]
    assert values == sorted(values), "分位数值必须单调递增"
    assert len(labels) == len(values)
    assert profile.min_pe <= values[0] and values[-1] <= profile.max_pe


def test_buckets_cover_all_valid_samples(profile):
    """各桶只数之和必须等于有效样本数，且没有一只被重复计入"""
    assert sum(b.stock_count for b in profile.buckets) == profile.valid_pe_count
    for bucket in profile.buckets:
        assert 0.0 <= bucket.ratio() <= 1.0


def test_last_bucket_is_open_ended(profile):
    """最后一档右端开放（上界 None），不能被当成闭区间丢掉大值"""
    last = profile.buckets[-1]
    assert last.upper_bound is None
    assert last.stock_count >= 1, "1200 倍应落在最后一档"


def test_buckets_are_left_open_right_closed(profile):
    for bucket in profile.buckets[:-1]:
        assert bucket.lower_bound < bucket.upper_bound


# ---------------------------------------------------------------------------
# 五、分析器：分交易所与极值榜
# ---------------------------------------------------------------------------
def test_market_distributions(profile):
    markets = {item.market: item for item in profile.market_distributions}
    # 000002.SZ 不在证券基础信息表里 -> 无交易所归属 -> 不参与分组，
    # 所以 SZ 是 2 只（平安 + 宁德），不是 3 只
    assert set(markets) == {"SH", "SZ"}, "只有 SH / SZ 有有效样本"
    assert markets["SH"].valid_pe_count == 1, "SH 只有茅台有效（退市股 PE<0、招行 NaN）"
    assert markets["SZ"].valid_pe_count == 2
    # 注意：分组合计可以小于 valid_pe_count —— 没有交易所归属的样本被整体略过
    grouped = sum(item.valid_pe_count for item in profile.market_distributions)
    assert grouped <= profile.valid_pe_count
    assert grouped == 3, "000002.SZ 无归属被略过，故 4 - 1 = 3"
    assert markets["SZ"].min_pe <= markets["SZ"].median_pe <= markets["SZ"].max_pe


def test_market_distributions_absent_without_securities(valuation_df):
    """没给证券基础信息时无法分组交易所，应整体不展示而不是渲染一行空归属"""
    result = ValuationDistributionAnalyzer().analyze(valuation_df)
    assert result.market_distributions == []
    assert result.valid_pe_count == 4, "样本口径不受影响"


def test_market_distributions_ignore_blank_market(valuation_df):
    securities = pd.DataFrame({
        "ts_code": ["600519.SH", "000001.SZ", "300750.SZ", "601234.SH", "600036.SH"],
        "name": ["a", "b", "c", "d", "e"],
        "market": ["SH", "  ", "SZ", "SH", ""],
    })
    result = ValuationDistributionAnalyzer().analyze(valuation_df, securities)
    assert {item.market for item in result.market_distributions} == {"SH", "SZ"}


def test_rank_entries_sorted_and_limited(profile):
    assert [e.pe_value for e in profile.lowest_pe_entries] == sorted(
        e.pe_value for e in profile.lowest_pe_entries)
    assert [e.pe_value for e in profile.highest_pe_entries] == sorted(
        (e.pe_value for e in profile.highest_pe_entries), reverse=True)
    assert profile.lowest_pe_entries[0].pe_value == 5.0
    assert profile.highest_pe_entries[0].pe_value == 1200.0


def test_rank_entry_count_is_capped(valuation_df):
    analyzer = ValuationDistributionAnalyzer(extreme_entry_count=2)
    result = analyzer.analyze(valuation_df)
    assert len(result.lowest_pe_entries) == 2
    assert len(result.highest_pe_entries) == 2


def test_rank_entry_name_falls_back_to_code(valuation_df, securities_df):
    """合并不到简称时必须用代码兜底，报告里不能出现空名称"""
    result = ValuationDistributionAnalyzer().analyze(valuation_df, securities_df)
    names = {entry.name for entry in result.lowest_pe_entries}
    assert "平安银行" in names, "能匹配上的应显示简称"
    assert "000002.SZ" in names, "不在证券表里的必须回落到代码本身"
    assert all(entry.name for entry in result.lowest_pe_entries)


def test_identity_columns_are_dropped_not_merged(valuation_df, securities_df):
    """估值表自带的 name/market 必须被丢弃而不是与证券表合并出 _x/_y

    理由：market.daily_valuations 只存数值，身份列以 reference.securities
    为准；估值表里若残留旧值，合并会让列变成 name_x / name_y，报告就取错了。
    """
    frame = valuation_df.copy()
    frame["name"] = ["脏名"] * len(frame)
    frame["market"] = ["ZZ"] * len(frame)
    result = ValuationDistributionAnalyzer().analyze(frame, securities_df)
    entry = next(e for e in result.lowest_pe_entries if e.ts_code == "600519.SH")
    assert entry.name == "贵州茅台", "以证券表为准"
    assert entry.market == "SH"


# ---------------------------------------------------------------------------
# 六、分析器：不可用的输入（必须明确降级，不能抛异常）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad,fragment", [
    (None, "未取到"),
    (pd.DataFrame(), "未取到"),
])
def test_analyze_empty_input(bad, fragment):
    result = ValuationDistributionAnalyzer().analyze(bad)
    assert result.is_empty() is True
    assert result.status != "正常"
    assert fragment in result.status, result.status


def test_analyze_missing_pe_column(valuation_df):
    result = ValuationDistributionAnalyzer(pe_column="pe").analyze(valuation_df)
    assert "缺少 PE 列" in result.status
    assert result.is_empty() is True


def test_analyze_all_loss_making(valuation_df):
    frame = valuation_df.copy()
    frame["pe_ttm"] = [-1.0, -2.0, 0.0, -3.0, -4.0, 0.0]
    result = ValuationDistributionAnalyzer().analyze(frame)
    assert result.is_empty() is True
    assert "无有效市盈率样本" in result.status
    assert result.non_positive_pe_count == 6, "PE=0 也算非正（不分位口径同样剔除）"


def test_analyze_securities_without_ts_code(valuation_df):
    """证券表结构不对时降级为代码兜底，而不是崩掉"""
    result = ValuationDistributionAnalyzer().analyze(
        valuation_df, pd.DataFrame({"name": ["x"], "market": ["SH"]}))
    assert result.market_distributions == []
    assert result.valid_pe_count == 4


def test_analyze_drops_stale_identity_columns(valuation_df, securities_df):
    """估值表里若已带同名列，合并前必须先剔除，否则会出现 _x / _y 后缀"""
    frame = valuation_df.copy()
    frame["name"] = ["脏名"] * len(frame)
    frame["market"] = ["ZZ"] * len(frame)
    result = ValuationDistributionAnalyzer().analyze(frame, securities_df)
    assert result.lowest_pe_entries[0].name == "平安银行", "应以证券表为准"
    assert "ZZ" not in {item.market for item in result.market_distributions}


# ---------------------------------------------------------------------------
# 七、三类报告渲染
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fragment", [
    "A 股全市场市盈率分布统计", "统计口径", "数据日期", "取数通路", "样本总数",
    "【样本构成】", "有效 PE 样本", "PE <= 0",
    "【集中趋势】", "算术均值", "中位数", "截尾均值",
    "【分位数分布】", "P50",
    "【区间分布】", "1000+",
    "【分交易所对比】", "SH", "SZ",
    "PE 最低", "PE 最高",
    "口径说明",
])
def test_profile_text_report_sections(profile, fragment):
    text = ValuationDistributionReporter().format_text_report(profile, "本地快照")
    assert fragment in text, "报告缺少「%s」" % fragment


def test_profile_text_report_shows_trade_date_and_source(profile):
    text = ValuationDistributionReporter().format_text_report(profile, "本地快照")
    assert "2026-09-30" in text
    assert "本地快照" in text, "来源说明必须出现"


def test_profile_text_report_warns_on_weak_sample(profile):
    """样本量不足时必须明确警告，否则会拿 4 只样本讲「全市场分布」"""
    text = ValuationDistributionReporter().format_text_report(profile)
    assert "样本代表性" in text
    assert "分布结论不成立" in text


def test_profile_text_report_right_skew_warning(profile):
    """A 股 PE 右偏，报告必须提示以中位数为准"""
    text = ValuationDistributionReporter().format_text_report(profile)
    assert "右偏" in text
    assert "中位数" in text


def test_profile_text_report_no_trailing_space(profile):
    """逐行 rstrip 过：报告被单独转发时不该携带行尾空白"""
    text = ValuationDistributionReporter().format_text_report(profile)
    assert all(line == line.rstrip() for line in text.split("\n"))


def test_profile_text_report_empty_shows_reason(valuation_df):
    """analyze 返回的空统计必须带真实原因，而不是空白或 0.00"""
    result = ValuationDistributionAnalyzer().analyze(None)
    text = ValuationDistributionReporter().format_text_report(result)
    assert "统计失败" in text
    assert "未取到" in text, "应给出可操作的原因"
    assert "样本构成" not in text, "无数据时不应渲染统计段落"


def test_profile_text_report_handcrafted_empty_is_self_contradictory():
    """【已知缺陷，钉住现状】手工构造的空 profile 会渲染成「[统计失败] 正常」

    ValuationDistributionProfile 的 status 默认值是「正常」，而报告层只要
    is_empty() 就打「[统计失败] %s」，于是两者拼出自相矛盾的一行。
    正常调用路径不可达（analyze 遇到空输入一定会先写好原因再返回），
    所以这是一处只可能被人为构造触发的表述缺陷。写这个用例是为了：
    哪天有人修好了它，本用例会红，从而提醒同步更新这里的期望值。
    """
    text = ValuationDistributionReporter().format_text_report(
        ValuationDistributionProfile("pe_ttm"))
    assert "[统计失败] 正常" in text


def test_profile_write_text_report(profile, tmp_path):
    path = tmp_path / "report.txt"
    written = ValuationDistributionReporter().write_text_report(
        profile, str(path), "本地快照")
    assert os.path.exists(written)
    assert path.read_text(encoding="utf-8").strip()


def test_markdown_report(profile):
    text = ValuationDistributionMarkdownReporter().format_markdown_report(
        profile, "本地快照")
    assert text.startswith("#") or text.lstrip().startswith("#"), "必须是 Markdown 标题开头"
    assert "| " in text or "|" in text, "应含表格"
    assert "本地快照" in text


def test_markdown_write_report(profile, tmp_path):
    path = tmp_path / "report.md"
    written = ValuationDistributionMarkdownReporter().write_markdown_report(
        profile, str(path))
    assert os.path.exists(written)


def test_markdown_escapes_pipe():
    """名称里带竖线会破坏 Markdown 表格，必须转义

    注意 name 必须经证券表带进来 —— 分析器会主动丢弃估值表自带的身份列。
    """
    frame = pd.DataFrame({"ts_code": ["600519.SH", "000001.SZ"],
                          "pe_ttm": [10.0, 20.0]})
    securities = pd.DataFrame({"ts_code": ["600519.SH", "000001.SZ"],
                               "name": ["某某|转债", "平安银行"],
                               "market": ["SH", "SZ"]})
    result = ValuationDistributionAnalyzer().analyze(frame, securities)
    text = ValuationDistributionMarkdownReporter().format_markdown_report(result)
    assert "\\|" in text, "竖线应被转义成 \\|，否则表格列会错位"
    for line in text.split("\n"):
        if "某某" in line:
            separators = line.replace("\\|", "").count("|")
            assert separators == 6, "5 列应恰好有 6 个未转义分隔符：%r" % line


def test_reporters_agree_on_conclusion(profile):
    """三个报告对同一份 profile 必须给出同一个中位数，不能各算各的"""
    text = ValuationDistributionReporter().format_text_report(profile)
    markdown = ValuationDistributionMarkdownReporter().format_markdown_report(profile)
    value = "%.2f" % profile.median_pe
    assert value in text
    assert value in markdown


# ---------------------------------------------------------------------------
# 八、单股分位报告
# ---------------------------------------------------------------------------
@pytest.fixture
def percentile_results():
    from stocklab.analytics import ValuationPercentileAnalyzer
    frame = pd.DataFrame({
        "trade_date": list(pd.date_range("2024-01-01", periods=40).date),
        "pe_ttm": [10.0 + i * 0.5 for i in range(40)],
        "pb": [1.0 + i * 0.02 for i in range(40)],
        "ps": [2.0] * 40,
        "pcf": [float("nan")] * 40,
    })
    return ValuationPercentileAnalyzer().analyze(frame)


def test_percentile_console_report(percentile_results):
    text = ValuationPercentileReporter().format_console_report(
        "600519.SH", percentile_results, "本地库")
    assert "600519.SH" in text
    assert "本地库" in text
    assert "PE-TTM" in text or "pe_ttm" in text


def test_percentile_markdown_report(percentile_results):
    text = ValuationPercentileReporter().format_markdown_report(
        "600519.SH", percentile_results, "本地库")
    assert "|" in text
    assert "600519.SH" in text


def test_percentile_reporter_skips_unavailable(percentile_results):
    """分位不可用的指标不能出现在报告里（NaN 百分比看起来最像 bug）"""
    text = ValuationPercentileReporter().format_console_report(
        "600519.SH", percentile_results)
    assert "nan%" not in text
    assert "%s" not in text or "nan" not in text.lower()


def test_percentile_report_empty_results():
    """没有任何可用指标时给出明确说明，而不是空表"""
    text = ValuationPercentileReporter().format_console_report("600519.SH", [])
    assert "600519.SH" in text
    assert text.strip()


def test_percentile_indicator_label():
    reporter = ValuationPercentileReporter()
    assert reporter.indicator_label("pe_ttm")
    assert reporter.indicator_label("no_such_indicator"), "未知指标也要有兜底名"


def test_percentile_pad_handles_wide_chars():
    """中文按两个字符宽计算，否则表格在终端里会错位"""
    reporter = ValuationPercentileReporter()
    assert reporter._visual_width("中文") == 4
    assert reporter._visual_width("ab") == 2
    padded = reporter._pad("中文", 10)
    assert reporter._visual_width(padded) == 10


def test_profile_reporter_pad_handles_wide_chars():
    reporter = ValuationDistributionReporter()
    assert reporter._visual_width("市盈率") == 6
    assert reporter._visual_width("PE") == 2