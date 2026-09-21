"""Database-backed admission control shared by API worker processes."""
import hashlib
import time

from fastapi import HTTPException


def admit(db, key: str, *, limit: int, window_seconds: int = 3600) -> None:
    now = int(time.time())
    window = now // window_seconds * window_seconds
    bucket = hashlib.sha256(key.encode()).hexdigest()
    with db.transaction(mode="IMMEDIATE"):
        db.execute("DELETE FROM request_limit WHERE window_start < ?", (now - 86400,))
        row = db.query_one("SELECT calls FROM request_limit WHERE bucket=? AND window_start=?", (bucket, window))
        if row and row['calls'] >= limit:
            raise HTTPException(429, "RATE_LIMITED", headers={"Retry-After": str(window + window_seconds - now)})
        db.execute("""INSERT INTO request_limit VALUES (?,?,1)
                      ON CONFLICT(bucket,window_start) DO UPDATE SET calls=calls+1""", (bucket, window))
