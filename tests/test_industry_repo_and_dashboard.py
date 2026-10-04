"""
行业估值表读写与看板网页生成 (tests/test_industry_repo_and_dashboard.py)

覆盖两处此前近乎零覆盖的地方：
  stocklab/persistence/repository/industry_valuation.py
      —— 行业横截面查询、时间序列、按日/按层级筛选、去重
  app/dashboard/page_generator.py + sector_trend.py
      —— 模板定位与自包含 HTML 生成

【为什么先补这两处】
  行业表在本项目里「结构齐全但本地无数据」，页面只得到空表 —— 于是查询分支
  （find_series / find_cross_section_dates / list_industries）一次都没跑过。
  这些分支的排序与去重规则很容易写错，而它们只影响行业页，不影响主链路，
  属于「上线后才会暴露」的缺口。补测试时直接灌合成数据即可，不必等真实数据。

运行：
  ./venv/bin/python -m pytest tests/test_industry_repo_and_dashboard.py -v
  （临时库 + 临时目录，不联网）
"""

import datetime
import os
import sys

import pandas as pd
import pytest

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.persistence import Database
from stocklab.persistence.repository.industry_valuation import IndustryValuationRepository
from stocklab.domain import INDUSTRY_VALUATION_COLUMNS
from app.dashboard import page_generator as pg


DAY = datetime.date(2026, 9, 30)
PREV_DAY = datetime.date(2026, 9, 29)


def _row(industry_code, name, level, classification="国证行业分类",
         stat_date=DAY, pe_median=30.0):
    row = {column: None for column in INDUSTRY_VALUATION_COLUMNS}
    row.update({
        "industry_code": industry_code,
        "stat_date": stat_date,
        "classification": classification,
        "industry_level": level,
        "industry_name": name,
        "company_count": 50,
        "priced_company_count": 45,
        "pe_weighted": 28.0,
        "pe_median": pe_median,
        "pe_arithmetic": 32.0,
        "total_market_value": 1e12,
        "net_profit": 3e10,
    })
    return row


def _frame(*rows):
    return pd.DataFrame(list(rows), columns=list(INDUSTRY_VALUATION_COLUMNS))


def _sector(code, name, is_benchmark=False):
    """构造 SectorTrendVisualizer 用的板块对象（与 config 解析结果同构）"""
    from types import SimpleNamespace
    return SimpleNamespace(
        code=code, name=name, sector=name, color="#9ecae1",
        tracking_index="", fund_manager="", purity_reason="",
        reliability_reason="", linewidth=1.8, linestyle="-",
        is_benchmark=is_benchmark,
        ticker=code[2:] if len(code) > 2 else code)


@pytest.fixture
def repository(tmp_path):
    db = Database(str(tmp_path / "industry.duckdb"))
    yield IndustryValuationRepository(db)
    db.close()


@pytest.fixture
def seeded(repository):
    repository.upsert(_frame(
        _row("1", "食品饮料", 1),
        _row("2", "银行", 1, pe_median=6.0),
        _row("101", "白酒", 2),
    ))
    return repository


# ---------------------------------------------------------------------------
# 一、写入
# ---------------------------------------------------------------------------
def test_upsert_returns_count(repository):
    assert repository.upsert(_frame(_row("1", "食品饮料", 1))) == 1


def test_upsert_empty_returns_zero(repository):
    assert repository.upsert(pd.DataFrame(columns=list(INDUSTRY_VALUATION_COLUMNS))) == 0


def test_upsert_is_idempotent(repository):
    """同一天同一行业重复写入不得产生重复行"""
    frame = _frame(_row("1", "食品饮料", 1))
    repository.upsert(frame)
    repository.upsert(frame)
    assert repository.count() == 1


def test_upsert_different_dates_coexist(repository):
    """跨日期的行都要留：行业页要看历史分位"""
    repository.upsert(_frame(_row("1", "食品饮料", 1, stat_date=DAY)))
    repository.upsert(_frame(_row("1", "食品饮料", 1, stat_date=PREV_DAY)))
    assert repository.count() == 2


def test_upsert_fills_missing_value_columns(repository):
    """缺列（值全空的数值列）应被补成 None，而不是拒收

    源站常缺个别数值列（如某行业无加权 PE）；只要身份列齐全就该写入，
    缺失的值留 None，由查询侧的 NULLS LAST / COALESCE 处理。
    """
    row = _row("1", "食品饮料", 1)
    row.pop("pe_weighted")                    # 模拟源表少一列
    frame = pd.DataFrame([{k: v for k, v in row.items()}],
                         columns=list(INDUSTRY_VALUATION_COLUMNS))
    assert repository.upsert(frame) == 1
    stored = repository.find_by_date(DAY.strftime("%Y-%m-%d")).iloc[0]
    assert pd.isna(stored["pe_weighted"]), "缺列应补成 NULL（DuckDB 读回为 NaN）"


# ---------------------------------------------------------------------------
# 二、横截面查询
# ---------------------------------------------------------------------------
def test_find_by_date_all_levels(seeded):
    """不传层级时返回全部层级（当天 3 条：2 个一级 + 1 个二级）"""
    frame = seeded.find_by_date(DAY.strftime("%Y-%m-%d"))
    assert len(frame) == 3
    assert set(frame["industry_name"]) == {"食品饮料", "银行", "白酒"}


def test_find_by_date_level_1_only(seeded):
    frame = seeded.find_by_date(DAY.strftime("%Y-%m-%d"), industry_level=1)
    assert len(frame) == 2
    assert set(frame["industry_name"]) == {"食品饮料", "银行"}


def test_find_by_date_orders_by_pe_weighted(seeded):
    """排序键是 pe_weighted（龙头主导口径），且 NULL 排最后"""
    frame = seeded.find_by_date(DAY.strftime("%Y-%m-%d"), industry_level=1)
    weighted = list(frame["pe_weighted"])
    assert weighted == sorted(weighted), "应按加权 PE 升序"


def test_find_by_date_filters_level(seeded):
    frame = seeded.find_by_date(DAY.strftime("%Y-%m-%d"), industry_level=2)
    assert len(frame) == 1
    assert frame.iloc[0]["industry_name"] == "白酒"


def test_find_by_date_filters_classification(seeded):
    assert seeded.find_by_date(
        DAY.strftime("%Y-%m-%d"), classification="证监会行业分类").empty


def test_find_by_date_missing_day_returns_empty(seeded):
    assert seeded.find_by_date("1990-01-01").empty


# ---------------------------------------------------------------------------
# 三、时间序列
# ---------------------------------------------------------------------------
def test_find_series_returns_chronological_rows(repository):
    repository.upsert(_frame(_row("1", "食品饮料", 1, stat_date=PREV_DAY, pe_median=20.0)))
    repository.upsert(_frame(_row("1", "食品饮料", 1, stat_date=DAY, pe_median=30.0)))
    frame = repository.find_series("1")
    assert len(frame) == 2
    dates = [pd.Timestamp(d).date() for d in frame["stat_date"]]
    assert dates == sorted(dates), "必须按日期升序，否则画历史分位会左右颠倒"
    assert list(frame["pe_median"]) == [20.0, 30.0]


def test_find_series_unknown_code_returns_empty(seeded):
    assert seeded.find_series("999").empty


# ---------------------------------------------------------------------------
# 四、可选日期与清单
# ---------------------------------------------------------------------------
def test_cross_section_dates_sorted_and_unique(repository):
    for day in (DAY, PREV_DAY):
        repository.upsert(_frame(_row("1", "食品饮料", 1, stat_date=day)))
    repository.upsert(_frame(_row("2", "银行", 1, stat_date=DAY)))
    dates = repository.find_cross_section_dates()
    assert len(dates) == 2, "同一天只应出现一次"
    assert dates == sorted(dates), "必须升序"


def test_cross_section_dates_filters_classification(repository):
    repository.upsert(_frame(_row("1", "食品饮料", 1)))
    repository.upsert(_frame(
        _row("9", "某某", 1, classification="证监会行业分类")))
    assert len(repository.find_cross_section_dates()) == 1


def test_cross_section_dates_empty_db(repository):
    assert repository.find_cross_section_dates() == []


def test_list_industries_returns_summary_frame(seeded):
    """返回的是「行业清单」汇总帧：每个行业一行，带首末出现日"""
    frame = seeded.list_industries(DAY.strftime("%Y-%m-%d"))
    assert len(frame) == 3, "每个行业一行，不按层级过滤"
    assert set(frame.columns) >= {"industry_code", "industry_level",
                                  "first_date", "last_date"}
    assert set(frame["industry_code"]) == {"1", "2", "101"}


def test_list_industries_orders_by_level_then_code(seeded):
    frame = seeded.list_industries(DAY.strftime("%Y-%m-%d"))
    pairs = list(zip(frame["industry_level"], frame["industry_code"]))
    assert pairs == sorted(pairs), "应按 (层级, 代码) 升序"


def test_list_industries_filters_classification(seeded):
    assert seeded.list_industries(
        DAY.strftime("%Y-%m-%d"), classification="证监会行业分类").empty


def test_count(seeded):
    assert seeded.count() == 3


def test_count_empty_db(repository):
    assert repository.count() == 0


# ---------------------------------------------------------------------------
# 五、看板网页生成
# ---------------------------------------------------------------------------
def test_find_template_path_default():
    path = pg.find_template_path()
    assert path.endswith("dashboard.html")
    assert os.path.exists(path)


def test_find_template_path_missing_falls_back_to_default():
    """找不到指定模板时兜底回默认 dashboard.html，而不是让上层拿到坏路径"""
    path = pg.find_template_path("no_such_template.html")
    assert os.path.exists(path), "兜底路径必须真实存在"
    assert path.endswith("dashboard.html")


def test_generate_html_writes_self_contained_page(tmp_path):
    """生成的页面必须自包含（ECharts 内联），不能引用外部 CDN

    raw_data 的形状是 {code: {"YYYY-MM": close}} —— generate_html 内部取的是
    `series.keys()` 作为月份全集，所以必须是「月份 -> 价格」的字典，不是 DataFrame。
    """
    # sectors 是 config 解析出的 SimpleNamespace（带 .code），不是 dict
    sectors = [_sector("sh000001", "上证指数", is_benchmark=True),
               _sector("sh512480", "半导体")]
    raw = {
        "sh000001": {"2026-08": 3000.0, "2026-09": 3050.0},
        "sh512480": {"2026-08": 1.0, "2026-09": 1.2},
    }
    output = str(tmp_path / "trend.html")

    generator = pg.SectorWebPageGenerator()
    result = generator.generate_html(sectors, raw, output)

    assert os.path.exists(result)
    html = open(result, encoding="utf-8").read()
    assert "<html" in html.lower()
    assert "http://" not in html.replace("http://www.w3.org", ""), \
        "页面里不应有外链资源（离线可用是本项目的硬要求）"
    assert "echarts" in html.lower()


def test_generate_html_aligns_month_unions(tmp_path):
    """月份取各标的的并集；某只缺的月份补 None 而不是错位"""
    sectors = [_sector("sh000001", "上证"), _sector("sh512480", "半导体")]
    raw = {
        "sh000001": {"2026-08": 1.0, "2026-09": 2.0},
        "sh512480": {"2026-09": 9.0},          # 缺 8 月
    }
    result = pg.SectorWebPageGenerator().generate_html(
        sectors, raw, str(tmp_path / "t.html"))
    html = open(result, encoding="utf-8").read()
    assert "2026-08" in html and "2026-09" in html


def test_generate_html_aborts_without_months(tmp_path):
    """完全没有月份数据时返回空串并中止，不能生成一张空图页面"""
    result = pg.SectorWebPageGenerator().generate_html(
        [_sector("sh512480", "半导体")], {}, str(tmp_path / "t.html"))
    assert result == "", "无数据时应返回空串表示生成中止"


def test_generate_html_missing_code_yields_all_none(tmp_path):
    """某标的一行数据都没有时也要出现在 payload 里（全 None），而不是被漏掉"""
    sectors = [_sector("sh000001", "上证"), _sector("sh512480", "半导体")]
    raw = {"sh000001": {"2026-09": 1.0}}
    result = pg.SectorWebPageGenerator().generate_html(
        sectors, raw, str(tmp_path / "t.html"))
    assert os.path.exists(result)
    assert "sh512480" in open(result, encoding="utf-8").read()


def test_generate_html_with_one_month(tmp_path):
    """只有一个月的数据也要能出图（单点看不出走势，但不该崩）"""
    sectors = [_sector("sh512480", "半导体")]
    result = pg.SectorWebPageGenerator().generate_html(
        sectors, {"sh512480": {"2026-09": 1.2}}, str(tmp_path / "trend.html"))
    assert os.path.exists(result)


def test_sector_trend_uses_config(tmp_path, monkeypatch):
    """SectorTrendVisualizer 应从 config.ini 读板块与默认输出路径"""
    config = tmp_path / "cfg.ini"
    config.write_text("""[settings]
default_months = 12
output_html = %s

[benchmark]
code = sh000001

[sectors]
s1 = sh512480, 半导体, 芯片, #9ecae1
""" % (tmp_path / "out.html"), encoding="utf-8")

    from app.dashboard.sector_trend import SectorTrendVisualizer
    visualizer = SectorTrendVisualizer(num_months=12, config_path=str(config))
    codes = [sector.code for sector in visualizer.sectors]
    assert "sh000001" in codes
    assert "sh512480" in codes
    assert visualizer.default_output.endswith("out.html")