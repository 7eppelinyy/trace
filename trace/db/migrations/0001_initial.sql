-- 0001_initial: 核心表结构（对应规划书 §23）

-- 数据源登记（合规：未授权默认 enabled=0）
CREATE TABLE IF NOT EXISTS source (
    source_id        TEXT PRIMARY KEY,
    source_name      TEXT NOT NULL UNIQUE,
    source_type      TEXT NOT NULL,               -- official/ir/industry_media/financial_media/rss/market_data
    priority         INTEGER NOT NULL DEFAULT 5,  -- 1(最高)-10
    base_reliability REAL    NOT NULL DEFAULT 5.0,-- 1-10
    license_mode     TEXT    NOT NULL DEFAULT 'unknown',
    retention_policy TEXT    NOT NULL DEFAULT 'default',
    enabled          INTEGER NOT NULL DEFAULT 0
);

-- 原始采集条目
CREATE TABLE IF NOT EXISTS raw_item (
    raw_item_id     TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES source(source_id),
    source_item_id  TEXT,
    title           TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL DEFAULT '',
    canonical_url   TEXT NOT NULL DEFAULT '',
    published_at    TEXT,
    fetched_at      TEXT,
    language        TEXT NOT NULL DEFAULT '',
    content         TEXT,
    reference       TEXT,
    title_hash      TEXT NOT NULL DEFAULT '',
    content_hash    TEXT NOT NULL DEFAULT '',
    event_id        TEXT,
    UNIQUE (source_id, source_item_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_item_source_item ON raw_item(source_id, source_item_id);
CREATE INDEX IF NOT EXISTS idx_raw_item_title_hash ON raw_item(title_hash);
CREATE INDEX IF NOT EXISTS idx_raw_item_canonical_url ON raw_item(canonical_url);
CREATE INDEX IF NOT EXISTS idx_raw_item_published ON raw_item(published_at);

-- 中心对象：Event（不是 News）
CREATE TABLE IF NOT EXISTS event (
    event_id           TEXT PRIMARY KEY,
    title              TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',
    event_type         TEXT NOT NULL DEFAULT 'other',
    status             TEXT NOT NULL DEFAULT 'reported',
    version            INTEGER NOT NULL DEFAULT 1,
    first_seen_at      TEXT,
    last_updated_at    TEXT,
    event_time         TEXT,
    language           TEXT NOT NULL DEFAULT '',
    first_source_id    TEXT,
    primary_source_id  TEXT,
    all_source_ids     TEXT NOT NULL DEFAULT '[]',  -- JSON list
    material_update    INTEGER NOT NULL DEFAULT 0,
    needs_human_review INTEGER NOT NULL DEFAULT 0,
    title_embedding    BLOB,
    summary_embedding  BLOB
);
CREATE INDEX IF NOT EXISTS idx_event_time ON event(event_time);
CREATE INDEX IF NOT EXISTS idx_event_status ON event(status);

-- Event 的 Evidence
CREATE TABLE IF NOT EXISTS event_source (
    event_id    TEXT NOT NULL REFERENCES event(event_id),
    raw_item_id TEXT NOT NULL REFERENCES raw_item(raw_item_id),
    role        TEXT NOT NULL DEFAULT 'supporting',  -- first/primary/supporting
    PRIMARY KEY (event_id, raw_item_id)
);

-- Event 版本演进
CREATE TABLE IF NOT EXISTS event_revision (
    revision_id     TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL REFERENCES event(event_id),
    version         INTEGER NOT NULL,
    revision_type   TEXT NOT NULL DEFAULT '',
    material_update INTEGER NOT NULL DEFAULT 0,
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_revision_event ON event_revision(event_id);

-- Security Master（美股 + A股统一管理）
CREATE TABLE IF NOT EXISTS security (
    security_id        TEXT PRIMARY KEY,
    market             TEXT NOT NULL,            -- US/CN
    exchange           TEXT NOT NULL DEFAULT '', -- NASDAQ/NYSE/SSE/SZSE/BSE
    ticker             TEXT NOT NULL UNIQUE,
    company_name_zh    TEXT NOT NULL DEFAULT '',
    company_name_en    TEXT NOT NULL DEFAULT '',
    cik                TEXT,
    aliases            TEXT NOT NULL DEFAULT '[]',        -- JSON list
    products           TEXT NOT NULL DEFAULT '[]',        -- JSON list
    industry_tags      TEXT NOT NULL DEFAULT '[]',        -- JSON list
    graph_node_ids     TEXT NOT NULL DEFAULT '[]',        -- JSON list
    is_watchlist_default INTEGER NOT NULL DEFAULT 0,
    is_context_universe  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_security_market ON security(market);

-- 非上市实体与别名（Samsung / SK hynix / YMTC / CXMT / TSMC ...）
CREATE TABLE IF NOT EXISTS entity_alias (
    entity_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    aliases     TEXT NOT NULL DEFAULT '[]',      -- JSON list
    entity_type TEXT NOT NULL DEFAULT 'company', -- company/agency/concept
    security_id TEXT                             -- 可关联 Security
);

-- 产业链边（Phase 1：SQLite industry_edge + Python adjacency graph）
CREATE TABLE IF NOT EXISTS industry_edge (
    edge_id         TEXT PRIMARY KEY,
    from_node       TEXT NOT NULL,
    to_node         TEXT NOT NULL,
    edge_type       TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 0.8,
    evidence_source TEXT NOT NULL DEFAULT '',
    valid_from      TEXT,
    valid_to        TEXT,
    direction_rule  TEXT NOT NULL DEFAULT '',
    UNIQUE (from_node, to_node, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_industry_edge_from ON industry_edge(from_node);
CREATE INDEX IF NOT EXISTS idx_industry_edge_to ON industry_edge(to_node);

-- 影响分析（Stage B 结构化输出 + 集中评分结果）
CREATE TABLE IF NOT EXISTS event_impact (
    impact_id          TEXT PRIMARY KEY,
    event_id           TEXT NOT NULL REFERENCES event(event_id),
    security_id        TEXT NOT NULL REFERENCES security(security_id),
    direction          TEXT NOT NULL DEFAULT 'uncertain',
    directness         TEXT NOT NULL DEFAULT 'indirect',
    magnitude          REAL NOT NULL DEFAULT 5.0,
    persistence        REAL NOT NULL DEFAULT 5.0,
    directness_score   REAL NOT NULL DEFAULT 5.0,
    confidence         REAL NOT NULL DEFAULT 0.5,
    reason             TEXT NOT NULL DEFAULT '',
    industry_path      TEXT NOT NULL DEFAULT '',
    evidence_ids       TEXT NOT NULL DEFAULT '[]',        -- JSON list
    source_reliability REAL NOT NULL DEFAULT 5.0,
    base_score         REAL NOT NULL DEFAULT 5.0,
    market_confirmation REAL NOT NULL DEFAULT 5.0,
    final_score        REAL NOT NULL DEFAULT 5.0,
    created_at         TEXT,
    UNIQUE (event_id, security_id)
);
CREATE INDEX IF NOT EXISTS idx_event_impact_security ON event_impact(security_id);

-- 行情快照（Market Confirmation）
CREATE TABLE IF NOT EXISTS market_snapshot (
    security_id     TEXT NOT NULL REFERENCES security(security_id),
    ts              TEXT NOT NULL,
    last_price      REAL,
    prev_close      REAL,
    change_pct_1m   REAL,
    change_pct_5m   REAL,
    change_pct_15m  REAL,
    volume          INTEGER,
    volume_ratio    REAL,
    session         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (security_id, ts)
);

-- 用户
CREATE TABLE IF NOT EXISTS user (
    user_id    TEXT PRIMARY KEY,             -- telegram chat id
    timezone   TEXT NOT NULL DEFAULT 'Asia/Tokyo',
    muted_until TEXT,
    created_at TEXT
);

-- Watchlist
CREATE TABLE IF NOT EXISTS watchlist (
    user_id     TEXT NOT NULL REFERENCES user(user_id),
    security_id TEXT NOT NULL REFERENCES security(security_id),
    added_at    TEXT,
    PRIMARY KEY (user_id, security_id)
);

-- 提醒规则（默认 final_score >= 7，可按证券独立调整）
CREATE TABLE IF NOT EXISTS alert_rule (
    user_id     TEXT NOT NULL REFERENCES user(user_id),
    security_id TEXT,                        -- NULL = all
    threshold   REAL NOT NULL DEFAULT 7.0,
    updated_at  TEXT,
    PRIMARY KEY (user_id, security_id)
);

-- 幂等投递记录：idempotency key = (user_id, event_id, security_id, event_version, alert_type)
CREATE TABLE IF NOT EXISTS alert_delivery (
    delivery_id   TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    event_id      TEXT NOT NULL,
    security_id   TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    alert_type    TEXT NOT NULL,
    final_score   REAL NOT NULL DEFAULT 0.0,
    sent_at       TEXT,
    status        TEXT NOT NULL DEFAULT 'sent',
    UNIQUE (user_id, event_id, security_id, event_version, alert_type)
);

-- 每日摘要
CREATE TABLE IF NOT EXISTS daily_digest (
    digest_id      TEXT PRIMARY KEY,
    date_str       TEXT NOT NULL UNIQUE,     -- YYYY-MM-DD
    content_markdown TEXT NOT NULL DEFAULT '',
    sent_at        TEXT
);
