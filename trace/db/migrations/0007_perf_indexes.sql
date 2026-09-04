-- 0007_perf_indexes: 补齐热路径缺失索引
--
-- raw_item.content_hash
--   Level 1 确定性去重的第 4 道检查（ExactDedup.check），每轮每条采集项都会
--   执行一次。0001 建了 title_hash / canonical_url / source_item 的索引，
--   唯独漏了 content_hash —— 该查询此前是全表扫描，随 raw_item 增长线性劣化。
--
-- raw_item.event_id
--   RawItemRepo.list_by_event 是 Stage B 取证据、/ask 取原文链接、日报取
--   来源的公共入口，同样此前全表扫描。

CREATE INDEX IF NOT EXISTS idx_raw_item_content_hash ON raw_item(content_hash);
CREATE INDEX IF NOT EXISTS idx_raw_item_event ON raw_item(event_id);
