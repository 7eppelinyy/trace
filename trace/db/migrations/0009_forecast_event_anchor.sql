-- 0009_forecast_event_anchor: 回测账本改为事件锚定区间收益
--
-- 此前 run_due_checks 用的是**核对时刻当前报价**的 change_pct_from_prev()，
-- 也就是"今天的日内涨跌"，而不是 event_time → event_time+horizon 的区间收益。
-- 后果：长驻循环停三天再拉起来，核对的是第三天那天的日内涨跌，却被当作
-- "24 小时方向预测命中率"落账 —— /accuracy 展示的数字没有统计意义。
--
-- 改为：锚点价取事件时刻附近的 market_snapshot，到期价取核对时刻的真实报价，
-- 区间收益 =（到期价 - 锚点价）/ 锚点价。新增列让每一条核对都可复算、可审计。
--
--   anchor_price / anchor_ts  事件锚点价与其快照时刻
--   exit_price                核对时刻价格
--   elapsed_hours             事件到核对的真实间隔（不等于 horizon_hours）
--   note                      unmeasurable 的具体原因
--
-- outcome 新增取值 unmeasurable：拿不到锚点价（事件早于快照采集）或核对
-- 严重超时。它不计入命中率，但必须落账 —— 否则这些 impact 会永远停在
-- pending，把"待核对"计数变成一个只增不减的垃圾桶。

ALTER TABLE forecast_check ADD COLUMN anchor_price REAL;
ALTER TABLE forecast_check ADD COLUMN anchor_ts TEXT;
ALTER TABLE forecast_check ADD COLUMN exit_price REAL;
ALTER TABLE forecast_check ADD COLUMN elapsed_hours REAL;
ALTER TABLE forecast_check ADD COLUMN note TEXT NOT NULL DEFAULT '';

-- 锚点检索：WHERE security_id=? ORDER BY ts
CREATE INDEX IF NOT EXISTS idx_market_snapshot_security_ts
    ON market_snapshot(security_id, ts);
