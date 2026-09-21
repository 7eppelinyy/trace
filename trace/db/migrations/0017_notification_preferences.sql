-- 0017_notification_preferences: 提醒偏好与时区日报缓存键（T10 / F10 / F27）

CREATE TABLE IF NOT EXISTS notification_preference (
    preference_id TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES user(user_id),
    security_id   TEXT,                        -- NULL 为全局偏好；非空为单标的覆盖
    threshold     REAL NOT NULL DEFAULT 7.0,
    enabled       INTEGER NOT NULL DEFAULT 1,  -- 1=开启, 0=关闭/退订
    quiet_start   TEXT,                        -- 用户当地时间 HH:MM (如 '22:00')
    quiet_end     TEXT,                        -- 用户当地时间 HH:MM (如 '08:00')
    channel       TEXT NOT NULL DEFAULT 'all', -- 'all', 'telegram', 'wechat'
    revision      INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL,
    UNIQUE (user_id, security_id)
);
CREATE INDEX IF NOT EXISTS idx_pref_user ON notification_preference(user_id);
CREATE INDEX IF NOT EXISTS idx_pref_user_sec ON notification_preference(user_id, security_id);

-- 重建 daily_digest 以消除单日期跨时区冲突，支持 (date_str, timezone, scope, version) 多维度隔离
CREATE TABLE IF NOT EXISTS daily_digest_v2 (
    digest_id        TEXT PRIMARY KEY,
    date_str         TEXT NOT NULL,
    content_markdown TEXT NOT NULL DEFAULT '',
    sent_at          TEXT,
    cache_key        TEXT UNIQUE
);

INSERT OR IGNORE INTO daily_digest_v2 (digest_id, date_str, content_markdown, sent_at, cache_key)
    SELECT digest_id, date_str, content_markdown, sent_at, date_str || ':default'
    FROM daily_digest;

DROP TABLE IF EXISTS daily_digest;
ALTER TABLE daily_digest_v2 RENAME TO daily_digest;
CREATE INDEX IF NOT EXISTS idx_daily_digest_date ON daily_digest(date_str);
CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_digest_cache_key ON daily_digest(cache_key);
