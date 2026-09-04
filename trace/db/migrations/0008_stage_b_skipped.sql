-- 0008_stage_b_skipped: 运行历史记录被跳过的 Stage B 次数
--
-- action="merged"（仅补充佐证、事件无实质更新）此前照样触发一次完整
-- Stage B：算出的 impact 因 event.version 未变而被 Alert 幂等键拦下，
-- 用户看不到任何变化，成本却真实发生。跳过后需要一个可观测的计数，
-- 否则"省了多少"只能靠猜。

ALTER TABLE run_history ADD COLUMN stage_b_skipped INTEGER NOT NULL DEFAULT 0;
