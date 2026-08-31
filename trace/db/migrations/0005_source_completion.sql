-- 0005_source_completion_gate: Source Completion Gate V1
-- source_registry 扩展字段（任务书 §7）：
--   authority_level   官方公司/官方政府/监管/产业媒体/财经媒体
--   poll_interval_seconds 建议轮询间隔（秒）
--   security_map      公司官方源 → 直接关联证券（不依赖 LLM 猜股票代码）
-- source_health 增加 last_http_status（真实 HTTP 状态码，429 显式可见）

ALTER TABLE source ADD COLUMN authority_level TEXT NOT NULL DEFAULT '';
ALTER TABLE source ADD COLUMN poll_interval_seconds INTEGER NOT NULL DEFAULT 600;
ALTER TABLE source ADD COLUMN security_map TEXT NOT NULL DEFAULT '[]';

ALTER TABLE source_health ADD COLUMN last_http_status INTEGER;
