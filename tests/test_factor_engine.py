#!/usr/bin/env python3
"""
==============================================================================
StockLab - 因子引擎测试 (tests/test_factor_engine.py)
==============================================================================

【功能用途】
  覆盖 V2 需求 §7 的 Factor Engine：
    1. 登记表：分类齐全、同名重复拒绝、未登记名字给出可用清单
    2. 因子契约：缺输入列 / 返回非 Series / 长度不符 -> FactorDataError
    3. 因子口径：安全除法（分母 0 -> NaN）、EV/EBITDA 负值 -> NaN、
       同比与趋势、分红口径，均用手算值断言
    4. 预处理：winsorize / rank / zscore / 行业中性化 / 市值中性化 / 缺失值处理
       以及非法配置必须失败

【运行方式】
  python tests/test_factor_engine.py
  （纯合成数据，不联网、不碰数据库）
==============================================================================
"""

import math
import os
import sys

# 将项目根目录加入模块搜索路径，保证直接运行本脚本时能 import stocklab
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

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


def _frame():
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


def run_registry_test():
    """测试登记表：分类齐全、同名重复拒绝、未登记名字的错误信息"""
    print("\n" + "=" * 65)
    print("【阶段一：测试因子登记表】")
    print("=" * 65)

    names = list_factors()
    assert len(names) == 27, len(names)

    # §7 各小节的因子必须都在
    expected = {
        "value": ["dividend_yield", "ev_ebitda", "fcf_yield", "pb", "pe_ttm", "ps"],
        "quality": ["cfo_to_net_profit", "debt_to_equity", "gross_margin",
                    "operating_margin", "roe", "roic"],
        "growth": ["eps_growth", "fcf_growth", "profit_growth",
                   "revenue_growth", "roe_trend"],
        "momentum": ["momentum_1m", "momentum_3m", "momentum_6m", "momentum_12m"],
        "dividend": ["dividend_growth", "dividend_stability", "dividend_yield",
                     "payout_ratio"],
        "risk": ["max_drawdown_12m", "volatility_12m", "volatility_6m"],
    }
    for category, required in expected.items():
        actual = list_factors(category)
        for factor_name in required:
            assert factor_name in actual, "%s 缺少因子 %s" % (category, factor_name)
    assert tuple(FACTOR_CATEGORIES) == ("value", "quality", "growth",
                                        "momentum", "dividend", "risk")
    assert FACTOR_VERSION.startswith("factor_v"), FACTOR_VERSION
    print("  -> %d 个因子，六大分类齐全（版本 %s）" % (len(names), FACTOR_VERSION))

    # 股息率同时归属 value 与 dividend（§7.1 与 §7.5 都列了它）
    assert "dividend_yield" in list_factors("value")
    assert "dividend_yield" in list_factors("dividend")
    print("  -> dividend_yield 同时出现在 value 与 dividend 分类")

    try:
        register(get("pe_ttm"))
        raise AssertionError("同名因子重复登记必须被拒绝")
    except FactorDataError as error:
        print("  -> 重复登记拒绝: %s" % error)

    try:
        get("not_a_factor")
        raise AssertionError("未登记因子必须抛异常")
    except FactorDataError as error:
        assert "pe_ttm" in str(error)  # 错误信息必须列出可用因子
        print("  -> 未登记因子报错并给出可用清单")

    # 分类/输入列非法的因子定义必须在构造时就失败
    for bad_args in [
        ("", ("value",), ("x",), lambda f: f["x"]),
        ("bad", ("nope",), ("x",), lambda f: f["x"]),
        ("bad", ("value",), (), lambda f: f["x"]),
    ]:
        try:
            Factor(*bad_args)
            raise AssertionError("非法因子定义必须抛异常: %s" % (bad_args,))
        except FactorDataError:
            pass
    print("  -> 因子定义的分类与输入列在构造期校验")


def run_factor_contract_test():
    """测试因子契约：缺列、返回类型、返回长度都必须显式失败"""
    print("\n" + "=" * 65)
    print("【阶段二：测试因子输入帧契约】")
    print("=" * 65)

    frame = _frame()

    try:
        get("fcf_yield").values(frame.drop(columns=["total_mv"]))
        raise AssertionError("缺输入列必须抛 FactorDataError")
    except FactorDataError as error:
        assert "total_mv" in str(error)
        print("  -> 缺列拒绝: %s" % error)

    register_directly(
        Factor("contract_probe", ("value",), ("pb",), lambda f: 1.0, "返回标量")
    )
    try:
        get("contract_probe").values(frame)
        raise AssertionError("返回标量必须抛 FactorDataError")
    except FactorDataError as error:
        print("  -> 返回非 Series 拒绝: %s" % error)

    register_directly(
        Factor(
            "contract_probe_length",
            ("value",),
            ("pb",),
            lambda f: f["pb"].iloc[:2],
            "返回长度不符",
        )
    )
    try:
        get("contract_probe_length").values(frame)
        raise AssertionError("返回长度不符必须抛 FactorDataError")
    except FactorDataError as error:
        print("  -> 长度不符拒绝: %s" % error)

    values = compute(frame, ["pe_ttm", "roe"])
    assert list(values.columns) == ["pe_ttm", "roe"]
    assert len(values) == len(frame)
    print("  -> compute 输出列与索引对齐帧")


def run_factor_math_test():
    """测试因子口径：安全除法、EV/EBITDA、同比与趋势、分红（手算断言）"""
    print("\n" + "=" * 65)
    print("【阶段三：测试因子计算口径】")
    print("=" * 65)

    frame = _frame()
    values = compute(
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

    # 透传类不改口径
    assert values["pe_ttm"].iloc[0] == 28.0
    assert values["dividend_yield"].iloc[1] == 3.8
    assert values["momentum_12m"].iloc[2] == 0.90
    assert values["max_drawdown_12m"].iloc[3] == -0.7
    print("  -> 透传类因子（PE / 股息率 / 12M 动量 / 最大回撤）原样返回")

    # EV/EBITDA：正 EBITDA 才算，负值 -> NaN（第 4 只 ebitda < 0）
    expected_ev = 3.0e12 / 1.2e11
    assert math.isclose(values["ev_ebitda"].iloc[0], expected_ev, rel_tol=1e-9)
    assert values["ev_ebitda"].isna().iloc[3]
    print("  -> EV/EBITDA = %.2f，EBITDA 为负的标的为 NaN" % expected_ev)

    # 自由现金流收益率 = free_cashflow / total_mv（分母 0 -> NaN 不在这里触发）
    assert math.isclose(values["fcf_yield"].iloc[0], 6.0e10 / 3.0e12, rel_tol=1e-9)
    assert values["fcf_yield"].iloc[2] < 0  # 现金流为负要如实保留负值
    print("  -> FCF 收益率 %.4f%%，负现金流保持负值（不伪造）"
          % (values["fcf_yield"].iloc[0] * 100))

    # CFO/净利润、产权比率
    assert math.isclose(values["cfo_to_net_profit"].iloc[0],
                        7.0e10 / 8.6e10, rel_tol=1e-9)
    assert math.isclose(values["debt_to_equity"].iloc[0],
                        3.0e10 / 2.5e11, rel_tol=1e-9)
    assert values["debt_to_equity"].isna().iloc[3]  # equity = 0 -> NaN
    print("  -> CFO/净利润 %.3f、Debt/Equity %.3f，净资产为 0 -> NaN"
          % (values["cfo_to_net_profit"].iloc[0], values["debt_to_equity"].iloc[0]))

    # 成长：透传同比 + 自由现金流同比 + ROE 趋势
    assert values["revenue_growth"].iloc[0] == 0.177
    assert math.isclose(values["fcf_growth"].iloc[0],
                        (6.0e10 - 5.0e10) / 5.0e10, rel_tol=1e-9)
    assert values["fcf_growth"].isna().iloc[3]  # 上年同期为 0 -> NaN
    assert math.isclose(values["roe_trend"].iloc[0], 0.37 - 0.34, rel_tol=1e-9)
    assert math.isclose(values["roe_trend"].iloc[1], 0.11 - 0.12, rel_tol=1e-9)
    print("  -> FCF 同比 %.2f、ROE 趋势 %+.3f（负值如实保留）"
          % (values["fcf_growth"].iloc[0], values["roe_trend"].iloc[1]))

    # 分红：增长率（上年为 0 -> NaN）、分红率、稳定性
    assert math.isclose(values["payout_ratio"].iloc[0],
                        30.88 / 66.6, rel_tol=1e-9)
    assert math.isclose(values["dividend_stability"].iloc[1], 1.0, rel_tol=1e-9)
    assert math.isclose(values["dividend_stability"].iloc[3], 0.0, rel_tol=1e-9)
    assert values["dividend_growth"].isna().iloc[2]  # dps_prior_year = 0
    print("  -> 分红率 %.3f、稳定性 %.1f / 0.0，上年分红为 0 -> 同比 NaN"
          % (values["payout_ratio"].iloc[0], values["dividend_stability"].iloc[1]))


def run_preprocessing_test():
    """测试预处理六种方法与非法配置的拒绝"""
    print("\n" + "=" * 65)
    print("【阶段四：测试因子预处理】")
    print("=" * 65)

    frame = pd.DataFrame(
        {
            "industry": ["a", "a", "b", "b", "c", ""],
            "total_mv": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
            "pe_ttm": [5.0, 7.0, 9.0, 11.0, 13.0, 1000.0],
            "roe": [0.1, 0.3, 0.2, 0.4, 0.5, float("nan")],
        }
    )

    # 1) winsorize：上下各裁 20% 尾部（即截断到 20% ~ 80% 分位）
    clipped = apply_preprocessing(
        frame, [{"method": "winsorize", "columns": ["pe_ttm"],
                 "lower": 0.2, "upper": 0.2}]
    )
    assert clipped["pe_ttm"].max() == frame["pe_ttm"].quantile(0.8)
    assert clipped["pe_ttm"].min() == frame["pe_ttm"].quantile(0.2)
    assert len(clipped) == len(frame)  # 不丢行
    assert frame["pe_ttm"].max() == 1000.0  # 原帧不被修改
    print("  -> winsorize 截断到上下尾分位（原极值 1000 被裁掉），且不修改原帧")

    # 2) rank：转百分位，缺失保持 NaN
    ranked = apply_preprocessing(frame, [{"method": "rank", "columns": ["roe"]}])
    assert pd.isna(ranked["roe"].iloc[5])
    assert ranked["roe"].max() == 1.0
    print("  -> rank 转百分位（0~1），缺失值保持 NaN")

    # 3) zscore：标准化后均值近 0、标准差近 1
    zed = apply_preprocessing(frame, [{"method": "zscore", "columns": ["pe_ttm"]}])
    assert abs(zed["pe_ttm"].mean()) < 1e-9
    assert math.isclose(zed["pe_ttm"].std(ddof=1), 1.0, rel_tol=1e-9)
    print("  -> zscore 后均值 0、样本标准差 1")

    # 4) 行业中性化：组内均值为 0；分组键缺失的行置 NaN；全空则拒绝
    neutral = apply_preprocessing(
        frame, [{"method": "industry_neutralize", "columns": ["roe"]}]
    )
    group_means = neutral.groupby(frame["industry"])["roe"].mean().drop("")
    assert all(abs(value) < 1e-12 for value in group_means), group_means
    assert pd.isna(neutral["roe"].iloc[5])  # 行业缺失 -> 无法中性化
    print("  -> 行业中性化组内均值为 0，行业缺失的行置 NaN")

    try:
        apply_preprocessing(
            frame.assign(industry=""),
            [{"method": "industry_neutralize", "columns": ["roe"]}],
        )
        raise AssertionError("分组列全空必须拒绝")
    except FactorDataError as error:
        print("  -> 分组列全空拒绝: %s" % error)

    # 5) 市值中性化：残差与 log(市值) 的相关性近 0
    capped = apply_preprocessing(
        frame.drop(index=5).reset_index(drop=True),
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
    print("  -> 市值中性化残差与 log(市值) 协方差 ≈ 0")

    # 6) 缺失值处理
    filled = apply_preprocessing(
        frame, [{"method": "missing", "columns": ["roe"], "action": "median"}]
    )
    assert not filled["roe"].isna().any()
    dropped = apply_preprocessing(
        frame, [{"method": "missing", "columns": ["roe"], "action": "drop"}]
    )
    assert len(dropped) == len(frame) - 1
    constant = apply_preprocessing(
        frame.assign(pe_ttm=1.0),
        [{"method": "missing", "columns": ["pe_ttm"], "action": "zero"},
         {"method": "zscore", "columns": ["pe_ttm"]}],
    )
    assert constant["pe_ttm"].isna().all()  # 无离散度 -> NaN，不产生 inf
    print("  -> 缺失值：中位数填充 / 丢行 / 无离散度置 NaN")

    # 7) 非法配置一律拒绝
    for bad_step in [
        {"method": "nope"},
        {"method": "rank", "columns": "missing_column"},
        {"method": "winsorize", "columns": ["roe"], "lower": 0.6, "upper": 0.6},
        {"method": "missing", "action": "nope"},
        {"method": "market_cap_neutralize", "columns": ["pe_ttm"],
         "column": "not_mv"},
        "not-a-dict",
    ]:
        try:
            apply_preprocessing(frame, [bad_step])
            raise AssertionError("非法预处理配置必须被拒绝: %s" % (bad_step,))
        except FactorDataError:
            pass
    print("  -> 6 类非法配置（未知方法/缺列/分位越界/非法动作/缺市值列/非字典）全部拒绝")

    assert apply_preprocessing(frame, None).equals(frame)
    assert apply_preprocessing(frame, [{"method": "zscore", "columns": ["roe"]}]).shape == frame.shape
    print("  -> 空步骤原样返回副本")


def main():
    """运行全部因子引擎测试"""
    run_registry_test()
    run_factor_contract_test()
    run_factor_math_test()
    run_preprocessing_test()
    print("\n" + "=" * 65)
    print("因子引擎测试全部通过！")
    print("=" * 65)


if __name__ == "__main__":
    main()
