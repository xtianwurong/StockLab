-- ==============================================================================
-- StockLab 迁移 004：研究快照（V2 需求 §6）
-- ==============================================================================
-- 说明：
--   1. 研究快照回答「任意筛选条件可以复现」（§17）：一条筛选从哪里来、
--      用了什么数据、什么因子版本、什么参数、什么股票池，必须全部落库；
--   2. research.snapshots 存快照元数据与可复现 spec（spec_json 内含
--      屏蔽条件、预处理配置、以及**展开后的 ts_code 列表**——
--      股票池按 as-of 展开后固化，即使后续生命周期数据变化也能原样重跑）；
--   3. research.snapshot_results 存每只股票的判定结果（通过与否、未满足条件、
--      各因子取值），用于与重跑结果比对，证明「重新生成」的结果一致；
--   4. 快照一经写入不可更新：复现结论必须建立在不可变记录之上。
-- ==============================================================================

CREATE SCHEMA IF NOT EXISTS research;

CREATE TABLE IF NOT EXISTS research.snapshots (
    snapshot_id    VARCHAR NOT NULL PRIMARY KEY,
    created_at     TIMESTAMP NOT NULL,
    as_of_date     DATE NOT NULL,
    universe       VARCHAR,
    universe_size  INTEGER NOT NULL,
    data_version   VARCHAR NOT NULL,
    factor_version VARCHAR NOT NULL,
    config_version VARCHAR NOT NULL,
    condition      VARCHAR,
    passed_count   INTEGER NOT NULL,
    result_count   INTEGER NOT NULL,
    spec_json      VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS research.snapshot_results (
    snapshot_id  VARCHAR NOT NULL,
    ts_code      VARCHAR NOT NULL,
    passed       BOOLEAN NOT NULL,
    failed_rules VARCHAR,
    factor_values VARCHAR,
    PRIMARY KEY (snapshot_id, ts_code)
);
