"""Repository 层：Domain Model <-> SQLite 行记录的映射。

所有业务代码通过 Repository 访问数据，不直接写 SQL，
保证后续迁移 PostgreSQL 时只需要替换本层实现。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from trace.db.connection import Database
from trace.domain.models import (
    AlertDelivery,
    AlertOutbox,
    AlertRule,
    ChannelBinding,
    DailyDigest,
    EntityAlias,
    Event,
    EventImpact,
    EventRevision,
    EventSource,
    ForecastCheck,
    ForecastSnapshot,
    IndustryEdge,
    LicenseMode,
    MarketSnapshot,
    NotificationPreference,
    ProcessingJob,
    RawItem,
    ResearchQuestion,
    ResearchQuestionState,
    Security,
    Source,
    User,
    UserSession,
    WatchlistEntry,
    AlertFeedback,
    SourceAuthorizationRecord,
)


def _dt(v: str | None) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(v)


def _dts(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------

class SourceRepo:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, s: Source) -> None:
        self.db.execute(
            """INSERT INTO source (source_id, source_name, source_type, priority,
                   base_reliability, license_mode, retention_policy, enabled,
                   authority_level, poll_interval_seconds, security_map,
                   can_fetch, can_store, can_display, can_forward, verified_at, verified_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_id) DO UPDATE SET
                 source_name=excluded.source_name, source_type=excluded.source_type,
                 priority=excluded.priority, base_reliability=excluded.base_reliability,
                 license_mode=excluded.license_mode, retention_policy=excluded.retention_policy,
                 enabled=CASE WHEN source.operational_override IS NOT NULL THEN source.enabled ELSE excluded.enabled END,
                 authority_level=excluded.authority_level,
                 poll_interval_seconds=excluded.poll_interval_seconds,
                 security_map=excluded.security_map,
                 can_fetch=excluded.can_fetch,
                 can_store=excluded.can_store,
                 can_display=excluded.can_display,
                 can_forward=excluded.can_forward,
                 verified_at=COALESCE(source.verified_at, excluded.verified_at),
                 verified_by=COALESCE(source.verified_by, excluded.verified_by)""",
            (s.source_id, s.source_name, s.source_type, s.priority, s.base_reliability,
             s.license_mode.value, s.retention_policy, int(s.enabled),
             s.authority_level, s.poll_interval_seconds, json.dumps(s.security_map),
             int(s.can_fetch), int(s.can_store), int(s.can_display), int(s.can_forward),
             s.verified_at, s.verified_by),
        )

    def record_authorization(self, r: SourceAuthorizationRecord) -> None:
        """登记法律/合规授权核验记录，并与技术配置权限明确区分。"""
        self.db.execute(
            """INSERT INTO source_authorization (
                   auth_id, source_id, scope_json, evidence_url_or_file,
                   verified_by, verified_at, expires_at, status, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(auth_id) DO UPDATE SET
                   scope_json=excluded.scope_json,
                   evidence_url_or_file=excluded.evidence_url_or_file,
                   verified_by=excluded.verified_by,
                   verified_at=excluded.verified_at,
                   expires_at=excluded.expires_at,
                   status=excluded.status,
                   notes=excluded.notes""",
            (r.auth_id, r.source_id, json.dumps(r.scope), r.evidence_url_or_file,
             r.verified_by, r.verified_at, r.expires_at, r.status, r.notes, r.created_at or _now()),
        )
        if r.status == "verified":
            self.db.execute(
                "UPDATE source SET verified_at=?, verified_by=? WHERE source_id=?",
                (r.verified_at, r.verified_by, r.source_id),
            )

    def list_authorizations(self, source_id: str | None = None) -> list[SourceAuthorizationRecord]:
        if source_id:
            rows = self.db.query("SELECT * FROM source_authorization WHERE source_id=? ORDER BY created_at DESC", (source_id,))
        else:
            rows = self.db.query("SELECT * FROM source_authorization ORDER BY created_at DESC")
        return [
            SourceAuthorizationRecord(
                auth_id=row["auth_id"],
                source_id=row["source_id"],
                scope=json.loads(row["scope_json"] or "[]"),
                evidence_url_or_file=row["evidence_url_or_file"],
                verified_by=row["verified_by"],
                verified_at=row["verified_at"],
                expires_at=row["expires_at"],
                status=row["status"],
                notes=row["notes"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def get_active_authorization(self, source_id: str) -> SourceAuthorizationRecord | None:
        row = self.db.query_one(
            """SELECT * FROM source_authorization
               WHERE source_id=? AND status='verified'
                 AND (expires_at IS NULL OR expires_at > datetime('now'))
               ORDER BY verified_at DESC LIMIT 1""",
            (source_id,),
        )
        if not row:
            return None
        return SourceAuthorizationRecord(
            auth_id=row["auth_id"],
            source_id=row["source_id"],
            scope=json.loads(row["scope_json"] or "[]"),
            evidence_url_or_file=row["evidence_url_or_file"],
            verified_by=row["verified_by"],
            verified_at=row["verified_at"],
            expires_at=row["expires_at"],
            status=row["status"],
            notes=row["notes"],
            created_at=row["created_at"],
        )

    def set_operational_override(
        self,
        source_id: str,
        enabled: bool | None,
        reason: str = "",
        updated_by: str = "",
    ) -> None:
        """设置运营 override（F30：启动或 seed 更新绝不覆盖运营手工停用/启用）。"""
        override_val = int(enabled) if enabled is not None else None
        now_str = _now()
        self.db.execute(
            """UPDATE source SET
                 operational_override=?,
                 override_reason=?,
                 override_updated_at=?,
                 override_updated_by=?
               WHERE source_id=?""",
            (override_val, reason, now_str, updated_by, source_id),
        )

    def get(self, source_id: str) -> Source | None:
        row = self.db.query_one("SELECT * FROM source WHERE source_id=?", (source_id,))
        return self._to_obj(row) if row else None

    def get_by_name(self, name: str) -> Source | None:
        row = self.db.query_one("SELECT * FROM source WHERE source_name=?", (name,))
        return self._to_obj(row) if row else None

    def list_enabled(self) -> list[Source]:
        """按有效启用状态过滤（支持运营 override 优先级覆盖）。"""
        return [self._to_obj(r) for r in self.db.query(
            "SELECT * FROM source WHERE (operational_override = 1 OR (operational_override IS NULL AND enabled = 1))"
        )]

    def list_all(self) -> list[Source]:
        """全部来源（含禁用）：doctor 显示 DISABLED 状态需要完整列表。"""
        return [self._to_obj(r) for r in self.db.query(
            "SELECT * FROM source ORDER BY source_id")]

    def _to_obj(self, r) -> Source:
        keys = set(r.keys())
        raw_enabled = bool(r["enabled"])
        raw_override = (bool(r["operational_override"]) if "operational_override" in keys and r["operational_override"] is not None else None)
        effective_enabled = (raw_override if raw_override is not None else raw_enabled)

        return Source(
            source_id=r["source_id"], source_name=r["source_name"], source_type=r["source_type"],
            priority=r["priority"], base_reliability=r["base_reliability"],
            license_mode=LicenseMode(r["license_mode"]), retention_policy=r["retention_policy"],
            enabled=effective_enabled,
            seed_enabled=raw_enabled,
            authority_level=r["authority_level"] if "authority_level" in keys else "",
            poll_interval_seconds=(r["poll_interval_seconds"]
                                   if "poll_interval_seconds" in keys else 600),
            security_map=(json.loads(r["security_map"] or "[]")
                          if "security_map" in keys else []),
            can_fetch=bool(r["can_fetch"]) if "can_fetch" in keys else True,
            can_store=bool(r["can_store"]) if "can_store" in keys else True,
            can_display=bool(r["can_display"]) if "can_display" in keys else True,
            can_forward=bool(r["can_forward"]) if "can_forward" in keys else True,
            verified_at=r["verified_at"] if "verified_at" in keys else None,
            verified_by=r["verified_by"] if "verified_by" in keys else None,
            operational_override=raw_override,
            override_reason=r["override_reason"] if "override_reason" in keys and r["override_reason"] else "",
            override_updated_at=r["override_updated_at"] if "override_updated_at" in keys else None,
            override_updated_by=r["override_updated_by"] if "override_updated_by" in keys else None,
        )


class RawItemRepo:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, item: RawItem) -> None:
        self.db.execute(
            """INSERT OR IGNORE INTO raw_item (raw_item_id, source_id, source_item_id, title, url,
                   canonical_url, published_at, fetched_at, language, content, reference,
                   title_hash, content_hash, event_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item.raw_item_id, item.source_id, item.source_item_id, item.title, item.url,
             item.canonical_url, _dts(item.published_at), _dts(item.fetched_at), item.language,
             item.content, item.reference, item.title_hash, item.content_hash, item.event_id),
        )

    def exists_same_source_item(self, source_id: str, source_item_id: str) -> bool:
        row = self.db.query_one(
            "SELECT 1 FROM raw_item WHERE source_id=? AND source_item_id=?",
            (source_id, source_item_id),
        )
        return row is not None

    def find_by_source_item(self, source_id: str, source_item_id: str) -> RawItem | None:
        row = self.db.query_one(
            "SELECT * FROM raw_item WHERE source_id=? AND source_item_id=? ORDER BY fetched_at DESC LIMIT 1",
            (source_id, source_item_id),
        )
        return self._to_obj(row) if row else None

    def exists_canonical_url(self, canonical_url: str) -> bool:
        if not canonical_url:
            return False
        row = self.db.query_one(
            "SELECT 1 FROM raw_item WHERE canonical_url=?", (canonical_url,))
        return row is not None

    def find_by_canonical_url(self, canonical_url: str) -> RawItem | None:
        if not canonical_url:
            return None
        row = self.db.query_one(
            "SELECT * FROM raw_item WHERE canonical_url=? ORDER BY fetched_at DESC LIMIT 1",
            (canonical_url,),
        )
        return self._to_obj(row) if row else None

    def exists_title_hash(self, title_hash: str) -> bool:
        row = self.db.query_one("SELECT 1 FROM raw_item WHERE title_hash=?", (title_hash,))
        return row is not None

    def find_by_title_hash(self, title_hash: str) -> list[RawItem]:
        if not title_hash:
            return []
        rows = self.db.query("SELECT * FROM raw_item WHERE title_hash=?", (title_hash,))
        return [self._to_obj(r) for r in rows]

    def exists_content_hash(self, content_hash: str) -> bool:
        if not content_hash:
            return False
        row = self.db.query_one("SELECT 1 FROM raw_item WHERE content_hash=?", (content_hash,))
        return row is not None

    def find_by_content_hash(self, content_hash: str) -> RawItem | None:
        if not content_hash:
            return None
        row = self.db.query_one(
            "SELECT * FROM raw_item WHERE content_hash=? ORDER BY fetched_at DESC LIMIT 1",
            (content_hash,),
        )
        return self._to_obj(row) if row else None

    def exists(self, raw_item_id: str) -> bool:
        row = self.db.query_one(
            "SELECT 1 FROM raw_item WHERE raw_item_id=?", (raw_item_id,))
        return row is not None

    def link_event(self, raw_item_id: str, event_id: str) -> None:
        self.db.execute("UPDATE raw_item SET event_id=? WHERE raw_item_id=?", (event_id, raw_item_id))

    def get(self, raw_item_id: str) -> RawItem | None:
        row = self.db.query_one("SELECT * FROM raw_item WHERE raw_item_id=?", (raw_item_id,))
        return self._to_obj(row) if row else None

    def list_by_event(self, event_id: str) -> list[RawItem]:
        rows = self.db.query("SELECT * FROM raw_item WHERE event_id=? ORDER BY published_at", (event_id,))
        return [self._to_obj(r) for r in rows]

    def urls_by_events(self, event_ids: list[str], *,
                       per_event: int = 3) -> dict[str, list[str]]:
        """批量取多个事件的原文链接（/ask 展示证据用，避免逐事件一次查询）。"""
        ids = [e for e in dict.fromkeys(event_ids) if e]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.db.query(
            f"""SELECT COALESCE(es.event_id, r.event_id) AS matched_event_id, r.url
                FROM raw_item r
                LEFT JOIN event_source es ON es.raw_item_id = r.raw_item_id
                WHERE (r.event_id IN ({placeholders}) OR es.event_id IN ({placeholders}))
                  AND r.url IS NOT NULL AND r.url <> ''
                ORDER BY r.published_at""", tuple(ids) + tuple(ids))
        out: dict[str, list[str]] = {}
        for r in rows:
            eid = r["matched_event_id"]
            if not eid:
                continue
            bucket = out.setdefault(eid, [])
            if len(bucket) < per_event and r["url"] not in bucket:
                bucket.append(r["url"])
        return out

    def _to_obj(self, r) -> RawItem:
        return RawItem(
            raw_item_id=r["raw_item_id"], source_id=r["source_id"], source_item_id=r["source_item_id"],
            title=r["title"], url=r["url"], canonical_url=r["canonical_url"],
            published_at=_dt(r["published_at"]), fetched_at=_dt(r["fetched_at"]),
            language=r["language"], content=r["content"], reference=r["reference"],
            title_hash=r["title_hash"], content_hash=r["content_hash"], event_id=r["event_id"],
        )


class EventRepo:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, e: Event) -> None:
        self.db.execute(
            """INSERT INTO event (event_id, title, summary, event_type, status, version,
                   first_seen_at, last_updated_at, event_time, language, first_source_id,
                   primary_source_id, all_source_ids, material_update, needs_human_review,
                   key_numbers, title_embedding, summary_embedding, embedding_model)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (e.event_id, e.title, e.summary, e.event_type, e.status, e.version,
             _dts(e.first_seen_at), _dts(e.last_updated_at), _dts(e.event_time), e.language,
             e.first_source_id, e.primary_source_id, json.dumps(e.all_source_ids),
             int(e.material_update), int(e.needs_human_review),
             json.dumps(e.key_numbers, ensure_ascii=False),
             e.title_embedding, e.summary_embedding, e.embedding_model),
        )

    def update(self, e: Event) -> None:
        self.db.execute(
            """UPDATE event SET title=?, summary=?, event_type=?, status=?, version=?,
                   last_updated_at=?, event_time=?, language=?, first_source_id=?,
                   primary_source_id=?, all_source_ids=?, material_update=?, needs_human_review=?,
                   key_numbers=?, title_embedding=?, summary_embedding=?, embedding_model=?
               WHERE event_id=?""",
            (e.title, e.summary, e.event_type, e.status, e.version,
             _dts(e.last_updated_at), _dts(e.event_time), e.language,
             e.first_source_id, e.primary_source_id, json.dumps(e.all_source_ids),
             int(e.material_update), int(e.needs_human_review),
             json.dumps(e.key_numbers, ensure_ascii=False),
             e.title_embedding, e.summary_embedding, e.embedding_model, e.event_id),
        )

    def get(self, event_id: str) -> Event | None:
        row = self.db.query_one("SELECT * FROM event WHERE event_id=?", (event_id,))
        return self._to_obj(row) if row else None

    def get_many(self, event_ids: list[str]) -> dict[str, Event]:
        """批量取事件（避免逐 impact 一次 get 的 N+1）。"""
        ids = [e for e in dict.fromkeys(event_ids) if e]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.db.query(
            f"SELECT * FROM event WHERE event_id IN ({placeholders})", tuple(ids))
        return {r["event_id"]: self._to_obj(r) for r in rows}

    def update_embeddings(self, event_id: str, title_embedding: bytes,
                          summary_embedding: bytes, embedding_model: str | None = None) -> None:
        """定向补写向量列（懒回填），不触碰 last_updated_at / version。"""
        self.db.execute(
            "UPDATE event SET title_embedding=?, summary_embedding=?, embedding_model=? WHERE event_id=?",
            (title_embedding, summary_embedding, embedding_model, event_id))

    def recent(self, hours: int = 72, limit: int = 500) -> list[Event]:
        rows = self.db.query(
            """SELECT * FROM event
               WHERE last_updated_at >= datetime('now', ?)
               ORDER BY last_updated_at DESC LIMIT ?""",
            (f"-{hours} hours", limit),
        )
        return [self._to_obj(r) for r in rows]

    def top_between(self, start_utc: datetime, end_utc: datetime,
                    limit: int = 20) -> list[Event]:
        """[start_utc, end_utc) 区间内按 final_score 最高的事件。

        刻意接受 UTC 区间而不是 date(first_seen_at)=？：first_seen_at 存的是
        UTC，而"今天"是用户时区的概念。两者直接比较会丢掉本地日凌晨那几个
        小时的事件 —— 对 Asia/Taipei(+8) 就是当地 00:00–08:00，正好覆盖
        美股收盘到盘后，是这套系统最不该漏的窗口。
        """
        rows = self.db.query(
            """SELECT e.*, MAX(i.final_score) AS top_score
               FROM event e JOIN event_impact i ON i.event_id = e.event_id
               WHERE e.first_seen_at >= ? AND e.first_seen_at < ?
               GROUP BY e.event_id ORDER BY top_score DESC LIMIT ?""",
            (_dts(start_utc), _dts(end_utc), limit),
        )
        return [self._to_obj(r) for r in rows]

    def paginate_events(
        self,
        *,
        page: int = 1,
        limit: int = 20,
        market: str | None = None,
        min_score: float | None = None,
        snapshot_ts: str | None = None,
        cursor: str | None = None,
        enforce_display: bool = False,
    ) -> tuple[list[Event], int, str]:
        """SQL级过滤、快照定界与稳定分页（T11 / F13 / F24）。

        返回：(当前页事件列表, 符合条件总数, 快照时间戳)
        """
        now_ts = _now()
        eff_snapshot_ts = snapshot_ts or now_ts

        target_markets = [m.strip().upper() for m in market.split(",") if m.strip()] if market else None

        where_clauses = ["e.first_seen_at <= ?", """NOT EXISTS (
            SELECT 1 FROM raw_item rp LEFT JOIN event_source ep ON ep.raw_item_id=rp.raw_item_id
            LEFT JOIN source sp ON sp.source_id=rp.source_id
            WHERE (rp.event_id=e.event_id OR ep.event_id=e.event_id) AND COALESCE(sp.can_display,0)=0)
            AND (EXISTS (SELECT 1 FROM raw_item rp LEFT JOIN event_source ep ON ep.raw_item_id=rp.raw_item_id
                         WHERE rp.event_id=e.event_id OR ep.event_id=e.event_id)
                 OR EXISTS (SELECT 1 FROM source sp WHERE sp.source_id=e.first_source_id AND sp.can_display=1))"""]
        if not enforce_display:
            where_clauses = where_clauses[:1]
        params: list[Any] = [eff_snapshot_ts]

        if cursor:
            from trace.common.cursors import decode_cursor
            c_ts, c_eid = decode_cursor(cursor)
            where_clauses.append("(e.first_seen_at < ? OR (e.first_seen_at = ? AND e.event_id < ?))")
            params.extend([c_ts, c_ts, c_eid])

        join_impact = (min_score is not None) or bool(target_markets)
        join_sql = ""
        if join_impact:
            join_sql = " JOIN event_impact i ON e.event_id = i.event_id"
            if min_score is not None:
                where_clauses.append("i.final_score >= ?")
                params.append(min_score)
            if target_markets:
                join_sql += " JOIN security s ON i.security_id = s.security_id"
                m_placeholders = ",".join("?" * len(target_markets))
                where_clauses.append(f"s.market IN ({m_placeholders})")
                params.extend(target_markets)

        where_str = " AND ".join(where_clauses)

        # 1. 统计总数 (COUNT)
        # 若为游标分页 (cursor 模式)，无须计算全表/余量总数，避免大表扫描破坏游标 O(1) 性能
        if cursor:
            total = 0
        else:
            if join_sql:
                count_sql = f"SELECT COUNT(DISTINCT e.event_id) AS total FROM event e{join_sql} WHERE {where_str}"
            else:
                count_sql = f"SELECT COUNT(*) AS total FROM event e WHERE {where_str}"
            row = self.db.query_one(count_sql, tuple(params))
            total = row["total"] if row else 0

        # 2. 获取当前页的 event_id
        offset = max(0, (page - 1) * limit) if not cursor else 0
        order_sql = "ORDER BY e.first_seen_at DESC, e.event_id DESC"
        distinct_kw = "DISTINCT " if join_sql else ""
        query_sql = (
            f"SELECT {distinct_kw}e.event_id, e.first_seen_at FROM event e{join_sql} "
            f"WHERE {where_str} {order_sql} LIMIT ? OFFSET ?"
        )
        page_params = tuple(params + [limit, offset])
        id_rows = self.db.query(query_sql, page_params)
        page_event_ids = [r["event_id"] for r in id_rows]

        # 3. 批量拉取当前页完整 Event 对象并保持分页顺序
        events_dict = self.get_many(page_event_ids)
        page_events = [events_dict[eid] for eid in page_event_ids if eid in events_dict]

        return page_events, total, eff_snapshot_ts

    def _to_obj(self, r) -> Event:
        return Event(
            event_id=r["event_id"], title=r["title"], summary=r["summary"],
            event_type=r["event_type"], status=r["status"], version=r["version"],
            first_seen_at=_dt(r["first_seen_at"]), last_updated_at=_dt(r["last_updated_at"]),
            event_time=_dt(r["event_time"]), language=r["language"],
            first_source_id=r["first_source_id"], primary_source_id=r["primary_source_id"],
            all_source_ids=json.loads(r["all_source_ids"] or "[]"),
            material_update=bool(r["material_update"]),
            needs_human_review=bool(r["needs_human_review"]),
            # top_of_day 等查询走 SELECT e.*，列存在；防御旧行/投影缺列
            key_numbers=json.loads(
                (r["key_numbers"] if "key_numbers" in r.keys() else None) or "[]"),
            title_embedding=r["title_embedding"], summary_embedding=r["summary_embedding"],
            embedding_model=r['embedding_model'] if 'embedding_model' in r.keys() else None,
        )


class EventSourceRepo:
    def __init__(self, db: Database):
        self.db = db

    def add(self, es: EventSource) -> None:
        self.db.execute(
            """INSERT INTO event_source (event_id, raw_item_id, role) VALUES (?,?,?)
               ON CONFLICT(event_id, raw_item_id) DO UPDATE SET role=excluded.role""",
            (es.event_id, es.raw_item_id, es.role),
        )

    def list_by_event(self, event_id: str) -> list[EventSource]:
        rows = self.db.query("SELECT * FROM event_source WHERE event_id=?", (event_id,))
        return [EventSource(event_id=r["event_id"], raw_item_id=r["raw_item_id"], role=r["role"])
                for r in rows]


class EventRevisionRepo:
    def __init__(self, db: Database):
        self.db = db

    def add(self, rev: EventRevision) -> None:
        self.db.execute(
            """INSERT INTO event_revision (revision_id, event_id, version, revision_type,
                   material_update, note, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (rev.revision_id, rev.event_id, rev.version, rev.revision_type,
             int(rev.material_update), rev.note, rev.created_at and rev.created_at.isoformat() or _now()),
        )

    def list_by_event(self, event_id: str) -> list[EventRevision]:
        rows = self.db.query(
            "SELECT * FROM event_revision WHERE event_id=? ORDER BY version", (event_id,))
        return [EventRevision(revision_id=r["revision_id"], event_id=r["event_id"],
                              version=r["version"], revision_type=r["revision_type"],
                              material_update=bool(r["material_update"]), note=r["note"],
                              created_at=_dt(r["created_at"])) for r in rows]


class SecurityRepo:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, s: Security) -> None:
        self.db.execute(
            """INSERT INTO security (security_id, market, exchange, ticker, company_name_zh,
                   company_name_en, cik, aliases, products, industry_tags, graph_node_ids,
                   is_watchlist_default, is_context_universe, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET
                 market=excluded.market, exchange=excluded.exchange,
                 company_name_zh=excluded.company_name_zh, company_name_en=excluded.company_name_en,
                 cik=excluded.cik, aliases=excluded.aliases, products=excluded.products,
                 industry_tags=excluded.industry_tags, graph_node_ids=excluded.graph_node_ids,
                 is_watchlist_default=excluded.is_watchlist_default,
                 is_context_universe=excluded.is_context_universe,
                 status=CASE
                   WHEN security.status = 'delisted' THEN 'delisted'
                   WHEN security.status = 'verified' AND excluded.status = 'unverified' THEN 'verified'
                   ELSE excluded.status
                 END""",
            (s.security_id, s.market, s.exchange, s.ticker, s.company_name_zh, s.company_name_en,
             s.cik, json.dumps(s.aliases), json.dumps(s.products), json.dumps(s.industry_tags),
             json.dumps(s.graph_node_ids), int(s.is_watchlist_default), int(s.is_context_universe),
             s.status),
        )

    def mark_status(self, security_id: str, status: str) -> None:
        """更新证券状态（如 verified / unverified / delisted）。"""
        self.db.execute("UPDATE security SET status=? WHERE security_id=?", (status, security_id))

    def get_by_ticker(self, ticker: str) -> Security | None:
        row = self.db.query_one("SELECT * FROM security WHERE ticker=?", (ticker,))
        return self._to_obj(row) if row else None

    def get(self, security_id: str) -> Security | None:
        row = self.db.query_one("SELECT * FROM security WHERE security_id=?", (security_id,))
        return self._to_obj(row) if row else None

    def find_by_alias(self, name: str) -> Security | None:
        """按公司名/别名模糊匹配（用于实体识别兜底）。"""
        rows = self.db.query("SELECT * FROM security")
        needle = name.strip().lower()
        for r in rows:
            s = self._to_obj(r)
            candidates = {s.ticker.lower(), s.company_name_en.lower(), s.company_name_zh}
            candidates.update(a.lower() for a in s.aliases)
            if needle in candidates:
                return s
        return None

    def list_all(self) -> list[Security]:
        return [self._to_obj(r) for r in self.db.query("SELECT * FROM security")]

    def list_watchlist_defaults(self) -> list[Security]:
        rows = self.db.query("SELECT * FROM security WHERE is_watchlist_default=1")
        return [self._to_obj(r) for r in rows]

    def _to_obj(self, r) -> Security:
        keys = set(r.keys())
        return Security(
            security_id=r["security_id"], market=r["market"], exchange=r["exchange"],
            ticker=r["ticker"], company_name_zh=r["company_name_zh"],
            company_name_en=r["company_name_en"], cik=r["cik"],
            aliases=json.loads(r["aliases"] or "[]"), products=json.loads(r["products"] or "[]"),
            industry_tags=json.loads(r["industry_tags"] or "[]"),
            graph_node_ids=json.loads(r["graph_node_ids"] or "[]"),
            is_watchlist_default=bool(r["is_watchlist_default"]),
            is_context_universe=bool(r["is_context_universe"]),
            status=r["status"] if "status" in keys and r["status"] else "verified",
        )


class EntityAliasRepo:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, e: EntityAlias) -> None:
        self.db.execute(
            """INSERT INTO entity_alias (entity_id, name, aliases, entity_type, security_id)
               VALUES (?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET aliases=excluded.aliases,
                 entity_type=excluded.entity_type, security_id=excluded.security_id""",
            (e.entity_id, e.name, json.dumps(e.aliases), e.entity_type, e.security_id),
        )

    def all_with_aliases(self) -> list[EntityAlias]:
        rows = self.db.query("SELECT * FROM entity_alias")
        return [EntityAlias(entity_id=r["entity_id"], name=r["name"],
                            aliases=json.loads(r["aliases"] or "[]"),
                            entity_type=r["entity_type"], security_id=r["security_id"])
                for r in rows]


class IndustryEdgeRepo:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, e: IndustryEdge) -> None:
        self.db.execute(
            """INSERT INTO industry_edge (edge_id, from_node, to_node, edge_type, confidence,
                   evidence_source, valid_from, valid_to, direction_rule)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(from_node, to_node, edge_type) DO UPDATE SET
                 confidence=excluded.confidence, evidence_source=excluded.evidence_source,
                 valid_from=excluded.valid_from, valid_to=excluded.valid_to,
                 direction_rule=excluded.direction_rule""",
            (e.edge_id, e.from_node, e.to_node, e.edge_type, e.confidence, e.evidence_source,
             _dts(e.valid_from), _dts(e.valid_to), e.direction_rule),
        )

    def list_all(self) -> list[IndustryEdge]:
        rows = self.db.query(
            "SELECT * FROM industry_edge WHERE (valid_to IS NULL OR valid_to >= datetime('now'))")
        return [IndustryEdge(edge_id=r["edge_id"], from_node=r["from_node"], to_node=r["to_node"],
                             edge_type=r["edge_type"], confidence=r["confidence"],
                             evidence_source=r["evidence_source"], valid_from=_dt(r["valid_from"]),
                             valid_to=_dt(r["valid_to"]), direction_rule=r["direction_rule"])
                for r in rows]


class EventImpactRepo:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, i: EventImpact) -> None:
        self.db.execute(
            """INSERT INTO event_impact (impact_id, event_id, security_id, direction, directness,
                   magnitude, persistence, directness_score, confidence, reason, industry_path,
                   evidence_ids, source_reliability, base_score, market_confirmation, final_score,
                   analysis_mode, market_data_mode, next_eligible_at, expires_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_id, security_id) DO UPDATE SET
                 direction=excluded.direction, directness=excluded.directness,
                 magnitude=excluded.magnitude, persistence=excluded.persistence,
                 directness_score=excluded.directness_score, confidence=excluded.confidence,
                 reason=excluded.reason, industry_path=excluded.industry_path,
                 evidence_ids=excluded.evidence_ids, source_reliability=excluded.source_reliability,
                 base_score=excluded.base_score, market_confirmation=excluded.market_confirmation,
                 final_score=excluded.final_score, analysis_mode=excluded.analysis_mode,
                 market_data_mode=excluded.market_data_mode,
                 next_eligible_at=excluded.next_eligible_at,
                 expires_at=excluded.expires_at""",
            (i.impact_id, i.event_id, i.security_id, i.direction, i.directness,
             i.magnitude, i.persistence, i.directness_score, i.confidence, i.reason,
             i.industry_path, json.dumps(i.evidence_ids), i.source_reliability,
             i.base_score, i.market_confirmation, i.final_score,
             i.analysis_mode, i.market_data_mode,
             _dts(i.next_eligible_at), _dts(i.expires_at),
             i.created_at and i.created_at.isoformat() or _now()),
        )

    def list_by_event(self, event_id: str) -> list[EventImpact]:
        rows = self.db.query("SELECT * FROM event_impact WHERE event_id=?", (event_id,))
        return [self._to_obj(r) for r in rows]

    def list_by_events(self, event_ids: list[str]) -> dict[str, list[EventImpact]]:
        """批量获取多个事件的影响列表（解决 N+1 查询问题）。"""
        if not event_ids:
            return {}
        ids = list(dict.fromkeys(event_ids))
        placeholders = ",".join("?" * len(ids))
        rows = self.db.query(
            f"SELECT * FROM event_impact WHERE event_id IN ({placeholders})",
            tuple(ids),
        )
        res: dict[str, list[EventImpact]] = {eid: [] for eid in ids}
        for r in rows:
            obj = self._to_obj(r)
            if obj.event_id in res:
                res[obj.event_id].append(obj)
        return res

    def list_pending_confirmation(self, modes: list[str], *,
                                  since_hours: float = 48.0,
                                  limit: int = 200) -> list[EventImpact]:
        """取回当时拿不到市场确认的影响（等开盘后重算用）。

        支持 next_eligible_at 交易日历调度感知（解决 F17 跨周末补算漏掉），
        同时保留历史记录与缺省调度时间的向后兼容。
        """
        if not modes:
            return []
        placeholders = ",".join("?" * len(modes))
        rows = self.db.query(
            f"""SELECT i.* FROM event_impact i
                JOIN event e ON e.event_id = i.event_id
                WHERE i.market_data_mode IN ({placeholders})
                  AND (
                    (i.next_eligible_at IS NOT NULL AND i.next_eligible_at <= datetime('now')
                     AND (i.expires_at IS NULL OR i.expires_at >= datetime('now')))
                    OR
                    (i.next_eligible_at IS NULL AND COALESCE(e.event_time, e.first_seen_at) >= datetime('now', ?))
                  )
                ORDER BY i.final_score DESC LIMIT ?""",
            (*modes, f"-{float(since_hours)} hours", limit))
        return [self._to_obj(r) for r in rows]

    def expire_stale_pending_confirmations(self) -> int:
        """将已超过反应窗口截止时间 (expires_at < now) 的未开盘/无行情影响标记为 reaction_window_expired。"""
        cur = self.db.execute(
            """UPDATE event_impact
               SET market_data_mode = 'reaction_window_expired'
               WHERE expires_at IS NOT NULL
                 AND expires_at < datetime('now')
                 AND market_data_mode IN ('market_not_opened_since_event', 'no_quote')"""
        )
        return cur.rowcount

    def exists_for_event(self, event_id: str) -> bool:
        """该事件是否已有分析结果（判断是否需要再跑 Stage B）。"""
        return self.db.query_one(
            "SELECT 1 FROM event_impact WHERE event_id=? LIMIT 1",
            (event_id,)) is not None

    def list_by_security(self, security_id: str, since: str | None = None) -> list[EventImpact]:
        sql = "SELECT * FROM event_impact WHERE security_id=?"
        params: tuple = (security_id,)
        if since:
            sql += " AND created_at >= ?"
            params += (since,)
        return [self._to_obj(r) for r in self.db.query(sql + " ORDER BY created_at DESC", params)]

    def get(self, event_id: str, security_id: str) -> EventImpact | None:
        row = self.db.query_one(
            "SELECT * FROM event_impact WHERE event_id=? AND security_id=?",
            (event_id, security_id))
        return self._to_obj(row) if row else None

    def list_by_securities(self, security_ids: list[str], *,
                           per_security: int = 20) -> dict[str, list[EventImpact]]:
        """一次取回多个证券的影响，按 security_id 分组（每组最多 per_security 条）。

        /ask 的图谱扩展会命中几十个证券，逐个 list_by_security 就是 N 次查询。
        排序与 list_by_security 一致（created_at DESC），保证"最近 N 条"语义。
        """
        ids = [s for s in dict.fromkeys(security_ids) if s]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.db.query(
            f"""SELECT * FROM event_impact WHERE security_id IN ({placeholders})
                ORDER BY created_at DESC""", tuple(ids))
        out: dict[str, list[EventImpact]] = {}
        for r in rows:
            bucket = out.setdefault(r["security_id"], [])
            if len(bucket) < per_security:
                bucket.append(self._to_obj(r))
        return out

    def _to_obj(self, r) -> EventImpact:
        keys = r.keys() if hasattr(r, "keys") else []
        return EventImpact(
            impact_id=r["impact_id"], event_id=r["event_id"], security_id=r["security_id"],
            direction=r["direction"], directness=r["directness"],
            magnitude=r["magnitude"], persistence=r["persistence"],
            directness_score=r["directness_score"], confidence=r["confidence"],
            reason=r["reason"], industry_path=r["industry_path"],
            evidence_ids=json.loads(r["evidence_ids"] or "[]"),
            source_reliability=r["source_reliability"], base_score=r["base_score"],
            market_confirmation=r["market_confirmation"], final_score=r["final_score"],
            analysis_mode=r["analysis_mode"], market_data_mode=r["market_data_mode"],
            next_eligible_at=_dt(r["next_eligible_at"]) if "next_eligible_at" in keys and r["next_eligible_at"] else None,
            expires_at=_dt(r["expires_at"]) if "expires_at" in keys and r["expires_at"] else None,
            created_at=_dt(r["created_at"]),
        )


class MarketSnapshotRepo:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, s: MarketSnapshot) -> None:
        self.db.execute(
            """INSERT INTO market_snapshot (security_id, ts, last_price, prev_close,
                   change_pct_day, change_pct_1m, change_pct_5m, change_pct_15m,
                   volume, volume_ratio, session, currency, source, is_delayed, market_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(security_id, ts) DO UPDATE SET
                 last_price=excluded.last_price, prev_close=excluded.prev_close,
                 change_pct_day=excluded.change_pct_day,
                 change_pct_1m=excluded.change_pct_1m, change_pct_5m=excluded.change_pct_5m,
                 change_pct_15m=excluded.change_pct_15m, volume=excluded.volume,
                 volume_ratio=excluded.volume_ratio, session=excluded.session,
                 currency=excluded.currency, source=excluded.source,
                 is_delayed=excluded.is_delayed, market_ts=excluded.market_ts""",
            (s.security_id, _dts(s.ts), s.last_price, s.prev_close,
             s.change_pct_day, s.change_pct_1m, s.change_pct_5m, s.change_pct_15m,
             s.volume, s.volume_ratio, s.session, s.currency, s.source,
             int(s.is_delayed), _dts(s.market_ts)),
        )

    def latest(self, security_id: str) -> MarketSnapshot | None:
        row = self.db.query_one(
            "SELECT * FROM market_snapshot WHERE security_id=? ORDER BY ts DESC LIMIT 1",
            (security_id,))
        return self._to_obj(row) if row else None

    def nearest(self, security_id: str, ts: datetime,
                max_delta_hours: float) -> MarketSnapshot | None:
        """离指定时刻最近的快照（超出 max_delta_hours 返回 None）。

        回测账本用它取事件锚点价：拿不到就是拿不到，不得用别的时刻的价格
        冒充事件时刻的价格。
        """
        target = _dts(ts)
        if not target:
            return None
        row = self.db.query_one(
            """SELECT *, ABS(julianday(ts) - julianday(?)) * 24.0 AS delta_hours
               FROM market_snapshot
               WHERE security_id=?
                 AND ABS(julianday(ts) - julianday(?)) * 24.0 <= ?
               ORDER BY delta_hours LIMIT 1""",
            (target, security_id, target, float(max_delta_hours)))
        return self._to_obj(row) if row else None

    def _to_obj(self, r) -> MarketSnapshot:
        keys = r.keys() if hasattr(r, "keys") else []
        return MarketSnapshot(
            security_id=r["security_id"], ts=_dt(r["ts"]), last_price=r["last_price"],
            prev_close=r["prev_close"],
            change_pct_day=r["change_pct_day"] if "change_pct_day" in keys else None,
            change_pct_1m=r["change_pct_1m"],
            change_pct_5m=r["change_pct_5m"], change_pct_15m=r["change_pct_15m"],
            volume=r["volume"], volume_ratio=r["volume_ratio"], session=r["session"],
            currency=r["currency"] if "currency" in keys and r["currency"] else "USD",
            source=r["source"] if "source" in keys and r["source"] else "",
            is_delayed=bool(r["is_delayed"]) if "is_delayed" in keys else False,
            market_ts=_dt(r["market_ts"]) if "market_ts" in keys and r["market_ts"] else None,
        )


class UserRepo:
    def __init__(self, db: Database):
        self.db = db

    def ensure(self, user_id: str, timezone: str = "Asia/Taipei") -> bool:
        """幂等创建用户。首次创建返回 True（登记 alert_activation_at 并允许初始分配），已存在返回 False。"""
        cur = self.db.execute(
            """INSERT INTO user (user_id, timezone, created_at, alert_activation_at)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id) DO NOTHING""",
            (user_id, timezone, _now(), _now()),
        )
        is_new = cur.rowcount > 0
        return is_new

    def get(self, user_id: str) -> User | None:
        row = self.db.query_one("SELECT * FROM user WHERE user_id=?", (user_id,))
        if not row:
            return None
        keys = row.keys()
        return User(user_id=row["user_id"], timezone=row["timezone"],
                    muted_until=_dt(row["muted_until"]), created_at=_dt(row["created_at"]),
                    alert_activation_at=_dt(row["alert_activation_at"])
                    if "alert_activation_at" in keys else None)

    def set_timezone(self, user_id: str, tz: str) -> None:
        self.db.execute("UPDATE user SET timezone=? WHERE user_id=?", (tz, user_id))

    def set_mute(self, user_id: str, muted_until: datetime | None) -> None:
        self.db.execute("UPDATE user SET muted_until=? WHERE user_id=?",
                        (_dts(muted_until), user_id))

    def all_users(self) -> list[User]:
        rows = self.db.query("SELECT * FROM user")
        out: list[User] = []
        for r in rows:
            keys = r.keys()
            out.append(User(
                user_id=r["user_id"], timezone=r["timezone"],
                muted_until=_dt(r["muted_until"]), created_at=_dt(r["created_at"]),
                alert_activation_at=_dt(r["alert_activation_at"])
                if "alert_activation_at" in keys else None))
        return out


class UserSessionRepo:
    """服务端会话凭据持久化仓库 (F02/T02)。"""

    def __init__(self, db: Database):
        self.db = db

    def create_session(self, user_id: str, ttl_days: int = 30, family_id: str | None = None) -> UserSession:
        import secrets
        token = "trc_sess_" + secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        expires_at = now + timedelta(days=ttl_days)
        cols = self.db.query("PRAGMA table_info(user_session)")
        has_family = any(c["name"] == "family_id" for c in cols)
        if has_family:
            self.db.execute(
                """INSERT INTO user_session (session_token, user_id, created_at, expires_at, is_revoked, family_id)
                   VALUES (?, ?, ?, ?, 0, ?)""",
                (token, user_id, now.isoformat(), expires_at.isoformat(), family_id),
            )
        else:
            self.db.execute(
                """INSERT INTO user_session (session_token, user_id, created_at, expires_at, is_revoked)
                   VALUES (?, ?, ?, ?, 0)""",
                (token, user_id, now.isoformat(), expires_at.isoformat()),
            )
        return UserSession(
            session_token=token,
            user_id=user_id,
            created_at=now,
            expires_at=expires_at,
            is_revoked=False,
            family_id=family_id,
        )

    def issue_refresh_token(self, user_id: str, *, family_id: str | None = None,
                            session_token: str | None = None, ttl_days: int = 90) -> tuple[str, str]:
        """签发刷新凭据。返回 (明文 refresh_token, family_id)。库中仅存储 SHA256 哈希。"""
        import secrets
        import hashlib
        raw_token = "trc_refr_" + secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        family = family_id or ("rfam_" + secrets.token_hex(12))
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        expires_at = now + timedelta(days=ttl_days)
        self.db.execute(
            """INSERT INTO user_refresh_token
               (token_hash, user_id, family_id, session_token, expires_at, used_at, revoked_at, created_at)
               VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)""",
            (token_hash, user_id, family, session_token, expires_at.isoformat(), now.isoformat())
        )
        return raw_token, family

    def rotate_refresh_token(self, refresh_token: str, *, access_ttl_days: int = 30,
                             refresh_ttl_days: int = 90) -> tuple[UserSession, str]:
        """原子轮换刷新凭据：已使用则触发 family 级防重放撤销，未到期则签发新 session 及轮换 refresh_token。"""
        import hashlib
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()

        err = None
        new_session = None
        new_refresh_token = None

        with self.db.transaction(mode="IMMEDIATE"):
            row = self.db.query_one("SELECT * FROM user_refresh_token WHERE token_hash=?", (token_hash,))
            if not row:
                err = "invalid_refresh_token"
            elif row["revoked_at"]:
                err = "refresh_token_revoked"
            elif row["used_at"]:
                # 重放攻击：提交撤销同 family 下所有 token 及关联 session
                self.db.execute(
                    "UPDATE user_refresh_token SET revoked_at=? WHERE family_id=? AND revoked_at IS NULL",
                    (now_str, row["family_id"])
                )
                cols = self.db.query("PRAGMA table_info(user_session)")
                if any(c["name"] == "family_id" for c in cols):
                    self.db.execute(
                        "UPDATE user_session SET is_revoked=1 WHERE family_id=?",
                        (row["family_id"],)
                    )
                err = "refresh_token_replayed"
            else:
                expires_at = _dt(row["expires_at"])
                if expires_at and expires_at <= now:
                    err = "refresh_token_expired"
                else:
                    user_id = row["user_id"]
                    family_id = row["family_id"]

                    # 标记当前 refresh token 已使用
                    self.db.execute(
                        "UPDATE user_refresh_token SET used_at=? WHERE token_hash=?",
                        (now_str, token_hash)
                    )

                    # 签发新 access session
                    new_session = self.create_session(user_id=user_id, ttl_days=access_ttl_days, family_id=family_id)

                    # 轮换签发新 refresh token (保持同一 family_id)
                    new_refresh_token, _ = self.issue_refresh_token(
                        user_id=user_id, family_id=family_id, session_token=new_session.session_token,
                        ttl_days=refresh_ttl_days
                    )

        if err:
            raise ValueError(err)
        return new_session, new_refresh_token

    def get_session(self, token: str) -> UserSession | None:
        if not token:
            return None
        row = self.db.query_one("SELECT * FROM user_session WHERE session_token=?", (token,))
        if not row:
            return None
        keys = row.keys() if hasattr(row, 'keys') else []
        return UserSession(
            session_token=row["session_token"],
            user_id=row["user_id"],
            created_at=_dt(row["created_at"]),
            expires_at=_dt(row["expires_at"]),
            is_revoked=bool(row["is_revoked"]),
            family_id=row["family_id"] if "family_id" in keys else None,
        )

    def revoke_session(self, token: str) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        cols = self.db.query("PRAGMA table_info(user_session)")
        row = self.db.query_one("SELECT family_id FROM user_session WHERE session_token=?", (token,)) if any(c["name"] == "family_id" for c in cols) else None
        self.db.execute("UPDATE user_session SET is_revoked=1 WHERE session_token=?", (token,))
        if row and row["family_id"]:
            self.db.execute(
                "UPDATE user_refresh_token SET revoked_at=? WHERE family_id=? AND revoked_at IS NULL",
                (now_str, row["family_id"])
            )

    def revoke_refresh_family(self, family_id: str) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "UPDATE user_refresh_token SET revoked_at=? WHERE family_id=? AND revoked_at IS NULL",
            (now_str, family_id)
        )
        cols = self.db.query("PRAGMA table_info(user_session)")
        if any(c["name"] == "family_id" for c in cols):
            self.db.execute(
                "UPDATE user_session SET is_revoked=1 WHERE family_id=?",
                (family_id,)
            )


# Compatibility export; durable workers live in a focused repository module.
from trace.db.jobs import ProcessingJobRepo


class ChannelBindingRepo:
    """用户渠道绑定仓库 (F06/T06)。"""

    def __init__(self, db: Database):
        self.db = db

    def bind(self, user_id: str, channel_type: str, channel_target: str) -> ChannelBinding:
        import secrets
        binding_id = f"bind_{secrets.token_hex(8)}"
        now_str = _now()
        self.db.execute(
            """INSERT INTO channel_binding (binding_id, user_id, channel_type, channel_target, is_active, verified_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(user_id, channel_type, channel_target)
               DO UPDATE SET is_active=1, updated_at=excluded.updated_at""",
            (binding_id, user_id, channel_type, channel_target, now_str, now_str, now_str),
        )
        row = self.db.query_one(
            "SELECT * FROM channel_binding WHERE user_id=? AND channel_type=? AND channel_target=?",
            (user_id, channel_type, channel_target),
        )
        return self._to_obj(row)

    def deactivate(self, user_id: str, channel_type: str, channel_target: str) -> bool:
        cur = self.db.execute(
            "UPDATE channel_binding SET is_active=0, updated_at=? WHERE user_id=? AND channel_type=? AND channel_target=?",
            (_now(), user_id, channel_type, channel_target),
        )
        return cur.rowcount > 0

    def list_active(self, user_id: str, channel_type: str | None = None) -> list[ChannelBinding]:
        if channel_type:
            rows = self.db.query(
                "SELECT * FROM channel_binding WHERE user_id=? AND channel_type=? AND is_active=1",
                (user_id, channel_type),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM channel_binding WHERE user_id=? AND is_active=1",
                (user_id,),
            )
        return [self._to_obj(r) for r in rows]

    def _to_obj(self, r) -> ChannelBinding:
        return ChannelBinding(
            binding_id=r["binding_id"],
            user_id=r["user_id"],
            channel_type=r["channel_type"],
            channel_target=r["channel_target"],
            is_active=bool(r["is_active"]),
            verified_at=_dt(r["verified_at"]),
            created_at=_dt(r["created_at"]),
            updated_at=_dt(r["updated_at"]),
        )


from trace.db.outbox import AlertOutboxRepo


class WatchlistRepo:
    def __init__(self, db: Database):
        self.db = db

    def add(self, entry: WatchlistEntry) -> None:
        self.db.execute(
            """INSERT INTO watchlist (user_id, security_id, added_at, user_alias) VALUES (?,?,?,?)
               ON CONFLICT(user_id, security_id) DO UPDATE SET
                 user_alias=CASE WHEN excluded.user_alias != '' THEN excluded.user_alias ELSE watchlist.user_alias END""",
            (entry.user_id, entry.security_id, _now(), entry.user_alias or ""),
        )

    def get_entry(self, user_id: str, security_id: str) -> WatchlistEntry | None:
        row = self.db.query_one("SELECT * FROM watchlist WHERE user_id=? AND security_id=?", (user_id, security_id))
        if not row:
            return None
        keys = set(row.keys())
        return WatchlistEntry(
            user_id=row["user_id"],
            security_id=row["security_id"],
            added_at=_dt(row["added_at"]),
            user_alias=row["user_alias"] if "user_alias" in keys and row["user_alias"] else "",
        )

    def list_entries_by_user(self, user_id: str) -> list[WatchlistEntry]:
        rows = self.db.query("SELECT * FROM watchlist WHERE user_id=? ORDER BY added_at ASC", (user_id,))
        out = []
        for r in rows:
            keys = set(r.keys())
            out.append(WatchlistEntry(
                user_id=r["user_id"],
                security_id=r["security_id"],
                added_at=_dt(r["added_at"]),
                user_alias=r["user_alias"] if "user_alias" in keys and r["user_alias"] else "",
            ))
        return out

    def set_user_alias(self, user_id: str, security_id: str, user_alias: str) -> None:
        self.db.execute(
            "UPDATE watchlist SET user_alias=? WHERE user_id=? AND security_id=?",
            (user_alias, user_id, security_id),
        )

    def remove(self, user_id: str, security_id: str) -> None:
        self.db.execute("DELETE FROM watchlist WHERE user_id=? AND security_id=?",
                        (user_id, security_id))

    def clear(self, user_id: str) -> int:
        """清空该用户的全部自选标的。"""
        cur = self.db.execute("DELETE FROM watchlist WHERE user_id=?", (user_id,))
        return cur.rowcount

    def list_by_user(self, user_id: str) -> list[str]:
        rows = self.db.query("SELECT security_id FROM watchlist WHERE user_id=?", (user_id,))
        return [r["security_id"] for r in rows]

    def users_of_security(self, security_id: str) -> list[str]:
        rows = self.db.query("SELECT user_id FROM watchlist WHERE security_id=?", (security_id,))
        return [r["user_id"] for r in rows]


class AlertRuleRepo:
    # SQLite 中 NULL 不参与 UNIQUE/PK 冲突判定，会导致"全部证券"规则
    # 重复插入且 ON CONFLICT 失效。统一用哨兵值表示 security_id=NULL（all）。
    ALL_SENTINEL = "__ALL__"

    def __init__(self, db: Database):
        self.db = db

    @classmethod
    def _sid(cls, security_id: str | None) -> str:
        return cls.ALL_SENTINEL if security_id is None else security_id

    def set_threshold(self, user_id: str, security_id: str | None, threshold: float) -> None:
        self.db.execute(
            """INSERT INTO alert_rule (user_id, security_id, threshold, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id, security_id) DO UPDATE SET threshold=excluded.threshold,
                 updated_at=excluded.updated_at""",
            (user_id, self._sid(security_id), threshold, _now()),
        )

    def get_threshold(self, user_id: str, security_id: str | None) -> float | None:
        row = self.db.query_one(
            "SELECT threshold FROM alert_rule WHERE user_id=? AND security_id=?",
            (user_id, self._sid(security_id)),
        )
        return row["threshold"] if row else None


class AlertDeliveryRepo:
    def __init__(self, db: Database):
        self.db = db

    def already_sent(self, user_id: str, event_id: str, security_id: str,
                     event_version: int, alert_type: str) -> bool:
        """只有 status='sent' 才算已投递；failed 记录允许下一轮重试。"""
        row = self.db.query_one(
            """SELECT 1 FROM alert_delivery WHERE user_id=? AND event_id=? AND security_id=?
               AND event_version=? AND alert_type=? AND status='sent'""",
            (user_id, event_id, security_id, event_version, alert_type),
        )
        return row is not None

    def record(self, d: AlertDelivery) -> None:
        """成功投递回执。幂等键冲突时用最新回执覆盖（failed → sent）。"""
        self.db.execute(
            """INSERT INTO alert_delivery (delivery_id, user_id, event_id, security_id,
                   event_version, alert_type, final_score, sent_at, status,
                   telegram_chat_id, telegram_message_id, response_status, run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id, event_id, security_id, event_version, alert_type)
               DO UPDATE SET status=excluded.status, sent_at=excluded.sent_at,
                 telegram_chat_id=excluded.telegram_chat_id,
                 telegram_message_id=excluded.telegram_message_id,
                 response_status=excluded.response_status, run_id=excluded.run_id""",
            (d.delivery_id, d.user_id, d.event_id, d.security_id, d.event_version,
             d.alert_type, d.final_score, _dts(d.sent_at) or _now(), d.status,
             d.telegram_chat_id, d.telegram_message_id, d.response_status, d.run_id),
        )

    def record_failed(self, d: AlertDelivery) -> None:
        """失败记录：不覆盖已成功的回执（sent 优先）。"""
        self.db.execute(
            """INSERT INTO alert_delivery (delivery_id, user_id, event_id, security_id,
                   event_version, alert_type, final_score, sent_at, status,
                   telegram_chat_id, telegram_message_id, response_status, run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id, event_id, security_id, event_version, alert_type)
               DO UPDATE SET status=CASE WHEN alert_delivery.status='sent' THEN 'sent'
                                          ELSE excluded.status END,
                 sent_at=CASE WHEN alert_delivery.status='sent' THEN alert_delivery.sent_at
                              ELSE excluded.sent_at END,
                 response_status=CASE WHEN alert_delivery.status='sent'
                                       THEN alert_delivery.response_status
                                       ELSE excluded.response_status END""",
            (d.delivery_id, d.user_id, d.event_id, d.security_id, d.event_version,
             d.alert_type, d.final_score, _dts(d.sent_at) or _now(), d.status,
             d.telegram_chat_id, d.telegram_message_id, d.response_status, d.run_id),
        )

    def record_bootstrap_suppressed(self, d: AlertDelivery) -> None:
        """首次同步保护抑制记录：显式标记，不宣称投递成功。

        使用 status='suppressed' + bootstrap_suppressed=1。
        由于 already_sent 只认 status='sent'，且 record() 用
        ON CONFLICT DO UPDATE，后续若同一 (user, event, security, version,
        alert_type) 真正满足条件并投递，sent 会覆盖此记录。
        """
        self.db.execute(
            """INSERT INTO alert_delivery (delivery_id, user_id, event_id, security_id,
                   event_version, alert_type, final_score, sent_at, status,
                   bootstrap_suppressed, response_status, run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id, event_id, security_id, event_version, alert_type)
               DO UPDATE SET status=CASE WHEN alert_delivery.status='sent' THEN 'sent'
                                          ELSE excluded.status END,
                 bootstrap_suppressed=CASE WHEN alert_delivery.status='sent'
                                           THEN alert_delivery.bootstrap_suppressed
                                           ELSE excluded.bootstrap_suppressed END,
                 sent_at=CASE WHEN alert_delivery.status='sent' THEN alert_delivery.sent_at
                              ELSE excluded.sent_at END,
                 response_status=CASE WHEN alert_delivery.status='sent'
                                       THEN alert_delivery.response_status
                                       ELSE excluded.response_status END""",
            (d.delivery_id, d.user_id, d.event_id, d.security_id, d.event_version,
             d.alert_type, d.final_score, _dts(d.sent_at) or _now(), d.status,
             1, d.response_status, d.run_id),
        )

    def list_by_event(self, event_id: str) -> list[AlertDelivery]:
        rows = self.db.query("SELECT * FROM alert_delivery WHERE event_id=?", (event_id,))
        return [self._to_obj(r) for r in rows]

    def _to_obj(self, r) -> AlertDelivery:
        keys = r.keys()
        return AlertDelivery(
            delivery_id=r["delivery_id"], user_id=r["user_id"], event_id=r["event_id"],
            security_id=r["security_id"], event_version=r["event_version"],
            alert_type=r["alert_type"], final_score=r["final_score"],
            sent_at=_dt(r["sent_at"]), status=r["status"],
            telegram_chat_id=r["telegram_chat_id"],
            telegram_message_id=r["telegram_message_id"],
            response_status=r["response_status"], run_id=r["run_id"],
            bootstrap_suppressed=bool(r["bootstrap_suppressed"])
            if "bootstrap_suppressed" in keys else False,
        )


class DailyDigestRepo:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, d: DailyDigest) -> None:
        key = d.cache_key or f"{d.date_str}:default"
        self.db.execute(
            """INSERT INTO daily_digest (digest_id, date_str, content_markdown, sent_at, cache_key)
               VALUES (?,?,?,?,?)
               ON CONFLICT(cache_key) DO UPDATE SET content_markdown=excluded.content_markdown,
                 sent_at=excluded.sent_at, date_str=excluded.date_str""",
            (d.digest_id, d.date_str, d.content_markdown, _dts(d.sent_at) or _now(), key),
        )

    def get(self, date_str: str) -> DailyDigest | None:
        row = self.db.query_one("SELECT * FROM daily_digest WHERE date_str=? ORDER BY sent_at DESC LIMIT 1", (date_str,))
        if not row:
            return None
        return self._to_obj(row)

    def get_by_cache_key(self, cache_key: str) -> DailyDigest | None:
        if not cache_key:
            return None
        row = self.db.query_one("SELECT * FROM daily_digest WHERE cache_key=?", (cache_key,))
        if not row:
            return None
        return self._to_obj(row)

    def _to_obj(self, row) -> DailyDigest:
        keys = row.keys()
        return DailyDigest(
            digest_id=row["digest_id"],
            date_str=row["date_str"],
            content_markdown=row["content_markdown"],
            sent_at=_dt(row["sent_at"]),
            cache_key=row["cache_key"] if "cache_key" in keys else None,
        )


class NotificationPreferenceRepo:
    def __init__(self, db: Database):
        self.db = db

    def _to_obj(self, r) -> NotificationPreference:
        return NotificationPreference(
            preference_id=r["preference_id"],
            user_id=r["user_id"],
            security_id=r["security_id"],
            threshold=float(r["threshold"]),
            enabled=bool(r["enabled"]),
            quiet_start=r["quiet_start"],
            quiet_end=r["quiet_end"],
            channel=r["channel"] or "all",
            revision=int(r["revision"]),
            updated_at=_dt(r["updated_at"]),
        )

    def get(self, user_id: str, security_id: str | None) -> NotificationPreference | None:
        if security_id is None:
            row = self.db.query_one(
                "SELECT * FROM notification_preference WHERE user_id=? AND security_id IS NULL",
                (user_id,),
            )
        else:
            row = self.db.query_one(
                "SELECT * FROM notification_preference WHERE user_id=? AND security_id=?",
                (user_id, security_id),
            )
        return self._to_obj(row) if row else None

    def get_effective_preference(self, user_id: str, security_id: str | None) -> NotificationPreference:
        """根据优先级返回实际生效偏好：单标的覆盖优先于全局默认。"""
        master = self.get(user_id, None)
        if master and not master.enabled:
            return master
        if security_id:
            sec_pref = self.get(user_id, security_id)
            if sec_pref:
                return sec_pref
        global_pref = self.get(user_id, None)
        if global_pref:
            return global_pref

        # 回退兼容旧 alert_rule 表配置
        rule_threshold = AlertRuleRepo(self.db).get_threshold(user_id, security_id)
        if rule_threshold is None:
            rule_threshold = AlertRuleRepo(self.db).get_threshold(user_id, None)
        fallback_threshold = rule_threshold if rule_threshold is not None else 7.0

        return NotificationPreference(
            preference_id=f"default_{user_id}",
            user_id=user_id,
            security_id=security_id,
            threshold=fallback_threshold,
            enabled=True,
            quiet_start=None,
            quiet_end=None,
            channel="all",
            revision=1,
            updated_at=datetime.now(timezone.utc),
        )

    def set_preference(self, user_id: str, security_id: str | None, *,
                       threshold: float = 7.0, enabled: bool = True,
                       quiet_start: str | None = None, quiet_end: str | None = None,
                       channel: str = "all", expected_revision: int | None = None) -> NotificationPreference:
        """原子设置偏好，支持基于 revision 的乐观并发锁（防止多设备覆写冲突）。"""
        from trace.common.ids import revision_id
        now = datetime.now(timezone.utc)
        with self.db.transaction(mode='IMMEDIATE'):
            current = self.get(user_id, security_id)

            if current:
                if expected_revision is not None and current.revision != expected_revision:
                    raise ValueError(
                        f"Revision conflict: current revision is {current.revision}, expected {expected_revision}"
                    )
                new_revision = current.revision + 1
                if security_id is None:
                    self.db.execute(
                        """UPDATE notification_preference
                           SET threshold=?, enabled=?, quiet_start=?, quiet_end=?, channel=?, revision=?, updated_at=?
                           WHERE user_id=? AND security_id IS NULL""",
                        (threshold, 1 if enabled else 0, quiet_start, quiet_end, channel, new_revision, _dts(now), user_id),
                    )
                else:
                    self.db.execute(
                        """UPDATE notification_preference
                           SET threshold=?, enabled=?, quiet_start=?, quiet_end=?, channel=?, revision=?, updated_at=?
                           WHERE user_id=? AND security_id=?""",
                        (threshold, 1 if enabled else 0, quiet_start, quiet_end, channel, new_revision, _dts(now), user_id, security_id),
                    )
                AlertRuleRepo(self.db).set_threshold(user_id, security_id, threshold)
                return self.get(user_id, security_id)
            else:
                pref_id = f"pref_{revision_id()}"
                self.db.execute(
                    """INSERT INTO notification_preference
                       (preference_id, user_id, security_id, threshold, enabled, quiet_start, quiet_end, channel, revision, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (pref_id, user_id, security_id, threshold, 1 if enabled else 0, quiet_start, quiet_end, channel, 1, _dts(now)),
                )
                AlertRuleRepo(self.db).set_threshold(user_id, security_id, threshold)
                return self.get(user_id, security_id)

    def list_by_user(self, user_id: str) -> list[NotificationPreference]:
        rows = self.db.query("SELECT * FROM notification_preference WHERE user_id=? ORDER BY security_id", (user_id,))
        return [self._to_obj(r) for r in rows]


class RunHistoryRepo:
    """运行历史：每轮 run_once 的结构化摘要（可观测性/事后审计）。"""

    def __init__(self, db: Database):
        self.db = db

    def insert(self, s) -> None:
        d = s.as_dict() if hasattr(s, "as_dict") else s
        self.db.execute(
            """INSERT INTO run_history (run_id, started_at, trace_mode, status,
                   sources_checked, sources_succeeded, sources_failed, sources_disabled,
                   raw_items_new, raw_items_duplicate, events_created, events_revised,
                   events_analyzed, alerts_eligible, alerts_sent, alerts_suppressed,
                   alerts_failed, human_review, keyword_filtered, stage_b_skipped,
                   rescored_events, cursors_committed,
                   llm_stage_a_calls, llm_stage_b_calls,
                   llm_verifier_calls, failed_sources, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET status=excluded.status""",
            (d.get("run_id", ""), d.get("started_at") or _now(),
             d.get("trace_mode", ""), d.get("status", ""),
             int(d.get("sources_checked", 0)), int(d.get("sources_succeeded", 0)),
             int(d.get("sources_failed", 0)), int(d.get("sources_disabled", 0)),
             int(d.get("raw_items_new", 0)), int(d.get("raw_items_duplicate", 0)),
             int(d.get("events_created", 0)), int(d.get("events_revised", 0)),
             int(d.get("events_analyzed", 0)), int(d.get("alerts_eligible", 0)),
             int(d.get("alerts_sent", 0)), int(d.get("alerts_suppressed", 0)),
             int(d.get("alerts_failed", 0)), int(d.get("human_review", 0)),
             int(d.get("keyword_filtered", 0)), int(d.get("stage_b_skipped", 0)),
             int(d.get("rescored_events", 0)), int(d.get("cursors_committed", 0)),
             int(d.get("llm_stage_a_calls", 0)), int(d.get("llm_stage_b_calls", 0)),
             int(d.get("llm_verifier_calls", 0)),
             json.dumps(d.get("failed_sources", []), ensure_ascii=False),
             json.dumps(d.get("notes", []), ensure_ascii=False)),
        )

    def recent(self, limit: int = 5) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM run_history ORDER BY started_at DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["failed_sources"] = json.loads(d.get("failed_sources") or "[]")
            d["notes"] = json.loads(d.get("notes") or "[]")
            out.append(d)
        return out

    def prune(self, keep: int = 500) -> int:
        """只保留最近 keep 轮，防止表无限膨胀。返回删除行数。"""
        cur = self.db.execute(
            """DELETE FROM run_history WHERE run_id NOT IN (
                   SELECT run_id FROM run_history ORDER BY started_at DESC LIMIT ?)""",
            (keep,))
        return cur.rowcount


class ForecastSnapshotRepo:
    """不可变预测快照仓库 (T13/F19/F27)。

    只追加写入，不可篡改，提供防前视偏差的时点行情锁定与到期调度。
    """

    def __init__(self, db: Database):
        self.db = db

    def insert(self, s: ForecastSnapshot) -> None:
        self.db.execute(
            """INSERT INTO forecast_snapshot (
                   snapshot_id, impact_id, event_id, security_id, event_version,
                   predicted_direction, predicted_score, confidence, model_version,
                   market, analysis_created_at, published_at, anchor_price, anchor_ts,
                   horizon_hours, due_at, benchmark_code, benchmark_anchor_price,
                   status, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(snapshot_id) DO NOTHING""",
            (
                s.snapshot_id,
                s.impact_id,
                s.event_id,
                s.security_id,
                s.event_version,
                s.predicted_direction,
                s.predicted_score,
                s.confidence,
                s.model_version,
                s.market,
                _dts(s.analysis_created_at) or _now(),
                _dts(s.published_at),
                s.anchor_price,
                _dts(s.anchor_ts),
                s.horizon_hours,
                _dts(s.due_at) or _now(),
                s.benchmark_code,
                s.benchmark_anchor_price,
                s.status,
                _dts(s.created_at) or _now(),
            ),
        )

    def get(self, snapshot_id: str) -> ForecastSnapshot | None:
        row = self.db.query_one(
            "SELECT * FROM forecast_snapshot WHERE snapshot_id=?", (snapshot_id,)
        )
        return self._to_obj(row) if row else None

    def list_by_event(self, event_id: str) -> list[ForecastSnapshot]:
        rows = self.db.query(
            "SELECT * FROM forecast_snapshot WHERE event_id=? ORDER BY event_version ASC, analysis_created_at ASC",
            (event_id,),
        )
        return [self._to_obj(r) for r in rows]

    def list_by_impact(self, impact_id: str) -> list[ForecastSnapshot]:
        rows = self.db.query(
            "SELECT * FROM forecast_snapshot WHERE impact_id=? ORDER BY created_at ASC",
            (impact_id,),
        )
        return [self._to_obj(r) for r in rows]

    def list_pending_due(self, now: datetime, limit: int = 200) -> list[ForecastSnapshot]:
        rows = self.db.query(
            """SELECT * FROM forecast_snapshot
               WHERE status='pending'
                 AND due_at <= ?
               ORDER BY due_at ASC LIMIT ?""",
            (_dts(now), limit),
        )
        return [self._to_obj(r) for r in rows]

    def update_status(self, snapshot_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE forecast_snapshot SET status=? WHERE snapshot_id=?",
            (status, snapshot_id),
        )

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM forecast_snapshot")
        return (row["n"] or 0) if row else 0

    def count_by_status(self) -> dict[str, int]:
        rows = self.db.query(
            "SELECT status, COUNT(*) AS n FROM forecast_snapshot GROUP BY status"
        )
        return {r["status"]: r["n"] for r in rows}

    def _to_obj(self, r) -> ForecastSnapshot:
        return ForecastSnapshot(
            snapshot_id=r["snapshot_id"],
            impact_id=r["impact_id"],
            event_id=r["event_id"],
            security_id=r["security_id"],
            event_version=r["event_version"],
            predicted_direction=r["predicted_direction"],
            predicted_score=float(r["predicted_score"] or 0.0),
            confidence=float(r["confidence"] or 0.0),
            model_version=r["model_version"] or "v1",
            market=r["market"] or "US",
            analysis_created_at=_dt(r["analysis_created_at"]),
            published_at=_dt(r["published_at"]),
            anchor_price=r["anchor_price"],
            anchor_ts=_dt(r["anchor_ts"]),
            horizon_hours=float(r["horizon_hours"] or 24.0),
            due_at=_dt(r["due_at"]),
            benchmark_code=r["benchmark_code"] or "SPX",
            benchmark_anchor_price=r["benchmark_anchor_price"],
            status=r["status"],
            created_at=_dt(r["created_at"]),
        )


class ForecastCheckRepo:
    """预测回测账本仓库。提供单次定格核对与结构化分组评估统计 (F27)。"""

    def __init__(self, db: Database):
        self.db = db

    def record(self, c: ForecastCheck) -> None:
        sid = c.snapshot_id or c.check_id or c.impact_id
        self.db.execute(
            """INSERT INTO forecast_check (
                   check_id, snapshot_id, impact_id, event_id, security_id,
                   event_version, predicted_direction, predicted_score, confidence,
                   actual_change_pct, actual_direction, outcome, horizon_hours,
                   evaluated_at, anchor_price, anchor_ts, exit_price,
                   elapsed_hours, note, model_version, market, benchmark_code,
                   benchmark_change_pct, excess_return_pct, excluded_reason
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(snapshot_id) DO NOTHING""",
            (
                c.check_id,
                sid,
                c.impact_id,
                c.event_id,
                c.security_id,
                c.event_version,
                c.predicted_direction,
                c.predicted_score,
                c.confidence,
                c.actual_change_pct,
                c.actual_direction,
                c.outcome,
                c.horizon_hours,
                _dts(c.evaluated_at) or _now(),
                c.anchor_price,
                _dts(c.anchor_ts),
                c.exit_price,
                c.elapsed_hours,
                c.note,
                c.model_version or "v1",
                c.market or "US",
                c.benchmark_code or "SPX",
                c.benchmark_change_pct,
                c.excess_return_pct,
                c.excluded_reason or "",
            ),
        )

    def summary(self) -> dict:
        """聚合：hit/miss/neutral 计数、基准超额收益与分组统计 (F27)。"""
        row = self.db.query_one(
            """SELECT SUM(CASE WHEN outcome='hit' THEN 1 ELSE 0 END) AS hits,
                      SUM(CASE WHEN outcome='miss' THEN 1 ELSE 0 END) AS misses,
                      SUM(CASE WHEN outcome='neutral' THEN 1 ELSE 0 END) AS neutrals,
                      SUM(CASE WHEN outcome='unmeasurable' THEN 1 ELSE 0 END) AS unmeasurable,
                      AVG(CASE WHEN outcome IN ('hit', 'miss') AND excess_return_pct IS NOT NULL
                               THEN excess_return_pct ELSE NULL END) AS avg_excess
               FROM forecast_check WHERE time_basis='analysis_recorded'"""
        )
        hits = (row["hits"] or 0) if row else 0
        misses = (row["misses"] or 0) if row else 0
        neutrals = (row["neutrals"] or 0) if row else 0
        unmeasurable = (row["unmeasurable"] or 0) if row else 0
        avg_excess = round(row["avg_excess"], 4) if row and row["avg_excess"] is not None else None

        directed = hits + misses
        total = hits + misses + neutrals

        # 统计快照总量与实测比例 (仅统计 analysis_recorded，排除 legacy_unverified)
        snap_row = self.db.query_one("SELECT COUNT(*) AS n FROM forecast_snapshot WHERE time_basis='analysis_recorded'")
        total_snaps = (snap_row["n"] or 0) if snap_row else 0
        if total_snaps == 0:
            total_snaps = total + unmeasurable

        measurement_rate = round(total / total_snaps, 4) if total_snaps > 0 else None
        unmeasurable_rate = round(unmeasurable / total_snaps, 4) if total_snaps > 0 else None
        neutral_rate = round(neutrals / total, 4) if total > 0 else None
        hit_rate = round(hits / directed, 4) if directed else None

        def _build_group(column_name: str) -> dict:
            g_rows = self.db.query(
                f"""SELECT {column_name} AS val,
                           SUM(CASE WHEN outcome='hit' THEN 1 ELSE 0 END) AS g_hits,
                           SUM(CASE WHEN outcome='miss' THEN 1 ELSE 0 END) AS g_misses,
                           SUM(CASE WHEN outcome='neutral' THEN 1 ELSE 0 END) AS g_neutrals,
                           SUM(CASE WHEN outcome='unmeasurable' THEN 1 ELSE 0 END) AS g_unmeasurable,
                           AVG(CASE WHEN outcome IN ('hit', 'miss') AND excess_return_pct IS NOT NULL
                                    THEN excess_return_pct ELSE NULL END) AS g_excess
                    FROM forecast_check
                    WHERE time_basis='analysis_recorded' AND {column_name} IS NOT NULL AND {column_name} != ''
                    GROUP BY {column_name}"""
            )
            res = {}
            for gr in g_rows:
                v = str(gr["val"])
                gh = gr["g_hits"] or 0
                gm = gr["g_misses"] or 0
                gn = gr["g_neutrals"] or 0
                gu = gr["g_unmeasurable"] or 0
                gd = gh + gm
                gt = gh + gm + gn
                ge = round(gr["g_excess"], 4) if gr["g_excess"] is not None else None
                res[v] = {
                    "total": gt,
                    "hits": gh,
                    "misses": gm,
                    "neutrals": gn,
                    "unmeasurable": gu,
                    "hit_rate": round(gh / gd, 4) if gd else None,
                    "avg_excess_return": ge,
                }
            return res

        return {
            "total_snapshots": total_snaps,
            "total": total,
            "hits": hits,
            "misses": misses,
            "neutrals": neutrals,
            "unmeasurable": unmeasurable,
            "measurement_rate": measurement_rate,
            "unmeasurable_rate": unmeasurable_rate,
            "neutral_rate": neutral_rate,
            "hit_rate": hit_rate,
            "avg_excess_return": avg_excess,
            "legacy_unverified": self.db.query_one("SELECT COUNT(*) AS n FROM forecast_snapshot WHERE time_basis='legacy_unverified'")['n'],
            "by_market": _build_group("market"),
            "by_direction": _build_group("predicted_direction"),
            "by_model": _build_group("model_version"),
            "by_horizon": _build_group("horizon_hours"),
        }

    def pending_count(self) -> int:
        """待核对的预测数（尚未落账且状态为 pending）。"""
        row = self.db.query_one(
            """SELECT COUNT(*) AS n
               FROM forecast_snapshot s
               LEFT JOIN forecast_check f ON f.snapshot_id = s.snapshot_id
                WHERE f.snapshot_id IS NULL
                  AND s.status = 'pending'
                  AND s.time_basis = 'analysis_recorded'
                  AND s.predicted_direction IN ('bullish','bearish')"""
        )
        if row and row["n"] > 0:
            return row["n"]
        return 0


# ---------------------------------------------------------------------------
# 假设跟踪与用户反馈仓储 (T16)
# ---------------------------------------------------------------------------

class ResearchQuestionRepo:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, q: ResearchQuestion) -> None:
        self.db.execute(
            """INSERT INTO research_question (
                   question_id, user_id, event_id, security_id,
                   title, hypothesis, supporting_conditions, contradicting_conditions,
                   next_check_at, state, user_notes, matched_evidence_ids,
                   created_at, updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                q.question_id,
                q.user_id,
                q.event_id,
                q.security_id,
                q.title,
                q.hypothesis,
                json.dumps(q.supporting_conditions, ensure_ascii=False),
                json.dumps(q.contradicting_conditions, ensure_ascii=False),
                _dts(q.next_check_at),
                q.state,
                q.user_notes,
                json.dumps(q.matched_evidence_ids, ensure_ascii=False),
                _dts(q.created_at) or _now(),
                _dts(q.updated_at) or _now(),
            ),
        )

    def update(self, q: ResearchQuestion) -> None:
        cur = self.db.execute(
            """UPDATE research_question SET
                   title = ?,
                   hypothesis = ?,
                   supporting_conditions = ?,
                   contradicting_conditions = ?,
                   next_check_at = ?,
                   state = ?,
                   user_notes = ?,
                   matched_evidence_ids = ?,
                   updated_at = ?, revision=revision+1
               WHERE question_id = ? AND user_id = ? AND revision=?""",
            (
                q.title,
                q.hypothesis,
                json.dumps(q.supporting_conditions, ensure_ascii=False),
                json.dumps(q.contradicting_conditions, ensure_ascii=False),
                _dts(q.next_check_at),
                q.state,
                q.user_notes,
                json.dumps(q.matched_evidence_ids, ensure_ascii=False),
                _dts(q.updated_at) or _now(),
                q.question_id,
                q.user_id,
                q.revision,
            ),
        )
        if cur.rowcount != 1:
            raise ValueError('Research revision conflict')
        q.revision += 1

    def get(self, question_id: str, user_id: str | None = None) -> ResearchQuestion | None:
        if user_id:
            row = self.db.query_one(
                "SELECT * FROM research_question WHERE question_id = ? AND user_id = ?",
                (question_id, user_id),
            )
        else:
            row = self.db.query_one(
                "SELECT * FROM research_question WHERE question_id = ?",
                (question_id,),
            )
        if not row:
            return None
        return self._to_model(row)

    def count_by_user(
        self,
        user_id: str,
        state: str | None = None,
        security_id: str | None = None,
    ) -> int:
        sql = "SELECT COUNT(*) AS c FROM research_question WHERE user_id = ?"
        params: list[Any] = [user_id]
        if state:
            sql += " AND state = ?"
            params.append(state)
        if security_id:
            sql += " AND security_id = ?"
            params.append(security_id)
        row = self.db.query_one(sql, tuple(params))
        return int(row["c"]) if row else 0

    def list_by_user(
        self,
        user_id: str,
        state: str | None = None,
        security_id: str | None = None,
        limit: int | None = 100,
        offset: int = 0,
    ) -> list[ResearchQuestion]:
        sql = "SELECT * FROM research_question WHERE user_id = ?"
        params: list[Any] = [user_id]
        if state:
            sql += " AND state = ?"
            params.append(state)
        if security_id:
            sql += " AND security_id = ?"
            params.append(security_id)
        sql += " ORDER BY updated_at DESC, question_id DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])

        rows = self.db.query(sql, tuple(params))
        return [self._to_model(r) for r in rows]

    def delete(self, question_id: str, user_id: str) -> bool:
        cursor = self.db.execute(
            "DELETE FROM research_question WHERE question_id = ? AND user_id = ?",
            (question_id, user_id),
        )
        return cursor.rowcount > 0

    def match_new_evidence(
        self,
        raw_item_id: str,
        event_id: str | None = None,
        security_id: str | None = None,
    ) -> list[ResearchQuestion]:
        """为处于 tracking 状态的假设匹配新到达的关联证据。"""
        if not event_id and not security_id:
            return []
        raw_row = self.db.query_one('SELECT source_id FROM raw_item WHERE raw_item_id=?', (raw_item_id,))
        if not raw_row:
            raise ValueError('Evidence not found')
        from trace.common.source_policy import permitted
        if not permitted(self.db, raw_row['source_id'], 'display'):
            return []

        clauses = []
        params: list[Any] = []
        if event_id:
            clauses.append("event_id = ?")
            params.append(event_id)
        if security_id:
            clauses.append("security_id = ?")
            params.append(security_id)
        
        sql = f"SELECT * FROM research_question WHERE state = 'tracking' AND ({' OR '.join(clauses)})"

        rows = self.db.query(sql, tuple(params))
        matched: list[ResearchQuestion] = []
        for r in rows:
            q = self._to_model(r)
            if raw_item_id not in q.matched_evidence_ids:
                q.matched_evidence_ids.append(raw_item_id)
                q.updated_at = datetime.now(timezone.utc)
                self.update(q)
                matched.append(q)
        return matched

    def _to_model(self, row: Any) -> ResearchQuestion:
        supp = []
        contra = []
        matched = []
        try:
            if row["supporting_conditions"]:
                supp = json.loads(row["supporting_conditions"])
        except Exception:
            pass
        try:
            if row["contradicting_conditions"]:
                contra = json.loads(row["contradicting_conditions"])
        except Exception:
            pass
        try:
            if row["matched_evidence_ids"]:
                matched = json.loads(row["matched_evidence_ids"])
        except Exception:
            pass

        return ResearchQuestion(
            question_id=row["question_id"],
            user_id=row["user_id"],
            event_id=row["event_id"],
            security_id=row["security_id"],
            title=row["title"],
            hypothesis=row["hypothesis"],
            supporting_conditions=supp,
            contradicting_conditions=contra,
            next_check_at=_dt(row["next_check_at"]),
            state=row["state"],
            user_notes=row["user_notes"] or "",
            matched_evidence_ids=matched,
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            revision=row['revision'],
        )


class AlertFeedbackRepo:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, fb: AlertFeedback) -> None:
        self.db.execute(
            """INSERT INTO alert_feedback (
                   feedback_id, user_id, event_id, security_id,
                   rating, reason, created_at
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                fb.feedback_id,
                fb.user_id,
                fb.event_id,
                fb.security_id,
                fb.rating,
                fb.reason,
                _dts(fb.created_at) or _now(),
            ),
        )

    def list_by_user(self, user_id: str, limit: int | None = 100) -> list[AlertFeedback]:
        sql = "SELECT * FROM alert_feedback WHERE user_id = ? ORDER BY created_at DESC,feedback_id"
        rows = self.db.query(sql + (" LIMIT ?" if limit is not None else ''), (user_id,limit) if limit is not None else (user_id,))
        return [self._to_model(r) for r in rows]

    def summary(self, user_id: str | None = None) -> dict[str, Any]:
        sql = "SELECT rating, COUNT(*) as cnt FROM alert_feedback"
        params: list[Any] = []
        if user_id:
            sql += " WHERE user_id = ?"
            params.append(user_id)
        sql += " GROUP BY rating"
        rows = self.db.query(sql, tuple(params))

        by_rating: dict[str, int] = {}
        total = 0
        useful_count = 0
        for r in rows:
            rating = r["rating"]
            cnt = r["cnt"]
            by_rating[rating] = cnt
            total += cnt
            if rating == "useful":
                useful_count += cnt

        not_useful_count = total - useful_count
        useful_rate = round(useful_count / total, 4) if total > 0 else 0.0

        return {
            "total": total,
            "useful_count": useful_count,
            "not_useful_count": not_useful_count,
            "useful_rate": useful_rate,
            "by_rating": by_rating,
        }

    def _to_model(self, row: Any) -> AlertFeedback:
        return AlertFeedback(
            feedback_id=row["feedback_id"],
            user_id=row["user_id"],
            event_id=row["event_id"],
            security_id=row["security_id"],
            rating=row["rating"],
            reason=row["reason"] or "",
            created_at=_dt(row["created_at"]),
        )

