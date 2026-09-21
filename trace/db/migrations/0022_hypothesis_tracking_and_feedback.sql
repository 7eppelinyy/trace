-- 0022_hypothesis_tracking_and_feedback.sql: T16 假设跟踪与真实用户验证
-- 1. 假设跟踪模型 (research_question)
CREATE TABLE IF NOT EXISTS research_question (
    question_id              TEXT PRIMARY KEY,
    user_id                  TEXT NOT NULL REFERENCES user(user_id),
    event_id                 TEXT REFERENCES event(event_id),
    security_id              TEXT REFERENCES security(security_id),
    title                    TEXT NOT NULL,
    hypothesis               TEXT NOT NULL,
    supporting_conditions    TEXT NOT NULL DEFAULT '[]',   -- JSON list
    contradicting_conditions TEXT NOT NULL DEFAULT '[]',   -- JSON list
    next_check_at            TEXT,
    state                    TEXT NOT NULL DEFAULT 'tracking', -- tracking / confirmed / falsified / archived
    user_notes               TEXT NOT NULL DEFAULT '',     -- 个人专属私有备注
    matched_evidence_ids     TEXT NOT NULL DEFAULT '[]',   -- JSON list of matched raw_item_ids
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rq_user_state ON research_question(user_id, state);
CREATE INDEX IF NOT EXISTS idx_rq_security ON research_question(security_id);
CREATE INDEX IF NOT EXISTS idx_rq_event ON research_question(event_id);

-- 2. 提醒有效性与反馈记录 (alert_feedback)
CREATE TABLE IF NOT EXISTS alert_feedback (
    feedback_id              TEXT PRIMARY KEY,
    user_id                  TEXT NOT NULL REFERENCES user(user_id),
    event_id                 TEXT NOT NULL REFERENCES event(event_id),
    security_id              TEXT REFERENCES security(security_id),
    rating                   TEXT NOT NULL,                -- useful / not_useful / irrelevant / too_late / incorrect_analysis / duplicate
    reason                   TEXT NOT NULL DEFAULT '',
    created_at               TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_afb_user ON alert_feedback(user_id);
CREATE INDEX IF NOT EXISTS idx_afb_event ON alert_feedback(event_id);
