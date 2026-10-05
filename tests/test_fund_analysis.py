#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基金调仓分析测试 (tests/test_fund_analysis.py)
==============================================================================

【功能用途】
  覆盖基金调仓分析的核心逻辑：
  1. FundStyleDecomposer 分解器单元测试
  2. FundAnalysisFacade 门面集成测试
  3. 接口层契约测试
  4. 资金流佐证逻辑

【运行方式】
  ./venv/bin/python -m pytest tests/test_fund_analysis.py -v
"""

import os
import sys

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import date, timedelta

import numpy as np
import pandas as pd

from stocklab.analytics.fund_style import (
    FundStyleDecomposer,
    decompose_fund_style,
    FundAllocationSignal,
    SignalStrength,
    ConfidenceLevel,
)


# =============================================================================
# 测试 Fixtures - 使用相对今天的日期，确保数据有效
# =============================================================================

def _make_sector_index(sector_code, start_date, days, base=1000.0, volatility=0.015, trend=0.0005):
    """构造行业指数序列"""
    dates = [start_date + timedelta(days=i) for i in range(days)]
    np.random.seed(hash(sector_code) % 1000)
    returns = np.random.normal(trend, volatility, days)
    closes = base * (1 + returns).cumprod()
    return pd.DataFrame({
        "trade_date": dates,
        "close": closes,
    })


def _make_capital_flow(sector_codes, start_date, days):
    """构造资金流数据"""
    dates = [start_date + timedelta(days=i) for i in range(days)]
    np.random.seed(123)
    rows = []
    for code in sector_codes:
        for i, d in enumerate(dates):
            np.random.seed(hash((code, d)) % 10000)
            net_inflow = np.random.normal(0, 1e8)
            rows.append({
                "sector_code": code,
                "sector_name": f"行业_{code}",
                "sector_type": "sw_level1",
                "trade_date": d,
                "net_inflow": net_inflow,
                "inflow": max(0, net_inflow) + np.random.uniform(0, 1e8),
                "outflow": max(0, -net_inflow) + np.random.uniform(0, 1e8),
                "net_inflow_rate": np.random.uniform(-5, 5),
                "main_net_inflow": net_inflow * 0.6,
                "retail_net_inflow": net_inflow * 0.4,
                "source": "test",
                "fetched_at": pd.Timestamp.now(),
            })
    return pd.DataFrame(rows)


# =============================================================================
# 测试 Fixtures - 使用相对今天的日期，确保数据有效
# =============================================================================

@pytest.fixture
def setup_data():
    """构造测试数据 - 使用相对今天的日期"""
    # 使用过去 150 个自然日作为基准
    end_date = pd.Timestamp(date.today() - timedelta(days=10))  # 留一点缓冲
    start_date = end_date - timedelta(days=150)

    # 3 个一级行业指数
    sector_codes = ["801010.SI", "801020.SI", "801030.SI"]
    sector_dfs = {}
    for i, code in enumerate(sector_codes):
        # 不同的基础趋势
        trends = [0.0003, 0.0008, -0.0002]
        sector_dfs[code] = _make_sector_index(code, start_date, 150, base=1000*(i+1), trend=trends[i])

    # 基金净值：使用加权合成 + 噪声
    fund_code = "000001.OF"
    weights = {"801010.SI": 0.4, "801020.SI": 0.3, "801030.SI": 0.2}

    # 先生成所有行业的收益率
    sector_returns = {}
    common_dates = None
    for code, df in sector_dfs.items():
        df = df.set_index("trade_date").sort_index()
        rets = df["close"].pct_change().fillna(0)
        if common_dates is None:
            common_dates = set(df.index)
        else:
            common_dates &= set(df.index)
    common_dates = sorted(common_dates)[-80:]  # 取最后 80 个交易日

    # 合成基金收益率 = 加权行业收益率 + 噪声
    sector_returns = {}
    for code in sector_codes:
        df = sector_dfs[code].set_index("trade_date").loc[common_dates]["close"]
        sector_returns[code] = df.pct_change().fillna(0).values

    np.random.seed(42)
    synthetic_returns = sum(
        sector_returns[code] * weights[code]
        for code in sector_codes
    )
    synthetic_returns += np.random.normal(0, 0.001, len(common_dates))

    # 重构净值
    nav_values = 1.0 * (1 + synthetic_returns).cumprod()

    # 只取最后 60 个交易日用于测试（保证有足够历史）
    test_dates = common_dates[-60:]

    fund_nav_aligned = pd.DataFrame({
        "fund_code": "000001.OF",
        "nav_date": test_dates,
        "nav": (1.0 * (1 + synthetic_returns[-60:]).cumprod()).tolist(),
    })

    # 行业指数 DataFrame（只包含需要的日期）
    sector_indices = {}
    for code in sector_codes:
        df = sector_dfs[code].set_index("trade_date").loc[test_dates][["close"]].reset_index()
        sector_indices[code] = df

    # 行业映射
    mapping = pd.DataFrame({
        "index_code": ["801010.SI", "801020.SI", "801030.SI"],
        "index_name": ["农林牧渔", "采掘", "化工"],
        "level": [1, 1, 1],
        "parent_code": ["", "", ""],
        "sw_first_code": ["", "", ""],
        "description": ["", "", ""],
    })

    # 资金流数据 - 使用最近 30 个自然日
    capital_flow_dates = [pd.Timestamp(date.today() - timedelta(days=i)) for i in range(30, 0, -1)]
    capital_flow = _make_capital_flow(sector_codes, pd.Timestamp(date.today() - timedelta(days=30)), 30)

    return {
        "fund_nav": pd.DataFrame({
            "fund_code": "000001.OF",
            "nav_date": test_dates,
            "nav": (1.0 * (1 + np.array([
                sum(sector_returns[code][-60+i] * weights[code] for code in sector_codes)
                for i in range(60)
            ]) + np.random.normal(0, 0.001, 60)).cumprod()).tolist(),
        }),
        "sector_indices": sector_indices,
        "mapping": pd.DataFrame({
            "index_code": ["801010.SI", "801020.SI", "801030.SI"],
            "index_name": ["农林牧渔", "采掘", "化工"],
            "level": [1, 1, 1],
            "parent_code": ["", "", ""],
            "sw_first_code": ["", "", ""],
            "description": ["", "", ""],
        }),
        "capital_flow": _make_capital_flow(["801010.SI", "801020.SI", "801030.SI"], pd.Timestamp(date.today() - timedelta(days=30)), 30),
    }


def _make_sector_index(sector_code, start_date, days, base=1000.0, volatility=0.015, trend=0.0005):
    """构造行业指数序列"""
    dates = [start_date + timedelta(days=i) for i in range(days)]
    np.random.seed(hash(sector_code) % 1000)
    returns = np.random.normal(trend, volatility, days)
    closes = base * (1 + returns).cumprod()
    return pd.DataFrame({
        "trade_date": dates,
        "close": closes,
    })


def _make_capital_flow(sector_codes, start_date, days):
    """构造资金流数据"""
    dates = [start_date + timedelta(days=i) for i in range(days)]
    np.random.seed(123)
    rows = []
    for code in sector_codes:
        for i, d in enumerate(dates):
            np.random.seed(hash((code, d)) % 10000)
            net_inflow = np.random.normal(0, 1e8)
            rows.append({
                "sector_code": code,
                "sector_name": f"行业_{code}",
                "sector_type": "sw_level1",
                "trade_date": d,
                "net_inflow": net_inflow,
                "inflow": max(0, net_inflow) + np.random.uniform(0, 1e8),
                "outflow": max(0, -net_inflow) + np.random.uniform(0, 1e8),
                "net_inflow_rate": np.random.uniform(-5, 5),
                "main_net_inflow": net_inflow * 0.6,
                "retail_net_inflow": net_inflow * 0.4,
                "source": "test",
                "fetched_at": pd.Timestamp.now(),
            })
    return pd.DataFrame(rows)


# =============================================================================
# 1. FundStyleDecomposer 单元测试
# =============================================================================

class TestFundStyleDecomposer:
    """风格分解器核心逻辑测试"""

    def test_decompose_single(self, setup_data):
        """单期分解测试"""
        decomposer = FundStyleDecomposer(
            fund_nav_df=setup_data["fund_nav"],
            sector_index_dfs=setup_data["sector_indices"],
            capital_flow_df=setup_data["capital_flow"],
            industry_mapping_df=setup_data["mapping"],
        )

        # 使用最近的交易日作为分析基准日
        analysis_date = setup_data["fund_nav"]["nav_date"].iloc[-1]

        result = decomposer.decompose_single(
            fund_code="000001.OF",
            end_date=analysis_date,
            window_days=60,
        )

        assert result.fund_code == "000001.OF"
        assert result.trade_date == analysis_date
        assert result.window_days == 60
        assert result.n_sectors == 3
        # 合成数据拟合度应该很高
        assert result.r_squared > 0.8
        assert sum(result.exposures.values()) <= 1.0 + 1e-6
        assert result.cash_exposure >= 0

    def test_rolling_decompose(self, setup_data):
        """滚动窗口分解测试"""
        decomposer = FundStyleDecomposer(
            fund_nav_df=setup_data["fund_nav"],
            sector_index_dfs=setup_data["sector_indices"],
            capital_flow_df=setup_data["capital_flow"],
            industry_mapping_df=setup_data["mapping"],
        )

        analysis_date = setup_data["fund_nav"]["nav_date"].iloc[-1]
        history = decomposer.rolling_decompose(
            fund_code="000001.OF",
            end_date=analysis_date,
            window_days=60,
            step_days=10,
        )

        # 至少应该有 3 个分解点
        assert len(history) >= 3
        for r in history:
            assert r.fund_code == "000001.OF"
            assert r.n_sectors == 3
            assert sum(r.exposures.values()) <= 1.0 + 1e-6

    def test_generate_signals(self, setup_data):
        """信号生成测试"""
        decomposer = FundStyleDecomposer(
            fund_nav_df=setup_data["fund_nav"],
            sector_index_dfs=setup_data["sector_indices"],
            capital_flow_df=setup_data["capital_flow"],
            industry_mapping_df=setup_data["mapping"],
        )

        analysis_date = setup_data["fund_nav"]["nav_date"].iloc[-1]
        signals = decomposer.generate_signals(
            fund_code="000001.OF",
            analysis_date=analysis_date,
            window_days=60,
            level=1,
        )

        assert len(signals) == 3
        for s in signals:
            assert isinstance(s, FundAllocationSignal)
            assert s.fund_code == "000001.OF"
            assert s.level == 1
            assert s.signal in [
                SignalStrength.STRONG_INCREASE,
                SignalStrength.MODERATE_INCREASE,
                SignalStrength.SLIGHT_INCREASE,
                SignalStrength.NEUTRAL,
                SignalStrength.SLIGHT_DECREASE,
                SignalStrength.MODERATE_DECREASE,
                SignalStrength.STRONG_DECREASE,
            ]
            assert s.confidence in [ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM, ConfidenceLevel.LOW]
            assert -1 <= s.capital_flow_corr <= 1

    def test_analyze_full(self, setup_data):
        """完整分析入口测试"""
        analysis_date = setup_data["fund_nav"]["nav_date"].iloc[-1]

        result = decompose_fund_style(
            fund_nav_df=setup_data["fund_nav"],
            sector_index_dfs=setup_data["sector_indices"],
            capital_flow_df=setup_data["capital_flow"],
            industry_mapping_df=setup_data["mapping"],
            fund_code="000001.OF",
            analysis_date=analysis_date,
            window_days=60,
            level=1,
        )

        assert "current_exposures" in result
        assert "exposure_history" in result
        assert "signals" in result
        assert "summary" in result
        assert result["fund_code"] == "000001.OF"
        assert result["level"] == 1
        assert result["window_days"] == 60
        assert len(result["signals"]) == 3
        assert "top_increase" in result["summary"]
        assert "top_decrease" in result["summary"]

    def test_r_squared_warning(self):
        """R² 过低时的处理"""
        # 构造噪声极大的数据，导致 R² 很低
        today = pd.Timestamp(date.today())
        fund_nav = pd.DataFrame({
            "fund_code": "999999.OF",
            "nav_date": [today - pd.Timedelta(days=i) for i in range(59, -1, -1)],
            "nav": np.random.uniform(0.5, 2.0, 60).cumprod(),
        })

        sector_dfs = {
            "801010.SI": pd.DataFrame({
                "trade_date": [today - pd.Timedelta(days=i) for i in range(59, -1, -1)],
                "close": np.random.uniform(500, 2000, 60).cumsum(),
            })
        }

        mapping = pd.DataFrame({
            "index_code": ["801010.SI"],
            "index_name": ["测试行业"],
            "level": [1],
            "parent_code": [""],
            "sw_first_code": [""],
            "description": [""],
        })

        result = decompose_fund_style(
            fund_nav_df=fund_nav,
            sector_index_dfs=sector_dfs,
            industry_mapping_df=mapping,
            fund_code="999999.OF",
            analysis_date=today,
            window_days=60,
            level=1,
        )

        # R² 应该很低（随机数据无相关性）
        assert result["r_squared"] < 0.5


class TestFundAllocationSignal:
    """调仓信号数据类测试"""

    def test_signal_enum_values(self):
        """信号枚举值检查"""
        assert SignalStrength.STRONG_INCREASE == "大幅加仓"
        assert SignalStrength.NEUTRAL == "持仓稳定"
        assert SignalStrength.STRONG_DECREASE == "大幅减仓"

    def test_confidence_enum_values(self):
        """置信度枚举检查"""
        assert ConfidenceLevel.HIGH == "高"
        assert ConfidenceLevel.MEDIUM == "中"
        assert ConfidenceLevel.LOW == "低"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])