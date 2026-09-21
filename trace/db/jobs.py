"""Durable versioned work with leased ownership and atomic stage transitions."""
import json
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from trace.domain.models import ProcessingJob


def stamp():
    return datetime.now(timezone.utc).isoformat()


class ProcessingJobRepo:
    def __init__(self, db):
        self.db = db

    def create_or_update(self, job_type, target_id, status='pending', processor_version='v1',
                         input_version=1, last_error=None, input_json=None):
        now = stamp()
        self.db.execute("""INSERT INTO processing_job
            (job_id,job_type,target_id,status,processor_version,input_version,last_error,created_at,updated_at,input_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_type,target_id,input_version,processor_version) DO NOTHING""",
            ('job_' + secrets.token_hex(12), job_type, target_id, status, processor_version,
             input_version, last_error, now, now, json.dumps(input_json or {}, ensure_ascii=False, default=str)))
        row = self.db.query_one("""SELECT * FROM processing_job WHERE job_type=? AND target_id=?
            AND input_version=? AND processor_version=?""", (job_type,target_id,input_version,processor_version))
        return self._obj(row)

    def get(self, job_id):
        row = self.db.query_one('SELECT * FROM processing_job WHERE job_id=?', (job_id,))
        return self._obj(row) if row else None

    def get_by_target(self, job_type, target_id):
        row = self.db.query_one('SELECT * FROM processing_job WHERE job_type=? AND target_id=? ORDER BY input_version DESC,created_at DESC LIMIT 1', (job_type,target_id))
        return self._obj(row) if row else None

    def list_pending(self, job_type, limit=100):
        now = stamp()
        rows = self.db.query("""SELECT * FROM processing_job WHERE job_type=? AND
            ((status IN ('pending','blocked_budget','failed') AND (next_retry_at IS NULL OR next_retry_at<=?))
             OR (status='processing' AND lease_until<=?))
            ORDER BY created_at,input_version LIMIT ?""", (job_type,now,now,limit))
        return [self._obj(r) for r in rows]

    def claim(self, job_id, lease_seconds=600):
        now = stamp()
        owner = secrets.token_hex(16)
        until = (datetime.now(timezone.utc)+timedelta(seconds=lease_seconds)).isoformat()
        with self.db.transaction(mode='IMMEDIATE'):
            row = self.db.query_one('SELECT * FROM processing_job WHERE job_id=?', (job_id,))
            if not row:
                return None
            eligible = (row['status'] in ('pending','blocked_budget','failed') and
                        (not row['next_retry_at'] or row['next_retry_at']<=now)) or (
                        row['status']=='processing' and row['lease_until'] and row['lease_until']<=now)
            if not eligible:
                return None
            if row['retry_count'] >= row['max_retries']:
                self.db.execute("UPDATE processing_job SET status='dead_letter',lease_owner=NULL,lease_until=NULL WHERE job_id=?", (job_id,))
                return None
            self.db.execute("""UPDATE processing_job SET status='processing',lease_owner=?,lease_until=?,
                retry_count=retry_count+1,updated_at=? WHERE job_id=?""", (owner,until,now,job_id))
            return self.get(job_id)

    def owns(self, job):
        return bool(self.db.query_one("""SELECT 1 FROM processing_job WHERE job_id=? AND status='processing'
            AND lease_owner=? AND lease_until>?""", (job.job_id,job.lease_owner,stamp())))

    def renew_lease(self, job, additional_seconds=600):
        now = stamp()
        until = (datetime.now(timezone.utc)+timedelta(seconds=additional_seconds)).isoformat()
        with self.db.transaction(mode='IMMEDIATE'):
            cur = self.db.execute("""UPDATE processing_job SET lease_until=?,updated_at=?
                WHERE job_id=? AND lease_owner=? AND status='processing' AND lease_until>?""",
                (until,now,job.job_id,job.lease_owner,now))
            if cur.rowcount != 1:
                raise RuntimeError('Processing lease lost')
            job.lease_until = datetime.fromisoformat(until)
            return True

    def finish(self, job, status, error=None, delay_seconds=0):
        now = stamp()
        retry = (datetime.now(timezone.utc)+timedelta(seconds=delay_seconds)).isoformat() if delay_seconds else None
        cur = self.db.execute("""UPDATE processing_job SET status=?,last_error=?,updated_at=?,next_retry_at=?,
            retry_count=CASE WHEN ?='blocked_budget' THEN MAX(0,retry_count-1) ELSE retry_count END,
            lease_owner=NULL,lease_until=NULL WHERE job_id=? AND lease_owner=? AND status='processing' AND lease_until>?""",
            (status,error,now,retry,status,job.job_id,job.lease_owner,now))
        if cur.rowcount != 1:
            raise RuntimeError('Processing lease lost')

    def mark_status(self, job_type, target_id, status, last_error=None):
        # Administrative compatibility path. Workers must use finish with ownership.
        job = self.get_by_target(job_type,target_id)
        if job is None:
            self.create_or_update(job_type,target_id,status=status,last_error=last_error)
        else:
            self.db.execute('UPDATE processing_job SET status=?,last_error=?,updated_at=?,lease_until=NULL,lease_owner=NULL WHERE job_id=?',
                            (status,last_error,stamp(),job.job_id))

    @staticmethod
    def _obj(row):
        data = dict(row)
        for key in ('lease_until','created_at','updated_at'):
            data[key] = datetime.fromisoformat(data[key]) if data[key] else None
        data['input_json'] = json.loads(data['input_json'])
        data.pop('next_retry_at',None)
        return ProcessingJob(**data)


def schedule_analysis(db, event, *, is_update=False, resend_allowed=True):
    fields = asdict(event)
    fields.pop('title_embedding',None)
    fields.pop('summary_embedding',None)
    evidence_ids = [r['raw_item_id'] for r in db.query('SELECT raw_item_id FROM event_source WHERE event_id=? ORDER BY raw_item_id', (event.event_id,))]
    payload = {'event': fields, 'evidence_ids': evidence_ids, 'is_update': is_update, 'resend_allowed': resend_allowed}
    serialized = json.dumps(payload,default=str,ensure_ascii=False)
    db.execute('INSERT OR IGNORE INTO event_version_snapshot VALUES (?,?,?,?)', (event.event_id,event.version,serialized,stamp()))
    return ProcessingJobRepo(db).create_or_update('stage_b_analyze',event.event_id,input_version=event.version,input_json=payload)


def event_from_job(job, fallback):
    from trace.domain.models import Event
    fields = job.input_json.get('event')
    if not fields:
        return fallback
    fields = dict(fields)
    for key in ('first_seen_at','last_updated_at','event_time'):
        if isinstance(fields.get(key),str):
            fields[key] = datetime.fromisoformat(fields[key])
    return Event(**fields)
