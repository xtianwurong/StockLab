-- ==============================================================================
-- StockLab 迁移 006：投资人观点库（价值投资 / 宏观判断 学习用）
-- ==============================================================================
-- 设计前提（与 stocklab/domain/insight.py 的模块说明一一对应）：
--
--   1. 拆三张表：投资人、平台账号、言论。账号会变（改名/封禁/换平台），
--      言论是既成事实；三者混在一张表会导致「改一次账号要 UPDATE 上万条言论」，
--      而言论不可变（改了就没法审计了）。
--
--   2. 只 INSERT 不 UPDATE。三张表都只追加；语录的纠正靠 verification 字段
--      表达，而不是改原文。
--
--   3. source_url 允许 NULL。这一列是「能否溯源」的诚实记录：抓不到就留空，
--      绝不用首页 URL 或搜索页 URL 凑数 —— 一个假的溯源链接比没有链接更糟，
--      因为它会让人以为这句话真的被核实过。
--
--   4. verification 默认 'unverified'，且**没有任何自动化路径能把它写成
--      verified**。国内财经平台上大量流传的「名人语录」无法溯源，同一句话
--      在不同帖子间以不同署名传播。把「已核实」写成代码逻辑就是伪自动化。
--
--   5. content_hash 用于跨平台去重：同一段话被多个平台转载时，
--      由同步侧按 hash 去重后再入库，而不是靠 quote_id（主键）——
--      因为不同平台的 id 体系互不相通。
--
--   6. raw_meta 保留原始 JSON。平台页面结构随时会改，保留原始载荷才能在
--      解析逻辑修好后重新归一化，而不必重新抓取（UGC 内容可能已被删）。
-- ==============================================================================

CREATE SCHEMA IF NOT EXISTS insight;

-- ---------------------------------------------------------------------------
-- 投资人主表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS insight.investors (
    investor_code   VARCHAR NOT NULL PRIMARY KEY,
    name            VARCHAR NOT NULL,
    aliases         VARCHAR,
    role            VARCHAR,
    organization    VARCHAR,
    style_tags      VARCHAR,
    profile_url     VARCHAR,
    is_active       BOOLEAN DEFAULT TRUE,
    created_time    TIMESTAMP NOT NULL
);

-- 按姓名 / 别名检索（「段永平」「大道无形我有型」都能命中同一人）
CREATE INDEX IF NOT EXISTS idx_investors_name
    ON insight.investors (name);

-- ---------------------------------------------------------------------------
-- 平台账号映射（投资人 × 平台）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS insight.investor_accounts (
    account_id      VARCHAR NOT NULL PRIMARY KEY,
    investor_code   VARCHAR NOT NULL,
    platform        VARCHAR NOT NULL,
    account_name    VARCHAR NOT NULL,
    account_uid     VARCHAR,
    home_url        VARCHAR,
    is_enabled      BOOLEAN DEFAULT TRUE,
    note            VARCHAR,
    created_time    TIMESTAMP NOT NULL
);

-- 同步入口按 (platform, is_enabled) 拉待抓账号
CREATE INDEX IF NOT EXISTS idx_accounts_platform_enabled
    ON insight.investor_accounts (platform, is_enabled);

-- 同一投资人可有多个平台账号，查询用
CREATE INDEX IF NOT EXISTS idx_accounts_investor
    ON insight.investor_accounts (investor_code);

-- ---------------------------------------------------------------------------
-- 理念与言论
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS insight.investor_quotes (
    quote_id        VARCHAR NOT NULL PRIMARY KEY,
    investor_code   VARCHAR NOT NULL,
    platform        VARCHAR NOT NULL,
    account_name    VARCHAR,
    source_url      VARCHAR,
    published_at    TIMESTAMP,
    captured_at     TIMESTAMP NOT NULL,
    content_hash    VARCHAR NOT NULL,
    content         VARCHAR NOT NULL,
    summary         VARCHAR,
    quote_type      VARCHAR,
    theme           VARCHAR,
    stock_codes     VARCHAR,
    verification    VARCHAR DEFAULT 'unverified',
    language        VARCHAR DEFAULT 'zh',
    raw_meta        VARCHAR
);

-- 1) 某投资人的言论流：页面按时间倒序展示的主力路径
CREATE INDEX IF NOT EXISTS idx_quotes_investor_published
    ON insight.investor_quotes (investor_code, published_at DESC NULLS LAST);

-- 2) 跨投资人按主题检索（「哪些人说过 PE 低于某个水平是机会」）
CREATE INDEX IF NOT EXISTS idx_quotes_theme
    ON insight.investor_quotes (theme);

-- 3) 按平台筛选（回答「这段话出自哪个平台」）
CREATE INDEX IF NOT EXISTS idx_quotes_platform_published
    ON insight.investor_quotes (platform, published_at DESC NULLS LAST);

-- 4) 跨平台去重的依据
CREATE INDEX IF NOT EXISTS idx_quotes_content_hash
    ON insight.investor_quotes (content_hash);

-- 5) 按类型分组（理念 / 宏观 / 选股标准）
CREATE INDEX IF NOT EXISTS idx_quotes_type
    ON insight.investor_quotes (quote_type);