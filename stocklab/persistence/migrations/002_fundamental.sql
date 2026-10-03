-- ==============================================================================
-- StockLab 迁移 002：基本面域（Point-in-Time 三大报表 + 财务指标）
-- ==============================================================================
-- 说明：
--   1. 四张表共享 FUNDAMENTAL_PIT_COLUMNS 六列（见 stocklab/domain/fundamental.py），
--      其中 announce_date / available_date 为 NOT NULL —— 缺公告日期的行不得入库；
--   2. 主键含 source，允许未来接入第二个数据源做交叉验证而不互相覆盖；
--   3. 禁止只保存 report_period：报告期早于 as-of 但公告日晚于 as-of 的数据
--      会构成未来信息泄漏，必须能靠 announce_date / available_date 挡住。
-- ==============================================================================

CREATE SCHEMA IF NOT EXISTS fundamental;

-- --- fundamental.income_statements（利润表） ---

CREATE TABLE IF NOT EXISTS fundamental.income_statements (
    ts_code                 VARCHAR NOT NULL,
    report_period           DATE NOT NULL,
    announce_date           DATE NOT NULL,
    available_date          DATE NOT NULL,
    source                  VARCHAR NOT NULL,
    source_record_id        VARCHAR,
    revenue                 DOUBLE,
    operating_cost          DOUBLE,
    operating_profit        DOUBLE,
    total_profit            DOUBLE,
    income_tax              DOUBLE,
    net_profit              DOUBLE,
    net_profit_attributable DOUBLE,
    eps                     DOUBLE,
    PRIMARY KEY (ts_code, report_period, source)
);

-- --- fundamental.balance_sheets（资产负债表） ---

CREATE TABLE IF NOT EXISTS fundamental.balance_sheets (
    ts_code                VARCHAR NOT NULL,
    report_period          DATE NOT NULL,
    announce_date          DATE NOT NULL,
    available_date         DATE NOT NULL,
    source                 VARCHAR NOT NULL,
    source_record_id       VARCHAR,
    total_assets           DOUBLE,
    total_liabilities      DOUBLE,
    equity                 DOUBLE,
    cash                   DOUBLE,
    interest_bearing_debt  DOUBLE,
    PRIMARY KEY (ts_code, report_period, source)
);

-- --- fundamental.cashflow_statements（现金流量表） ---

CREATE TABLE IF NOT EXISTS fundamental.cashflow_statements (
    ts_code              VARCHAR NOT NULL,
    report_period        DATE NOT NULL,
    announce_date        DATE NOT NULL,
    available_date       DATE NOT NULL,
    source               VARCHAR NOT NULL,
    source_record_id     VARCHAR,
    operating_cashflow   DOUBLE,
    investing_cashflow   DOUBLE,
    financing_cashflow   DOUBLE,
    free_cashflow        DOUBLE,
    PRIMARY KEY (ts_code, report_period, source)
);

-- --- fundamental.financial_indicators（质量 / 成长指标，由报表派生） ---

CREATE TABLE IF NOT EXISTS fundamental.financial_indicators (
    ts_code          VARCHAR NOT NULL,
    report_period    DATE NOT NULL,
    announce_date    DATE NOT NULL,
    available_date   DATE NOT NULL,
    source           VARCHAR NOT NULL,
    source_record_id VARCHAR,
    roe              DOUBLE,
    roa              DOUBLE,
    roic             DOUBLE,
    gross_margin     DOUBLE,
    operating_margin DOUBLE,
    net_margin       DOUBLE,
    revenue_yoy      DOUBLE,
    profit_yoy       DOUBLE,
    eps_yoy          DOUBLE,
    PRIMARY KEY (ts_code, report_period, source)
);
