-- 0021_security_and_source_governance.sql: T15 (F23, F29, F30)
-- 1. 证券主数据增加 status (verified / unverified / delisted)
ALTER TABLE security ADD COLUMN status TEXT NOT NULL DEFAULT 'verified';

-- 2. 自选增加用户专属别名，隔离公共公司名称 (F30)
ALTER TABLE watchlist ADD COLUMN user_alias TEXT NOT NULL DEFAULT '';

-- 3. 数据源增加 4 维许可门禁、核验责任人与运营 Override 隔离字段 (F29, F30)
ALTER TABLE source ADD COLUMN can_fetch INTEGER NOT NULL DEFAULT 1;
ALTER TABLE source ADD COLUMN can_store INTEGER NOT NULL DEFAULT 1;
ALTER TABLE source ADD COLUMN can_display INTEGER NOT NULL DEFAULT 1;
ALTER TABLE source ADD COLUMN can_forward INTEGER NOT NULL DEFAULT 1;
ALTER TABLE source ADD COLUMN verified_at TEXT;
ALTER TABLE source ADD COLUMN verified_by TEXT;
ALTER TABLE source ADD COLUMN operational_override INTEGER DEFAULT NULL;
ALTER TABLE source ADD COLUMN override_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE source ADD COLUMN override_updated_at TEXT;
ALTER TABLE source ADD COLUMN override_updated_by TEXT;
