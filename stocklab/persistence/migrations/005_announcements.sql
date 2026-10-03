-- ==============================================================================
-- StockLab 迁移 005：公司公告索引（免费数据源扩展需求 §25 / §26）
-- ==============================================================================
-- 说明：
--   1. 第一阶段只增加这一张表：reports / shareholders / dividend /
--      corporate_events 等公告链路稳定后再扩展（需求 §25）；
--   2. 只存**索引**（元数据 + PDF URL），不下载公告 PDF 正文（需求 §27）；
--   3. 写入语义：只 INSERT 不 UPDATE —— 披露记录是既成事实，
--      重复同步按主键 announcement_id 忽略冲突；
--   4. 去重：announcement_id 为主键；跨 id 的同内容公告由同步侧按
--      (ts_code, announcement_date, title) 二次去重（需求 §24）；
--   5. category 允许 NULL：巨潮 announcementTypeName 常为 null；
--      publish_time 保留原始毫秒时间戳，announcement_date 为按 UTC+8
--      换算后的公告日期。
-- ==============================================================================

CREATE SCHEMA IF NOT EXISTS corporate;

CREATE TABLE IF NOT EXISTS corporate.announcements (
    announcement_id   VARCHAR NOT NULL PRIMARY KEY,
    ts_code           VARCHAR NOT NULL,
    announcement_date DATE NOT NULL,
    publish_time      BIGINT,
    title             VARCHAR NOT NULL,
    category          VARCHAR,
    pdf_url           VARCHAR NOT NULL,
    source            VARCHAR NOT NULL,
    source_url        VARCHAR,
    crawl_time        TIMESTAMP NOT NULL,
    content_hash      VARCHAR
);

-- 按个股 + 日期区间做增量同步与查询的主路径
CREATE INDEX IF NOT EXISTS idx_announcements_ts_date
    ON corporate.announcements (ts_code, announcement_date);
