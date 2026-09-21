ALTER TABLE event ADD COLUMN embedding_model TEXT;
ALTER TABLE research_question ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE alert_feedback ADD COLUMN event_version INTEGER;
-- Old feedback lacks a reliable version and remains explicitly NULL.
CREATE UNIQUE INDEX idx_feedback_current_version ON alert_feedback(user_id,event_id,event_version,COALESCE(security_id,'')) WHERE event_version IS NOT NULL;
