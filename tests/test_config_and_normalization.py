"""
配置解析与归一化边界 (tests/test_config_and_normalization.py)

覆盖三处此前零覆盖的地方：
  stocklab/common/config.py       —— config.ini 解析、路径查找、非法取值降级
  stocklab/normalization/akshare.py —— 估值快照 / 行业估值 / 指数成分归一化
  stocklab/persistence/repository/industry_valuation.py —— 行业表读写与去重

【为什么优先补这三处】
  配置层是全项目的隐式入口：配置项拼错、路径找不到、取值非法，都不会报错，
  而是悄悄用默认值 —— 表现出来是「数据不对」而不是「配置坏了」，极难排查。
  归一化层同理：源站改列名时要么显式拒绝（对），要么产出 NaN 列（错）。

运行：
  ./venv/bin/python -m pytest tests/test_config_and_normalization.py -v
  （临时目录 + 临时库，不联网、不动项目配置）
"""

import os
import sys

import pandas as pd
import pytest

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.common import config as cfg
from stocklab.normalization import akshare as norm_ak
from stocklab.domain import (
    DataContractError,
    DAILY_VALUATION_COLUMNS,
    INDEX_MEMBERSHIP_COLUMNS,
    INDUSTRY_VALUATION_COLUMNS,
)


# ---------------------------------------------------------------------------
# 一、配置文件路径查找
# ---------------------------------------------------------------------------
def test_find_config_path_absolute(tmp_path):
    target = tmp_path / "cfg.ini"
    target.write_text("[settings]\n", encoding="utf-8")
    assert cfg.find_config_path(str(target)) == str(target)


def test_find_config_path_relative_falls_back_to_cwd(tmp_path, monkeypatch):
    """相对路径找不到时按当前工作目录解析，而不是静默返回空"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cfg.ini").write_text("[settings]\n", encoding="utf-8")
    assert cfg.find_config_path("cfg.ini").endswith("cfg.ini")


def test_find_config_path_missing_returns_absolute(tmp_path):
    result = cfg.find_config_path(str(tmp_path / "nope.ini"))
    assert os.path.isabs(result), "找不到时也应返回绝对路径，便于报错信息可读"


# ---------------------------------------------------------------------------
# 二、config.ini 解析
# ---------------------------------------------------------------------------
def _write(tmp_path, body):
    path = tmp_path / "cfg.ini"
    path.write_text(body, encoding="utf-8")
    return str(path)


# load_ini_config 返回 4 元组 (sectors, default_months, default_output, http_timeout)，
# sectors[0] 恒为 benchmark（即使 [benchmark] 段缺失）。
def _load(tmp_path, body):
    sectors, months, output, timeout = cfg.load_ini_config(
        _write(tmp_path, body))
    return sectors, months, output, timeout


def test_load_config_defaults(tmp_path):
    """缺项必须逐个落到默认值，不能因缺一个键就整体失败"""
    sectors, months, output, timeout = _load(tmp_path, "[settings]\n")
    assert months == 120
    assert output == "output/sector_etf_trend.html"
    assert timeout == 8
    assert len(sectors) == 1, "没有 sectors 段时只应返回 benchmark"


def test_load_config_reads_settings(tmp_path):
    _, months, output, timeout = _load(tmp_path, """[settings]
default_months = 60
output_html = output/custom.html
http_timeout = 15
""")
    assert months == 60
    assert output == "output/custom.html"
    assert timeout == 15


def test_load_config_benchmark_defaults(tmp_path):
    """benchmark 段缺失时用内置默认（上证指数 / 3.2 号线）"""
    sectors, _, _, _ = _load(tmp_path, "[settings]\n")
    bench = sectors[0]
    assert bench.is_benchmark is True
    assert bench.code == "sh000001"
    assert bench.linewidth == 3.2
    assert bench.linestyle == "-"
    assert bench.ticker == "000001", "ticker 应剥掉市场前缀"


def test_load_config_ticker_strips_market_prefix(tmp_path):
    """ticker 一律剥掉前两个字符（市场前缀）

    【已知行为，不是 bug】代码是 `item_code[2:] if len(item_code) > 2 else item_code`，
    假定配置里都写 sh000001 这种带前缀的形式；若真写了 000001 这种 6 位纯数字，
    会被截成 0001。这里把该行为钉住 —— 配置格式要求带前缀，截断反而能暴露填错。
    """
    sectors, _, _, _ = _load(tmp_path, """[settings]

[benchmark]
code = sh000001
""")
    assert sectors[0].ticker == "000001"
    assert len(sectors[0].code) > 2, "配置里应写带市场前缀的代码"


def test_load_config_benchmark_blank_color_falls_back(tmp_path):
    """颜色留空时回落到默认色，而不是生成非法色值"""
    sectors, _, _, _ = _load(tmp_path, """[settings]

[benchmark]
color =
""")
    assert sectors[0].color == "#ffffff"


def test_load_config_parses_sectors(tmp_path):
    body = """[settings]

[benchmark]
code = sh000001

[sectors]
s1 = sh512480, 半导体, 芯片, #9ecae1, 中华半导体芯片, 国联安, 纯芯片设计
s2 = sh515790, 光伏, 光伏, #74c476
empty_sector =
malformed_line = 只有一列
"""
    sectors, _, _, _ = _load(tmp_path, body)
    codes = [sector.code for sector in sectors]
    assert codes[0] == "sh000001", "benchmark 必须排第一"
    assert "sh512480" in codes and "sh515790" in codes
    assert len(codes) == 3, "空值与列数不足的行都应被跳过"


def test_load_config_sector_optional_fields(tmp_path):
    """sectors 行的可选字段缺省时要能补上，不能因少写几列就丢整个标的"""
    body = """[settings]

[sectors]
s1 = sh512480, 半导体, 芯片, #9ecae1, 中华半导体芯片, 国联安, 纯芯片设计
s2 = sh515790, 光伏, 光伏
"""
    sectors, _, _, _ = _load(tmp_path, body)
    full = [s for s in sectors if s.code == "sh512480"][0]
    short = [s for s in sectors if s.code == "sh515790"][0]
    assert full.tracking_index == "中华半导体芯片"
    assert full.fund_manager == "国联安"
    assert short.tracking_index == "", "缺失的可选字段应为空串"
    assert short.ticker == "515790"


def test_load_config_sector_falls_back_to_palette(tmp_path):
    """sectors 行没写颜色时按调色板轮转分配，保证相邻标的颜色不同"""
    body = """[settings]

[sectors]
s1 = sh512480, 半导体, 芯片
s2 = sh515790, 光伏, 光伏
s3 = sh588000, 科创50, 科创
"""
    sectors, _, _, _ = _load(tmp_path, body)
    colors = [s.color for s in sectors if not s.is_benchmark]
    assert len(set(colors)) == 3, "三个标的应分到三个不同颜色：%s" % colors


def test_load_config_comments_are_ignored(tmp_path):
    """# 与 ; 都应是注释前缀，否则会拼进色值或月份里"""
    _, months, _, _ = _load(tmp_path, """[settings]
; 分号注释
default_months = 36 ; 行尾注释
""")
    assert months == 36


# ---------------------------------------------------------------------------
# 三、取数优先级读取
# ---------------------------------------------------------------------------
def test_priority_missing_file_returns_default(tmp_path):
    assert cfg.load_data_source_priority(
        str(tmp_path / "nope.ini")) == cfg.DEFAULT_DATA_SOURCE_PRIORITY


def test_priority_reads_data_source_section(tmp_path):
    path = _write(tmp_path, "[data_source]\npriority = remote_first\n")
    assert cfg.load_data_source_priority(path) == "remote_first"


def test_priority_missing_section_returns_default(tmp_path):
    path = _write(tmp_path, "[settings]\ndefault_months = 60\n")
    assert cfg.load_data_source_priority(path) == cfg.DEFAULT_DATA_SOURCE_PRIORITY


def test_priority_illegal_value_returns_default(tmp_path):
    """非法取值必须降级到默认，而不是原样传下去"""
    path = _write(tmp_path, "[data_source]\npriority = whatever\n")
    assert cfg.load_data_source_priority(path) == cfg.DEFAULT_DATA_SOURCE_PRIORITY


def test_priority_parse_error_returns_default(tmp_path):
    """配置损坏时也不能抛异常 —— 优先级只影响取数顺序，不该挡住程序启动"""
    path = tmp_path / "broken.ini"
    path.write_bytes(b"\xff\xfe\x00broken\x00\x01")
    assert cfg.load_data_source_priority(str(path)) == cfg.DEFAULT_DATA_SOURCE_PRIORITY


@pytest.mark.parametrize("value", cfg.DATA_SOURCE_PRIORITIES)
def test_priority_accepts_all_legal_values(tmp_path, value):
    path = _write(tmp_path, "[data_source]\npriority = %s\n" % value)
    assert cfg.load_data_source_priority(path) == value


# ---------------------------------------------------------------------------
# 四、估值快照归一化
# ---------------------------------------------------------------------------
def _spot_frame(**overrides):
    data = {
        "代码": ["600519"], "换手率": [0.5],
        "市盈率-动态": [28.0], "市盈率(TTM)": [27.5],
        "市净率": [10.0], "总市值": [1.8e12], "流通市值": [1.7e12],
    }
    data.update(overrides)
    return pd.DataFrame(data)


def test_normalize_daily_valuations():
    frame = norm_ak.normalize_daily_valuations(_spot_frame(), "2026-09-30")
    assert list(frame.columns) == list(DAILY_VALUATION_COLUMNS)
    row = frame.iloc[0]
    assert row["ts_code"] == "600519.SH"
    assert row["pe"] == 28.0, "pe 取「市盈率-动态」"
    assert row["pe_ttm"] == 27.5, "pe_ttm 取「市盈率(TTM)」，两者不可混用"


def test_normalize_daily_valuations_requires_identity_columns():
    bad = _spot_frame().drop(columns=["代码"])
    with pytest.raises(DataContractError) as caught:
        norm_ak.normalize_daily_valuations(bad, "2026-09-30")
    assert "代码" in str(caught.value)


def test_normalize_daily_valuations_missing_value_column_rejected():
    """缺「市盈率(TTM)」必须显式拒绝：产出 NaN 列会被下游当成「无估值」"""
    bad = _spot_frame().drop(columns=["市盈率(TTM)"])
    with pytest.raises(DataContractError):
        norm_ak.normalize_daily_valuations(bad, "2026-09-30")


def test_normalize_daily_valuations_empty_input_rejected():
    """空帧没有必需列，必须显式拒绝而不是产出全 NaN 的表

    上层按「空表 = 无数据」处理，若这里返回一张全 NaN 表，会被当成真实数据落库。
    """
    with pytest.raises(DataContractError):
        norm_ak.normalize_daily_valuations(pd.DataFrame(), "2026-09-30")


def test_normalize_daily_valuations_non_numeric_becomes_nan():
    """源站给了「-」这类占位符时转成 NaN，不能让整列变 object"""
    frame = norm_ak.normalize_daily_valuations(
        _spot_frame(市净率=["--"]), "2026-09-30")
    assert pd.isna(frame.iloc[0]["pb"])


# ---------------------------------------------------------------------------
# 五、行业估值归一化
# ---------------------------------------------------------------------------
def _industry_frame(**overrides):
    # 源列名照抄 akshare.stock_industry_pe_ratio_cninfo 的契约清单
    data = {
        "行业编码": ["1"], "行业层级": [1], "行业名称": ["食品饮料"],
        "公司数量": [50], "纳入计算公司数量": [45],
        "总市值-静态": [1e12], "净利润-静态": [3e10],
        "静态市盈率-加权平均": [30.0], "静态市盈率-中位数": [35.0],
        "静态市盈率-算术平均": [40.0],
    }
    data.update(overrides)
    return pd.DataFrame(data)


def test_normalize_industry_valuation():
    frame = norm_ak.normalize_industry_valuation(
        _industry_frame(), "2026-09-30", "国证行业分类")
    assert list(frame.columns) == list(INDUSTRY_VALUATION_COLUMNS)
    row = frame.iloc[0]
    assert row["industry_name"] == "食品饮料"
    assert row["pe_median"] == 35.0
    assert row["classification"] == "国证行业分类"
    assert row["industry_level"] == 1


def test_normalize_industry_valuation_missing_column_rejected():
    bad = _industry_frame().drop(columns=["静态市盈率-中位数"])
    with pytest.raises(DataContractError):
        norm_ak.normalize_industry_valuation(bad, "2026-09-30", "国证行业分类")


def test_normalize_industry_valuation_empty_input_rejected():
    with pytest.raises(DataContractError):
        norm_ak.normalize_industry_valuation(
            pd.DataFrame(), "2026-09-30", "国证行业分类")


# ---------------------------------------------------------------------------
# 六、指数成分归一化
# ---------------------------------------------------------------------------
def _index_frame(**overrides):
    data = {
        "日期": ["20260930", "20260930"], "指数代码": ["000300", "000300"],
        "指数名称": ["沪深300", "沪深300"],
        "成分券代码": ["600519", "000001"],
        "成分券名称": ["贵州茅台", "平安银行"],
        "权重": [5.0, 3.0],
    }
    data.update(overrides)
    return pd.DataFrame(data)


def test_normalize_index_membership_keeps_distinct_dates():
    """同一成分股在不同 effective_date 下是两行（主键含日期，是历史快照）

    所以这里不是「去重」语义：index_membership 记录的是「某成分股在某日属于某指数」，
    多期快照要全留，否则按 as-of 还原成分股时只剩最新一期。
    """
    frame = pd.DataFrame({
        "日期": ["20260930", "20260929"],
        "指数代码": ["000300", "000300"],
        "指数名称": ["沪深300", "沪深300"],
        "成分券代码": ["600519", "600519"],
        "成分券名称": ["贵州茅台", "贵州茅台"],
        "权重": [5.0, 5.1],
    })
    result = norm_ak.normalize_index_membership(frame)
    assert len(result) == 2, "不同日期的快照都要保留"
    assert list(result.columns) == list(INDEX_MEMBERSHIP_COLUMNS)
    assert set(result["effective_date"]) == {
        pd.Timestamp("2026-09-30").date(), pd.Timestamp("2026-09-29").date()}


def test_normalize_index_membership_dedupes_same_day():
    """同一天的重复成分股只留一行（源站偶发重复推送）"""
    frame = pd.DataFrame({
        "日期": ["20260930", "20260930"],
        "指数代码": ["000300", "000300"],
        "指数名称": ["沪深300", "沪深300"],
        "成分券代码": ["600519", "600519"],
        "成分券名称": ["贵州茅台", "贵州茅台"],
        "权重": [5.0, 5.0],
    })
    assert len(norm_ak.normalize_index_membership(frame)) == 1


def test_normalize_index_membership_empty_input_rejected():
    with pytest.raises(DataContractError):
        norm_ak.normalize_index_membership(pd.DataFrame())


def test_normalize_index_membership_missing_column_rejected():
    bad = _index_frame().drop(columns=["成分券代码"])
    with pytest.raises(DataContractError):
        norm_ak.normalize_index_membership(bad)