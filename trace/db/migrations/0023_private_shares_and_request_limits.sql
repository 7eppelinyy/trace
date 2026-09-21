CREATE TABLE research_share (
    token_hash TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES research_question(question_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL REFERENCES user(user_id),
    snapshot_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX research_share_question ON research_share(question_id);

CREATE TABLE request_limit (
    bucket TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    calls INTEGER NOT NULL,
    PRIMARY KEY(bucket, window_start)
);
