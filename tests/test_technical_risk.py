"""
技术面与风险模块测试 (tests/test_technical_risk.py)

覆盖：
  1. stocklab.analytics.technical_risk —— 纯计算，全部用合成序列（不联网、不碰库）
  2. /api/technical —— 参数校验分支 + 正常路径（monkeypatch 掉 store 取数）
  3. /technical 页面路由可达

【为什么单列一个文件】
  技术面/风险是本项目新增的一等分析能力，其口径（年化 252、回撤含修复、
  分母为 0 一律留空而非填 0）都在这里钉死，避免以后被悄悄改宽。

运行：
  ./venv/bin/python -m pytest tests/test_technical_risk.py -v
"""

import datetime
import os
import sys

import numpy as np
import pandas as pd
import pytest

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.analytics import technical_risk


# ---------------------------------------------------------------------------
# 合成序列
# ---------------------------------------------------------------------------
def _business_days(count):
    return pd.date_range("2024-01-01", periods=count, freq="B")


@pytest.fixture
def rising_prices():
    """单调上涨：没有回撤，年化收益为正"""
    index = _business_days(120)
    return pd.Series(np.linspace(100.0, 160.0, 120), index=index)


@pytest.fixture
def v_shape_prices():
    """先跌 40% 再回到前高：已知最大回撤约 -40% 且有修复日"""
    index = _business_days(101)
    values = np.concatenate([np.linspace(100.0, 60.0, 50), np.linspace(60.0, 100.0, 51)])
    return pd.Series(values, index=index)


# ---------------------------------------------------------------------------
# 一、基础变换
# ---------------------------------------------------------------------------
def test_to_returns_length_and_first_value(rising_prices):
    returns = technical_risk.to_returns(rising_prices)
    assert len(returns) == len(rising_prices) - 1
    assert np.isfinite(returns.iloc[0])


def test_to_returns_empty_input():
    assert technical_risk.to_returns(pd.Series(dtype="float64")).empty


def test_annualized_volatility_uses_252(rising_prices):
    returns = technical_risk.to_returns(rising_prices)
    expected = float(returns.std(ddof=1)) * np.sqrt(252)
    assert technical_risk.annualized_volatility(returns) == pytest.approx(expected)


def test_annualized_volatility_too_short():
    assert technical_risk.annualized_volatility(pd.Series([0.01])) is None


# ---------------------------------------------------------------------------
# 二、最大回撤
# ---------------------------------------------------------------------------
def test_max_drawdown_monotonic_is_zero(rising_prices):
    result = technical_risk.max_drawdown(rising_prices)
    assert result["max_drawdown"] == 0.0
    assert result["peak_date"] is None, "全程不回落时没有可报的峰谷"


def test_max_drawdown_v_shape_has_recovery(v_shape_prices):
    result = technical_risk.max_drawdown(v_shape_prices)
    assert result["max_drawdown"] == pytest.approx(-0.40, abs=1e-9)
    assert result["recovery_date"] is not None, "回到前高后应记录修复日"
    assert result["drawdown_days"] == 49
    assert result["recovery_days"] is not None


def test_max_drawdown_empty_returns_none_fields():
    result = technical_risk.max_drawdown(pd.Series(dtype="float64"))
    assert result["max_drawdown"] is None
    assert result["trough_date"] is None


# ---------------------------------------------------------------------------
# 三、风险指标
# ---------------------------------------------------------------------------
def test_risk_metrics_zero_volatility_leaves_ratio_none():
    """常数序列没有波动：夏普无定义，必须留空而不是报 0 或 inf"""
    flat = pd.Series([100.0] * 30, index=_business_days(30))
    result = technical_risk.risk_metrics(flat)
    assert result["annual_volatility"] == 0.0
    assert result["sharpe"] is None


def test_risk_metrics_beta_close_to_two():
    """基金日收益 = 2 × 基准日收益时，Beta 应为 2、Alpha 约 0"""
    index = _business_days(200)
    rng = np.random.default_rng(11)
    bench_returns = rng.normal(0.0004, 0.01, len(index))
    fund_returns = 2.0 * bench_returns
    fund = pd.Series(100.0 * np.cumprod(1.0 + fund_returns), index=index)
    bench = pd.Series(100.0 * np.cumprod(1.0 + bench_returns), index=index)
    result = technical_risk.risk_metrics(fund, benchmark_closes=bench)
    assert result["beta"] == pytest.approx(2.0, abs=1e-6)
    assert result["alpha"] == pytest.approx(0.0, abs=1e-6)
    assert result["r_squared"] == pytest.approx(1.0, abs=1e-6)


def test_risk_metrics_constant_benchmark_leaves_beta_none():
    """基准零波动：Beta 无定义，不能拿 0 或 1 冒充"""
    index = _business_days(60)
    fund = pd.Series(np.linspace(100, 120, 60), index=index)
    bench = pd.Series([100.0] * 60, index=index)
    result = technical_risk.risk_metrics(fund, benchmark_closes=bench)
    assert result["beta"] is None


def test_risk_metrics_empty_input():
    result = technical_risk.risk_metrics(pd.Series(dtype="float64"))
    assert result["annual_return"] is None
    assert result["sample_days"] == 0


# ---------------------------------------------------------------------------
# 四、技术指标
# ---------------------------------------------------------------------------
def test_moving_averages_columns_and_warmup(rising_prices):
    frame = technical_risk.moving_averages(rising_prices, windows=(5, 20))
    assert list(frame.columns) == ["ma5", "ma20"]
    assert frame["ma20"].iloc[:19].isna().all(), "窗口未满前应留 NaN"
    assert np.isfinite(frame["ma20"].iloc[-1])


def test_rsi_within_bounds_and_high_when_rising(rising_prices):
    rsi = technical_risk.relative_strength_index(rising_prices, window=14)
    valid = rsi.dropna()
    assert not valid.empty
    assert valid.between(0.0, 100.0).all()
    assert valid.iloc[-1] > 90.0, "持续上涨的 RSI 应接近 100"


def test_macd_columns(rising_prices):
    frame = technical_risk.macd(rising_prices)
    assert list(frame.columns) == ["dif", "dea", "hist"]
    assert np.isfinite(frame["dif"].iloc[-1])


def test_macd_short_series_is_nan():
    frame = technical_risk.macd(pd.Series([1.0, 2.0, 3.0]))
    assert frame["dif"].isna().all()


def test_momentum_reports_each_window(rising_prices):
    result = technical_risk.momentum(rising_prices, windows=(5, 20, 500))
    assert result["ret_5d"] > 0
    assert result["ret_20d"] > result["ret_5d"], "时间越长累计涨幅越大"
    assert result["ret_500d"] is None, "样本不足的窗口必须留空"


def test_technical_snapshot_keys(rising_prices):
    snapshot = technical_risk.technical_snapshot(rising_prices)
    for key in ("as_of", "close", "ma5", "ma20", "ma60", "rsi14",
                "macd_dif", "macd_dea", "macd_hist", "ret_20d"):
        assert key in snapshot


def test_technical_snapshot_empty():
    assert technical_risk.technical_snapshot(pd.Series(dtype="float64")) == {
        "as_of": None, "close": None,
    }


# ---------------------------------------------------------------------------
# 五、Web 接口
# ---------------------------------------------------------------------------
@pytest.fixture
def price_frame():
    index = _business_days(160)
    rng = np.random.default_rng(3)
    closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.012, len(index)))
    return pd.DataFrame({
        "ts_code": "600519.SH",
        "trade_date": index.date,
        "close": closes,
    })


@pytest.fixture
def stubbed_store(monkeypatch, price_frame):
    """把取数与证券表替换为合成数据，避免联网与真实库依赖"""
    from app.web import store

    monkeypatch.setattr(store, "call_facade", lambda *args, **kwargs: price_frame.copy())
    monkeypatch.setattr(store, "load_securities", lambda: pd.DataFrame({
        "ts_code": ["600519.SH"], "name": ["贵州茅台"],
    }))
    monkeypatch.setattr(store, "current_priority", lambda: "local_first")
    return store


def test_technical_page_reachable(client):
    response = client.get("/technical")
    assert response.status_code == 200
    assert "技术面与风险" in response.get_data(as_text=True)


def test_api_technical_requires_code(client):
    response = client.get("/api/technical")
    assert response.status_code == 400
    assert "code" in response.get_json()["error"]


def test_api_technical_rejects_reversed_range(client):
    response = client.get("/api/technical?code=600519.SH&start_date=2024-06-01&end_date=2024-01-01")
    assert response.status_code == 400
    assert response.get_json()["error"]


def test_api_technical_rejects_overlong_range(client):
    response = client.get("/api/technical?code=600519.SH&start_date=2000-01-01&end_date=2024-01-01")
    assert response.status_code == 400


def test_api_technical_ok(client, stubbed_store):
    response = client.get("/api/technical?code=600519.SH")
    assert response.status_code == 200
    data = response.get_json()
    assert data["code"] == "600519.SH"
    assert data["name"] == "贵州茅台"
    assert data["risk"]["sample_days"] > 2
    assert len(data["price"]["dates"]) == len(data["price"]["close"])
    assert data["technical"]["rsi14"] is not None
    # 序列里绝不能出现 NaN / Infinity（StrictJSONProvider 已兜底，但这里显式盯住）
    for value in data["price"]["close"]:
        assert value is None or np.isfinite(value)


def test_api_technical_empty_prices_returns_message(client, monkeypatch):
    from app.web import store

    monkeypatch.setattr(store, "call_facade", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(store, "load_securities", lambda: pd.DataFrame({
        "ts_code": ["600519.SH"], "name": ["贵州茅台"],
    }))
    monkeypatch.setattr(store, "current_priority", lambda: "local_first")

    response = client.get("/api/technical?code=600519.SH")
    assert response.status_code == 200, "无数据不应是 500"
    assert response.get_json()["message"]
    assert response.get_json()["risk"] is None


def test_api_technical_resolves_chinese_name(client, stubbed_store):
    response = client.get("/api/technical?code=贵州茅台")
    assert response.status_code == 200
    assert response.get_json()["code"] == "600519.SH"


def test_api_technical_unknown_name_is_rejected(client, stubbed_store):
    response = client.get("/api/technical?code=不存在股份")
    assert response.status_code == 400
    assert "未找到" in response.get_json()["error"]


def test_api_technical_ambiguous_name_is_rejected(client, monkeypatch):
    from app.web import store

    monkeypatch.setattr(store, "load_securities", lambda: pd.DataFrame({
        "ts_code": ["600519.SH", "000858.SZ"], "name": ["贵州茅台", "贵州燃气"],
    }))
    response = client.get("/api/technical?code=贵州")
    assert response.status_code == 400
    assert "匹配到" in response.get_json()["error"]


def test_api_technical_unavailable_securities_is_rejected(client, monkeypatch):
    from app.web import store

    monkeypatch.setattr(store, "load_securities", lambda: pd.DataFrame())
    response = client.get("/api/technical?code=贵州茅台")
    assert response.status_code == 400
    assert "证券表" in response.get_json()["error"]


def test_api_technical_short_series_returns_message(client, monkeypatch, price_frame):
    from app.web import store

    short_frame = price_frame.head(1)
    monkeypatch.setattr(store, "call_facade", lambda *args, **kwargs: short_frame.copy())
    monkeypatch.setattr(store, "load_securities", lambda: pd.DataFrame({
        "ts_code": ["600519.SH"], "name": ["贵州茅台"],
    }))
    monkeypatch.setattr(store, "current_priority", lambda: "local_first")

    response = client.get("/api/technical?code=600519.SH")
    assert response.status_code == 200
    assert response.get_json()["technical"] is None
    assert "不足以" in response.get_json()["message"]


def test_api_technical_facade_error_returns_500(client, monkeypatch):
    from app.web import store

    def boom(*args, **kwargs):
        raise RuntimeError("数据库损坏")

    monkeypatch.setattr(store, "call_facade", boom)
    monkeypatch.setattr(store, "load_securities", lambda: pd.DataFrame({
        "ts_code": ["600519.SH"], "name": ["贵州茅台"],
    }))
    response = client.get("/api/technical?code=600519.SH")
    assert response.status_code == 500
    assert response.get_json()["error"]


def test_api_technical_with_benchmark_computes_beta(client, monkeypatch, price_frame):
    from app.web import store

    rng = np.random.default_rng(7)
    benchmark_frame = price_frame.copy()
    benchmark_frame["close"] = 100.0 * np.cumprod(
        1.0 + rng.normal(0.0005, 0.012, len(price_frame)))
    frames = {"600519.SH": price_frame.copy(), "000300.SH": benchmark_frame}

    monkeypatch.setattr(store, "call_facade", lambda method, code, *a, **k: frames[code])
    monkeypatch.setattr(store, "load_securities", lambda: pd.DataFrame({
        "ts_code": ["600519.SH"], "name": ["贵州茅台"],
    }))
    monkeypatch.setattr(store, "current_priority", lambda: "local_first")

    response = client.get("/api/technical?code=600519.SH&benchmark=000300.SH")
    assert response.status_code == 200
    data = response.get_json()
    assert data["benchmark"] == "000300.SH"
    assert data["risk"]["beta"] is not None
