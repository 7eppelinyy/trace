-- 0011_run_history_rescored: 运行历史记录开盘后补算市场确认的事件数
--
-- 盘后/周末事件在分析当时被时段门禁判为"市场还没开过盘"，市场确认只能取
-- 中性 5.0。次日开盘后重算才拿得到真实确认，此前低于阈值的事件可能因此
-- 跨过阈值并首次推送。需要可观测计数，否则这条链路是否在工作只能靠翻日志。

ALTER TABLE run_history ADD COLUMN rescored_events INTEGER NOT NULL DEFAULT 0;
