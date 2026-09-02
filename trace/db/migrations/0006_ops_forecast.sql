-- 运营可观测性：运行历史 + 预测回测账本

-- 每轮 run_once 的结构化摘要落库：/status 与事后审计可查（此前只进日志）
CREATE TABLE IF NOT EXISTS run_history (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    trace_mode TEXT,
    status TEXT,
    sources_checked INTEGER DEFAULT 0,
    sources_succeeded INTEGER DEFAULT 0,
    sources_failed INTEGER DEFAULT 0,
    sources_disabled INTEGER DEFAULT 0,
    raw_items_new INTEGER DEFAULT 0,
    raw_items_duplicate INTEGER DEFAULT 0,
    events_created INTEGER DEFAULT 0,
    events_revised INTEGER DEFAULT 0,
    events_analyzed INTEGER DEFAULT 0,
    alerts_eligible INTEGER DEFAULT 0,
    alerts_sent INTEGER DEFAULT 0,
    alerts_suppressed INTEGER DEFAULT 0,
    alerts_failed INTEGER DEFAULT 0,
    human_review INTEGER DEFAULT 0,
    keyword_filtered INTEGER DEFAULT 0,
    llm_stage_a_calls INTEGER DEFAULT 0,
    llm_stage_b_calls INTEGER DEFAULT 0,
    llm_verifier_calls INTEGER DEFAULT 0,
    failed_sources TEXT DEFAULT '[]',
    notes TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_run_history_started ON run_history(started_at DESC);

-- 预测回测账本：Stage B 影响预测 vs 事后真实行情的方向核对。
-- 每个 impact 只核对一次（impact_id UNIQUE）；行情缺失时不落行（下轮重试）。
CREATE TABLE IF NOT EXISTS forecast_check (
    check_id TEXT PRIMARY KEY,
    impact_id TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL,
    security_id TEXT NOT NULL,
    predicted_direction TEXT NOT NULL,
    predicted_score REAL,
    confidence REAL,
    actual_change_pct REAL,
    actual_direction TEXT,
    outcome TEXT NOT NULL,            -- hit / miss / neutral
    horizon_hours REAL,
    evaluated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forecast_check_eval ON forecast_check(evaluated_at DESC);
