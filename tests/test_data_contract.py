#!/usr/bin/env python3
"""
==============================================================================
StockLab - 数据契约测试 (tests/test_data_contract.py)
==============================================================================

【功能用途】
  验证「数据源 → 归一化 → 领域契约 → 显式列名入库」这条链路：
    1. align_columns：缺列拒绝 / 多列丢弃 / 按契约重排
    2. normalizer：源列缺失必须抛 DataContractError（不允许静默写 NULL 污染库）
    3. 契约列与建表 DDL 的列名集合必须一致（改一侧不改另一侧即失败）
    4. 乱序 DataFrame 写入后列值不错位（Repository 不依赖 DataFrame 列序）
    5. 缺契约列的 DataFrame 写入被拒绝（返回 0 行，库里不留脏数据）

【运行方式】
  python tests/test_data_contract.py
  （使用临时数据库，不触碰 data/stocklab.duckdb；运行前请停掉 serve_web）
==============================================================================
"""

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
    DataContractError,
    FINANCIAL_INDICATOR_COLUMNS,
    INDUSTRY_VALUATION_COLUMNS,
    INDEX_MEMBERSHIP_COLUMNS,
    INCOME_STATEMENT_COLUMNS,
    SECURITY_COLUMNS,
    SECURITY_EVENT_COLUMNS,
    VALUATION_HISTORY_COLUMNS,
    align_columns,
    require_columns,
)
from stocklab.normalization.akshare import (
    normalize_daily_prices,
    normalize_securities,
)
from stocklab.normalization.eastmoney import normalize_income_statements
from stocklab.persistence import DailyPriceRepository, Database
from stocklab.persistence.repository import BaseRepository
from stocklab.persistence.storage import initialize_database


def _sample_raw_daily_prices():
    """构造东财日 K 源结构（中文列名、可任意改列序）"""
    return pd.DataFrame(
        {
            "日期": ["2026-09-01", "2026-09-02"],
            "股票代码": ["600519", "600519"],
            "开盘": [100.0, 101.0],
            "收盘": [102.0, 103.0],
            "最高": [103.0, 104.0],
            "最低": [99.0, 100.0],
            "昨收": [99.5, 102.0],
            "涨跌幅": [2.51, 0.98],
        }
    )


def run_align_columns_test():
    """测试 align_columns 的三件事：缺列拒绝、多列丢弃、按契约重排"""
    print("\n" + "=" * 65)
    print("【阶段一：测试 align_columns 契约对齐】")
    print("=" * 65)

    # 缺列必须抛 DataContractError
    missing = pd.DataFrame({"ts_code": ["600519.SH"], "name": ["贵州茅台"]})
    try:
        align_columns(missing, SECURITY_COLUMNS, "reference.securities")
        raise AssertionError("缺契约列时必须抛 DataContractError")
    except DataContractError as error:
        print("  -> 缺列拒绝: %s" % error)

    # 契约外列必须被丢弃，且列序与契约一致（先取一帧完整契约数据再打乱）
    source = normalize_securities(pd.DataFrame({"code": ["600519"], "name": ["贵州茅台"]}))
    shuffled = source[list(reversed(source.columns))]
    shuffled["extra_column"] = "x"
    aligned = align_columns(shuffled, SECURITY_COLUMNS, "reference.securities")
    assert list(aligned.columns) == list(SECURITY_COLUMNS), list(aligned.columns)
    assert "extra_column" not in aligned.columns
    assert aligned["name"].iloc[0] == "贵州茅台"
    print("  -> 多列丢弃 + 列序重排正确（%d 列）" % len(aligned.columns))

    # require_columns：owner 名出现在异常里，便于定位是哪个数据源改版了
    try:
        require_columns(pd.DataFrame(columns=["a"]), ("a", "b"), "akshare.demo")
        raise AssertionError("缺列时必须抛 DataContractError")
    except DataContractError as error:
        assert "akshare.demo" in str(error) and "b" in str(error)
    print("  -> require_columns 异常信息包含来源与缺失列名")


def run_normalizer_contract_test():
    """测试 normalizer 对源列缺失的处理：必须显式失败，绝不静默产出缺列帧"""
    print("\n" + "=" * 65)
    print("【阶段二：测试归一化层的源列契约】")
    print("=" * 65)

    raw = _sample_raw_daily_prices()

    # 源列齐全 → 输出契约列序
    frame = normalize_daily_prices(raw, "600519.SH")
    assert list(frame.columns) == list(DAILY_PRICE_COLUMNS)
    assert str(frame["trade_date"].iloc[0]) == "2026-09-01"
    print("  -> 日 K 归一化输出 %d 列，列序与契约一致" % len(frame.columns))

    # 数据源把「收盘」改名 → 必须抛 DataContractError，而不是写一列 None
    broken = raw.drop(columns=["收盘"])
    try:
        normalize_daily_prices(broken, "600519.SH")
        raise AssertionError("源列缺失时必须抛 DataContractError")
    except DataContractError as error:
        print("  -> 源列缺失拒绝: %s" % error)

    # 利润表缺公告日期列 → 拒绝（否则 Point-in-Time 无从谈起）
    income_raw = pd.DataFrame(
        {
            "SECURITY_CODE": ["600519"],
            "REPORT_DATE": ["2024-12-31"],
            "OPERATE_INCOME": [1.0],
            "OPERATE_COST": [0.1],
            "OPERATE_PROFIT": [0.5],
            "TOTAL_PROFIT": [0.5],
            "INCOME_TAX": [0.1],
            "NETPROFIT": [0.4],
            "PARENT_NETPROFIT": [0.4],
            "BASIC_EPS": [1.0],
        }
    )
    try:
        normalize_income_statements(income_raw, "600519.SH")
        raise AssertionError("利润表缺 NOTICE_DATE 必须抛 DataContractError")
    except DataContractError as error:
        print("  -> 财报缺公告日期列拒绝: %s" % error)

    # 公告日期列存在但值缺失 → 该行被丢弃，禁止入库「只有报告期」的数据
    income_raw["NOTICE_DATE"] = [None]
    frame = normalize_income_statements(income_raw, "600519.SH")
    assert frame.empty, "缺公告日期的行必须被丢弃"
    print("  -> 缺公告日期的行被丢弃（不入库）")


def run_contract_matches_ddl_test():
    """测试领域契约与建表 DDL 的列名集合一致（改一侧必须同步改另一侧）"""
    print("\n" + "=" * 65)
    print("【阶段三：测试契约列与建表 DDL 一致】")
    print("=" * 65)

    temp_dir = tempfile.mkdtemp(prefix="sl_contract_")
    db_path = os.path.join(temp_dir, "contract.duckdb")
    try:
        initialize_database(db_path)
        contracts = {
            "reference.securities": SECURITY_COLUMNS,
            "reference.security_events": SECURITY_EVENT_COLUMNS,
            "market.daily_prices": DAILY_PRICE_COLUMNS,
            "market.daily_valuations": DAILY_VALUATION_COLUMNS,
            "market.valuation_history": VALUATION_HISTORY_COLUMNS,
            "market.industry_valuations": INDUSTRY_VALUATION_COLUMNS,
            "reference.index_memberships": INDEX_MEMBERSHIP_COLUMNS,
            "fundamental.income_statements": INCOME_STATEMENT_COLUMNS,
            "fundamental.balance_sheets": BALANCE_SHEET_COLUMNS,
            "fundamental.cashflow_statements": CASHFLOW_STATEMENT_COLUMNS,
            "fundamental.financial_indicators": FINANCIAL_INDICATOR_COLUMNS,
        }
        with Database(db_path) as database:
            conn = database.get_connection()
            for table, columns in contracts.items():
                schema, name = table.split(".")
                rows = conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = ? AND table_name = ?",
                    [schema, name],
                ).fetchall()
                actual = set(row[0] for row in rows)
                expected = set(columns)
                assert actual == expected, (
                    "%s 列不一致：DDL 多出 %s，契约多出 %s"
                    % (table, sorted(actual - expected), sorted(expected - actual))
                )
        print("  -> %d 张表的契约列与 DDL 列名集合完全一致" % len(contracts))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_shuffled_upsert_test():
    """测试乱序 DataFrame 写入后列值不错位（Repository 不依赖 DataFrame 列序）"""
    print("\n" + "=" * 65)
    print("【阶段四：测试显式列名写入（乱序 / 缺列 / 多列）】")
    print("=" * 65)

    temp_dir = tempfile.mkdtemp(prefix="sl_upsert_")
    db_path = os.path.join(temp_dir, "upsert.duckdb")
    try:
        initialize_database(db_path)
        with Database(db_path) as database:
            repository = DailyPriceRepository(database)

            frame = normalize_daily_prices(_sample_raw_daily_prices(), "600519.SH")

            # 1) 列序整体反转后写入，读回的值必须仍落在正确的列上
            reversed_frame = frame[list(reversed(frame.columns))]
            count = repository.upsert(reversed_frame)
            assert count == 2, count

            back = repository.find_by_code("600519.SH")
            row = back.iloc[0]
            assert row["ts_code"] == "600519.SH"
            assert row["open"] == 100.0 and row["close"] == 102.0
            assert row["high"] == 103.0 and row["low"] == 99.0
            assert row["pre_close"] == 99.5 and row["pct_chg"] == 2.51
            print("  -> 反转列序写入 %d 行，列值无错位" % count)

            # 2) 缺契约列 → 拒绝写入（返回 0），库里原有数据不受影响
            missing_column = frame.drop(columns=["close"])
            rejected = repository.upsert(missing_column)
            assert rejected == 0, rejected
            after = repository.find_by_code("600519.SH")
            assert len(after) == 2 and after["close"].notna().all()
            print("  -> 缺列写入被拒绝（返回 0 行，原数据完好）")

            # 3) 多出契约外的列 → 丢弃后正常写入
            extra = frame.copy()
            extra["not_a_column"] = 1
            count = repository.upsert(extra)
            assert count == 2, count
            print("  -> 契约外列被丢弃后正常写入")

            # 4) 冲突列不属于契约 → 拒绝（防止把任意列当主键）。
            #    DailyPriceRepository 固定了冲突列，这里直接用 BaseRepository 验证校验规则
            class _PermissiveRepository(BaseRepository):
                _TABLE_NAME = "market.daily_prices"
                _COLUMNS = DAILY_PRICE_COLUMNS

            rejected = _PermissiveRepository(database).upsert(
                frame, conflict_columns=["not_a_column"], update_columns=["open"]
            )
            assert rejected == 0, rejected
            print("  -> 非契约列被当作冲突列时写入被拒绝")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_securities_normalizer_test():
    """测试证券名录归一化：交易所后缀推断与状态字段"""
    print("\n" + "=" * 65)
    print("【阶段五：测试证券名录归一化】")
    print("=" * 65)

    raw = pd.DataFrame(
        {
            "code": ["600519", "000001", "430047", "900901"],
            "name": ["贵州茅台", "平安银行", "粤证券5", "ST永久B"],
        }
    )
    frame = normalize_securities(raw)
    assert list(frame.columns) == list(SECURITY_COLUMNS)
    assert list(frame["ts_code"]) == [
        "600519.SH", "000001.SZ", "430047.BJ", "900901.SH",
    ]
    assert list(frame["exchange"]) == ["SH", "SZ", "BJ", "SH"]
    assert set(frame["status"]) == {"LISTED"}
    print("  -> 代码后缀推断与 status 字段正确")

    try:
        normalize_securities(raw.drop(columns=["name"]))
        raise AssertionError("缺 name 列必须抛 DataContractError")
    except DataContractError as error:
        print("  -> 缺列拒绝: %s" % error)


def main():
    """运行全部数据契约测试"""
    run_align_columns_test()
    run_normalizer_contract_test()
    run_contract_matches_ddl_test()
    run_shuffled_upsert_test()
    run_securities_normalizer_test()
    print("\n" + "=" * 65)
    print("数据契约测试全部通过！")
    print("=" * 65)


if __name__ == "__main__":
    main()
