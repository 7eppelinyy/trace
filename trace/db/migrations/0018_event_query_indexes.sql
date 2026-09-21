-- 0018_event_query_indexes: 事件查询与稳定分页性能索引 (T11 / F13 / F24)

CREATE INDEX IF NOT EXISTS idx_event_updated ON event(last_updated_at DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS idx_event_impact_event_score ON event_impact(event_id, final_score DESC);
CREATE INDEX IF NOT EXISTS idx_event_impact_sec_score ON event_impact(security_id, final_score DESC);
CREATE INDEX IF NOT EXISTS idx_event_status_updated ON event(status, last_updated_at DESC);
