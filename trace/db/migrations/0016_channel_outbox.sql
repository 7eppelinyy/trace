-- 0016_channel_outbox.sql: 渠道绑定与投递 Outbox 机制 (F06/T06)

-- 1. 用户通知渠道绑定表
CREATE TABLE IF NOT EXISTS channel_binding (
    binding_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    channel_type    TEXT NOT NULL,         -- 'telegram', 'wechat', 'webhook'
    channel_target  TEXT NOT NULL,         -- e.g. telegram chat_id
    is_active       INTEGER NOT NULL DEFAULT 1,
    verified_at     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (user_id, channel_type, channel_target)
);
CREATE INDEX IF NOT EXISTS idx_channel_user ON channel_binding(user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_channel_target ON channel_binding(channel_type, channel_target);

-- 2. 投递 Outbox 任务表
CREATE TABLE IF NOT EXISTS alert_outbox (
    outbox_id       TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    channel_type    TEXT NOT NULL,         -- 'telegram', etc.
    channel_target  TEXT NOT NULL,         -- chat_id
    event_id        TEXT NOT NULL,
    impact_id       TEXT,
    event_version   INTEGER NOT NULL DEFAULT 1,
    alert_type      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    content_text    TEXT NOT NULL,
    content_url     TEXT,
    status          TEXT NOT NULL,         -- 'pending', 'sending', 'sent', 'failed', 'suppressed'
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 3,
    retry_after     TEXT,
    lease_until     TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_poll ON alert_outbox(status, lease_until);
CREATE INDEX IF NOT EXISTS idx_outbox_event ON alert_outbox(event_id);

-- 3. 兼容迁移现有非 usr_/dev_ 用户为 Telegram 渠道绑定
INSERT OR IGNORE INTO channel_binding (binding_id, user_id, channel_type, channel_target, is_active, verified_at, created_at, updated_at)
SELECT 'bind_' || hex(randomblob(8)), user_id, 'telegram', user_id, 1, datetime('now'), datetime('now'), datetime('now')
FROM user
WHERE user_id NOT LIKE 'usr_%' AND user_id NOT LIKE 'dev_%';
