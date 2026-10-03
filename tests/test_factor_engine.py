"""
StockLab 因子引擎测试 (tests/test_factor_engine.py)

覆盖 Factor Engine：
  1. 登记表：分类齐全、同名重复拒绝、未登记名字给出可用清单
  2. 因子契约：缺输入列 / 返回非 Series / 长度不符 -> FactorDataError
  3. 因子口径：安全除法（分母 0 -> NaN）、EV/EBITDA 负值 -> NaN、
     同比与趋势、分红口径，均用手算值断言
  4. 预处理：winsorize / rank / zscore / 行业中性化 / 市值中性化 / 缺失值处理
     以及非法配置必须失败

运行：
  ./venv/bin/python -m pytest tests/test_factor_engine.py -v
  （纯合成数据，不联网、不碰数据库）
"""

import os
import sys
import math

import pandas as pd
import pytest

# 允许直接执行本文件（./venv/bin/python tests/xxx.py）；走 pytest 时由
# pytest.ini 的 `pythonpath = .` 统一负责，不会重复插入。
if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.factor import (
    FACTOR_CATEGORIES,
    FACTOR_VERSION,
    Factor,
    FactorDataError,
    apply_preprocessing,
    compute,
    get,
    list_factors,
    register,
)
from stocklab.factor.registry import register as register_directly


@pytest.fixture
def frame():
    """构造一张覆盖全部因子输入列的合成输入帧（4 只标的）"""
    data = {
        "ts_code": ["600519.SH", "000001.SZ", "300750.SZ", "601234.SH"],
        "name": ["贵州茅台", "平安银行", "宁德时代", "某某退"],
        "industry": ["食品饮料", "银行", "电气设备", "化工"],
        # 估值
        "pe_ttm": [28.0, 7.5, 22.0, 0.0],
        "pb": [10.0, 0.9, 4.5, 1.2],
        "ps": [13.0, 1.2, 3.0, 0.4],
        "ev": [3.0e12, 2.1e11, 1.0e12, 1.0e10],
        "ebitda": [1.2e11, 6.0e10, 8.0e10, -1.0e9],
        "total_mv": [3.0e12, 2.0e11, 9.0e11, 1.0e10],
        "free_cashflow": [6.0e10, 4.0e10, -2.0e10, 5.0e8],
        "free_cashflow_prior_year": [5.0e10, 4.0e10, 1.0e10, 0.0],
        # 质量
        "roe": [0.37, 0.11, 0.16, -0.4],
        "roe_prior_year": [0.34, 0.12, 0.10, -0.1],
        "roic": [0.35, 0.10, 0.14, -0.35],
        "gross_margin": [0.91, 0.4, 0.22, 0.05],
        "operating_margin": [0.6, 0.3, 0.11, -0.2],
        "operating_cashflow": [7.0e10, 5.0e10, 1.0e10, -1.0e9],
        "net_profit": [8.6e10, 4.4e10, 5.0e10, -2.0e9],
        "total_liabilities": [3.0e10, 3.6e12, 6.0e11, 2.0e10],
        "equity": [2.5e11, 3.0e11, 3.0e11, 0.0],
        # 成长
        "revenue_yoy": [0.177, -0.03, 0.22, -0.5],
        "profit_yoy": [0.15, -0.05, 0.4, -1.2],
        "eps_yoy": [0.15, -0.05, 0.4, -1.2],
        # 分红
        "dv_ttm": [2.3, 3.8, 0.4, 0.0],
        "dps": [30.88, 1.72, 0.5, 0.0],
        "dps_prior_year": [25.91, 1.93, 0.0, 0.0],
        "eps": [66.6, 1.6, 1.4, -0.1],
        "dividend_years_paid": [5.0, 5.0, 1.0, 0.0],
        "dividend_years_total": [5.0, 5.0, 5.0, 3.0],
        # 动量 / 风险（由价格序列预计算后拼入）
        "ret_1m": [0.05, -0.02, 0.11, 0.0],
        "ret_3m": [0.12, -0.04, 0.30, -0.1],
        "ret_6m": [0.20, 0.01, 0.45, -0.3],
        "ret_12m": [0.35, 0.06, 0.90, -0.6],
        "vol_6m": [0.018, 0.015, 0.03, 0.05],
        "vol_12m": [0.02, 0.016, 0.032, 0.06],
        "max_dd_12m": [-0.18, -0.12, -0.35, -0.7],
    }
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# 一、登记表
# ---------------------------------------------------------------------------
def test_factor_count():
    assert len(list_factors()) == 27


@pytest.mark.parametrize("category,required", [
    ("value", ["dividend_yield", "ev_ebitda", "fcf_yield", "pb", "pe_ttm", "ps"]),
    ("quality", ["cfo_to_net_profit", "debt_to_equity", "gross_margin",
                 "operating_margin", "roe", "roic"]),
    ("growth", ["eps_growth", "fcf_growth", "profit_growth",
                "revenue_growth", "roe_trend"]),
    ("momentum", ["momentum_1m", "momentum_3m", "momentum_6m", "momentum_12m"]),
    ("dividend", ["dividend_growth", "dividend_stability", "dividend_yield",
                  "payout_ratio"]),
    ("risk", ["max_drawdown_12m", "volatility_12m", "volatility_6m"]),
])
def test_factor_registered_in_category(category, required):
    """§7 各小节的因子必须都在对应分类里"""
    actual = list_factors(category)
    for factor_name in required:
        assert factor_name in actual, "%s 缺少因子 %s" % (category, factor_name)


def test_factor_categories_order():
    assert tuple(FACTOR_CATEGORIES) == ("value", "quality", "growth",
                                        "momentum", "dividend", "risk")


def test_factor_version_prefixed():
    assert FACTOR_VERSION.startswith("factor_v"), FACTOR_VERSION


def test_dividend_yield_in_two_categories():
    """股息率同时归属 value 与 dividend（§7.1 与 §7.5 都列了它）"""
    assert "dividend_yield" in list_factors("value")
    assert "dividend_yield" in list_factors("dividend")


def test_duplicate_registration_rejected():
    with pytest.raises(FactorDataError):
        register(get("pe_ttm"))


def test_unregistered_factor_lists_available():
    with pytest.raises(FactorDataError) as caught:
        get("not_a_factor")
    assert "pe_ttm" in str(caught.value)  # 错误信息必须列出可用因子


@pytest.mark.parametrize("name,categories,inputs", [
    ("", ("value",), ("x",)),                # 名字为空
    ("bad", ("nope",), ("x",)),              # 分类不存在
    ("bad", ("value",), ()),                 # 输入列为空
])
def test_invalid_factor_definition_rejected(name, categories, inputs):
    """分类 / 输入列非法的因子定义必须在构造时就失败"""
    with pytest.raises(FactorDataError):
        Factor(name, categories, inputs, lambda f: f["x"])


# ---------------------------------------------------------------------------
# 二、因子输入帧契约
# ---------------------------------------------------------------------------
def test_missing_input_column_rejected(frame):
    with pytest.raises(FactorDataError) as caught:
        get("fcf_yield").values(frame.drop(columns=["total_mv"]))
    assert "total_mv" in str(caught.value)


def test_scalar_return_rejected(frame):
    register_directly(
        Factor("contract_probe", ("value",), ("pb",), lambda f: 1.0, "返回标量")
    )
    with pytest.raises(FactorDataError):
        get("contract_probe").values(frame)


def test_wrong_length_return_rejected(frame):
    register_directly(
        Factor(
            "contract_probe_length",
            ("value",),
            ("pb",),
            lambda f: f["pb"].iloc[:2],
            "返回长度不符",
        )
    )
    with pytest.raises(FactorDataError):
        get("contract_probe_length").values(frame)


def test_compute_aligns_columns_and_index(frame):
    values = compute(frame, ["pe_ttm", "roe"])
    assert list(values.columns) == ["pe_ttm", "roe"]
    assert len(values) == len(frame)


# ---------------------------------------------------------------------------
# 三、因子计算口径
# ---------------------------------------------------------------------------
@pytest.fixture
def values(frame):
    """一次算全 15 个被断言口径的因子"""
    return compute(
        frame,
        [
            "pe_ttm",
            "ev_ebitda",
            "fcf_yield",
            "roe",
            "cfo_to_net_profit",
            "debt_to_equity",
            "revenue_growth",
            "fcf_growth",
            "roe_trend",
            "dividend_yield",
            "dividend_growth",
            "payout_ratio",
            "dividend_stability",
            "momentum_12m",
            "max_drawdown_12m",
        ],
    )


@pytest.mark.parametrize("column,row,expected", [
    ("pe_ttm", 0, 28.0),           # 透传类不改口径
    ("dividend_yield", 1, 3.8),
    ("momentum_12m", 2, 0.90),
    ("max_drawdown_12m", 3, -0.7),
])
def test_passthrough_factors(values, column, row, expected):
    assert values[column].iloc[row] == expected


def test_ev_ebitda(values):
    """正 EBITDA 才算，负值 -> NaN（第 4 只 ebitda < 0）"""
    expected_ev = 3.0e12 / 1.2e11
    assert math.isclose(values["ev_ebitda"].iloc[0], expected_ev, rel_tol=1e-9)
    assert values["ev_ebitda"].isna().iloc[3]


def test_fcf_yield(values):
    assert math.isclose(values["fcf_yield"].iloc[0], 6.0e10 / 3.0e12, rel_tol=1e-9)
    assert values["fcf_yield"].iloc[2] < 0  # 现金流为负要如实保留负值


def test_quality_ratios(values):
    assert math.isclose(values["cfo_to_net_profit"].iloc[0],
                        7.0e10 / 8.6e10, rel_tol=1e-9)
    assert math.isclose(values["debt_to_equity"].iloc[0],
                        3.0e10 / 2.5e11, rel_tol=1e-9)
    assert values["debt_to_equity"].isna().iloc[3]  # equity = 0 -> NaN


def test_growth_factors(values):
    assert values["revenue_growth"].iloc[0] == 0.177
    assert math.isclose(values["fcf_growth"].iloc[0],
                        (6.0e10 - 5.0e10) / 5.0e10, rel_tol=1e-9)
    assert values["fcf_growth"].isna().iloc[3]  # 上年同期为 0 -> NaN
    assert math.isclose(values["roe_trend"].iloc[0], 0.37 - 0.34, rel_tol=1e-9)
    assert math.isclose(values["roe_trend"].iloc[1], 0.11 - 0.12, rel_tol=1e-9)


def test_dividend_factors(values):
    assert math.isclose(values["payout_ratio"].iloc[0],
                        30.88 / 66.6, rel_tol=1e-9)
    assert math.isclose(values["dividend_stability"].iloc[1], 1.0, rel_tol=1e-9)
    assert math.isclose(values["dividend_stability"].iloc[3], 0.0, rel_tol=1e-9)
    assert values["dividend_growth"].isna().iloc[2]  # dps_prior_year = 0


# ---------------------------------------------------------------------------
# 四、预处理
# ---------------------------------------------------------------------------
@pytest.fixture
def prep_frame():
    return pd.DataFrame(
        {
            "industry": ["a", "a", "b", "b", "c", ""],
            "total_mv": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
            "pe_ttm": [5.0, 7.0, 9.0, 11.0, 13.0, 1000.0],
            "roe": [0.1, 0.3, 0.2, 0.4, 0.5, float("nan")],
        }
    )


def test_winsorize(prep_frame):
    """上下各裁 20% 尾部（即截断到 20% ~ 80% 分位）"""
    clipped = apply_preprocessing(
        prep_frame, [{"method": "winsorize", "columns": ["pe_ttm"],
                      "lower": 0.2, "upper": 0.2}]
    )
    assert clipped["pe_ttm"].max() == prep_frame["pe_ttm"].quantile(0.8)
    assert clipped["pe_ttm"].min() == prep_frame["pe_ttm"].quantile(0.2)
    assert len(clipped) == len(prep_frame)  # 不丢行
    assert prep_frame["pe_ttm"].max() == 1000.0  # 原帧不被修改


def test_rank(prep_frame):
    ranked = apply_preprocessing(prep_frame, [{"method": "rank", "columns": ["roe"]}])
    assert pd.isna(ranked["roe"].iloc[5])
    assert ranked["roe"].max() == 1.0


def test_zscore(prep_frame):
    zed = apply_preprocessing(prep_frame,
                              [{"method": "zscore", "columns": ["pe_ttm"]}])
    assert abs(zed["pe_ttm"].mean()) < 1e-9
    assert math.isclose(zed["pe_ttm"].std(ddof=1), 1.0, rel_tol=1e-9)


def test_industry_neutralize(prep_frame):
    neutral = apply_preprocessing(
        prep_frame, [{"method": "industry_neutralize", "columns": ["roe"]}]
    )
    group_means = neutral.groupby(prep_frame["industry"])["roe"].mean().drop("")
    assert all(abs(value) < 1e-12 for value in group_means), group_means
    assert pd.isna(neutral["roe"].iloc[5])  # 行业缺失 -> 无法中性化


def test_industry_neutralize_rejects_empty_group(prep_frame):
    with pytest.raises(FactorDataError):
        apply_preprocessing(
            prep_frame.assign(industry=""),
            [{"method": "industry_neutralize", "columns": ["roe"]}],
        )


def test_market_cap_neutralize(prep_frame):
    """残差与 log(市值) 的相关性近 0"""
    capped = apply_preprocessing(
        prep_frame.drop(index=5).reset_index(drop=True),
        [{"method": "market_cap_neutralize", "columns": ["pe_ttm"],
          "column": "total_mv"}],
    )
    x = capped["total_mv"].map(math.log)
    y = capped["pe_ttm"]
    x_dev = x - x.mean()
    y_dev = y - y.mean()
    covariance = float((x_dev * y_dev).sum())
    scale = math.sqrt(float((x_dev ** 2).sum()) * float((y_dev ** 2).sum()))
    assert abs(covariance) <= 1e-9 * scale, covariance


def test_missing_median(prep_frame):
    filled = apply_preprocessing(
        prep_frame, [{"method": "missing", "columns": ["roe"], "action": "median"}]
    )
    assert not filled["roe"].isna().any()


def test_missing_drop(prep_frame):
    dropped = apply_preprocessing(
        prep_frame, [{"method": "missing", "columns": ["roe"], "action": "drop"}]
    )
    assert len(dropped) == len(prep_frame) - 1


def test_zero_variance_yields_nan_not_inf(prep_frame):
    """无离散度 -> NaN，不产生 inf"""
    constant = apply_preprocessing(
        prep_frame.assign(pe_ttm=1.0),
        [{"method": "missing", "columns": ["pe_ttm"], "action": "zero"},
         {"method": "zscore", "columns": ["pe_ttm"]}],
    )
    assert constant["pe_ttm"].isna().all()


@pytest.mark.parametrize("bad_step", [
    {"method": "nope"},
    {"method": "rank", "columns": "missing_column"},
    {"method": "winsorize", "columns": ["roe"], "lower": 0.6, "upper": 0.6},
    {"method": "missing", "action": "nope"},
    {"method": "market_cap_neutralize", "columns": ["pe_ttm"], "column": "not_mv"},
    "not-a-dict",
])
def test_invalid_preprocessing_rejected(prep_frame, bad_step):
    """6 类非法配置（未知方法/缺列/分位越界/非法动作/缺市值列/非字典）全部拒绝"""
    with pytest.raises(FactorDataError):
        apply_preprocessing(prep_frame, [bad_step])


def test_empty_steps_return_copy(prep_frame):
    assert apply_preprocessing(prep_frame, None).equals(prep_frame)
    assert apply_preprocessing(
        prep_frame, [{"method": "zscore", "columns": ["roe"]}]
    ).shape == prep_frame.shape


if __name__ == "__main__":
    # 保住旧的直接执行入口：委托给 pytest，退出码语义一致
    raise SystemExit(pytest.main([__file__, "-v"]))
