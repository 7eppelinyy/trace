-- 0029_outbox_audit_and_management.sql
-- Outbox 运维处置审计日志与受控处理

CREATE TABLE IF NOT EXISTS outbox_audit_log (
    audit_id        TEXT PRIMARY KEY,
    outbox_id       TEXT NOT NULL,
    action          TEXT NOT NULL,          -- 'confirm_delivered', 'requeue', 'discard'
    operator        TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    new_status      TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    FOREIGN KEY(outbox_id) REFERENCES alert_outbox(outbox_id)
);

CREATE INDEX IF NOT EXISTS idx_outbox_audit_outbox_id ON outbox_audit_log(outbox_id);
CREATE INDEX IF NOT EXISTS idx_outbox_audit_created ON outbox_audit_log(created_at);
