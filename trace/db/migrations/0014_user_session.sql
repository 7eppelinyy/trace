-- 0014_user_session: 服务端用户会话与凭据存储 (F02/T02)
--
-- 凭据与内部 user_id 分离，支持有期限、可撤销的会话。
-- 客户端仅持有不透明 token，禁止任意通过 header/query 声明他人身份。

CREATE TABLE IF NOT EXISTS user_session (
    session_token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    is_revoked INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES user(user_id)
);

CREATE INDEX IF NOT EXISTS idx_user_session_user ON user_session(user_id);
