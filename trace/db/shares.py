"""Explicit, expiring, revocable capabilities for immutable public research snapshots."""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone


class ResearchShareRepo:
    def __init__(self, db):
        self.db = db

    def create(self, question_id: str, owner_id: str, snapshot: dict, days: int) -> dict:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=days)
        self.db.execute("""INSERT INTO research_share
            (token_hash,question_id,owner_id,snapshot_json,expires_at,created_at)
            VALUES (?,?,?,?,?,?)""", (hashlib.sha256(token.encode()).hexdigest(), question_id, owner_id,
            json.dumps(snapshot, ensure_ascii=False), expires.isoformat(), now.isoformat()))
        return {"share_token": token, "expires_at": expires.isoformat()}

    def read(self, question_id: str, token: str) -> dict | None:
        row = self.db.query_one("""SELECT snapshot_json FROM research_share
            WHERE token_hash=? AND question_id=? AND revoked_at IS NULL AND expires_at>?""",
            (hashlib.sha256(token.encode()).hexdigest(), question_id, datetime.now(timezone.utc).isoformat()))
        return json.loads(row['snapshot_json']) if row else None

    def revoke(self, question_id: str, owner_id: str) -> None:
        self.db.execute("UPDATE research_share SET revoked_at=? WHERE question_id=? AND owner_id=?",
                        (datetime.now(timezone.utc).isoformat(), question_id, owner_id))
