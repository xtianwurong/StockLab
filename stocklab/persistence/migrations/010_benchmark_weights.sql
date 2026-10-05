-- =============================================================================
-- StockLab - 基准指数行业权重快照
-- =============================================================================
-- 【用途】
--   Brinson 归因需要「基准指数按行业拆分的权重」。
--   权重来自中证指数公司公布的成分券权重（akshare: index_stock_cons_weight_csindex），
--   再按本地 fund.stock_industry_mapping 聚合到申万行业层级。
--
-- 【为何落库而非每次实时算】
--   - 成分券权重为季频披露，实时抓取需 300~500 次映射查询且依赖外网；
--   - 落库后 Web 层可保持「纯本地读」，接口不因外网抖动而超时；
--   - 同步脚本 app/scripts/sync_benchmark.py 负责刷新快照。
--
-- 【快照语义】
--   以 (benchmark_code, level, sector_code) 为复合主键，仅保留最新一期快照；
--   as_of_date 记录指数公司披露日期，coverage 记录已映射成分券的权重占比。
--   coverage 偏低时说明本地行业映射覆盖不足，归因结果需降级提示。
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS reference;

CREATE TABLE IF NOT EXISTS reference.benchmark_industry_weights (
    benchmark_code VARCHAR NOT NULL,     -- 指数代码 000300.SH
    level          SMALLINT NOT NULL,    -- 行业层级 1 / 2 / 3
    sector_code    VARCHAR NOT NULL,     -- 申万行业代码 801010
    sector_name    VARCHAR,              -- 申万行业名称 农林牧渔
    weight         DOUBLE NOT NULL,      -- 行业权重（小数，同层级合计 = 1）
    as_of_date     DATE NOT NULL,        -- 权重披露日期（中证指数公司）
    coverage       DOUBLE NOT NULL,      -- 已映射成分券权重占基准总权重比例
    source         VARCHAR NOT NULL,     -- csindex / manual
    fetched_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (benchmark_code, level, sector_code)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_industry_weights_bench
    ON reference.benchmark_industry_weights (benchmark_code, level);
