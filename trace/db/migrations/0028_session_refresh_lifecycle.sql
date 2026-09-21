-- 0028_session_refresh_lifecycle.sql: 用户会话刷新凭据与生命周期隔离 (N02 / RV01)
--
-- 1. 刷新凭据 (refresh token) 采用 SHA-256 哈希存储，支持令牌轮换 (rotation)、
--    单次消费与防重放检测 (family_id 级作废)。
-- 2. access token 与 refresh token 分离生命周期，续期保持同一 user_id。

CREATE TABLE IF NOT EXISTS user_refresh_token (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    session_token TEXT,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES user(user_id)
);

CREATE INDEX IF NOT EXISTS idx_refresh_user ON user_refresh_token(user_id);
CREATE INDEX IF NOT EXISTS idx_refresh_family ON user_refresh_token(family_id);

ALTER TABLE user_session ADD COLUMN family_id TEXT;
