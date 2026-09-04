-- 0010_event_key_numbers: 持久化 Stage A 抽取的关键数字
--
-- ExtractedEvent.key_numbers 一直被抽出来又丢掉：事件本体不存，于是
-- alerts.revision_resend_rules 里的 key_number_changed 永远无从判断
-- （"投资 100 亿" 修订为 "投资 300 亿" 这类实质变化识别不出来）。
--
-- 存成 JSON 数组，与 all_source_ids 同口径。

ALTER TABLE event ADD COLUMN key_numbers TEXT NOT NULL DEFAULT '[]';
