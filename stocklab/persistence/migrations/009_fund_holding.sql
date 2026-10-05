-- ==============================================================================
-- StockLab 迁移 009：基金持仓与行业暴露（Phase 1 核心）
-- ==============================================================================
-- 设计前提：
--   1. 股票行业映射是静态维表，月更；来源 AKShare 申万/中信/国证分类
--   2. 基金持仓来自季报/半年报/年报前十大重仓股；来源东方财富/基金公司官网
--   3. 基金行业暴露 = 持仓权重 × 股票行业映射；按报告期存储，支持线性插值到每日
--   4. 归因表预留，供 Phase 2 Brinson 归因使用
-- ==============================================================================

CREATE SCHEMA IF NOT EXISTS fund;

-- ---------------------------------------------------------------------------
-- 1. 股票行业映射表（静态维表，月更）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fund.stock_industry_mapping (
    ts_code            VARCHAR PRIMARY KEY,      -- 标准代码 000001.SZ
    sw_l1_code         VARCHAR,                  -- 申万一级代码 801010
    sw_l1_name         VARCHAR,                  -- 申万一级名称 农林牧渔
    sw_l2_code         VARCHAR,                  -- 申万二级代码 801011
    sw_l2_name         VARCHAR,                  -- 申万二级名称 农业种植
    sw_l3_code         VARCHAR,                  -- 申万三级代码 801012
    sw_l3_name         VARCHAR,                  -- 申万三级名称 蔬菜
    citics_l1_code     VARCHAR,                  -- 中信一级代码
    citics_l1_name     VARCHAR,                  -- 中信一级名称
    citics_l2_code     VARCHAR,                  -- 中信二级代码
    citics_l2_name     VARCHAR,                  -- 中信二级名称
    wind_l1_code       VARCHAR,                  -- Wind 一级代码
    wind_l1_name       VARCHAR,                  -- Wind 一级名称
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_stock_industry_sw_l1 ON fund.stock_industry_mapping (sw_l1_code);
CREATE INDEX IF NOT EXISTS idx_stock_industry_sw_l2 ON fund.stock_industry_mapping (sw_l2_code);
CREATE INDEX IF NOT EXISTS idx_stock_industry_citics_l1 ON fund.stock_industry_mapping (citics_l1_code);

-- ---------------------------------------------------------------------------
-- 2. 基金持仓表（季报/半年报/年报前十大重仓股）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fund.fund_holding (
    fund_code          VARCHAR NOT NULL,         -- 基金代码 000001.OF
    report_date        DATE NOT NULL,            -- 报告期 2026-06-30
    report_type        VARCHAR NOT NULL,         -- quarterly / semi_annual / annual
    stock_code         VARCHAR NOT NULL,         -- 股票代码 000001.SZ
    stock_name         VARCHAR NOT NULL,         -- 股票名称 平安银行
    weight             DOUBLE NOT NULL,          -- 占净值比例 0.0523 (5.23%)
    market_value       DOUBLE,                   -- 持仓市值(元)
    rank               SMALLINT NOT NULL,        -- 排名 1~10
    source             VARCHAR NOT NULL,         -- eastmoney / pdf / manual
    fetched_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (fund_code, report_date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_fund_holding_fund_date ON fund.fund_holding (fund_code, report_date);
CREATE INDEX IF NOT EXISTS idx_fund_holding_stock ON fund.fund_holding (stock_code);

-- ---------------------------------------------------------------------------
-- 3. 基金行业暴露表（按报告期存储，支持线性插值到每日）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fund.fund_industry_exposure (
    fund_code          VARCHAR NOT NULL,
    report_date        DATE NOT NULL,
    level              SMALLINT NOT NULL,        -- 1=一级 2=二级 3=三级
    sector_code        VARCHAR NOT NULL,         -- 行业代码 801010 / 801011
    sector_name        VARCHAR NOT NULL,
    weight             DOUBLE NOT NULL,          -- 暴露度 0.1523
    source             VARCHAR NOT NULL,         -- holding / rbsa / blended
    PRIMARY KEY (fund_code, report_date, level, sector_code)
);

CREATE INDEX IF NOT EXISTS idx_fund_ind_exp_fund_date ON fund.fund_industry_exposure (fund_code, report_date);
CREATE INDEX IF NOT EXISTS idx_fund_ind_exp_sector ON fund.fund_industry_exposure (sector_code);

-- ---------------------------------------------------------------------------
-- 4. 基金行业暴露日线表（线性插值到每日，供前端时序图直接用）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fund.fund_industry_exposure_daily (
    fund_code          VARCHAR NOT NULL,
    trade_date         DATE NOT NULL,
    level              SMALLINT NOT NULL,
    sector_code        VARCHAR NOT NULL,
    sector_name        VARCHAR NOT NULL,
    weight             DOUBLE NOT NULL,
    source             VARCHAR NOT NULL,         -- holding_interpolated / rbsa / blended
    PRIMARY KEY (fund_code, trade_date, level, sector_code)
);

CREATE INDEX IF NOT EXISTS idx_fund_ind_daily_fund_date ON fund.fund_industry_exposure_daily (fund_code, trade_date);
CREATE INDEX IF NOT EXISTS idx_fund_ind_daily_sector ON fund.fund_industry_exposure_daily (sector_code);

-- ---------------------------------------------------------------------------
-- 5. Brinson 归因结果表（Phase 2 预留）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fund.fund_attribution (
    fund_code             VARCHAR NOT NULL,
    trade_date            DATE NOT NULL,
    benchmark_code        VARCHAR NOT NULL,       -- 基准指数代码 000300.SH
    total_return          DOUBLE NOT NULL,        -- 组合收益率
    benchmark_return      DOUBLE NOT NULL,        -- 基准收益率
    excess_return         DOUBLE NOT NULL,        -- 超额收益
    allocation_effect     DOUBLE NOT NULL,        -- 资产配置效应
    selection_effect      DOUBLE NOT NULL,        -- 个股选择效应
    interaction_effect    DOUBLE NOT NULL,        -- 交互效应
    PRIMARY KEY (fund_code, trade_date, benchmark_code)
);

CREATE INDEX IF NOT EXISTS idx_fund_attr_fund_date ON fund.fund_attribution (fund_code, trade_date);

-- ---------------------------------------------------------------------------
-- 6. 基金经理任职记录（Phase 3 预留）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fund.fund_manager_tenure (
    fund_code         VARCHAR NOT NULL,
    manager_name      VARCHAR NOT NULL,
    start_date        DATE NOT NULL,
    end_date          DATE,                       -- NULL 表示在任
    is_current        BOOLEAN NOT NULL DEFAULT TRUE,
    aum               DOUBLE,                     -- 管理规模(元)
    PRIMARY KEY (fund_code, manager_name, start_date)
);

CREATE INDEX IF NOT EXISTS idx_fund_mgr_fund ON fund.fund_manager_tenure (fund_code);
CREATE INDEX IF NOT EXISTS idx_fund_mgr_name ON fund.fund_manager_tenure (manager_name);