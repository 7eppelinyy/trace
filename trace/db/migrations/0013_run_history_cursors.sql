-- 0013_run_history_cursors: 运行历史记录本轮提交的采集游标数
--
-- 采集游标改为两阶段提交（ingest 循环走完才推进）。cursors_committed=0
-- 同时 raw_items_new>0 就意味着本轮中途退出、这批条目会重新采集 ——
-- 这是排查"为什么同一批新闻反复出现"时的第一个可查信号。

ALTER TABLE run_history ADD COLUMN cursors_committed INTEGER NOT NULL DEFAULT 0;
