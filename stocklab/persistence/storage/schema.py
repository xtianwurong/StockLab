#!/usr/bin/env python3
"""
==============================================================================
StockLab - DuckDB 数据库 Schema 定义 (stocklab.persistence.storage.schema)
==============================================================================

【模块职责】
   定义本地 A 股数据仓库的全部 DDL（数据定义语言），包括：
     1. Schema 创建（reference / market / sys）
     2. 表结构定义（securities / daily_prices / daily_valuations /
        valuation_history / sync_tasks）
     3. 初始化入口 initialize_database()

【设计原则】
   - 幂等设计：重复执行 CREATE IF NOT EXISTS 不会报错
   - 数据域分离：reference（证券身份）、market（行情估值）、sys（同步状态）
   - 主键约束：所有时间序列表使用 (ts_code, trade_date) 复合主键
   - NULL 语义保留：估值字段允许 NULL，不强制填充默认值
"""

import logging
import os

import duckdb

_logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DB_PATH",
    "initialize_database",
]


# 默认数据库文件路径（相对于项目根目录）
DEFAULT_DB_PATH = os.path.join("data", "stocklab.duckdb")


# ============================================================================
# DDL 语句定义
# ============================================================================

# --- Schema 创建 ---

_CREATE_SCHEMA_REFERENCE = """
CREATE SCHEMA IF NOT EXISTS reference;
"""

_CREATE_SCHEMA_MARKET = """
CREATE SCHEMA IF NOT EXISTS market;
"""

_CREATE_SCHEMA_SYS = """
CREATE SCHEMA IF NOT EXISTS sys;
"""

# --- reference.securities ---

_CREATE_TABLE_SECURITIES = """
CREATE TABLE IF NOT EXISTS reference.securities (
    ts_code      VARCHAR PRIMARY KEY,
    symbol       VARCHAR,
    name         VARCHAR,
    exchange     VARCHAR,
    market       VARCHAR,
    industry     VARCHAR,
    area         VARCHAR,
    list_date    DATE,
    delist_date  DATE,
    list_status  VARCHAR,
    is_hs        VARCHAR
);
"""

# --- market.daily_prices ---

_CREATE_TABLE_DAILY_PRICES = """
CREATE TABLE IF NOT EXISTS market.daily_prices (
    ts_code      VARCHAR,
    trade_date   DATE,
    open         DOUBLE,
    high         DOUBLE,
    low          DOUBLE,
    close        DOUBLE,
    pre_close    DOUBLE,
    change       DOUBLE,
    pct_chg      DOUBLE,
    volume       DOUBLE,
    amount       DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
"""

# --- market.daily_valuations ---

_CREATE_TABLE_DAILY_VALUATIONS = """
CREATE TABLE IF NOT EXISTS market.daily_valuations (
    ts_code          VARCHAR,
    trade_date       DATE,
    turnover_rate    DOUBLE,
    turnover_rate_f  DOUBLE,
    pe               DOUBLE,
    pe_ttm           DOUBLE,
    pb               DOUBLE,
    ps               DOUBLE,
    ps_ttm           DOUBLE,
    dv_ratio         DOUBLE,
    dv_ttm           DOUBLE,
    total_share      DOUBLE,
    float_share      DOUBLE,
    free_share       DOUBLE,
    total_mv         DOUBLE,
    circ_mv          DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
"""

# --- market.valuation_history ---

_CREATE_TABLE_VALUATION_HISTORY = """
CREATE TABLE IF NOT EXISTS market.valuation_history (
    ts_code          VARCHAR,
    trade_date       DATE,
    pe_ttm           DOUBLE,
    pe_static        DOUBLE,
    pb               DOUBLE,
    ps               DOUBLE,
    pcf              DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
"""

# --- sys.sync_tasks ---

_CREATE_TABLE_SYNC_TASKS = """
CREATE TABLE IF NOT EXISTS sys.sync_tasks (
    task_id       BIGINT,
    data_type     VARCHAR,
    start_date    DATE,
    end_date      DATE,
    status        VARCHAR,
    row_count     BIGINT,
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    error_message VARCHAR
);
"""

# 按执行顺序排列的全部 DDL
_ALL_DDL_STATEMENTS = [
    _CREATE_SCHEMA_REFERENCE,
    _CREATE_SCHEMA_MARKET,
    _CREATE_SCHEMA_SYS,
    _CREATE_TABLE_SECURITIES,
    _CREATE_TABLE_DAILY_PRICES,
    _CREATE_TABLE_DAILY_VALUATIONS,
    _CREATE_TABLE_VALUATION_HISTORY,
    _CREATE_TABLE_SYNC_TASKS,
]


# ============================================================================
# 初始化入口
# ============================================================================

def initialize_database(db_path=None):
    """
    初始化 DuckDB 数据库：创建目录、建立 Schema 与全部表结构

    幂等设计：重复执行不会报错，已存在的 Schema 与表将被跳过。

    Args:
        db_path (str, optional): 数据库文件路径，默认使用 data/stocklab.duckdb

    Returns:
        str: 实际使用的数据库文件路径
    """
    actual_path = db_path if db_path else DEFAULT_DB_PATH

    # 自动创建父目录
    parent_dir = os.path.dirname(actual_path)
    if parent_dir and not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)
        _logger.info("已创建数据目录: %s", parent_dir)

    conn = duckdb.connect(actual_path)
    try:
        for ddl in _ALL_DDL_STATEMENTS:
            conn.execute(ddl)
        _logger.info("数据库初始化完成: %s", actual_path)
    finally:
        conn.close()

    return actual_path
