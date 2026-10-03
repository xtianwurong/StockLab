-- ==============================================================================
-- StockLab 迁移 003：证券生命周期体系
-- ==============================================================================
-- 说明：
--   1. 证券主表以 status 取代 list_status：list_status 只有 L/D/P 三个数据源
--      代码且当前恒为 L，无法表达 PRE_LIST / PAUSED；status 使用领域语义
--      （见 stocklab/domain/security.py SECURITY_STATUSES），避免同一含义两列并存；
--   2. 旧值按 L→LISTED / D→DELISTED / P→PAUSED 映射后丢弃原列；
--   3. reference.security_events 记录 LISTED / SUSPENDED / RESUMED / DELISTED /
--      ST / DE_ST / NAME_CHANGED / CODE_CHANGED / INDEX_ADDED / INDEX_REMOVED
--      等事件，历史股票池（universe as-of）据此与 list_date/delist_date 推导，
--      而不是拿「当前仍在市的股票」当历史样本（否则产生幸存者偏差）。
-- ==============================================================================

ALTER TABLE reference.securities ADD COLUMN status VARCHAR;

UPDATE reference.securities
SET status = CASE list_status
    WHEN 'L' THEN 'LISTED'
    WHEN 'D' THEN 'DELISTED'
    WHEN 'P' THEN 'PAUSED'
    ELSE 'LISTED'
END;

ALTER TABLE reference.securities DROP COLUMN list_status;

CREATE TABLE IF NOT EXISTS reference.security_events (
    ts_code    VARCHAR NOT NULL,
    event_date DATE NOT NULL,
    event_type VARCHAR NOT NULL,
    detail     VARCHAR,
    source     VARCHAR NOT NULL,
    PRIMARY KEY (ts_code, event_date, event_type)
);
