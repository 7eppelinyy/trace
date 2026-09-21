PRAGMA defer_foreign_keys=ON;
CREATE TABLE raw_item_v2 (
    raw_item_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source(source_id),
    source_item_id TEXT,
    title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', canonical_url TEXT NOT NULL DEFAULT '',
    published_at TEXT, fetched_at TEXT, language TEXT NOT NULL DEFAULT '', content TEXT, reference TEXT,
    title_hash TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '', event_id TEXT,
    UNIQUE(source_id, source_item_id, content_hash, title_hash)
);
INSERT INTO raw_item_v2 SELECT * FROM raw_item;
DROP TABLE raw_item;
ALTER TABLE raw_item_v2 RENAME TO raw_item;
CREATE INDEX idx_raw_item_source_item ON raw_item(source_id,source_item_id);
CREATE INDEX idx_raw_item_canonical_url ON raw_item(canonical_url);
CREATE INDEX idx_raw_item_title_hash ON raw_item(title_hash);
CREATE INDEX idx_raw_item_content_hash ON raw_item(content_hash);
CREATE INDEX idx_raw_item_event ON raw_item(event_id);
CREATE INDEX idx_raw_item_published ON raw_item(published_at);

CREATE TABLE processing_job_v2 (
    job_id TEXT PRIMARY KEY, job_type TEXT NOT NULL, target_id TEXT NOT NULL, status TEXT NOT NULL,
    processor_version TEXT NOT NULL DEFAULT 'v1', input_version INTEGER NOT NULL DEFAULT 1,
    lease_until TEXT, retry_count INTEGER NOT NULL DEFAULT 0, max_retries INTEGER NOT NULL DEFAULT 3,
    last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    lease_owner TEXT, next_retry_at TEXT, input_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(job_type,target_id,input_version,processor_version)
);
INSERT INTO processing_job_v2(job_id,job_type,target_id,status,processor_version,input_version,
    lease_until,retry_count,max_retries,last_error,created_at,updated_at)
SELECT job_id,job_type,target_id,
    CASE WHEN status IN ('processing','failed') THEN 'pending' ELSE status END,
    processor_version,input_version,NULL,retry_count,max_retries,last_error,created_at,updated_at
FROM processing_job;
DROP TABLE processing_job;
ALTER TABLE processing_job_v2 RENAME TO processing_job;
CREATE INDEX idx_processing_job_status ON processing_job(job_type,status,next_retry_at,lease_until);
CREATE INDEX idx_processing_job_target ON processing_job(job_type,target_id,input_version);

CREATE TABLE analysis_run (
    analysis_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, event_version INTEGER NOT NULL,
    processor_version TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL, impacts_json TEXT NOT NULL, status TEXT NOT NULL,
    UNIQUE(event_id,event_version,processor_version)
);
CREATE TABLE event_version_snapshot (
    event_id TEXT NOT NULL, version INTEGER NOT NULL, payload_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
    PRIMARY KEY(event_id,version)
);
ALTER TABLE alert_outbox ADD COLUMN lease_owner TEXT;
ALTER TABLE alert_outbox ADD COLUMN security_id TEXT;
ALTER TABLE alert_outbox ADD COLUMN final_score REAL;
ALTER TABLE alert_outbox ADD COLUMN analysis_id TEXT;
ALTER TABLE alert_outbox ADD COLUMN expires_at TEXT;
UPDATE alert_outbox SET security_id=(SELECT security_id FROM event_impact i WHERE i.impact_id=alert_outbox.impact_id);
UPDATE alert_outbox SET status='ambiguous',last_error='legacy sending state requires review',lease_until=NULL WHERE status='sending';
