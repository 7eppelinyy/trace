-- 0003_alert_receipts: Telegram 投递回执扩展字段 + 分析模式标记

ALTER TABLE alert_delivery ADD COLUMN telegram_chat_id TEXT;
ALTER TABLE alert_delivery ADD COLUMN telegram_message_id TEXT;
ALTER TABLE alert_delivery ADD COLUMN response_status TEXT;
ALTER TABLE alert_delivery ADD COLUMN run_id TEXT;

-- event_impact 记录该次分析是 LLM 还是规则降级（生产模式不得把降级包装成完整真实分析）
ALTER TABLE event_impact ADD COLUMN analysis_mode TEXT NOT NULL DEFAULT 'llm';
ALTER TABLE event_impact ADD COLUMN market_data_mode TEXT NOT NULL DEFAULT 'real';
