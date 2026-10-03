#!/usr/bin/env python3
"""
==============================================================================
StockLab - 筛选规则 (stocklab.screener.rules)
==============================================================================

【模块职责】
   V2 需求 §8.1 的结构化筛选条件：
     - ScreenRule   单条规则：因子 × 操作符 × 阈值
     - ScreenGroup  规则组：AND / OR，可任意嵌套
     - OPERATORS    操作符表（lt/le/gt/ge/eq/ne/in/isna/notna）

【设计原则】
   - 条件是**数据**不是代码：用 spec 字典描述，禁止在业务里写 if xxx 链；
   - 每条规则都能自解释（describe / explain），筛选结果据此回答「为什么进入 /
     为什么被排除」（§8.2）；
   - 缺失值语义统一：除 isna 外，因子值缺失一律判为**不通过**，并明确写
     「无数据」，绝不因为 NaN 比较结果为 False 就含糊过去；
   - 规则必须指向已登记因子，拼错因子名在构造期就报错。
"""

import pandas as pd

from stocklab.factor import FactorDataError
from stocklab.factor import registry as factor_registry

__all__ = [
    "ScreenRule",
    "ScreenGroup",
    "ScreenError",
    "OPERATORS",
]


class ScreenError(Exception):
    """筛选条件非法（未知操作符/阈值类型不对/结构不完整/因子未登记）"""


def _masked_compare(compare):
    """把二元比较包一层：因子值缺失时一律返回 False（不通过）"""

    def _apply(values, threshold):
        valid = values.notna()
        result = pd.Series(False, index=values.index, dtype=bool)
        result[valid] = compare(values[valid], threshold)
        return result

    return _apply


def _apply_in(values, options):
    """属于集合（NaN 不属于任何集合 -> False）"""
    return values.isin(list(options))


def _apply_isna(values, threshold):
    """因子值缺失"""
    return values.isna()


def _apply_notna(values, threshold):
    """因子值存在"""
    return values.notna()


# 操作符 -> (计算函数, 中文描述)；表驱动，避免长 if/elif
OPERATORS = {
    "lt": (_masked_compare(lambda values, threshold: values < threshold), "小于"),
    "le": (_masked_compare(lambda values, threshold: values <= threshold), "等于或小于"),
    "gt": (_masked_compare(lambda values, threshold: values > threshold), "大于"),
    "ge": (_masked_compare(lambda values, threshold: values >= threshold), "等于或大于"),
    "eq": (_masked_compare(lambda values, threshold: values == threshold), "等于"),
    "ne": (_masked_compare(lambda values, threshold: values != threshold), "不等于"),
    "in": (_apply_in, "属于"),
    "isna": (_apply_isna, "缺失"),
    "notna": (_apply_notna, "存在"),
}


def format_number(value):
    """
    把阈值/因子值格式化成可读文本（供 explain 与结果列展示）

    Args:
        value: 数值或 None

    Returns:
        str: 缺失 -> "无数据"；其余用 %.6g（3e12 之类的大数也紧凑）
    """
    if value is None:
        return "-"
    if isinstance(value, float) and value != value:
        return "无数据"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:
        return "无数据"
    return "%.6g" % number


class ScreenRule:
    """
    单条筛选规则

    【示例】
       ScreenRule("pe_ttm", "lt", 20)     ->  PE-TTM < 20
       ScreenRule("roe", "gt", 0.12)      ->  ROE > 12%
       ScreenRule("dividend_yield", "gt", 0.03)
    """

    def __init__(self, factor, operator, value=None, label=None):
        if operator not in OPERATORS:
            raise ScreenError(
                "筛选操作符 %r 非法，可用: %s" % (operator, sorted(OPERATORS))
            )
        try:
            factor_registry.get(factor)  # 未登记因子 -> ScreenError（附可用清单）
        except FactorDataError as error:
            raise ScreenError("筛选条件非法: %s" % error)

        if operator == "in":
            if not isinstance(value, (list, tuple, set)):
                raise ScreenError(
                    "操作符 in 的取值必须是列表，实际: %r" % (value,)
                )
        elif operator in ("isna", "notna"):
            if value is not None:
                raise ScreenError("操作符 %s 不需要阈值" % operator)
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ScreenError(
                    "操作符 %s 的阈值必须是数字，实际: %r" % (operator, value)
                )

        self.factor = factor
        self.operator = operator
        self.value = value
        self.label = label or factor

    @property
    def description(self):
        """操作符中文描述"""
        return OPERATORS[self.operator][1]

    def evaluate(self, values):
        """
        逐行判定

        Args:
            values (pd.DataFrame): 因子值表，必须含本规则的因子列

        Returns:
            pd.Series: bool，True 表示该行通过本规则
        """
        if self.factor not in values.columns:
            raise ScreenError(
                "因子 %s 不在待评估的因子值表中（可用: %s）"
                % (self.factor, list(values.columns))
            )
        apply_function = OPERATORS[self.operator][0]
        return apply_function(values[self.factor].astype("float64"), self.value)

    def threshold_text(self):
        """阈值的可读文本"""
        if self.operator in ("isna", "notna"):
            return ""
        if self.operator == "in":
            return "[" + ", ".join(str(item) for item in self.value) + "]"
        return format_number(self.value)

    def describe(self):
        """结构化条件的一行文本，如 `pe_ttm < 20`"""
        if self.operator in ("isna", "notna"):
            return "%s %s" % (self.factor, self.description)
        if self.operator == "in":
            return "%s %s %s" % (self.factor, self.operator, self.threshold_text())
        return "%s %s %s" % (self.factor, self.operator, self.threshold_text())

    def explain(self, factor_value, passed):
        """
        解释单行的判定结果（§8.2：为什么进入 / 为什么被排除）

        Args:
            factor_value (float): 该行的因子值
            passed (bool): 判定结果

        Returns:
            str: 人类可读的判定说明
        """
        threshold = self.threshold_text()
        criterion = (
            self.description
            if not threshold
            else "%s %s" % (self.description, threshold)
        )
        if pd.isna(factor_value) and self.operator != "isna":
            return "%s 无数据（要求 %s）" % (self.label, criterion)
        if passed:
            return "%s = %s 满足 %s" % (
                self.label,
                format_number(factor_value),
                criterion,
            )
        return "%s = %s 未满足 %s" % (
            self.label,
            format_number(factor_value),
            criterion,
        )

    def to_spec(self):
        """序列化回 spec 字典（研究快照存 spec_json 用）"""
        spec = {"factor": self.factor, "operator": self.operator}
        if self.value is not None:
            spec["value"] = self.value
        if self.label != self.factor:
            spec["label"] = self.label
        return spec


class ScreenGroup:
    """
    筛选规则组：把多条规则/子组按 AND 或 OR 组合，可任意嵌套

    【示例】
       pe_ttm < 20 AND roe > 12% AND (dividend_yield > 3% OR momentum_12m > 0.2)
    """

    def __init__(self, logic, children):
        logic = (logic or "and").lower()
        if logic not in ("and", "or"):
            raise ScreenError("组逻辑 %r 只能是 and / or" % (logic,))
        if not children:
            raise ScreenError("筛选组至少需要一个条件")
        for child in children:
            if not isinstance(child, (ScreenRule, ScreenGroup)):
                raise ScreenError(
                    "筛选组的成员必须是 ScreenRule 或 ScreenGroup，实际: %s"
                    % type(child).__name__
                )
        self.logic = logic
        self.children = list(children)

    def rules(self):
        """按书写顺序摊平出全部规则（评估与结果展示都用它）"""
        flattened = []
        for child in self.children:
            if isinstance(child, ScreenGroup):
                flattened.extend(child.rules())
            else:
                flattened.append(child)
        return flattened

    def factors(self):
        """本组涉及的全部因子名（去重、保序）"""
        names = []
        for rule in self.rules():
            if rule.factor not in names:
                names.append(rule.factor)
        return names

    def evaluate(self, values):
        """
        逐行求值（AND/OR 递归组合）

        Args:
            values (pd.DataFrame): 因子值表

        Returns:
            pd.Series: bool，True 表示该行整体通过本组
        """
        flags = {}
        for rule in self.rules():
            flags[id(rule)] = rule.evaluate(values)
        return self.combine(flags)

    def combine(self, flags):
        """
        用已算好的逐规则结果组合出本组判定（同一条规则只算一次）

        Args:
            flags (dict): id(ScreenRule) -> bool Series

        Returns:
            pd.Series: bool，True 表示该行整体通过本组

        Raises:
            ScreenError: 某条规则缺少评估结果
        """
        result = None
        for child in self.children:
            if isinstance(child, ScreenRule):
                if id(child) not in flags:
                    raise ScreenError("缺少规则 %s 的评估结果" % child.describe())
                child_result = flags[id(child)]
            else:
                child_result = child.combine(flags)
            if result is None:
                result = child_result
            elif self.logic == "and":
                result = result & child_result
            else:
                result = result | child_result
        return result

    def describe(self):
        """结构化条件的整体文本"""
        parts = [child.describe() for child in self.children]
        joiner = " %s " % self.logic.upper()
        text = joiner.join(parts)
        if len(parts) > 1:
            return "(%s)" % text if self.logic == "or" else text
        return text

    def to_spec(self):
        """序列化回 spec 字典"""
        return {
            "logic": self.logic,
            "rules": [child.to_spec() for child in self.children],
        }
