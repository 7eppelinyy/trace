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

    def pending(self, limit: int | None = None) -> list[dict]:
        sql = ("SELECT * FROM human_review WHERE status='pending'"
               " ORDER BY created_at DESC")
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        return [dict(r) for r in self.db.query(sql, params)]

    def pending_count(self) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM human_review WHERE status='pending'")
        return int(row["n"]) if row else 0

    def reason_breakdown(self) -> list[tuple[str, int]]:
        """按失败类型聚合待处理项（发现 prompt 退化的主要信号）。

        reason 形如 "stage_a_schema_validation_failed: <详情>"，
        详情里带具体字段名，按冒号前的类型归并才看得出趋势。
        """
        counts: dict[str, int] = {}
        for row in self.pending():
            kind = str(row.get("reason") or "unknown").split(":", 1)[0].strip()
            counts[kind] = counts.get(kind, 0) + 1
        return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)

    def resolve(self, review_id: str) -> bool:
        """标记为已处理。返回是否命中一条 pending 记录。"""
        cur = self.db.execute(
            "UPDATE human_review SET status='resolved'"
            " WHERE review_id=? AND status='pending'", (review_id,))
        return cur.rowcount > 0

    def resolve_all(self) -> int:
        cur = self.db.execute(
            "UPDATE human_review SET status='resolved' WHERE status='pending'")
        return cur.rowcount

    # ------------------------------------------------------------------
    def render(self, limit: int = 10) -> str:
        """人工检查队列摘要（/review 与 CLI 共用同一份文案）。

        这些是 Schema 校验重试后仍然失败的样本 —— 唯一能看出 prompt 或模型
        输出退化的信号，此前只进不出（写进表里没有任何界面能看到）。
        """
        total = self.pending_count()
        lines = ["🔎 人工检查队列", ""]
        if total == 0:
            lines.append("没有待处理项：Stage A/B 的结构化输出全部通过校验。")
            return "\n".join(lines)

        lines.append(f"待处理: {total} 条")
        breakdown = self.reason_breakdown()
        if breakdown:
            lines.append("按类型: " + " ｜ ".join(f"{k} {n}" for k, n in breakdown))
        lines.append("")
        for row in self.pending(limit=limit):
            target = row.get("event_id") or row.get("raw_item_id") or "-"
            created = str(row.get("created_at") or "")[:19]
            lines.append(f"- [{created}] {target}")
            lines.append(f"  {str(row.get('reason') or '')[:200]}")
        if total > limit:
            lines.append(f"…… 另有 {total - limit} 条未显示")
        lines.append("")
        lines.append("这些条目没有进入 Alert 链路（Schema 校验失败不得推送）。")
        lines.append("处理完可用 `python -m trace.main review --resolve <id>` 标记。")
        return "\n".join(lines)
