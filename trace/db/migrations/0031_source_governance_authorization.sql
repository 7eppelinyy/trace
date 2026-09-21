-- 0031_source_governance_authorization: 来源合规授权证据登记表 (N07 / §10)
CREATE TABLE IF NOT EXISTS source_authorization (
    auth_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source(source_id),
    scope_json TEXT NOT NULL DEFAULT '[]',
    evidence_url_or_file TEXT NOT NULL DEFAULT '',
    verified_by TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_auth_source ON source_authorization(source_id);
