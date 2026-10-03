-- ==============================================================================
-- StockLab 迁移 001：基线 Schema（= 迁移机制上线前 schema.py 的既有结构）
-- ==============================================================================
-- 说明：
--   1. 本文件是「基线迁移」，代表数据库 v1 结构。已存在的老库首次运行迁移时
--      本文件全部以 IF NOT EXISTS 幂等跳过，随后由后续迁移升级到最新结构；
--   2. 新库同样先执行本文件，保证新老数据库经过完全相同的迁移路径；
--   3. 自本机制上线起，数据库结构变更一律新增 NNN_*.sql，禁止再修改 schema.py。
-- ==============================================================================

-- --- Schema ---

CREATE SCHEMA IF NOT EXISTS reference;
CREATE SCHEMA IF NOT EXISTS market;
CREATE SCHEMA IF NOT EXISTS sys;

-- --- reference.securities（v1：生命周期状态列为 list_status） ---

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

-- --- market.daily_prices ---

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

-- --- market.daily_valuations ---

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

-- --- market.valuation_history ---

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

-- --- reference.index_memberships ---

CREATE TABLE IF NOT EXISTS reference.index_memberships (
    ts_code          VARCHAR,
    index_code       VARCHAR,
    index_name       VARCHAR,
    effective_date   DATE,
    PRIMARY KEY (ts_code, index_code, effective_date)
);

-- --- market.industry_valuations ---

CREATE TABLE IF NOT EXISTS market.industry_valuations (
    industry_code         VARCHAR,
    stat_date             DATE,
    classification        VARCHAR,
    industry_level        INTEGER,
    industry_name         VARCHAR,
    company_count         BIGINT,
    priced_company_count  BIGINT,
    total_market_value    DOUBLE,
    net_profit            DOUBLE,
    pe_weighted           DOUBLE,
    pe_median             DOUBLE,
    pe_arithmetic         DOUBLE,
    PRIMARY KEY (industry_code, stat_date)
);

-- --- sys.sync_tasks ---

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
