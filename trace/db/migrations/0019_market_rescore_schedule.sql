-- 0019_market_rescore_schedule.sql: 行情语义、跨周末补算与快照存证增强 (F15-F18/T12)

-- 1. 为 event_impact 增加补算调度字段 (解决 F17: 跨周末/跨节假日补算不漏掉)
ALTER TABLE event_impact ADD COLUMN next_eligible_at TEXT;
ALTER TABLE event_impact ADD COLUMN expires_at TEXT;

CREATE INDEX IF NOT EXISTS idx_event_impact_rescore 
ON event_impact(market_data_mode, next_eligible_at, expires_at);

-- 2. 为 market_snapshot 增加日涨跌与来源存证字段 (解决 F15: 日涨跌与 15m 区分、来源存证)
ALTER TABLE market_snapshot ADD COLUMN change_pct_day REAL;
ALTER TABLE market_snapshot ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD';
ALTER TABLE market_snapshot ADD COLUMN source TEXT NOT NULL DEFAULT '';
ALTER TABLE market_snapshot ADD COLUMN is_delayed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE market_snapshot ADD COLUMN market_ts TEXT;
