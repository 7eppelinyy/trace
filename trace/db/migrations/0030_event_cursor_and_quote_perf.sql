-- 0030_event_cursor_and_quote_perf.sql
-- 为事件列表游标稳定分页 (first_seen_at DESC, event_id DESC) 建立复合索引，
-- 支持海量事件（10万+）在 O(limit) 复杂度内基于索引完成稳定翻页，避免全表扫描。

CREATE INDEX IF NOT EXISTS idx_event_first_seen ON event(first_seen_at DESC, event_id DESC);
