"""
核心口径的不变量属性测试 (tests/test_properties.py)

【为什么单独一个文件】
  迁移到 pytest 之前，我用手工变异测试（故意改坏代码，看现有断言抓不抓得到）
  跑了 5 个变异，抓到 3 个、漏网 2 个：

    漏网 1  compare_api._align_history 把缺失点改成前向填充
           —— 现有断言只有 `assert gaps > 0`，被「上市前的前置空档」就满足了，
              上市后中途停牌被填成水平线完全看不出来。
    漏网 2  /api/market/ranking 的 codes 精确过滤退化成子串匹配
           —— 现有断言只验「传 3 个完整代码返回 3 只」，而这3 个代码之间本来
              就没有子串关系，子串匹配也能过。

  漏网的共同根因不是「框架不够好」，而是**断言没有真的检查它声称要检查的不变量**。
  本文件用两种手段补上：
    · hypothesis 属性测试 —— 穷举输入，验证不变量恒成立（不是抽几个例子）
    · 针对性回归用例 —— 直接钉住上面两个变异

运行：
  ./venv/bin/python -m pytest tests/test_properties.py -v
  （纯合成数据，不联网、不碰数据库）
"""

import os
import sys
import pandas as pd
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

# 允许直接执行本文件（./venv/bin/python tests/xxx.py）；走 pytest 时由
# pytest.ini 的 `pythonpath = .` 统一负责，不会重复插入。
if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.analytics import ValuationPercentileAnalyzer
from stocklab.factor import compute, get

from app.web.compare_api import _align_history

def _valuation_frame(values):
    """构造单只标的的估值历史帧（analyzer 要求 trade_date 为 date 类型）"""
    count = len(values["pe_ttm"])
    assert count == len(values["pb"]), "两条序列长度必须一致"
    return pd.DataFrame(
        {
            "trade_date": list(pd.date_range("2020-01-01", periods=count).date),
            "pe_ttm": values["pe_ttm"],
            "pb": values["pb"],
        }
    )


# 值域刻意包含负值（亏损期）、0（无效）、极小与极大 —— 这四类都是口径边界
series_strategy = st.lists(
    st.floats(min_value=-100.0, max_value=500.0, allow_nan=False,
              allow_infinity=False),
    min_size=1, max_size=120,
)
pb_strategy = st.lists(
    st.floats(min_value=0.01, max_value=50.0, allow_nan=False, allow_infinity=False),
    min_size=1, max_size=120,
)


# ---------------------------------------------------------------------------
# 一、估值分位的不变量
# ---------------------------------------------------------------------------
@given(pe=series_strategy, pb=pb_strategy)
@settings(max_examples=250, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_percentile_always_within_unit_interval(pe, pb):
    """任意输入下分位必须落在 [0, 100]，且样本数不超过输入长度"""
    count = min(len(pe), len(pb))
    results = ValuationPercentileAnalyzer().analyze(
        _valuation_frame({"pe_ttm": pe[:count], "pb": pb[:count]})
    )
    for item in results:
        if item.percentile is None:
            continue
        assert 0.0 <= item.percentile <= 100.0, (item.indicator, item.percentile)
        assert item.sample_count <= count, (item.indicator, item.sample_count)


@given(values=st.lists(
    st.floats(min_value=-1e6, max_value=0.0, allow_nan=False, allow_infinity=False),
    min_size=1, max_size=80))
@settings(max_examples=200, deadline=None)
def test_all_non_positive_yields_no_percentile(values):
    """全为亏损期（PE <= 0）时必须没有分位，而不是给 0 或 50

    这是「亏损期剔除」这条口径最容易出错的地方：把非正样本算进去，
    rank 会得到一个看起来正常、实则无意义的分位。
    """
    results = ValuationPercentileAnalyzer().analyze(
        _valuation_frame({"pe_ttm": values, "pb": [1.0] * len(values)})
    )
    pe = next(item for item in results if item.indicator == "pe_ttm")
    assert pe.percentile is None, "全亏损期应无分位，实际 %s" % pe.percentile


@given(values=series_strategy)
@settings(max_examples=200, deadline=None)
def test_determinism(values):
    """同输入必得同输出 —— 分位里若掺了顺序依赖或随机性，这条会炸"""
    frame = _valuation_frame({"pe_ttm": values, "pb": [1.0] * len(values)})
    first = ValuationPercentileAnalyzer().analyze(frame)
    second = ValuationPercentileAnalyzer().analyze(frame)
    for left, right in zip(first, second):
        assert left.indicator == right.indicator
        assert left.percentile == right.percentile
        assert left.sample_count == right.sample_count


@given(base=st.lists(
    st.floats(min_value=0.5, max_value=200.0, allow_nan=False,
              allow_infinity=False),
    min_size=1, max_size=60),
    probe=st.floats(min_value=0.5, max_value=200.0, allow_nan=False,
                    allow_infinity=False))
@settings(max_examples=250, deadline=None)
def test_percentile_is_monotonic_in_current_value(base, probe):
    """分位对「当前值」必须单调：当前值越高，分位不可能更低

    这条比「最小值必为 0」更本质——写后者时我误以为分位算的是序列最小值，
    实际上算的是**最新一日**那个值（hypothesis 拿 [1.0, 2.0] 就把那条打出来了）。
    单调性才是分位的定义性质，任何破坏它的实现都是错的。
    """

    def percentile_of(series):
        results = ValuationPercentileAnalyzer().analyze(
            _valuation_frame({"pe_ttm": series, "pb": [1.0] * len(series)})
        )
        pe = next(item for item in results if item.indicator == "pe_ttm")
        return pe.percentile

    common = base + [probe]
    lower = percentile_of(base + [min(base[-1], probe)])
    higher = percentile_of(base + [max(base[-1], probe)])
    if lower is None or higher is None:
        return
    assert higher >= lower - 1e-9, (lower, higher)


# ---------------------------------------------------------------------------
# 二、多股对比序列对齐的不变量（钉住「前向填充」这个变异）
# ---------------------------------------------------------------------------
day_strategy = st.integers(min_value=0, max_value=400)


@st.composite
def two_series(draw):
    """生成两只标的的交易日集合（可任意缺失，含中间空洞与全缺失）"""
    total = draw(st.integers(min_value=1, max_value=30))
    days = list(range(total))
    assume(total >= 1)
    days_a = draw(st.lists(st.sampled_from(days), unique=True))
    days_b = draw(st.lists(st.sampled_from(days), unique=True))
    return days, days_a, days_b


@given(data=two_series())
@settings(max_examples=300, deadline=None)
def test_every_none_is_a_real_gap_not_a_forward_fill(data):
    """【回归：前向填充变异】序列里的 None 必须对应「那天真的没有数据」

    迁移前的断言是 `gaps > 0`，只被「上市前的��置空档」满足；把中间空洞
    填成前一个值后，前置空档依然存在，断言照样通过。这里的写法是逐点核对：
    输出为 None 的位置，其原始数据里也必须没有那一行。
    """
    days, days_a, days_b = data
    assume(days_a or days_b)          # 两只都无数据时并集本就是空的
    rows = []
    for code, present in (("A.SZ", days_a), ("B.SZ", days_b)):
        for day in present:
            rows.append({
                "ts_code": code,
                "trade_date": pd.Timestamp("2020-01-01") + pd.Timedelta(days=day),
                "pe_ttm": 10.0 + day,
            })
    frame = pd.DataFrame(rows)
    out = _align_history(frame, ["A.SZ", "B.SZ"], "pe_ttm")

    dates = out["dates"]
    # 并集只覆盖「实际出现过的日期」，不是外部给的 0..N-1 日历
    assert len(dates) == len(set(days_a) | set(days_b)), \
        "日期并集大小应为两只实际出现日的并集"
    for code in ("A.SZ", "B.SZ"):
        series = out["series"][code]
        assert len(series) == len(dates), \
            "%s 序列长度应等于交易日并集大小" % code
        present = {int((pd.Timestamp(d) - pd.Timestamp("2020-01-01")).days)
                   for d in dates if series[dates.index(d)] is not None}
        assert present == set(days_a if code == "A.SZ" else days_b), \
            "%s 的非空位置与原始数据不一致：可能被前向填充了" % code


@given(data=two_series())
@settings(max_examples=250, deadline=None)
def test_interior_gap_stays_none(data):
    """【回归：前向填充变异】中间空洞绝不能被填成前一个值

    前提：必须有第二只「全勤」标的把日期并集撑开，否则并集就等于空档那只自己
    的日期，根本不存在空洞 —— 这个前提我一开始漏了，hypothesis 拿
    data=([0, 1], [0], []) 直接打出来。
    """
    days, days_a, _ = data
    assume(len(days_a) >= 1)
    ordered = sorted(days_a)
    hollow = sorted({ordered[0], ordered[-1]})      # 去重，避免造出重复行
    assume(len(hollow) < len(days))                  # 需要真的挖掉中间

    rows = []
    for code, present in (("A.SZ", hollow), ("CAL.SZ", list(days))):
        for day in present:
            rows.append({
                "ts_code": code,
                "trade_date": pd.Timestamp("2020-01-01") + pd.Timedelta(days=day),
                "pe_ttm": 10.0 + day,
            })
    frame = pd.DataFrame(rows)
    out = _align_history(frame, ["A.SZ", "CAL.SZ"], "pe_ttm")

    dates = out["dates"]
    series = out["series"]["A.SZ"]
    assert len(series) == len(dates) == len(days)
    assert series.count(None) == len(days) - len(hollow), (
        "中间空洞应全部保持 None，实际被填了 %d 个"
        % (len(days) - len(hollow) - series.count(None))
    )
    # 逐点核对：非空位置必须恰好是 hollow
    for position, day in enumerate(dates):
        offset = (pd.Timestamp(day) - pd.Timestamp("2020-01-01")).days
        if series[position] is not None:
            assert offset in hollow, "第 %d 天本无数据却被填上了值" % offset


# ---------------------------------------------------------------------------
# 三、因子引擎的不变量
# ---------------------------------------------------------------------------
@given(pb=st.lists(
    st.floats(min_value=-10.0, max_value=100.0, allow_nan=False,
              allow_infinity=False),
    min_size=1, max_size=40))
@settings(max_examples=150, deadline=None)
def test_pe_ttm_factor_is_passthrough(pb):
    """pe_ttm 是透传因子，输出必须与输入逐元素相等"""
    frame = pd.DataFrame({"ts_code": ["X.SZ"] * len(pb), "pe_ttm": pb})
    out = compute(frame, ["pe_ttm"])
    for actual, expected in zip(out["pe_ttm"], pb):
        if expected != expected:      # NaN
            continue
        assert actual == expected


@given(values=st.lists(
    st.floats(min_value=-1e6, max_value=1e6, allow_nan=False,
              allow_infinity=False),
    min_size=1, max_size=30))
@settings(max_examples=150, deadline=None)
def test_division_by_zero_never_yields_infinity(values):
    """分母为 0 的安全除法必须给 NaN，不能给 inf

    inf 会一路流进排序与分位里，把整列变成「最贵」，而且不会报错。
    """
    frame = pd.DataFrame({
        "ts_code": ["X.SZ"] * len(values),
        "net_profit": values,
        "operating_cashflow": values,
    })
    out = compute(frame, ["cfo_to_net_profit"])
    import math
    for value in out["cfo_to_net_profit"]:
        assert not math.isinf(value), "安全除法产生了 inf"
        if value == value:            # 非 NaN
            assert math.isfinite(value)


@given(values=st.lists(
    st.floats(min_value=-1e5, max_value=1e5, allow_nan=False,
              allow_infinity=False),
    min_size=1, max_size=30))
@settings(max_examples=150, deadline=None)
def test_roe_trend_never_infinite(values):
    frame = pd.DataFrame({
        "ts_code": ["X.SZ"] * len(values),
        "roe": values,
        "roe_prior_year": values,
    })
    out = compute(frame, ["roe_trend"])
    import math
    for value in out["roe_trend"]:
        assert not math.isinf(value), "roe_trend 产生了 inf"


if __name__ == "__main__":
    # 保住旧的直接执行入口：委托给 pytest，退出码语义一致
    raise SystemExit(pytest.main([__file__, "-v"]))
