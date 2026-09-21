CREATE TABLE llm_usage_scope (
    date_str TEXT NOT NULL,
    scope TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(date_str, scope)
);
