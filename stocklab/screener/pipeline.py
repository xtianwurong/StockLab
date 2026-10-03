#!/usr/bin/env python3
"""
==============================================================================
StockLab - 筛选流水线 (stocklab.screener.pipeline)
==============================================================================

【模块职责】
   V2 需求 §8 的筛选执行与结果：
     - ScreenPipeline  一条筛选流水线 = 规则组 + 可选预处理 + 额外输出因子
     - ScreenResult    结果：summary（每只股票一行）+ detail（每只股票每条规则一行）

【执行顺序】
   1. 计算规则涉及的因子（含 spec.factors 里额外指定的因子）
   2. 把因子值按列名并入工作帧（同名列被因子值覆盖，口径唯一）
   3. 按配置做预处理（§7.6），**预处理之后才是判定**，所以阈值作用在处理后的值上
   4. 逐规则判定，再按 AND/OR 组合出整体 passed
   5. 产出 summary / detail 两张表

【结果契约（§8.2）】
   每只股票必须能回答：symbol、name、factor_value、threshold、passed、failed_rules，
   并且能回答「为什么进入 / 为什么被排除」——
   summary 给出整体 passed 与 failed_rules 列表，
   detail 给出（股票 × 规则）粒度的 factor_value / threshold / passed / reason。
   因子缺失一律判不通过且原因写「无数据」，不静默放行。
"""

import pandas as pd

from stocklab.factor import FactorDataError, apply_preprocessing
from stocklab.factor import registry as factor_engine
from stocklab.screener.rules import ScreenError, ScreenGroup, ScreenRule

__all__ = [
    "ScreenPipeline",
    "ScreenResult",
]


def _child_from_spec(spec, path):
    """
    把一个 spec 节点解析成规则或子组

    Args:
        spec (dict): {"factor","operator","value"} 或 {"logic","rules"}
        path (str): 节点路径，仅用于报错定位

    Returns:
        ScreenRule | ScreenGroup

    Raises:
        ScreenError: 结构非法
    """
    if not isinstance(spec, dict):
        raise ScreenError("%s 必须是字典，实际: %r" % (path, spec))

    if "rules" in spec:
        children = [
            _child_from_spec(child, "%s.rules[%d]" % (path, index))
            for index, child in enumerate(spec["rules"])
        ]
        if not children:
            raise ScreenError("%s 的 rules 不能为空" % path)
        return ScreenGroup(spec.get("logic"), children)

    if "factor" not in spec or "operator" not in spec:
        raise ScreenError(
            "%s 必须含 factor 与 operator（或作为组提供 rules），实际: %s"
            % (path, sorted(spec))
        )
    return ScreenRule(
        spec["factor"],
        spec["operator"],
        spec.get("value"),
        spec.get("label"),
    )


def _group_from_spec(spec, path):
    """顶层组：允许直接写 {\"rules\": [...]}（默认 AND）"""
    if not isinstance(spec, dict):
        raise ScreenError("%s 必须是字典，实际: %r" % (path, spec))
    if "rules" not in spec:
        raise ScreenError("%s 缺少 rules 列表" % path)
    return _child_from_spec(spec, path)


class ScreenResult:
    """筛选结果：summary（每只股票一行）+ detail（股票 × 规则长表）"""

    def __init__(self, summary, detail, condition):
        self.summary = summary
        self.detail = detail
        self.condition = condition

    @property
    def counts(self):
        """总标的数与通过数"""
        return {"total": int(len(self.summary)), "passed": int(self.summary["passed"].sum()) if len(self.summary) else 0}

    @property
    def codes(self):
        """通过筛选的股票代码列表（保持输入帧顺序）"""
        if not len(self.summary):
            return []
        return self.summary.loc[self.summary["passed"], "ts_code"].tolist()

    def to_records(self):
        """把 summary 转成可 JSON 化的记录列表（failed_rules 保持为列表）"""
        records = []
        for row in self.summary.to_dict(orient="records"):
            records.append(row)
        return records

    def __repr__(self):
        counts = self.counts
        return "ScreenResult(total=%d, passed=%d)" % (counts["total"], counts["passed"])


class ScreenPipeline:
    """
    筛选流水线

    【示例】
       pipeline = ScreenPipeline.from_spec({
           "rules": [
               {"factor": "pe_ttm", "operator": "lt", "value": 20},
               {"factor": "roe", "operator": "gt", "value": 0.12},
               {"logic": "or", "rules": [
                   {"factor": "dividend_yield", "operator": "gt", "value": 0.03},
                   {"factor": "momentum_12m", "operator": "gt", "value": 0.2},
               ]},
           ],
           "preprocess": [{"method": "winsorize", "columns": ["pe_ttm"]}],
       })
       result = pipeline.run(frame)
    """

    def __init__(self, group, preprocess=None, factors=None):
        self._group = group
        self._preprocess = list(preprocess) if preprocess else []
        self._extra_factors = list(factors) if factors else []

        for name in self._extra_factors:
            try:
                factor_engine.get(name)  # 未登记因子 -> ScreenError
            except FactorDataError as error:
                raise ScreenError("筛选配置非法: %s" % error)

    @classmethod
    def from_spec(cls, spec):
        """
        从 spec 字典构建流水线（spec 可来自 YAML/JSON，条件必须结构化）

        Args:
            spec (dict): {"rules": [...], "preprocess": [...], "factors": [...]}

        Returns:
            ScreenPipeline
        """
        if not isinstance(spec, dict):
            raise ScreenError("筛选 spec 必须是字典，实际: %r" % (spec,))
        group = _group_from_spec(spec, "spec")
        return cls(
            group,
            spec.get("preprocess"),
            spec.get("factors"),
        )

    def spec(self):
        """序列化回 spec 字典（用于研究快照 spec_json，保证可复现）"""
        spec = self._group.to_spec()
        if self._preprocess:
            spec["preprocess"] = self._preprocess
        if self._extra_factors:
            spec["factors"] = self._extra_factors
        return spec

    @property
    def condition(self):
        """整体条件的可读文本，如 `pe_ttm < 20 AND roe > 0.12`"""
        return self._group.describe()

    @property
    def factor_names(self):
        """流水线需要的全部因子（规则因子 + 额外输出因子），去重保序"""
        names = list(self._group.factors())
        for name in self._extra_factors:
            if name not in names:
                names.append(name)
        return names

    def run(self, frame):
        """
        在因子输入帧上执行筛选

        Args:
            frame (pd.DataFrame): 因子输入帧，必须含 ts_code（可选 name）

        Returns:
            ScreenResult: summary + detail

        Raises:
            ScreenError: 帧缺 ts_code
            FactorDataError: 因子输入列缺失（帧构造方的缺陷，属编程错误）
        """
        if "ts_code" not in frame.columns:
            raise ScreenError("因子输入帧必须包含 ts_code 列")

        factor_names = self.factor_names
        factor_values = factor_engine.compute(frame, factor_names)

        work = frame.copy()
        for name in factor_names:
            work[name] = factor_values[name]
        work = apply_preprocessing(work, self._preprocess)
        evaluated = work[factor_names]

        # 逐规则判定一次，再按 AND/OR 组合（同一条规则不重复计算）
        rule_flags = {}
        for rule in self._group.rules():
            rule_flags[id(rule)] = rule.evaluate(evaluated)
        passed = self._group.combine(rule_flags)

        summary, detail = self._build_output(work, evaluated, rule_flags, passed)
        return ScreenResult(summary, detail, self.condition)

    def _build_output(self, work, evaluated, rule_flags, passed):
        """生成 summary（每只股票一行）与 detail（股票 × 规则长表）"""
        ts_codes = work["ts_code"].tolist()
        has_name = "name" in work.columns
        names = work["name"].tolist() if has_name else [None] * len(ts_codes)

        failed_by_code = {code: [] for code in ts_codes}
        detail_columns = ["ts_code"]
        if has_name:
            detail_columns.append("name")
        detail_columns += [
            "factor",
            "operator",
            "threshold",
            "factor_value",
            "passed",
            "reason",
        ]

        detail_frames = []
        for rule in self._group.rules():
            flags = rule_flags[id(rule)]
            column_values = evaluated[rule.factor].tolist()
            flag_values = flags.tolist()
            reasons = [
                rule.explain(value, bool(flag))
                for value, flag in zip(column_values, flag_values)
            ]
            for code, flag, reason in zip(ts_codes, flag_values, reasons):
                if not flag:
                    failed_by_code[code].append(reason)

            column = {
                "ts_code": ts_codes,
                "factor": rule.factor,
                "operator": rule.operator,
                "threshold": rule.threshold_text() or "-",
                "factor_value": column_values,
                "passed": flag_values,
                "reason": reasons,
            }
            if has_name:
                column["name"] = names
            detail_frames.append(pd.DataFrame(column, columns=detail_columns))

        if detail_frames:
            detail = pd.concat(detail_frames, ignore_index=True)
        else:
            detail = pd.DataFrame(columns=detail_columns)

        summary_column = {"ts_code": ts_codes, "passed": passed.tolist()}
        if has_name:
            summary_column["name"] = names
        summary_column["failed_rules"] = [failed_by_code[code] for code in ts_codes]
        for name in self.factor_names:
            summary_column[name] = evaluated[name].tolist()

        summary_columns = ["ts_code"]
        if has_name:
            summary_columns.append("name")
        summary_columns += ["passed", "failed_rules"] + list(self.factor_names)
        summary = pd.DataFrame(summary_column, columns=summary_columns)
        return summary, detail
