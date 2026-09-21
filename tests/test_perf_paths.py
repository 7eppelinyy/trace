"""热路径性能回归测试。

这些测试锁定的不是"跑得多快"（会随机器波动），而是**访问模式**：
    - Level 1 去重与证据检索必须走索引，不得退化为全表扫描
    - 同一轮内重复取行情必须命中缓存，不得反复打 Provider
    - /ask 的图谱扩展不得在内层循环里全表扫 security
    - 语义聚类不得对每条 item 重复拉取整个时间窗口
    - 非实质性合并不得触发 Stage B（LLM 成本）

访问模式退化是静默的：功能测试全绿、只有账单和延迟会变。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trace.alerts.ask import AskEngine
from trace.collectors.base import CollectResult
from trace.common.ids import raw_item_id
from trace.db.repositories import EventImpactRepo, EventRepo, RawItemRepo
from trace.domain.models import Event, EventImpact, RawItem
from trace.event_engine.embeddings import HashEmbedder
from trace.event_engine.engine import EventEngine, ExtractedEvent
from trace.graph.industry_graph import IndustryGraph
from trace.pipeline import Pipeline


class QueryCounter:
    """统计 Database.query / query_one 的调用次数（访问模式断言用）。"""

    def __init__(self, db):
        self.db = db
        self.count = 0
        self._query = db.query
        self._query_one = db.query_one

    def __enter__(self):
        def query(sql, params=()):
            self.count += 1
            return self._query(sql, params)

        def query_one(sql, params=()):
            self.count += 1
            return self._query_one(sql, params)

        self.db.query = query
        self.db.query_one = query_one
        return self

    def __exit__(self, *exc):
        self.db.query = self._query
        self.db.query_one = self._query_one
        return False


# ---------------------------------------------------------------------------
# 索引：Level 1 去重 + 证据检索
# ---------------------------------------------------------------------------

def _plan(db, sql: str, params: tuple = ()) -> str:
    rows = db.query(f"EXPLAIN QUERY PLAN {sql}", params)
    return " | ".join(str(r["detail"]) for r in rows)


def test_dedup_lookups_use_indexes(db):
    """ExactDedup 的四道检查每轮每条都跑，必须全部走索引。"""
    checks = {
        "source_item_id": ("SELECT 1 FROM raw_item WHERE source_id=? AND source_item_id=?",
                           ("src_sec_edgar", "x")),
        "canonical_url": ("SELECT 1 FROM raw_item WHERE canonical_url=?", ("http://a",)),
        "title_hash": ("SELECT 1 FROM raw_item WHERE title_hash=?", ("h",)),
        "content_hash": ("SELECT 1 FROM raw_item WHERE content_hash=?", ("h",)),
    }
    for name, (sql, params) in checks.items():
        detail = _plan(db, sql, params)
        assert "USING INDEX" in detail or "USING COVERING INDEX" in detail, \
            f"{name} 去重查询退化为全表扫描: {detail}"
        assert "SCAN raw_item" not in detail, f"{name} 全表扫描: {detail}"


def test_list_by_event_uses_index(db):
    """Stage B 取证据 / /ask 取原文链接的公共入口。"""
    detail = _plan(db, "SELECT * FROM raw_item WHERE event_id=? ORDER BY published_at",
                   ("EV-1",))
    assert "SCAN raw_item" not in detail, f"list_by_event 全表扫描: {detail}"


def test_dedup_still_correct_after_indexing(db):
    """索引不得改变去重语义：四条路径逐一命中。"""
    repo = RawItemRepo(db)
    now = datetime.now(timezone.utc)
    repo.insert(RawItem(
        raw_item_id="RAW-1", source_id="src_sec_edgar", source_item_id="S1",
        title="t", url="http://u", canonical_url="http://u",
        published_at=now, fetched_at=now,
        title_hash="TH", content_hash="CH"))

    assert repo.exists_same_source_item("src_sec_edgar", "S1")
    assert not repo.exists_same_source_item("src_sec_edgar", "S2")
    assert repo.exists_canonical_url("http://u")
    assert not repo.exists_canonical_url("http://other")
    assert repo.exists_title_hash("TH")
    assert not repo.exists_title_hash("NOPE")
    assert repo.exists_content_hash("CH")
    assert not repo.exists_content_hash("NOPE")
    # 空值不得误判为重复
    assert not repo.exists_content_hash("")
    assert not repo.exists_canonical_url("")


# ---------------------------------------------------------------------------
# /ask：图谱扩展不得随图规模放大查询次数
# ---------------------------------------------------------------------------

def _seed_event_with_impact(db, event_id: str, security_id: str, *,
                            title: str, score: float) -> None:
    now = datetime.now(timezone.utc)
    EventRepo(db).insert(Event(
        event_id=event_id, title=title, summary=title, event_type="other",
        status="reported", version=1, first_seen_at=now, last_updated_at=now,
        event_time=now, first_source_id="src_sec_edgar",
        primary_source_id="src_sec_edgar", all_source_ids=["src_sec_edgar"]))
    RawItemRepo(db).insert(RawItem(
        raw_item_id=f"RAW-{event_id}", source_id="src_sec_edgar",
        source_item_id=f"SI-{event_id}", title=title,
        url=f"https://example.test/{event_id}", canonical_url=f"https://example.test/{event_id}",
        published_at=now, fetched_at=now, event_id=event_id,
        title_hash=f"th-{event_id}", content_hash=f"ch-{event_id}"))
    EventImpactRepo(db).upsert(EventImpact(
        impact_id=f"IMP-{event_id}", event_id=event_id, security_id=security_id,
        direction="bullish", directness="direct", magnitude=6.0, persistence=5.0,
        directness_score=9.0, confidence=0.8, reason="test",
        source_reliability=9.0, base_score=score, final_score=score,
        created_at=now))


def test_ask_query_count_does_not_scale_with_graph(db):
    """图谱扩展命中几十个证券，查询次数必须保持常数级。

    旧实现在 "for node → for edge → for edge2" 的最内层调 security_repo.list_all()，
    查询次数随图的边数相乘放大。
    """
    _seed_event_with_impact(db, "EV-DIRECT", "SEC-US-SNDK",
                            title="SanDisk 官方公告", score=8.0)
    _seed_event_with_impact(db, "EV-HOP", "SEC-US-MU",
                            title="Micron 产能公告", score=7.0)

    engine = AskEngine(db, IndustryGraph(db))
    with QueryCounter(db) as counter:
        answer = engine.ask("SNDK", "为什么波动？")

    assert answer is not None
    assert counter.count <= 12, f"/ask 查询次数失控: {counter.count}"


def test_ask_still_returns_direct_and_industry_events(db):
    """访问模式改写不得改变检索语义：直接事件与产业链邻居事件都要能召回。"""
    _seed_event_with_impact(db, "EV-DIRECT", "SEC-US-SNDK",
                            title="SanDisk 官方公告", score=8.0)
    _seed_event_with_impact(db, "EV-HOP", "SEC-US-MU",
                            title="Micron 产能公告", score=7.0)

    answer = AskEngine(db, IndustryGraph(db)).ask("SNDK", "为什么波动？")
    assert answer is not None
    by_event = {c.event.event_id: c for c in answer.candidates}

    # 自身事件：direct，相关度不衰减
    assert by_event["EV-DIRECT"].relation == "direct"
    assert by_event["EV-DIRECT"].distance_score == 8.0
    # 产业链邻居（sndk ←nand→ mu）：跳数衰减后仍进候选
    assert "EV-HOP" in by_event
    assert by_event["EV-HOP"].relation in ("1-hop", "2-hop")
    assert by_event["EV-HOP"].distance_score < 7.0
    # 入选候选必须带得回原文链接
    assert by_event["EV-DIRECT"].evidence_urls == ["https://example.test/EV-DIRECT"]


# ---------------------------------------------------------------------------
# 语义聚类：时间窗口按轮取一次，且不得破坏同轮聚类
# ---------------------------------------------------------------------------

def _ingest_item(engine, title: str, url: str, source_item_id: str):
    now = datetime.now(timezone.utc)
    item = RawItem(
        raw_item_id=raw_item_id(), source_id="src_reuters",
        source_item_id=source_item_id, title=title, url=url,
        published_at=now, language="en", content=title)
    extracted = ExtractedEvent(
        title=title, summary=title, entities=["Micron"],
        event_type="regulation", event_status="reported", event_time=now)
    return engine.ingest(item, extracted)


def test_cluster_window_loaded_once_per_round(db, config):
    """窗口事件集每轮取一次，不随本轮 item 数线性放大。"""
    engine = EventEngine(db, config, embedder=HashEmbedder())
    engine.refresh_caches()

    calls = {"n": 0}
    original = engine.cluster.event_repo.recent

    def counting_recent(*a, **kw):
        calls["n"] += 1
        return original(*a, **kw)

    engine.cluster.event_repo.recent = counting_recent
    for i in range(8):
        _ingest_item(engine, f"Unrelated headline number {i}",
                     f"https://x.test/{i}", f"S{i}")
    assert calls["n"] == 1, f"窗口被重复拉取 {calls['n']} 次"

    # 下一轮必须重新拉取（跨轮不得沿用陈旧窗口）
    engine.refresh_caches()
    _ingest_item(engine, "Fresh round headline", "https://x.test/next", "S-next")
    assert calls["n"] == 2


def test_same_round_items_still_cluster_into_one_event(db, config):
    """缓存不得把同一轮里的同一事件拆成多个 Event。

    这是窗口缓存最危险的失效模式：第一条 item 建的事件如果没进缓存，
    第二条 item 就看不到它，于是重复建事件（正确性回归，不只是性能）。
    """
    engine = EventEngine(db, config, embedder=HashEmbedder())
    engine.refresh_caches()

    first = _ingest_item(engine, "Micron raises DRAM contract prices",
                         "https://a.test/1", "A1")
    assert first.action == "created"

    # 同一轮、同一事件的另一家转载（标题/正文不同 → 躲过 Level 1 确定性去重，
    # 必须由语义聚类接住）
    second = _ingest_item(engine, "Micron raises DRAM contract prices for Q4",
                          "https://b.test/2", "B2")
    assert second.action in ("merged", "revised"), \
        f"同轮同事件被拆开: {second.action}"
    assert second.event.event_id == first.event.event_id
    assert len(EventRepo(db).recent(hours=72)) == 1


# ---------------------------------------------------------------------------
# Stage B 成本：仅补充佐证的合并不得重复调用 LLM
# ---------------------------------------------------------------------------

def test_evidence_only_merge_skips_stage_b(app, monkeypatch):
    """merged（无实质更新）且事件已有分析结果 → 不再跑 Stage B。"""
    app.db.execute("UPDATE source SET can_store=1,can_display=1 WHERE source_id='src_reuters'")
    pipeline = Pipeline(app)
    analyzed: list[str] = []

    def spy(event, entity_nodes=None, extra_entities=None, **kwargs):
        analyzed.append(event.event_id)
        return []

    monkeypatch.setattr(app.pipeline, "analyze_event", spy)

    now = datetime.now(timezone.utc)
    event = Event(
        event_id="EV-MERGE", title="Micron raises DRAM contract prices",
        summary="Micron raises DRAM contract prices", event_type="regulation",
        status="reported", version=1, first_seen_at=now, last_updated_at=now,
        event_time=now, first_source_id="src_reuters",
        primary_source_id="src_reuters", all_source_ids=["src_reuters"])
    EventRepo(app.db).insert(event)
    from trace.db.jobs import ProcessingJobRepo
    ProcessingJobRepo(app.db).create_or_update('stage_b_analyze','EV-MERGE',status='completed')
    # 事件已有持久化完成回执 → 仅补充佐证时无需重算
    EventImpactRepo(app.db).upsert(EventImpact(
        impact_id="IMP-MERGE", event_id="EV-MERGE", security_id="SEC-US-MU",
        direction="bullish", directness="direct", magnitude=6.0, persistence=5.0,
        directness_score=9.0, confidence=0.8, reason="test",
        source_reliability=9.0, base_score=7.0, final_score=7.0, created_at=now))

    item = RawItem(
        raw_item_id=raw_item_id(), source_id="src_reuters", source_item_id="M1",
        title="Micron raises DRAM contract prices for Q4",
        url="https://m.test/1", published_at=now, language="en",
        content="Micron raises DRAM contract prices for Q4")
    monkeypatch.setattr(app.collectors, "run_all",
                        lambda **kw: ([item], [CollectResult(source_ids=["src_reuters"])]))
    monkeypatch.setattr(app.pipeline.extractor, "extract", lambda it: ExtractedEvent(
        title=it.title, summary=it.title, entities=["Micron"],
        event_type="regulation", event_status="reported", event_time=now))

    summary = pipeline.run_once()
    assert summary.stage_b_skipped == 1, "仅补充佐证的合并仍触发了 Stage B"
    assert analyzed == [], f"不该调用 Stage B: {analyzed}"
    assert summary.events_analyzed == 0


def test_merge_without_prior_analysis_still_runs_stage_b(app, monkeypatch):
    """事件还没有任何分析结果时，合并必须补跑 Stage B（不能被优化掉）。"""
    app.db.execute("UPDATE source SET can_store=1,can_display=1 WHERE source_id='src_reuters'")
    pipeline = Pipeline(app)
    analyzed: list[str] = []
    monkeypatch.setattr(app.pipeline, "analyze_event",
                        lambda event, **kw: (analyzed.append(event.event_id), [])[1])

    now = datetime.now(timezone.utc)
    EventRepo(app.db).insert(Event(
        event_id="EV-NOIMPACT", title="Micron raises DRAM contract prices",
        summary="Micron raises DRAM contract prices", event_type="regulation",
        status="reported", version=1, first_seen_at=now, last_updated_at=now,
        event_time=now, first_source_id="src_reuters",
        primary_source_id="src_reuters", all_source_ids=["src_reuters"]))

    item = RawItem(
        raw_item_id=raw_item_id(), source_id="src_reuters", source_item_id="M2",
        title="Micron raises DRAM contract prices for Q4",
        url="https://m.test/2", published_at=now, language="en",
        content="Micron raises DRAM contract prices for Q4")
    monkeypatch.setattr(app.collectors, "run_all",
                        lambda **kw: ([item], [CollectResult(source_ids=["src_reuters"])]))
    monkeypatch.setattr(app.pipeline.extractor, "extract", lambda it: ExtractedEvent(
        title=it.title, summary=it.title, entities=["Micron"],
        event_type="regulation", event_status="reported", event_time=now))

    summary = pipeline.run_once()
    assert analyzed == ["EV-NOIMPACT"], f"缺分析结果的事件被漏跑: {analyzed}"
    assert summary.stage_b_skipped == 0
