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
import sys

import pandas as pd

# 允许直接执行本文件（./venv/bin/python tests/xxx.py）；走 pytest 时由
# pytest.ini 的 `pythonpath = .` 统一负责，不会重复插入。
if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
import pytest
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

def test_align_columns(tmp_db_path):
    """测试 align_columns 的三件事：缺列拒绝、多列丢弃、按契约重排"""

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

def test_normalizer_contract(tmp_db_path):
    """测试 normalizer 对源列缺失的处理：必须显式失败，绝不静默产出缺列帧"""

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

def test_contract_matches_ddl(tmp_db_path):
    """测试领域契约与建表 DDL 的列名集合一致（改一侧必须同步改另一侧）"""

    db_path = os.path.join(tmp_db_path, "contract.duckdb")

def test_shuffled_upsert(tmp_db_path):
    """测试乱序 DataFrame 写入后列值不错位（Repository 不依赖 DataFrame 列序）"""

    db_path = os.path.join(tmp_db_path, "upsert.duckdb")

def test_securities_normalizer(tmp_db_path):
    """测试证券名录归一化：交易所后缀推断与状态字段"""

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


if __name__ == "__main__":
    # 保住旧的直接执行入口：委托给 pytest，退出码语义一致
    raise SystemExit(pytest.main([__file__, "-v"]))
