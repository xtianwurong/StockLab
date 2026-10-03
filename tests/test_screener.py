#!/usr/bin/env python3
"""
==============================================================================
StockLab - 选股器测试 (tests/test_screener.py)
==============================================================================

【功能用途】
  覆盖 V2 需求 §8 的 Stock Screener：
    1. 规则与操作符：9 种操作符的判定、缺失值语义、非法操作符/阈值/因子名
    2. 规则组：AND / OR / 嵌套组合与条件文本
    3. 流水线执行：summary 与 detail 的字段（symbol/name/factor_value/threshold/
       passed/failed_rules）、整体通过数、逐只「为什么进入/为什么被排除」
    4. 预处理接入：winsorize 在判定前生效、spec 序列化往返可复现
    5. 非法 spec 全部拒绝，空帧不崩

【运行方式】
  python tests/test_screener.py
  （纯合成数据，不联网、不碰数据库）
==============================================================================
"""

import math
import os
import sys

# 将项目根目录加入模块搜索路径，保证直接运行本脚本时能 import stocklab
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from stocklab.factor import FactorDataError
from stocklab.screener import (
    OPERATORS,
    ScreenError,
    ScreenGroup,
    ScreenPipeline,
    ScreenRule,
)

NAN = float("nan")


def _frame():
    """4 只标的的合成因子输入帧（含 2 处缺失，用于验证缺失值语义）"""
    return pd.DataFrame(
        {
            "ts_code": ["A", "B", "C", "D"],
            "name": ["甲", "乙", "丙", "丁"],
            "pe_ttm": [15.0, 25.0, NAN, 8.0],
            "roe": [0.20, 0.05, 0.30, 0.15],
            "dv_ttm": [0.04, 0.02, 0.05, NAN],  # 因子 dividend_yield 的输入列
            "pb": [3.0, 0.9, 5.0, 1.5],
            "industry": ["x", "y", "x", "y"],
            "total_mv": [1.0e11, 2.0e11, 3.0e11, 4.0e11],
        }
    )


def run_rule_test():
    """测试单条规则：9 种操作符、缺失值语义、解释文本、非法输入"""
    print("\n" + "=" * 65)
    print("【阶段一：测试筛选规则与操作符】")
    print("=" * 65)

    values = pd.DataFrame({"pe_ttm": [15.0, 25.0, NAN]})
    cases = [
        ("lt", 20, [True, False, False]),
        ("le", 15, [True, False, False]),
        ("gt", 20, [False, True, False]),
        ("ge", 25, [False, True, False]),
        ("eq", 25, [False, True, False]),
        ("ne", 25, [True, False, False]),
        ("in", [15, 25], [True, True, False]),
        ("isna", None, [False, False, True]),
        ("notna", None, [True, True, False]),
    ]
    for operator, threshold, expected in cases:
        rule = ScreenRule("pe_ttm", operator, threshold)
        flags = rule.evaluate(values).tolist()
        assert flags == expected, "%s -> %s != %s" % (operator, flags, expected)
    assert sorted(OPERATORS) == ["eq", "ge", "gt", "in", "isna", "le", "lt",
                                 "ne", "notna"]
    print("  -> 9 种操作符判定与期望一致（含缺失值一律不通过，isna 除外）")

    rule = ScreenRule("pe_ttm", "lt", 20)
    assert rule.describe() == "pe_ttm lt 20"
    assert rule.explain(15.0, True) == "pe_ttm = 15 满足 小于 20"
    assert rule.explain(25.0, False) == "pe_ttm = 25 未满足 小于 20"
    assert rule.explain(NAN, False) == "pe_ttm 无数据（要求 小于 20）"
    print("  -> 解释文本可直接回答「为什么进入 / 为什么被排除」")

    assert rule.to_spec() == {"factor": "pe_ttm", "operator": "lt", "value": 20}
    labelled = ScreenRule("pe_ttm", "lt", 20, label="估值")
    assert labelled.to_spec() == {"factor": "pe_ttm", "operator": "lt",
                                  "value": 20, "label": "估值"}
    assert labelled.explain(15.0, True).startswith("估值 = 15")
    print("  -> 规则可序列化回 spec，自定义 label 用于结果展示")

    for bad_args in [
        ("pe_ttm", "gt"),              # 缺阈值
        ("pe_ttm", "nope", 1),         # 未知操作符
        ("not_a_factor", "lt", 1),     # 未登记因子
        ("pe_ttm", "in", 5),           # in 的取值不是列表
        ("pe_ttm", "isna", 1),         # isna 不需要阈值
        ("pe_ttm", "lt", "20"),        # 阈值不是数字
    ]:
        try:
            ScreenRule(*bad_args)
            raise AssertionError("非法规则必须被拒绝: %s" % (bad_args,))
        except ScreenError as error:
            assert str(error)
    print("  -> 6 类非法规则（缺阈值/未知操作符/未登记因子/取值类型）全部拒绝")


def run_group_test():
    """测试规则组：AND / OR / 嵌套、条件文本、序列化"""
    print("\n" + "=" * 65)
    print("【阶段二：测试规则组与整体条件】")
    print("=" * 65)

    group = ScreenGroup(
        "and",
        [
            ScreenRule("pe_ttm", "lt", 20),
            ScreenGroup(
                "or",
                [
                    ScreenRule("dividend_yield", "gt", 0.03),
                    ScreenRule("momentum_12m", "gt", 0.2),
                ],
            ),
        ],
    )
    assert len(group.rules()) == 3
    assert group.factors() == ["pe_ttm", "dividend_yield", "momentum_12m"]
    print("  -> 规则摊平 3 条，因子名去重保序")

    values = pd.DataFrame(
        {"pe_ttm": [15.0, 25.0, 15.0],
         "dividend_yield": [0.01, 0.05, NAN],
         "momentum_12m": [0.3, NAN, 0.05]}
    )
    assert group.evaluate(values).tolist() == [True, False, False]
    # 第 1 行：pe 合格 + 动量>0.2 成立 -> 通过；第 3 行：pe 合格但两个 OR 条件都不成立
    print("  -> AND(嵌套 OR) 组合判定正确")

    condition = group.describe()
    assert condition == "pe_ttm lt 20 AND (dividend_yield gt 0.03 OR momentum_12m gt 0.2)"
    print("  -> 条件文本: %s" % condition)

    spec = group.to_spec()
    assert spec["logic"] == "and"
    assert spec["rules"][1]["logic"] == "or"
    assert spec["rules"][1]["rules"][0] == {
        "factor": "dividend_yield",
        "operator": "gt",
        "value": 0.03,
    }
    print("  -> spec 序列化保留嵌套结构")

    for bad_logic in ["maybe", "AND/OR"]:
        try:
            ScreenGroup(bad_logic, [ScreenRule("pe_ttm", "lt", 20)])
            raise AssertionError("非法组逻辑必须被拒绝: %s" % bad_logic)
        except ScreenError as error:
            assert str(error)
    try:
        ScreenGroup("and", [])
        raise AssertionError("空组必须被拒绝")
    except ScreenError as error:
        assert str(error)
    try:
        ScreenGroup("and", ["not a rule"])
        raise AssertionError("组成员类型必须校验")
    except ScreenError as error:
        assert str(error)
    print("  -> 非法组逻辑 / 空组 / 成员类型全部拒绝")


def run_and_pipeline_test():
    """测试 AND 流水线：summary / detail 字段、通过数、逐只原因"""
    print("\n" + "=" * 65)
    print("【阶段三：测试筛选流水线（AND）】")
    print("=" * 65)

    pipeline = ScreenPipeline.from_spec(
        {
            "rules": [
                {"factor": "pe_ttm", "operator": "lt", "value": 20},
                {"factor": "roe", "operator": "gt", "value": 0.12},
            ]
        }
    )
    assert pipeline.condition == "pe_ttm lt 20 AND roe gt 0.12"
    assert pipeline.factor_names == ["pe_ttm", "roe"]
    print("  -> 条件: %s" % pipeline.condition)

    result = pipeline.run(_frame())
    summary = result.summary

    # §8.2：每只股票必须有 symbol / name / factor_value / threshold / passed / failed_rules
    assert list(summary["ts_code"]) == ["A", "B", "C", "D"]
    assert list(summary["name"]) == ["甲", "乙", "丙", "丁"]
    assert list(summary["passed"]) == [True, False, False, True]
    assert len(summary.loc[0, "failed_rules"]) == 0
    assert len(summary.loc[1, "failed_rules"]) == 2  # PE 与 ROE 都不合格
    assert len(summary.loc[2, "failed_rules"]) == 1  # 仅 PE 无数据
    assert "无数据" in summary.loc[2, "failed_rules"][0]
    print("  -> summary: 通过 [A, D]，逐只 failed_rules 与原因就位")

    assert result.counts == {"total": 4, "passed": 2}
    assert result.codes == ["A", "D"]
    print("  -> counts=%s，codes=%s" % (result.counts, result.codes))

    detail = result.detail
    assert len(detail) == 8  # 4 只 × 2 条规则
    assert set(["ts_code", "name", "factor", "operator", "threshold",
                "factor_value", "passed", "reason"]) <= set(detail.columns)
    row = detail[(detail["ts_code"] == "C") & (detail["factor"] == "pe_ttm")].iloc[0]
    assert bool(row["passed"]) is False
    assert pd.isna(row["factor_value"])
    assert row["threshold"] == "20"
    assert "无数据" in row["reason"]
    row = detail[(detail["ts_code"] == "A") & (detail["factor"] == "roe")].iloc[0]
    assert bool(row["passed"]) is True
    assert math.isclose(row["factor_value"], 0.20, rel_tol=1e-9)
    assert "满足" in row["reason"]
    print("  -> detail: 每只股票每条规则给出 factor_value / threshold / passed / reason")

    records = result.to_records()
    assert len(records) == 4
    assert isinstance(records[1]["failed_rules"], list)
    print("  -> to_records 可直接 JSON 化（failed_rules 保持列表）")


def run_or_pipeline_test():
    """测试 OR 流水线：整体通过但仍有未满足条件时，failed_rules 如实记录"""
    print("\n" + "=" * 65)
    print("【阶段四：测试筛选流水线（OR 与缺失值）】")
    print("=" * 65)

    pipeline = ScreenPipeline.from_spec(
        {
            "logic": "or",
            "rules": [
                {"factor": "roe", "operator": "gt", "value": 0.12},
                {"factor": "dividend_yield", "operator": "gt", "value": 0.03},
            ],
        }
    )
    result = pipeline.run(_frame())
    summary = result.summary
    # A: ROE 0.20 通过；B: 两个都不过；C: ROE 0.30 通过；D: ROE 0.15 通过（股息率无数据）
    assert list(summary["passed"]) == [True, False, True, True]
    assert len(summary.loc[1, "failed_rules"]) == 2
    # D 整体通过，但仍如实记录「股息率无数据」这条未满足的条件
    assert len(summary.loc[3, "failed_rules"]) == 1
    assert "无数据" in summary.loc[3, "failed_rules"][0]
    print("  -> OR 判定正确；通过的股票若有个别条件未满足也会如实记录")

    isna_result = ScreenPipeline.from_spec(
        {"rules": [{"factor": "dividend_yield", "operator": "isna"}]}
    ).run(_frame())
    assert isna_result.codes == ["D"]
    assert isna_result.detail.iloc[0]["threshold"] == "-"
    print("  -> isna 操作符：只有股息率缺失的 D 通过")

    empty = ScreenPipeline.from_spec(
        {"rules": [{"factor": "pe_ttm", "operator": "lt", "value": 20}]}
    ).run(_frame().iloc[0:0])
    assert empty.counts == {"total": 0, "passed": 0}
    assert len(empty.detail) == 0
    assert "ts_code" in empty.detail.columns
    print("  -> 空帧：结构完整、不崩溃")


def run_preprocess_and_repro_test():
    """测试预处理在判定前生效，以及 spec 往返可复现"""
    print("\n" + "=" * 65)
    print("【阶段五：测试预处理接入与 spec 复现】")
    print("=" * 65)

    base_spec = {"rules": [{"factor": "pe_ttm", "operator": "lt", "value": 21}]}
    raw = ScreenPipeline.from_spec(base_spec).run(_frame())
    assert list(raw.summary["passed"]) == [True, False, False, True]

    winsorized_spec = dict(base_spec)
    winsorized_spec["preprocess"] = [
        {"method": "winsorize", "columns": ["pe_ttm"], "lower": 0.25, "upper": 0.25}
    ]
    winsorized_spec["factors"] = ["pb"]
    winsorized = ScreenPipeline.from_spec(winsorized_spec)
    processed = winsorized.run(_frame())
    # 25 被缩尾到 80% 分位 20 -> 20 < 21 成立，B 因此由「排除」变「通过」
    assert list(processed.summary["passed"]) == [True, True, False, True]
    assert "pb" in processed.summary.columns
    print("  -> winsorize 在判定前生效：B 的 PE 被缩尾后通过；pb 作为额外因子输出")
    assert "preprocess" in winsorized.spec()
    print("  -> 条件文本不变: %s" % winsorized.condition)

    # spec 往返：序列化 -> 重新解析 -> 再序列化必须一致（研究快照据此复现）
    spec = winsorized.spec()
    rebuilt = ScreenPipeline.from_spec(spec)
    assert rebuilt.spec() == spec
    assert rebuilt.condition == winsorized.condition
    rerun = rebuilt.run(_frame())
    assert list(rerun.summary["passed"]) == list(processed.summary["passed"])
    assert rerun.detail.equals(processed.detail)
    print("  -> spec 序列化往返一致，重跑结果逐行相同（可复现）")

    nested_spec = {
        "logic": "and",
        "rules": [
            {"factor": "pe_ttm", "operator": "lt", "value": 20},
            {"logic": "or", "rules": [
                {"factor": "dividend_yield", "operator": "gt", "value": 0.03},
                {"factor": "pb", "operator": "lt", "value": 1.0},
            ]},
        ],
    }
    nested = ScreenPipeline.from_spec(nested_spec)
    assert ScreenPipeline.from_spec(nested.spec()).spec() == nested.spec()
    assert nested.condition == (
        "pe_ttm lt 20 AND (dividend_yield gt 0.03 OR pb lt 1)"
    )
    # A: PE 合格 + 股息率 4% 成立 -> 通过；C 的 PE 无数据；D 的两个 OR 条件都不成立
    assert nested.run(_frame()).codes == ["A"]
    print("  -> 嵌套 OR spec 往返一致，判定 [A]")


def run_invalid_spec_test():
    """测试非法 spec 一律在构造期拒绝"""
    print("\n" + "=" * 65)
    print("【阶段六：测试非法筛选配置】")
    print("=" * 65)

    for bad_spec in [
        {},
        {"rules": []},
        {"rules": ["not a dict"]},
        {"rules": [{"factor": "nope", "operator": "lt", "value": 1}]},
        {"rules": [{"factor": "pe_ttm"}]},
        {"rules": [{"factor": "pe_ttm", "operator": "nope", "value": 1}]},
        {"rules": [{"logic": "maybe",
                    "rules": [{"factor": "pe_ttm", "operator": "lt",
                               "value": 1}]}]},
        {"rules": [{"factor": "pe_ttm", "operator": "lt", "value": 1}],
         "factors": ["not_a_factor"]},
    ]:
        try:
            ScreenPipeline.from_spec(bad_spec)
            raise AssertionError("非法 spec 必须被拒绝: %s" % (bad_spec,))
        except ScreenError as error:
            assert str(error)
    print("  -> 8 类非法 spec（空结构/坏节点/未登记因子/坏操作符/坏逻辑/坏额外因子）全部拒绝")

    # 非法预处理在执行期（配置被真正使用时）失败
    try:
        ScreenPipeline.from_spec(
            {"rules": [{"factor": "pe_ttm", "operator": "lt", "value": 1}],
             "preprocess": [{"method": "nope"}]}
        ).run(_frame())
        raise AssertionError("非法预处理必须被拒绝")
    except FactorDataError as error:
        message = str(error)
    assert "nope" in message
    print("  -> 非法预处理步骤在执行期拒绝: %s" % message)

    pipeline = ScreenPipeline.from_spec(
        {"rules": [{"factor": "pe_ttm", "operator": "lt", "value": 20}]}
    )
    try:
        pipeline.run(pd.DataFrame({"pe_ttm": [15.0]}))
        raise AssertionError("缺 ts_code 必须被拒绝")
    except ScreenError as error:
        assert "ts_code" in str(error)
    print("  -> 输入帧缺 ts_code 拒绝")

    try:
        pipeline.run(pd.DataFrame({"ts_code": ["A"]}))
        raise AssertionError("因子输入列缺失必须抛 FactorDataError")
    except FactorDataError as error:
        message = str(error)
    assert "pe_ttm" in message
    print("  -> 因子输入列缺失（帧构造缺陷）抛 FactorDataError: %s" % message)


def main():
    """运行全部选股器测试"""
    run_rule_test()
    run_group_test()
    run_and_pipeline_test()
    run_or_pipeline_test()
    run_preprocess_and_repro_test()
    run_invalid_spec_test()
    print("\n" + "=" * 65)
    print("选股器测试全部通过！")
    print("=" * 65)


if __name__ == "__main__":
    main()
