"""Versioned opaque pagination cursors; timestamps must not be delimiter-split."""
import base64
import binascii
import json
from datetime import datetime


def encode_cursor(timestamp: str, event_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([1, timestamp, event_id], separators=(',', ':')).encode()).decode().rstrip('=')


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        if len(cursor) > 2048:
            raise ValueError()
        version, timestamp, event_id = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
        dt = datetime.fromisoformat(timestamp)
        if version != 1 or dt.tzinfo is None or not isinstance(event_id, str) or not event_id or len(event_id) > 200:
            raise ValueError()
        return timestamp, event_id
    except (ValueError, TypeError, binascii.Error, UnicodeError) as exc:
        raise ValueError('Invalid pagination cursor') from exc
