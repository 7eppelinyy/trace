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
    AlertRule,
    DailyDigest,
    EntityAlias,
    Event,
    EventImpact,
    EventRevision,
    EventSource,
    IndustryEdge,
    LicenseMode,
    MarketSnapshot,
    RawItem,
    Security,
    Source,
    User,
    WatchlistEntry,
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
                   authority_level, poll_interval_seconds, security_map)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_id) DO UPDATE SET
                 source_name=excluded.source_name, source_type=excluded.source_type,
                 priority=excluded.priority, base_reliability=excluded.base_reliability,
                 license_mode=excluded.license_mode, retention_policy=excluded.retention_policy,
                 enabled=excluded.enabled,
                 authority_level=excluded.authority_level,
                 poll_interval_seconds=excluded.poll_interval_seconds,
                 security_map=excluded.security_map""",
            (s.source_id, s.source_name, s.source_type, s.priority, s.base_reliability,
             s.license_mode.value, s.retention_policy, int(s.enabled),
             s.authority_level, s.poll_interval_seconds, json.dumps(s.security_map)),
        )

    def get(self, source_id: str) -> Source | None:
        row = self.db.query_one("SELECT * FROM source WHERE source_id=?", (source_id,))
        return self._to_obj(row) if row else None

    def get_by_name(self, name: str) -> Source | None:
        row = self.db.query_one("SELECT * FROM source WHERE source_name=?", (name,))
        return self._to_obj(row) if row else None

    def list_enabled(self) -> list[Source]:
        return [self._to_obj(r) for r in self.db.query("SELECT * FROM source WHERE enabled=1")]

    def list_all(self) -> list[Source]:
        """全部来源（含禁用）：doctor 显示 DISABLED 状态需要完整列表。"""
        return [self._to_obj(r) for r in self.db.query(
            "SELECT * FROM source ORDER BY source_id")]

    def _to_obj(self, r) -> Source:
        keys = set(r.keys())
        return Source(
            source_id=r["source_id"], source_name=r["source_name"], source_type=r["source_type"],
            priority=r["priority"], base_reliability=r["base_reliability"],
            license_mode=LicenseMode(r["license_mode"]), retention_policy=r["retention_policy"],
            enabled=bool(r["enabled"]),
            authority_level=r["authority_level"] if "authority_level" in keys else "",
            poll_interval_seconds=(r["poll_interval_seconds"]
                                   if "poll_interval_seconds" in keys else 600),
            security_map=(json.loads(r["security_map"] or "[]")
                          if "security_map" in keys else []),
        )


class RawItemRepo:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, item: RawItem) -> None:
        self.db.execute(
            """INSERT INTO raw_item (raw_item_id, source_id, source_item_id, title, url,
                   canonical_url, published_at, fetched_at, language, content, reference,
                   title_hash, content_hash, event_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_id, source_item_id) DO NOTHING""",
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

    def exists_canonical_url(self, canonical_url: str) -> bool:
        if not canonical_url:
            return False
        row = self.db.query_one(
            "SELECT 1 FROM raw_item WHERE canonical_url=?", (canonical_url,))
        return row is not None

    def exists_title_hash(self, title_hash: str) -> bool:
        row = self.db.query_one("SELECT 1 FROM raw_item WHERE title_hash=?", (title_hash,))
        return row is not None

    def exists_content_hash(self, content_hash: str) -> bool:
        if not content_hash:
            return False
        row = self.db.query_one("SELECT 1 FROM raw_item WHERE content_hash=?", (content_hash,))
        return row is not None

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
                   title_embedding, summary_embedding)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (e.event_id, e.title, e.summary, e.event_type, e.status, e.version,
             _dts(e.first_seen_at), _dts(e.last_updated_at), _dts(e.event_time), e.language,
             e.first_source_id, e.primary_source_id, json.dumps(e.all_source_ids),
             int(e.material_update), int(e.needs_human_review),
             e.title_embedding, e.summary_embedding),
        )

    def update(self, e: Event) -> None:
        self.db.execute(
            """UPDATE event SET title=?, summary=?, event_type=?, status=?, version=?,
                   last_updated_at=?, event_time=?, language=?, first_source_id=?,
                   primary_source_id=?, all_source_ids=?, material_update=?, needs_human_review=?,
                   title_embedding=?, summary_embedding=?
               WHERE event_id=?""",
            (e.title, e.summary, e.event_type, e.status, e.version,
             _dts(e.last_updated_at), _dts(e.event_time), e.language,
             e.first_source_id, e.primary_source_id, json.dumps(e.all_source_ids),
             int(e.material_update), int(e.needs_human_review),
             e.title_embedding, e.summary_embedding, e.event_id),
        )

    def get(self, event_id: str) -> Event | None:
        row = self.db.query_one("SELECT * FROM event WHERE event_id=?", (event_id,))
        return self._to_obj(row) if row else None

    def recent(self, hours: int = 72, limit: int = 500) -> list[Event]:
        rows = self.db.query(
            """SELECT * FROM event
               WHERE last_updated_at >= datetime('now', ?)
               ORDER BY last_updated_at DESC LIMIT ?""",
            (f"-{hours} hours", limit),
        )
        return [self._to_obj(r) for r in rows]

    def top_of_day(self, date_str: str, limit: int = 20) -> list[Event]:
        """当日按 final_score 最高的事件（取该事件最高 impact 分）。"""
        rows = self.db.query(
            """SELECT e.*, MAX(i.final_score) AS top_score
               FROM event e JOIN event_impact i ON i.event_id = e.event_id
               WHERE date(e.first_seen_at) = ?
               GROUP BY e.event_id ORDER BY top_score DESC LIMIT ?""",
            (date_str, limit),
        )
        return [self._to_obj(r) for r in rows]

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
            title_embedding=r["title_embedding"], summary_embedding=r["summary_embedding"],
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
                   is_watchlist_default, is_context_universe)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET
                 market=excluded.market, exchange=excluded.exchange,
                 company_name_zh=excluded.company_name_zh, company_name_en=excluded.company_name_en,
                 cik=excluded.cik, aliases=excluded.aliases, products=excluded.products,
                 industry_tags=excluded.industry_tags, graph_node_ids=excluded.graph_node_ids,
                 is_watchlist_default=excluded.is_watchlist_default,
                 is_context_universe=excluded.is_context_universe""",
            (s.security_id, s.market, s.exchange, s.ticker, s.company_name_zh, s.company_name_en,
             s.cik, json.dumps(s.aliases), json.dumps(s.products), json.dumps(s.industry_tags),
             json.dumps(s.graph_node_ids), int(s.is_watchlist_default), int(s.is_context_universe)),
        )

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
        return Security(
            security_id=r["security_id"], market=r["market"], exchange=r["exchange"],
            ticker=r["ticker"], company_name_zh=r["company_name_zh"],
            company_name_en=r["company_name_en"], cik=r["cik"],
            aliases=json.loads(r["aliases"] or "[]"), products=json.loads(r["products"] or "[]"),
            industry_tags=json.loads(r["industry_tags"] or "[]"),
            graph_node_ids=json.loads(r["graph_node_ids"] or "[]"),
            is_watchlist_default=bool(r["is_watchlist_default"]),
            is_context_universe=bool(r["is_context_universe"]),
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
                   analysis_mode, market_data_mode, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_id, security_id) DO UPDATE SET
                 direction=excluded.direction, directness=excluded.directness,
                 magnitude=excluded.magnitude, persistence=excluded.persistence,
                 directness_score=excluded.directness_score, confidence=excluded.confidence,
                 reason=excluded.reason, industry_path=excluded.industry_path,
                 evidence_ids=excluded.evidence_ids, source_reliability=excluded.source_reliability,
                 base_score=excluded.base_score, market_confirmation=excluded.market_confirmation,
                 final_score=excluded.final_score, analysis_mode=excluded.analysis_mode,
                 market_data_mode=excluded.market_data_mode""",
            (i.impact_id, i.event_id, i.security_id, i.direction, i.directness,
             i.magnitude, i.persistence, i.directness_score, i.confidence, i.reason,
             i.industry_path, json.dumps(i.evidence_ids), i.source_reliability,
             i.base_score, i.market_confirmation, i.final_score,
             i.analysis_mode, i.market_data_mode,
             i.created_at and i.created_at.isoformat() or _now()),
        )

    def list_by_event(self, event_id: str) -> list[EventImpact]:
        rows = self.db.query("SELECT * FROM event_impact WHERE event_id=?", (event_id,))
        return [self._to_obj(r) for r in rows]

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

    def _to_obj(self, r) -> EventImpact:
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
            created_at=_dt(r["created_at"]),
        )


class MarketSnapshotRepo:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, s: MarketSnapshot) -> None:
        self.db.execute(
            """INSERT INTO market_snapshot (security_id, ts, last_price, prev_close,
                   change_pct_1m, change_pct_5m, change_pct_15m, volume, volume_ratio, session)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(security_id, ts) DO UPDATE SET
                 last_price=excluded.last_price, prev_close=excluded.prev_close,
                 change_pct_1m=excluded.change_pct_1m, change_pct_5m=excluded.change_pct_5m,
                 change_pct_15m=excluded.change_pct_15m, volume=excluded.volume,
                 volume_ratio=excluded.volume_ratio, session=excluded.session""",
            (s.security_id, _dts(s.ts), s.last_price, s.prev_close, s.change_pct_1m,
             s.change_pct_5m, s.change_pct_15m, s.volume, s.volume_ratio, s.session),
        )

    def latest(self, security_id: str) -> MarketSnapshot | None:
        row = self.db.query_one(
            "SELECT * FROM market_snapshot WHERE security_id=? ORDER BY ts DESC LIMIT 1",
            (security_id,))
        return self._to_obj(row) if row else None

    def _to_obj(self, r) -> MarketSnapshot:
        return MarketSnapshot(
            security_id=r["security_id"], ts=_dt(r["ts"]), last_price=r["last_price"],
            prev_close=r["prev_close"], change_pct_1m=r["change_pct_1m"],
            change_pct_5m=r["change_pct_5m"], change_pct_15m=r["change_pct_15m"],
            volume=r["volume"], volume_ratio=r["volume_ratio"], session=r["session"],
        )


class UserRepo:
    def __init__(self, db: Database):
        self.db = db

    def ensure(self, user_id: str, timezone: str = "Asia/Taipei") -> None:
        """幂等创建用户。首次创建即登记 alert_activation_at（首次同步保护：
        只有该时间点之后发布/更新的事件才允许即时推送，历史事件不集中推送）。"""
        self.db.execute(
            """INSERT INTO user (user_id, timezone, created_at, alert_activation_at)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id) DO NOTHING""",
            (user_id, timezone, _now(), _now()),
        )

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
        return [self.get(r["user_id"]) or User(user_id=r["user_id"])
                for r in rows]


class WatchlistRepo:
    def __init__(self, db: Database):
        self.db = db

    def add(self, entry: WatchlistEntry) -> None:
        self.db.execute(
            """INSERT INTO watchlist (user_id, security_id, added_at) VALUES (?,?,?)
               ON CONFLICT(user_id, security_id) DO NOTHING""",
            (entry.user_id, entry.security_id, _now()),
        )

    def remove(self, user_id: str, security_id: str) -> None:
        self.db.execute("DELETE FROM watchlist WHERE user_id=? AND security_id=?",
                        (user_id, security_id))

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
        self.db.execute(
            """INSERT INTO daily_digest (digest_id, date_str, content_markdown, sent_at)
               VALUES (?,?,?,?)
               ON CONFLICT(date_str) DO UPDATE SET content_markdown=excluded.content_markdown,
                 sent_at=excluded.sent_at""",
            (d.digest_id, d.date_str, d.content_markdown, _dts(d.sent_at) or _now()),
        )

    def get(self, date_str: str) -> DailyDigest | None:
        row = self.db.query_one("SELECT * FROM daily_digest WHERE date_str=?", (date_str,))
        if not row:
            return None
        return DailyDigest(digest_id=row["digest_id"], date_str=row["date_str"],
                           content_markdown=row["content_markdown"], sent_at=_dt(row["sent_at"]))
