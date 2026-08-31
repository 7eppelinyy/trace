-- 0004_bootstrap_suppression: 首次同步保护。
--
-- 用户注册（首次创建 Telegram User）时，之前已经真实采集的历史事件
-- 不得集中推送为即时 Alert：
--   - user.alert_activation_at：该用户即时推送的激活时间点
--   - alert_delivery.bootstrap_suppressed：因历史事件被抑制的显式标记
-- 历史事件保留在 Event DB，仍可通过 /event、/digest 查询。

ALTER TABLE user ADD COLUMN alert_activation_at TEXT;

-- alert_delivery 幂等键不变；仅新增标记列（抑制记录不占用幂等键，允许真实新事件后续推送）
ALTER TABLE alert_delivery ADD COLUMN bootstrap_suppressed INTEGER NOT NULL DEFAULT 0;
