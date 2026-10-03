#!/usr/bin/env python3
"""
==============================================================================
StockLab - 研究快照测试 (tests/test_research_snapshot.py)
==============================================================================

【功能用途】
  覆盖 V2 需求 §6（Research Snapshot）与取数层的 Point-in-Time 正确性：
    1. 因子输入帧：股票池按 as-of 推导（含退市/后上市的排除与保留）、
       估值与基本面只读可见数据（未来行不泄漏）、无数据源的列按 NaN 落地、
       动量/波动率/最大回撤由日线正确派生
    2. 筛选执行：summary/detail 判定与「无数据」原因
    3. 快照写入：元数据 + 逐股结果 + 三个版本号（data/factor/config）
    4. 复现：按快照 spec 重新生成与存档逐行一致；篡改存档后必须报差异
    5. 空库与非法输入不崩

【运行方式】
  python tests/test_research_snapshot.py
  （全部使用临时数据库，不触碰 data/stocklab.duckdb，也不依赖联网）
==============================================================================
"""

import datetime
import os
import shutil
import sys
import tempfile

# 将项目根目录加入模块搜索路径，保证直接运行本脚本时能 import stocklab
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from stocklab.domain import (
    BALANCE_SHEET_COLUMNS,
    CASHFLOW_STATEMENT_COLUMNS,
    DAILY_PRICE_COLUMNS,
    DAILY_VALUATION_COLUMNS,
    FINANCIAL_INDICATOR_COLUMNS,
    INCOME_STATEMENT_COLUMNS,
    SECURITY_COLUMNS,
    VALUATION_HISTORY_COLUMNS,
)
from stocklab.factor import FACTOR_VERSION, FactorDataError
from stocklab.persistence.repository import (
    BalanceSheetRepository,
    CashflowStatementRepository,
    DailyPriceRepository,
    DailyValuationRepository,
    FinancialIndicatorRepository,
    IncomeStatementRepository,
    ResearchSnapshotRepository,
    SecurityRepository,
    ValuationHistoryRepository,
)
from stocklab.persistence.storage import Database
from stocklab.research import (
    SnapshotError,
    build_factor_frame,
    config_version,
    create_snapshot,
    data_version,
    list_snapshots,
    load_snapshot,
    rerun_snapshot,
)
from stocklab.screener import ScreenPipeline

# 研究时点：晚于 2024-06-28 收盘，早于一切 2024-06-30 之后的数据
AS_OF = datetime.date(2024, 6, 30)

# 参与 as-of 股票池的四只（另有退市/后上市两只用于验证股票池推导）
UNIVERSE_CODES = ["600519.SH", "000001.SZ", "300750.SZ", "600518.SH"]


def _security_rows():
    """六只证券：四只在 as-of 存续，一只 as-of 前已退市，一只 as-of 后才上市"""
    def row(ts_code, name, industry, list_date, delist_date, status):
        return {
            "ts_code": ts_code,
            "symbol": ts_code.split(".")[0],
            "name": name,
            "exchange": ts_code.split(".")[1],
            "market": "主板",
            "industry": industry,
            "area": "上海",
            "list_date": list_date,
            "delist_date": delist_date,
            "status": status,
            "is_hs": "Y",
        }

    return pd.DataFrame(
        [
            row("600519.SH", "贵州茅台", "食品饮料", datetime.date(2001, 8, 27),
                None, "LISTED"),
            row("000001.SZ", "平安银行", "银行", datetime.date(1991, 4, 3),
                None, "LISTED"),
            row("300750.SZ", "宁德时代", "电气设备", datetime.date(2018, 6, 11),
                None, "LISTED"),
            # as-of 之后才退市 -> 在 as-of 股票池内（幸存者偏差检验）
            row("600518.SH", "ST海鸟", "公用事业", datetime.date(2000, 1, 1),
                datetime.date(2025, 6, 1), "DELISTED"),
            # as-of 之前已退市 -> 不在股票池
            row("600000.SH", "已退市", "银行", datetime.date(1999, 1, 1),
                datetime.date(2024, 1, 1), "DELISTED"),
            # as-of 之后才上市 -> 不在股票池
            row("688999.SH", "后上市", "电子", datetime.date(2024, 12, 1),
                None, "LISTED"),
        ]
    )


def _valuation_rows():
    """历史估值：旧值、可见值与 as-of 之后的未来值各一行，验证只取可见行"""
    def row(ts_code, trade_date, pe_ttm):
        return {
            "ts_code": ts_code,
            "trade_date": trade_date,
            "pe_ttm": pe_ttm,
            "pe_static": pe_ttm,
            "pb": pe_ttm / 10.0,
            "ps": pe_ttm / 5.0,
            "pcf": pe_ttm / 2.0,
        }

    rows = []
    for code, visible in [
        ("600519.SH", 30.0), ("000001.SZ", 5.0),
        ("300750.SZ", 20.0), ("600518.SH", 10.0),
        ("600000.SH", 7.0),
    ]:
        rows.append(row(code, datetime.date(2024, 1, 2), 111.0))   # 更早的旧值
        rows.append(row(code, datetime.date(2024, 6, 28), visible))  # as-of 可见
        rows.append(row(code, datetime.date(2024, 7, 5), 999.0))    # as-of 之后（未来）
    return pd.DataFrame(rows, columns=VALUATION_HISTORY_COLUMNS)


def _daily_valuation_rows():
    """每日估值快照：股息率与总市值（as-of 之后一行用于验证不被读到）"""
    def row(ts_code, trade_date, dv_ttm, total_mv):
        return {
            "ts_code": ts_code,
            "trade_date": trade_date,
            "turnover_rate": 0.5,
            "turnover_rate_f": 0.4,
            "pe": 30.0,
            "pe_ttm": 30.0,
            "pb": 10.0,
            "ps": 13.0,
            "ps_ttm": 13.0,
            "dv_ratio": dv_ttm + 0.2,
            "dv_ttm": dv_ttm,
            "total_share": 1.0e9,
            "float_share": 8.0e8,
            "free_share": 7.0e8,
            "total_mv": total_mv,
            "circ_mv": total_mv * 0.8,
        }

    rows = []
    for code, dv, mv in [
        ("600519.SH", 2.3, 3.0e12), ("000001.SZ", 3.8, 2.0e11),
        ("300750.SZ", 0.4, 9.0e11), ("600518.SH", 1.0, 5.0e10),
    ]:
        rows.append(row(code, datetime.date(2024, 6, 28), dv, mv))
        rows.append(row(code, datetime.date(2024, 7, 5), 99.0, 1.0e15))
    return pd.DataFrame(rows, columns=DAILY_VALUATION_COLUMNS)


def _fundamental_rows():
    """基本面四表：as-of 可见的一期 + as-of 后才公告的一期 + 上年同期对照"""
    def pit(ts_code, report_period, available_date):
        return {
            "ts_code": ts_code,
            "report_period": report_period,
            "announce_date": available_date,
            "available_date": available_date,
            "source": "test",
            "source_record_id": "%s@%s" % (ts_code, report_period),
        }

    indicators = []
    income = []
    balance = []
    cashflow = []

    profiles = {
        "600519.SH": dict(roe=0.30, roe_prior=0.25, profit=8.6e10, eps=66.6,
                          liabilities=3.0e10, equity=2.5e11, cash=1.0e11,
                          debt=0.0, cfo=7.0e10, fcf=6.0e10, fcf_prior=5.0e10,
                          yoy=0.177),
        "000001.SZ": dict(roe=0.10, roe_prior=0.12, profit=4.4e10, eps=1.6,
                          liabilities=3.6e12, equity=3.0e11, cash=5.0e11,
                          debt=2.0e12, cfo=5.0e10, fcf=4.0e10, fcf_prior=4.0e10,
                          yoy=-0.03),
        "300750.SZ": dict(roe=0.15, roe_prior=0.08, profit=5.0e10, eps=1.4,
                          liabilities=6.0e11, equity=3.0e11, cash=8.0e11,
                          debt=1.0e11, cfo=1.0e10, fcf=-2.0e10, fcf_prior=1.0e10,
                          yoy=0.22),
        "600518.SH": dict(roe=0.05, roe_prior=0.06, profit=1.0e9, eps=0.2,
                          liabilities=2.0e10, equity=1.0e10, cash=2.0e9,
                          debt=5.0e9, cfo=5.0e8, fcf=2.0e8, fcf_prior=2.0e8,
                          yoy=-0.5),
    }

    for code, profile in profiles.items():
        # 上年同期（as-of 减一年时点可见）
        indicators.append(dict(
            pit(code, "2023-03-31", "2023-04-30"),
            roe=profile["roe_prior"], roa=0.05, roic=0.08, gross_margin=0.4,
            operating_margin=0.2, net_margin=0.15, revenue_yoy=0.1,
            profit_yoy=0.1, eps_yoy=0.1,
        ))
        # as-of 可见的一期
        indicators.append(dict(
            pit(code, "2024-03-31", "2024-04-30"),
            roe=profile["roe"], roa=0.06, roic=0.09, gross_margin=0.5,
            operating_margin=0.25, net_margin=0.18, revenue_yoy=profile["yoy"],
            profit_yoy=profile["yoy"], eps_yoy=profile["yoy"],
        ))
        # as-of 之后才公告的一期（未来信息，必须读不到）
        indicators.append(dict(
            pit(code, "2024-06-30", "2024-08-31"),
            roe=0.99, roa=0.99, roic=0.99, gross_margin=0.99,
            operating_margin=0.99, net_margin=0.99, revenue_yoy=9.9,
            profit_yoy=9.9, eps_yoy=9.9,
        ))

        income.append(dict(
            pit(code, "2024-03-31", "2024-04-30"),
            revenue=2.0e11, operating_cost=1.0e11, operating_profit=1.1e11,
            total_profit=1.0e11, income_tax=2.0e10, net_profit=profile["profit"],
            net_profit_attributable=profile["profit"], eps=profile["eps"],
        ))
        balance.append(dict(
            pit(code, "2024-03-31", "2024-04-30"),
            total_assets=5.0e11, total_liabilities=profile["liabilities"],
            equity=profile["equity"], cash=profile["cash"],
            interest_bearing_debt=profile["debt"],
        ))
        cashflow.append(dict(
            pit(code, "2023-03-31", "2023-04-30"),
            operating_cashflow=profile["cfo"], investing_cashflow=-1.0e10,
            financing_cashflow=-1.0e10, free_cashflow=profile["fcf_prior"],
        ))
        cashflow.append(dict(
            pit(code, "2024-03-31", "2024-04-30"),
            operating_cashflow=profile["cfo"], investing_cashflow=-1.0e10,
            financing_cashflow=-1.0e10, free_cashflow=profile["fcf"],
        ))

    return (
        pd.DataFrame(indicators, columns=FINANCIAL_INDICATOR_COLUMNS),
        pd.DataFrame(income, columns=INCOME_STATEMENT_COLUMNS),
        pd.DataFrame(balance, columns=BALANCE_SHEET_COLUMNS),
        pd.DataFrame(cashflow, columns=CASHFLOW_STATEMENT_COLUMNS),
    )


def _price_rows():
    """日线：恒定 / 单调上行 / 中途腰斩三种形态，用于验证动量、波动率与回撤"""
    dates = pd.bdate_range(end=AS_OF, periods=400).date
    patterns = {
        "600519.SH": [100.0] * 400,                       # 恒定 -> 收益 0 / 波动 0 / 回撤 0
        "000001.SZ": [100.0 * 1.001 ** i for i in range(400)],  # 单调 -> 收益>0 / 回撤 0
        "300750.SZ": [200.0] * 251 + [100.0] * 149,       # 腰斩 -> 最大回撤 -0.5
        "600518.SH": [50.0] * 400,                        # 恒定
    }
    rows = []
    for code, closes in patterns.items():
        for trade_date, close in zip(dates, closes):
            rows.append(
                {
                    "ts_code": code,
                    "trade_date": trade_date,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "pre_close": close,
                    "change": 0.0,
                    "pct_chg": 0.0,
                    "volume": 1.0e6,
                    "amount": 1.0e9,
                }
            )
    return pd.DataFrame(rows, columns=DAILY_PRICE_COLUMNS)


def _seed(database):
    """写入合成数据（一次到位，全部走 Repository 契约写入）"""
    securities = _security_rows()
    SecurityRepository(database).upsert(securities)
    SecurityRepository(database).upsert_lifecycle(securities)

    ValuationHistoryRepository(database).upsert(_valuation_rows())
    DailyValuationRepository(database).upsert(_daily_valuation_rows())

    indicators, income, balance, cashflow = _fundamental_rows()
    FinancialIndicatorRepository(database).upsert(indicators)
    IncomeStatementRepository(database).upsert(income)
    BalanceSheetRepository(database).upsert(balance)
    CashflowStatementRepository(database).upsert(cashflow)

    DailyPriceRepository(database).upsert(_price_rows())


def _row(frame, ts_code):
    """按 ts_code 取单行（Series）"""
    matched = frame[frame["ts_code"] == ts_code]
    assert len(matched) == 1, "%s 在帧里应恰好一行，实际 %d 行" % (ts_code, len(matched))
    return matched.iloc[0]


def run_frame_test(database):
    """测试因子输入帧：股票池、Point-in-Time、派生列与无数据源列"""
    print("\n" + "=" * 65)
    print("【阶段一：测试因子输入帧（Point-in-Time）】")
    print("=" * 65)

    frame = build_factor_frame(AS_OF, database=database)
    codes = sorted(frame["ts_code"].tolist())
    assert codes == sorted(UNIVERSE_CODES), codes
    assert "600000.SH" not in codes, "as-of 前已退市的标的不得入池"
    assert "688999.SH" not in codes, "as-of 后才上市的标的不得入池"
    print("  -> 股票池 %d 只（含 as-of 后才退市的 600518.SH，排除已退市与后上市）" % len(codes))

    row = _row(frame, "600519.SH")
    assert row["pe_ttm"] == 30.0, "必须取 2024-06-28 的可见值，实际 %s" % row["pe_ttm"]
    assert _row(frame, "300750.SZ")["pe_ttm"] == 20.0
    assert row["dv_ttm"] == 2.3 and row["total_mv"] == 3.0e12
    print("  -> 估值取 as-of 前最近一日（旧值 111 与未来值 999 都没被读到）")

    assert row["roe"] == 0.30, "2024-08-31 才公告的 0.99 不得读到，实际 %s" % row["roe"]
    assert row["roe_prior_year"] == 0.25, row["roe_prior_year"]
    assert abs(row["roe"] - row["roe_prior_year"] - 0.05) < 1e-12
    assert row["revenue_yoy"] == 0.177
    print("  -> 基本面按 available_date 取可见期（roe=0.30，上年同期 0.25）")

    assert row["net_profit"] == 8.6e10 and row["eps"] == 66.6
    assert row["free_cashflow"] == 6.0e10
    assert row["free_cashflow_prior_year"] == 5.0e10
    expected_ev = 3.0e12 + 0.0 - 1.0e11
    assert abs(row["ev"] - expected_ev) < 1.0, row["ev"]
    print("  -> 现金流/资产负债拼装完成，EV = 市值 + 有息负债 - 现金 = %.3e" % expected_ev)

    assert pd.isna(row["ebitda"]), "折旧摊销无数据源 -> NaN（不得伪造）"
    assert pd.isna(row["dps"]) and pd.isna(row["dividend_years_paid"])
    print("  -> 无数据源的列（ebitda / dps / 分红年数）按 NaN 落地")

    constant = _row(frame, "600519.SH")
    assert constant["ret_1m"] == 0.0 and constant["ret_12m"] == 0.0
    assert constant["vol_12m"] == 0.0 and constant["max_dd_12m"] == 0.0
    rising = _row(frame, "000001.SZ")
    assert rising["ret_1m"] > 0 and rising["ret_12m"] > 0
    assert rising["max_dd_12m"] == 0.0 and rising["vol_12m"] > 0
    crash = _row(frame, "300750.SZ")
    assert abs(crash["max_dd_12m"] - (-0.5)) < 1e-12, crash["max_dd_12m"]
    assert pd.isna(crash["ret_1m"]) is False
    print("  -> 动量/波动率/最大回撤由日线派生（恒定 0、单调 >0、腰斩 -0.5）")

    assert row["industry"] == "食品饮料" and row["name"] == "贵州茅台"
    print("  -> 帧带 name / industry（行业中性化与结果展示需要）")

    # 指定股票池：保序 + 只取所选因子的输入列（不触发日线查询）
    subset = build_factor_frame(
        AS_OF,
        ts_codes=["300750.SZ", "600519.SH"],
        factor_names=["pe_ttm"],
        database=database,
    )
    assert subset["ts_code"].tolist() == ["300750.SZ", "600519.SH"]
    assert "pe_ttm" in subset.columns and "ret_1m" not in subset.columns
    print("  -> 指定股票池保序，且按因子裁剪输入列（未取的因子列不出现）")

    try:
        build_factor_frame(AS_OF, factor_names=["not_a_factor"], database=database)
        raise AssertionError("未登记因子必须抛 FactorDataError")
    except FactorDataError as error:
        message = str(error)
    assert "not_a_factor" in message
    print("  -> 未登记因子拒绝: %s" % message)


def run_screen_test(database):
    """测试在真实帧上的筛选执行"""
    print("\n" + "=" * 65)
    print("【阶段二：测试筛选执行】")
    print("=" * 65)

    frame = build_factor_frame(AS_OF, database=database)
    pipeline = ScreenPipeline.from_spec(
        {
            "rules": [
                {"factor": "pe_ttm", "operator": "lt", "value": 25},
                {"factor": "roe", "operator": "gt", "value": 0.10},
            ]
        }
    )
    result = pipeline.run(frame)
    # pe: 30 / 5 / 20 / 10；roe: 0.30 / 0.10(不满足 gt) / 0.15 / 0.05
    assert result.codes == ["300750.SZ"], result.codes
    assert result.counts == {"total": 4, "passed": 1}
    print("  -> 判定通过 [300750.SZ]（000001.SZ 的 roe=0.10 不满足 gt 0.10）")

    summary = result.summary.set_index("ts_code")
    assert summary.loc["600519.SH", "failed_rules"] == [
        "pe_ttm = 30 未满足 小于 25"
    ]
    assert len(summary.loc["000001.SZ", "failed_rules"]) == 1
    assert len(summary.loc["600518.SH", "failed_rules"]) == 1
    print("  -> 逐只失败原因就位: %s" % summary.loc["600519.SH", "failed_rules"][0])

    detail = result.detail
    assert len(detail) == 8
    assert set(detail["factor"]) == {"pe_ttm", "roe"}
    assert detail[detail["ts_code"] == "600519.SH"]["threshold"].tolist() == ["25", "0.1"]
    print("  -> detail 覆盖 4 只 × 2 条规则，含 factor_value / threshold / passed / reason")

    # 分红因子当前没有数据源 -> 逐只报「无数据」，绝不静默通过
    missing = ScreenPipeline.from_spec(
        {"rules": [{"factor": "payout_ratio", "operator": "gt", "value": 0.1}]}
    ).run(frame)
    assert missing.counts == {"total": 4, "passed": 0}
    reasons = missing.detail["reason"].tolist()
    assert reasons and all("无数据" in reason for reason in reasons), reasons
    print("  -> 无数据源因子（payout_ratio）全部报「无数据」，通过数为 0")
    return pipeline, result, frame


def run_snapshot_test(database, pipeline, result):
    """测试快照写入与元数据"""
    print("\n" + "=" * 65)
    print("【阶段三：测试研究快照写入】")
    print("=" * 65)

    snapshot_id = create_snapshot(
        AS_OF, "A股全市场(测试)", pipeline, result.summary, database
    )
    assert snapshot_id.startswith("RS")
    print("  -> 写入 snapshot_id = %s" % snapshot_id)

    loaded = load_snapshot(snapshot_id, database)
    assert loaded["as_of_date"] == AS_OF
    assert loaded["universe"] == "A股全市场(测试)"
    assert loaded["universe_size"] == 4
    assert loaded["result_count"] == 4 and loaded["passed_count"] == 1
    assert loaded["factor_version"] == FACTOR_VERSION
    assert loaded["config_version"] == config_version(pipeline.spec())
    assert loaded["condition"] == pipeline.condition
    print("  -> 元数据完整: as_of / 股票池 / 通过数 / 三个版本号 / 条件文本")

    # 数据版本号必须反映**实际取到的数据截止日**（as-of 之前的最近交易日）
    assert loaded["data_version"].startswith("schema_v"), loaded["data_version"]
    assert "2024-06-28" in loaded["data_version"], loaded["data_version"]
    assert "2024-07-05" not in loaded["data_version"]
    assert data_version(database, AS_OF) == loaded["data_version"]
    print("  -> data_version = %s（含真实数据截止日，非 as-of 本身）" % loaded["data_version"])

    spec = loaded["spec"]
    assert spec["universe"]["label"] == "A股全市场(测试)"
    assert sorted(spec["universe"]["ts_codes"]) == sorted(UNIVERSE_CODES)
    assert spec["screen"]["rules"][0] == {
        "factor": "pe_ttm", "operator": "lt", "value": 25,
    }
    print("  -> spec_json 存下条件与展开后的股票池（可原样重跑）")

    results = loaded["results"].set_index("ts_code")
    assert bool(results.loc["300750.SZ", "passed"]) is True
    assert bool(results.loc["600519.SH", "passed"]) is False
    values = results.loc["300750.SZ", "factor_values"]
    assert values["pe_ttm"] == 20.0 and values["roe"] == 0.15
    assert results.loc["600519.SH", "failed_rules"] == ["pe_ttm = 30 未满足 小于 25"]
    print("  -> 逐股结果含 passed / failed_rules / factor_values")

    snapshots = list_snapshots(database)
    assert len(snapshots) == 1
    assert snapshots.iloc[0]["snapshot_id"] == snapshot_id
    print("  -> list_snapshots 返回 1 条")

    other_id = create_snapshot(AS_OF, "测试股票池", pipeline, result.summary, database)
    assert other_id != snapshot_id, "快照只增不改，重复执行必须是新编号"
    assert list_snapshots(database).shape[0] == 2
    print("  -> 再次执行生成新快照 %s（历史结论不被覆盖）" % other_id)
    return snapshot_id


def run_rerun_test(database, snapshot_id, original_counts):
    """测试按 spec 重新生成并与存档比对"""
    print("\n" + "=" * 65)
    print("【阶段四：测试研究结果复现】")
    print("=" * 65)

    outcome = rerun_snapshot(snapshot_id, database)
    assert outcome["identical"] is True, outcome["differences"]
    assert outcome["result"].counts == original_counts
    print("  -> 重跑与存档逐行一致（%s）" % outcome["result"].counts)

    # 篡改存档：复现必须报出差异，而不是悄悄通过
    conn = database.get_connection()
    conn.execute(
        "UPDATE research.snapshot_results SET factor_values = ? "
        "WHERE snapshot_id = ? AND ts_code = ?",
        ['{"pe_ttm": 1.0, "roe": 0.15}', snapshot_id, "300750.SZ"],
    )
    tampered = rerun_snapshot(snapshot_id, database)
    assert tampered["identical"] is False
    assert any("pe_ttm" in difference for difference in tampered["differences"])
    assert any("300750.SZ" in difference for difference in tampered["differences"])
    print("  -> 篡改存档后复现失败: %s" % tampered["differences"][0])

    try:
        load_snapshot("RS19700101000000-abcdef", database)
        raise AssertionError("不存在的快照必须抛 SnapshotError")
    except SnapshotError as error:
        assert "不存在" in str(error)
    print("  -> 不存在的快照报 SnapshotError")

    try:
        rerun_snapshot("RS19700101000000-abcdef", database)
        raise AssertionError("不存在的快照复现必须抛 SnapshotError")
    except SnapshotError as error:
        assert "不存在" in str(error)
    print("  -> 复现不存在的快照同样拒绝")


def run_empty_database_test():
    """测试空库：帧为空、筛选为空、快照可写可复现"""
    print("\n" + "=" * 65)
    print("【阶段五：测试空库与非法输入】")
    print("=" * 65)

    temp_dir = tempfile.mkdtemp(prefix="sl_research_empty_")
    try:
        database = Database(os.path.join(temp_dir, "empty.duckdb"))
        frame = build_factor_frame(AS_OF, database=database)
        assert len(frame) == 0
        pipeline = ScreenPipeline.from_spec(
            {"rules": [{"factor": "pe_ttm", "operator": "lt", "value": 25}]}
        )
        result = pipeline.run(frame)
        assert result.counts == {"total": 0, "passed": 0}

        snapshot_id = create_snapshot(
            AS_OF, "空股票池", pipeline, result.summary, database
        )
        loaded = load_snapshot(snapshot_id, database)
        assert loaded["universe_size"] == 0
        assert len(loaded["results"]) == 0
        assert rerun_snapshot(snapshot_id, database)["identical"] is True
        print("  -> 空库：帧 0 行、筛选 0 行、快照可写可复现")

        try:
            create_snapshot(
                AS_OF, "坏快照", pipeline,
                pd.DataFrame({"passed": [True]}), database,
            )
            raise AssertionError("summary 缺 ts_code 必须拒绝")
        except SnapshotError as error:
            message = str(error)
        assert "ts_code" in message
        print("  -> summary 缺 ts_code 拒绝: %s" % message)

        # 迁移 004 必须已生效（表存在 + 版本已记录）
        conn = database.get_connection()
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'research' ORDER BY table_name"
        ).fetchall()
        assert [table for table, in tables] == ["snapshot_results", "snapshots"]
        versions = conn.execute(
            "SELECT version FROM sys.schema_version ORDER BY version"
        ).fetchall()
        assert [version for version, in versions] == [1, 2, 3, 4, 5]
        print("  -> 迁移 004 已应用（research 两表 + 版本 1/2/3/4）")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    """运行全部研究快照测试"""
    temp_dir = tempfile.mkdtemp(prefix="sl_research_")
    try:
        # 注意：库文件名会成为 DuckDB 的 catalog 名，不能与 research schema 同名
        database = Database(os.path.join(temp_dir, "sl_research.duckdb"))
        _seed(database)
        run_frame_test(database)
        pipeline, result, frame = run_screen_test(database)
        snapshot_id = run_snapshot_test(database, pipeline, result)
        run_rerun_test(database, snapshot_id, result.counts)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    run_empty_database_test()

    print("\n" + "=" * 65)
    print("研究快照测试全部通过！")
    print("=" * 65)


if __name__ == "__main__":
    main()
