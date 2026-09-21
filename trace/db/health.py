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


class StagedCursorRepo:
    """缓冲游标写入，直到流水线确认条目已落库。

    采集器的增量游标（seen_accessions / seen_item_ids / etag …）此前在
    collect() 里就直接落库，而 RawItem 的持久化发生在流水线的后续阶段。
    两者之间只要中断 —— LLM 预算耗尽 STOP、生产模式缺 Key、进程重启、
    未捕获异常 —— 这批条目就**既没进库、又被游标永久跳过**，再也采不回来。
    对 SEC 8-K 这种一次性事件来说是不可恢复的数据丢失。

    改成两阶段：collect() 期间的 set() 只写进内存，流水线走完 ingest 循环
    （RawItem 已落库）才 commit()；中途退出则 discard()，下一轮重新采集
    （重复条目由 Level 1 确定性去重零成本拦掉）。

    注意不能简单改成"查 raw_item 判断是否已处理"：游标里还记着
    bootstrap 窗口外、关键词初筛掉的条目 —— 那些是**故意没采**的决策，
    数据库里查不到，丢掉会导致老新闻被重新灌进来。
    """

    def __init__(self, repo: CursorRepo):
        self._repo = repo
        self._pending: dict[str, dict] = {}

    def get(self, source_id: str) -> dict:
        # 本轮内 set 过的以内存值为准（同一轮里 get→set→get 要自洽）
        if source_id in self._pending:
            return json.loads(json.dumps(self._pending[source_id]))
        return self._repo.get(source_id)

    def set(self, source_id: str, cursor: dict) -> None:
        self._pending[source_id] = cursor

    def commit(self) -> int:
        """条目已落库：把本轮游标真正写下去。返回提交的游标数。"""
        count = len(self._pending)
        if count:
            with self._repo.db.transaction():
                for source_id, cursor in self._pending.items():
                    self._repo.set(source_id, cursor)
            self._pending.clear()
        return count

    def discard(self) -> int:
        """本轮中途退出：丢弃未提交的游标，下一轮重新采集。"""
        count = len(self._pending)
        self._pending.clear()
        return count

    @property
    def pending_count(self) -> int:
        return len(self._pending)


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

    def retry(self, review_id: str) -> bool:
        """Explicitly requeue a reviewed payload; resolving alone never fabricates success."""
        with self.db.transaction(mode='IMMEDIATE'):
            review = self.db.query_one('SELECT * FROM human_review WHERE review_id=?', (review_id,))
            if not review:
                return False
            stage = 'stage_a_extract' if review['raw_item_id'] else 'stage_b_analyze'
            target = review['raw_item_id'] or review['event_id']
            if review['raw_item_id'] and not self.db.query_one('SELECT 1 FROM raw_item WHERE raw_item_id=?', (target,)):
                raise ValueError('Original payload is missing; cannot retry')
            if review['event_id'] and not self.db.query_one('SELECT 1 FROM event WHERE event_id=?', (target,)):
                raise ValueError('Original payload is missing; cannot retry')
            job = self.db.query_one("""SELECT job_id FROM processing_job WHERE job_type=? AND target_id=?
                AND status IN ('human_review','dead_letter') ORDER BY input_version DESC LIMIT 1""", (stage,target))
            if not job:
                raise ValueError('No reviewable job found; running/completed jobs cannot be overwritten')
            self.db.execute("""UPDATE processing_job SET status='pending',retry_count=0,last_error=NULL,
                next_retry_at=NULL,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE job_id=?""", (_now(),job['job_id']))
            if review['event_id']:
                self.db.execute('UPDATE event SET needs_human_review=0 WHERE event_id=?', (review['event_id'],))
            self.db.execute("UPDATE human_review SET status='retried' WHERE review_id=?", (review_id,))
            return True

    def get_with_raw(self, review_id: str) -> dict | None:
        """获取人审记录及关联的原始采集内容（Probe 11.2 / T03）。"""
        row = self.db.query_one(
            """SELECT h.*, r.title AS raw_title, r.content AS raw_content,
                      r.source_id AS raw_source_id, r.url AS raw_url,
                      r.published_at AS raw_published_at
               FROM human_review h
               LEFT JOIN raw_item r ON h.raw_item_id = r.raw_item_id
               WHERE h.review_id = ?""",
            (review_id,),
        )
        return dict(row) if row else None

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
