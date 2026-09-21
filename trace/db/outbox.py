"""One-at-a-time delivery leases; expired sends are uncertain, never blindly replayed."""
import secrets
from datetime import datetime, timedelta, timezone
from trace.domain.models import AlertOutbox


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class AlertOutboxRepo:
    def __init__(self, db): self.db = db

    def enqueue(self, item):
        fields = ('outbox_id','user_id','channel_type','channel_target','event_id','impact_id','event_version',
                  'alert_type','idempotency_key','content_text','content_url','max_retries','security_id','final_score','analysis_id')
        now = now_iso()
        expires = item.expires_at or (datetime.now(timezone.utc)+timedelta(hours=24))
        cur = self.db.execute(f"""INSERT INTO alert_outbox ({','.join(fields)},status,retry_count,created_at,updated_at,expires_at)
            VALUES ({','.join('?' for _ in fields)},'pending',0,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING""",
            tuple(getattr(item,f) for f in fields)+(now,now,expires.isoformat()))
        return cur.rowcount == 1

    def claim_batch(self, limit=1, lease_seconds=120):
        if limit <= 0: return []
        now = now_iso()
        owner = secrets.token_hex(16)
        until = (datetime.now(timezone.utc)+timedelta(seconds=lease_seconds)).isoformat()
        with self.db.transaction(mode='IMMEDIATE'):
            self.db.execute("""UPDATE alert_outbox SET status='ambiguous',last_error='delivery_lease_expired',
                lease_owner=NULL,lease_until=NULL WHERE status='sending' AND lease_until<=?""", (now,))
            self.db.execute("""UPDATE alert_outbox SET status='expired',last_error='notification_expired'
                WHERE status='pending' AND expires_at IS NOT NULL AND expires_at<=?""", (now,))
            row = self.db.query_one("""SELECT outbox_id FROM alert_outbox WHERE status='pending'
                AND (retry_after IS NULL OR retry_after<=?) ORDER BY created_at,outbox_id LIMIT 1""", (now,))
            if not row: return []
            self.db.execute("UPDATE alert_outbox SET status='sending',lease_owner=?,lease_until=?,updated_at=? WHERE outbox_id=?",
                            (owner,until,now,row['outbox_id']))
            return [self.get(row['outbox_id'])]

    @staticmethod
    def _to_obj(row):
        data = dict(row)
        for key in ('retry_after','lease_until','created_at','updated_at','expires_at'):
            data[key] = datetime.fromisoformat(data[key]) if data[key] else None
        return AlertOutbox(**data)

    def get(self, outbox_id):
        row = self.db.query_one('SELECT * FROM alert_outbox WHERE outbox_id=?', (outbox_id,))
        if not row: return None
        return self._to_obj(row)

    def list_ambiguous(self, limit=100):
        rows = self.db.query("""SELECT * FROM alert_outbox WHERE status='ambiguous'
            ORDER BY created_at DESC LIMIT ?""", (limit,))
        return [self._to_obj(r) for r in rows]

    def resolve_ambiguous(self, outbox_id: str, action: str, operator: str, note: str = "") -> AlertOutbox:
        if action not in ('confirm_delivered', 'requeue', 'discard'):
            raise ValueError(f"Invalid resolution action: {action}. Must be one of confirm_delivered, requeue, discard")
        if not operator or not str(operator).strip():
            raise ValueError("Operator must be provided for audit trail")

        now = now_iso()
        audit_id = f"aud_{secrets.token_hex(8)}"
        with self.db.transaction(mode='IMMEDIATE'):
            row = self.db.query_one("SELECT * FROM alert_outbox WHERE outbox_id=?", (outbox_id,))
            if not row:
                raise ValueError(f"Outbox item not found: {outbox_id}")
            prev_status = row['status']
            if prev_status != 'ambiguous':
                raise ValueError(f"Only ambiguous outbox items can be resolved; current status is '{prev_status}'")

            note_clean = (note or "").strip()
            if action == 'confirm_delivered':
                new_status = 'sent'
                last_err = f"manually confirmed by {operator}: {note_clean}".strip()
                self.db.execute("""UPDATE alert_outbox SET status='sent', last_error=?,
                    lease_owner=NULL, lease_until=NULL, updated_at=? WHERE outbox_id=?""",
                    (last_err, now, outbox_id))
                from trace.common.ids import alert_delivery_id
                from trace.domain.models import AlertDelivery
                from trace.db.repositories import AlertDeliveryRepo
                existing_deliv = self.db.query_one(
                    "SELECT 1 FROM alert_delivery WHERE user_id=? AND event_id=? AND alert_type=?",
                    (row['user_id'], row['event_id'], row['alert_type']))
                if not existing_deliv:
                    AlertDeliveryRepo(self.db).record(AlertDelivery(
                        delivery_id=alert_delivery_id(), user_id=row['user_id'], event_id=row['event_id'],
                        security_id=row['security_id'] or '', event_version=row['event_version'],
                        alert_type=row['alert_type'], final_score=row['final_score'] or 0,
                        sent_at=datetime.now(timezone.utc), status='sent',
                        telegram_chat_id=row['channel_target'], telegram_message_id='manual_confirm',
                        response_status='admin_confirmed'))
            elif action == 'requeue':
                new_status = 'pending'
                last_err = f"manually requeued by {operator}: {note_clean}".strip()
                self.db.execute("""UPDATE alert_outbox SET status='pending', retry_after=NULL, retry_count=0,
                    last_error=?, lease_owner=NULL, lease_until=NULL, updated_at=? WHERE outbox_id=?""",
                    (last_err, now, outbox_id))
            elif action == 'discard':
                new_status = 'suppressed'
                last_err = f"manually discarded by {operator}: {note_clean}".strip()
                self.db.execute("""UPDATE alert_outbox SET status='suppressed', last_error=?,
                    lease_owner=NULL, lease_until=NULL, updated_at=? WHERE outbox_id=?""",
                    (last_err, now, outbox_id))

            self.db.execute("""INSERT INTO outbox_audit_log
                (audit_id, outbox_id, action, operator, previous_status, new_status, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (audit_id, outbox_id, action, operator, prev_status, new_status, note_clean, now))

        return self.get(outbox_id)

    def list_audit_logs(self, outbox_id: str | None = None, limit: int = 100) -> list[dict]:
        if outbox_id:
            rows = self.db.query("""SELECT * FROM outbox_audit_log WHERE outbox_id=?
                ORDER BY created_at DESC, rowid DESC LIMIT ?""", (outbox_id, limit))
        else:
            rows = self.db.query("""SELECT * FROM outbox_audit_log
                ORDER BY created_at DESC, rowid DESC LIMIT ?""", (limit,))
        return [dict(r) for r in rows]

    def finish(self, item, status, error=None, retry_after_seconds=None):
        now = now_iso()
        retry_at = (datetime.now(timezone.utc)+timedelta(seconds=retry_after_seconds)).isoformat() if retry_after_seconds else None
        count = item.retry_count + (1 if status == 'pending' else 0)
        if count >= item.max_retries and status == 'pending': status = 'failed'
        cur = self.db.execute("""UPDATE alert_outbox SET status=?,last_error=?,retry_after=?,retry_count=?,
            lease_owner=NULL,lease_until=NULL,updated_at=?
            WHERE outbox_id=? AND status='sending' AND lease_owner=? AND lease_until>?""",
            (status,error,retry_at,count,now,item.outbox_id,item.lease_owner,now))
        if cur.rowcount != 1: raise RuntimeError('Delivery lease lost')
