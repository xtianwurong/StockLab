-- ==============================================================================
-- StockLab 迁移 008：申万行业指数与基金分析数据
-- ==============================================================================
-- 设计前提：
--   1. 申万一级行业 31 个，二级行业 100+ 个，指数代码格式如 801010.SI (申万)
--   2. 指数日线行情与个股日线结构一致，复用 market.daily_prices 表结构
--   3. 基金净值单独建表：fund.nav_history (fund_code, nav_date, nav, acc_nav)
--   4. 基金基本信息：fund.fund_info (fund_code, name, manager, type, etc.)
--   5. 资金流数据：capital.flow_daily (sector_code, date, net_inflow, etc.)
-- ==============================================================================

CREATE SCHEMA IF NOT EXISTS sw;
CREATE SCHEMA IF NOT EXISTS fund;
CREATE SCHEMA IF NOT EXISTS capital;

-- ---------------------------------------------------------------------------
-- 申万行业指数日线行情（复用 market.daily_prices 的列结构，但放在 sw schema 下）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sw.index_daily (
    symbol      VARCHAR NOT NULL,   -- 如 801010.SI
    trade_date  DATE    NOT NULL,
    open        DOUBLE  NOT NULL,
    high        DOUBLE  NOT NULL,
    low         DOUBLE  NOT NULL,
    close       DOUBLE  NOT NULL,
    volume      BIGINT  NOT NULL,
    amount      DOUBLE  NOT NULL,
    source      VARCHAR NOT NULL,
    fetched_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_sw_index_daily_symbol_date ON sw.index_daily (symbol, trade_date);

-- ---------------------------------------------------------------------------
-- 申万行业分类映射表（指数代码 -> 行业名称/层级/父级）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sw.industry_mapping (
    index_code      VARCHAR PRIMARY KEY,   -- 如 801010.SI
    index_name      VARCHAR NOT NULL,      -- 如 "农林牧渔"
    level           SMALLINT NOT NULL,     -- 1=一级, 2=二级
    parent_code     VARCHAR,               -- 二级行业对应的一级行业代码
    sw_first_code   VARCHAR,               -- 归属的一级行业代码
    description     VARCHAR
);

-- ---------------------------------------------------------------------------
-- 基金基本信息
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fund.fund_info (
    fund_code       VARCHAR PRIMARY KEY,   -- 如 000001.OF
    fund_name       VARCHAR NOT NULL,
    fund_short_name VARCHAR,
    fund_type       VARCHAR,               -- 股票型/混合型/债券型/指数型/QDII/FOF
    manager_name    VARCHAR,
    company_name    VARCHAR,
    establish_date  DATE,
    benchmark       VARCHAR,               -- 业绩比较基准
    status          VARCHAR DEFAULT 'active', -- active/terminated
    source          VARCHAR NOT NULL,
    fetched_at      TIMESTAMP NOT NULL
);

-- ---------------------------------------------------------------------------
-- 基金净值历史
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fund.nav_history (
    fund_code       VARCHAR NOT NULL,
    nav_date        DATE NOT NULL,
    nav             DOUBLE NOT NULL,       -- 单位净值
    acc_nav         DOUBLE,                -- 累计净值
    change_pct      DOUBLE,                -- 日涨跌幅 %
    source          VARCHAR NOT NULL,
    fetched_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (fund_code, nav_date)
);

CREATE INDEX IF NOT EXISTS idx_fund_nav_date ON fund.nav_history (fund_code, nav_date);

-- ---------------------------------------------------------------------------
-- 板块资金流数据（申万一/二级行业 + 概念板块）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS capital.flow_daily (
    sector_code     VARCHAR NOT NULL,      -- 板块代码，如 SW1_801010 / BK0501
    sector_name     VARCHAR NOT NULL,
    sector_type     VARCHAR NOT NULL,      -- sw_level1 / sw_level2 / concept / industry
    trade_date      DATE NOT NULL,
    net_inflow      DOUBLE,                -- 净流入额（元）
    inflow          DOUBLE,                -- 流入额
    outflow         DOUBLE,                -- 流出额
    net_inflow_rate DOUBLE,                -- 净流入率 %
    main_net_inflow DOUBLE,                -- 主力净流入
    retail_net_inflow DOUBLE,              -- 散户净流入
    source          VARCHAR NOT NULL,
    fetched_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (sector_code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_capital_flow_date ON capital.flow_daily (sector_code, trade_date);

-- ---------------------------------------------------------------------------
-- 基金调仓分析结果缓存（可选，用于加速页面加载）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fund.allocation_analysis (
    fund_code           VARCHAR NOT NULL,
    analysis_date       DATE NOT NULL,     -- 分析基准日
    window_days         SMALLINT NOT NULL, -- 回看窗口
    level               SMALLINT NOT NULL, -- 1=一级行业, 2=二级行业
    sector_code         VARCHAR NOT NULL,
    sector_name         VARCHAR NOT NULL,
    exposure            DOUBLE,            -- 当前暴露度 (0-1)
    exposure_change     DOUBLE,            -- 环比变化
    exposure_change_5d  DOUBLE,            -- 5日变化
    exposure_change_20d DOUBLE,            -- 20日变化
    r_squared           DOUBLE,            -- 回归拟合度
    capital_flow_corr   DOUBLE,            -- 与资金流相关性
    confidence          VARCHAR,           -- high/medium/low
    source              VARCHAR NOT NULL,
    computed_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (fund_code, analysis_date, window_days, level, sector_code)
);

CREATE INDEX IF NOT EXISTS idx_fund_alloc_fund_date ON fund.allocation_analysis (fund_code, analysis_date);