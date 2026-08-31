"""来源健康状态与采集游标仓库。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from trace.db.connection import Database
from trace.domain.models import (
    HEALTH_BROKEN,
    HEALTH_DEGRADED,
    HEALTH_DISABLED,
    HEALTH_HEALTHY,
    HEALTH_RATE_LIMITED,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def derive_health_status(row: dict, *, enabled: bool = True) -> str:
    """把 source_health 原始行派生为显式状态（任务书 §12）。

    DISABLED      来源被禁用
    RATE_LIMITED  最近一次失败类别为限流（429）—— 不得显示为"无新数据"
    BROKEN        从未成功，或连续失败 >= 3
    DEGRADED      最近一次操作失败（但此前成功过），或成功但长期无数据
    HEALTHY       最近一次成功
    """
    if not enabled:
        return HEALTH_DISABLED
    if not row or not row.get("last_success_at"):
        if row and row.get("last_error_category") == "rate_limited":
            return HEALTH_RATE_LIMITED
        if row and row.get("last_failure_at"):
            return HEALTH_BROKEN
        return HEALTH_DEGRADED
    if row.get("last_error_category") == "rate_limited":
        last_fail = row.get("last_failure_at") or ""
        last_ok = row.get("last_success_at") or ""
        # >= ：回退路线成功后紧跟的 429 记录（时间戳可能同刻）必须
        # 保持 RATE_LIMITED 显式可见，不得被误判为 HEALTHY
        if last_fail >= last_ok:
            return HEALTH_RATE_LIMITED
    if (row.get("consecutive_failures") or 0) >= 3:
        return HEALTH_BROKEN
    if row.get("last_error") and (row.get("last_failure_at") or "") > \
            (row.get("last_success_at") or ""):
        return HEALTH_DEGRADED
    return HEALTH_HEALTHY


class SourceHealthRepo:
    def __init__(self, db: Database):
        self.db = db

    def _ensure(self, source_id: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO source_health (source_id) VALUES (?)", (source_id,))

    def mark_success(self, source_id: str, has_items: bool,
                     last_item_at: str | None = None,
                     http_status: int | None = None) -> None:
        self._ensure(source_id)
        if has_items:
            self.db.execute(
                """UPDATE source_health SET last_success_at=?, consecutive_failures=0,
                       last_error=NULL, last_error_category=NULL,
                       last_item_at=COALESCE(?, last_item_at)
                   WHERE source_id=?""",
                (_now(), last_item_at, source_id))
        else:
            self.db.execute(
                """UPDATE source_health SET last_success_at=?, consecutive_failures=0,
                       last_error=NULL, last_error_category=NULL
                   WHERE source_id=?""",
                (_now(), source_id))
        if http_status is not None:
            self._set_http_status(source_id, http_status)

    def mark_failure(self, source_id: str, error: str, category: str,
                     http_status: int | None = None) -> None:
        self._ensure(source_id)
        self.db.execute(
            """UPDATE source_health SET last_failure_at=?, last_error=?,
                   last_error_category=?, consecutive_failures=consecutive_failures+1
               WHERE source_id=?""",
            (_now(), error[:500], category, source_id))
        if http_status is not None:
            self._set_http_status(source_id, http_status)

    def _set_http_status(self, source_id: str, status: int) -> None:
        """记录最近一次真实 HTTP 状态码（429 必须显式可见）。"""
        try:
            self.db.execute(
                "UPDATE source_health SET last_http_status=? WHERE source_id=?",
                (status, source_id))
        except Exception:
            # migration 0005 之前的库无该列：不阻塞主流程
            pass

    def all(self) -> list[dict]:
        rows = self.db.query("SELECT * FROM source_health ORDER BY source_id")
        return [dict(r) for r in rows]

    def get(self, source_id: str) -> dict | None:
        row = self.db.query_one("SELECT * FROM source_health WHERE source_id=?", (source_id,))
        return dict(row) if row else None


class CursorRepo:
    def __init__(self, db: Database):
        self.db = db

    def get(self, source_id: str) -> dict:
        row = self.db.query_one("SELECT cursor_json FROM collector_cursor WHERE source_id=?",
                                (source_id,))
        return json.loads(row["cursor_json"]) if row else {}

    def set(self, source_id: str, cursor: dict) -> None:
        self.db.execute(
            """INSERT INTO collector_cursor (source_id, cursor_json, updated_at) VALUES (?,?,?)
               ON CONFLICT(source_id) DO UPDATE SET cursor_json=excluded.cursor_json,
                 updated_at=excluded.updated_at""",
            (source_id, json.dumps(cursor), _now()))


class HumanReviewRepo:
    def __init__(self, db: Database):
        self.db = db

    def add(self, review_id: str, reason: str, event_id: str | None = None,
            raw_item_id: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO human_review (review_id, event_id, raw_item_id, reason, created_at)"
            " VALUES (?,?,?,?,?)",
            (review_id, event_id, raw_item_id, reason[:500], _now()))

    def pending(self) -> list[dict]:
        rows = self.db.query("SELECT * FROM human_review WHERE status='pending'")
        return [dict(r) for r in rows]
