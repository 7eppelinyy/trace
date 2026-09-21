-- 0015_processing_job.sql: 持久化处理作业与阶段状态追踪 (F03/T03)

CREATE TABLE IF NOT EXISTS processing_job (
    job_id             TEXT PRIMARY KEY,
    job_type           TEXT NOT NULL,            -- 'stage_a_extract', 'stage_b_analyze'
    target_id          TEXT NOT NULL,           -- raw_item_id (stage_a) or event_id (stage_b)
    status             TEXT NOT NULL,            -- 'pending', 'processing', 'completed', 'failed', 'blocked_budget', 'human_review', 'succeeded_empty'
    processor_version  TEXT NOT NULL DEFAULT 'v1',
    input_version      INTEGER NOT NULL DEFAULT 1,
    lease_until        TEXT,
    retry_count        INTEGER NOT NULL DEFAULT 0,
    max_retries        INTEGER NOT NULL DEFAULT 3,
    last_error         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (job_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_processing_job_status ON processing_job(job_type, status);
CREATE INDEX IF NOT EXISTS idx_processing_job_target ON processing_job(job_type, target_id);
