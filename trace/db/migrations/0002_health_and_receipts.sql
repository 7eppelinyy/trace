-- 0002_health_and_receipts: 来源健康状态、采集游标、Telegram 投递回执、人工检查

-- 来源健康状态（发现"看起来在运行但数小时无产出"的情况）
CREATE TABLE IF NOT EXISTS source_health (
    source_id            TEXT PRIMARY KEY,
    last_success_at      TEXT,
    last_failure_at      TEXT,
    last_error           TEXT,
    last_error_category  TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_item_at         TEXT
);

-- 采集游标/checkpoint（增量采集，不重复下载已处理数据）
CREATE TABLE IF NOT EXISTS collector_cursor (
    source_id   TEXT PRIMARY KEY,
    cursor_json TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT
);

-- 人工检查队列（Schema 校验失败且重试仍失败的事件不得进入 Alert Engine）
CREATE TABLE IF NOT EXISTS human_review (
    review_id   TEXT PRIMARY KEY,
    event_id    TEXT,
    raw_item_id TEXT,
    reason      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending/resolved
    created_at  TEXT
);
