"""ID 生成工具：统一事件/证券/提醒等对象的 ID 规则。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def _short_uuid(n: int = 12) -> str:
    return uuid.uuid4().hex[:n]


def event_id() -> str:
    """Event ID，如 EVT-20260825-a1b2c3d4e5f6。"""
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"EVT-{date}-{_short_uuid()}"


def raw_item_id() -> str:
    return f"RAW-{_short_uuid(16)}"


def revision_id() -> str:
    return f"REV-{_short_uuid()}"


def alert_delivery_id() -> str:
    return f"ALD-{_short_uuid(16)}"


def digest_id() -> str:
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"DGST-{date}-{_short_uuid(8)}"


def forecast_check_id() -> str:
    return f"FCK-{_short_uuid()}"
