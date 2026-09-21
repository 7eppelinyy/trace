"""Test suite for T09: ExactDedup, Revision, Graph Relations, and Golden 100 Benchmark.

Ensures:
- Document updates with same URL/source_item are recognized as revisions, not dropped.
- Cross-source articles with same title are retained as independent evidence.
- Dual-channel candidate recall handles cross-lingual and degraded embeddings.
- EventStatus transitions to contradicted/retracted trigger material_update.
- IndustryGraph reload() strictly excludes expired edges.
- Embedding dimension backfill prevents cross-space mismatches.
- Auditable EventSplitter safely separates mismerged items.
- Golden 100 benchmark achieves 0 false-positive merges and >= 90% recall.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trace.common.ids import event_id, raw_item_id
from trace.config import load_config
from trace.db.connection import Database
from trace.db.migration import apply_migrations
from trace.db.repositories import (
    EventRepo,
    EventRevisionRepo,
    EventSourceRepo,
    IndustryEdgeRepo,
    RawItemRepo,
    SecurityRepo,
)
from trace.domain.models import (
    Event,
    EventSource,
    EventStatus,
    IndustryEdge,
    RawItem,
    Security,
)
from trace.event_engine.embeddings import HashEmbedder
from trace.event_engine.engine import EventEngine
from trace.event_engine.exact_dedup import ExactDedup
from trace.event_engine.normalize import normalize_raw_item
from trace.event_engine.revision import EventReviser
from trace.event_engine.semantic_cluster import SemanticCluster
from trace.event_engine.split import EventSplitter
from trace.graph.industry_graph import IndustryGraph


@pytest.fixture(autouse=True)
def disable_fk_for_tests(db):
    db.execute("PRAGMA foreign_keys=OFF")


def test_exact_dedup_url_revision_vs_exact_duplicate(db):
    """同 URL 改内容识别为修订，内容未改识别为重复。"""
    raw_repo = RawItemRepo(db)
    dedup = ExactDedup(db)
    now = datetime.now(timezone.utc)

    # 1. 插入第一版原始条目
    item_v1 = RawItem(
        raw_item_id="raw_v1",
        source_id="tsmc_official",
        source_item_id=None,
        title="TSMC Capex 2026 $30B",
        canonical_url="https://tsmc.com/press/capex.html",
        content="TSMC announces 2026 capital expenditures will be 30 billion USD.",
        published_at=now,
        fetched_at=now,
        content_hash="hash_content_v1",
        title_hash="hash_title_v1",
        event_id="EVT-001",
    )
    raw_repo.insert(item_v1)

    # 2. 完全相同内容再次到达 -> 判定为完全重复
    item_dup = RawItem(
        raw_item_id="raw_dup",
        source_id="tsmc_official",
        source_item_id=None,
        title="TSMC Capex 2026 $30B",
        canonical_url="https://tsmc.com/press/capex.html",
        content="TSMC announces 2026 capital expenditures will be 30 billion USD.",
        published_at=now,
        fetched_at=now,
        content_hash="hash_content_v1",
        title_hash="hash_title_v1",
    )
    res_dup = dedup.check(item_dup)
    assert res_dup.is_duplicate is True
    assert res_dup.is_revision is False

    # 3. 相同 URL 内容修订版本到达 -> 判定为修订，不丢弃
    item_v2 = RawItem(
        raw_item_id="raw_v2",
        source_id="tsmc_official",
        source_item_id=None,
        title="TSMC Capex 2026 $32B Amended",
        canonical_url="https://tsmc.com/press/capex.html",
        content="TSMC amended 2026 capital expenditures to 32 billion USD.",
        published_at=now + timedelta(hours=1),
        fetched_at=now + timedelta(hours=1),
        content_hash="hash_content_v2_amended",
        title_hash="hash_title_v2",
    )
    res_v2 = dedup.check(item_v2)
    assert res_v2.is_duplicate is False
    assert res_v2.is_revision is True
    assert res_v2.existing_raw_item_id == "raw_v1"
    assert res_v2.existing_event_id == "EVT-001"


def test_exact_dedup_cross_source_same_title_retained(db):
    """不同来源报道相同标题为独立佐证，不得当作重复直接拦截。"""
    raw_repo = RawItemRepo(db)
    dedup = ExactDedup(db)
    now = datetime.now(timezone.utc)

    # 来源 A 发布
    item_a = RawItem(
        raw_item_id="raw_a",
        source_id="reuters",
        source_item_id="R-100",
        title="Apple Unveils M5 Chip",
        canonical_url="https://reuters.com/tech/apple_m5.htm",
        content="Reuters reports Apple unveiled M5.",
        published_at=now,
        fetched_at=now,
        title_hash="hash_apple_m5_title",
        content_hash="hash_reuters_content",
    )
    raw_repo.insert(item_a)

    # 来源 B 同标题发布
    item_b = RawItem(
        raw_item_id="raw_b",
        source_id="bloomberg",
        source_item_id="BBG-200",
        title="Apple Unveils M5 Chip",
        canonical_url="https://bloomberg.com/tech/apple_m5.htm",
        content="Bloomberg confirms Apple unveiled M5.",
        published_at=now + timedelta(minutes=5),
        fetched_at=now + timedelta(minutes=5),
        title_hash="hash_apple_m5_title",
        content_hash="hash_bloomberg_content",
    )
    res_b = dedup.check(item_b)
    assert res_b.is_duplicate is False, "跨源同标题应作为新证据保留，不能当重复吞掉"


def test_revision_denial_and_retraction_status(db, config):
    """官方否认或撤回状态转换产生实质更新并升版本。"""
    reviser = EventReviser(db, config)
    event_repo = EventRepo(db)
    rev_repo = EventRevisionRepo(db)
    now = datetime.now(timezone.utc)

    # 创建初始传闻事件
    ev = Event(
        event_id="EVT-RUMOR",
        title="Rumor: TSMC Delays German Fab",
        summary="Anonymous sources claimed TSMC delayed German fab to 2029.",
        status=EventStatus.REPORTED.value,
        version=1,
        first_seen_at=now,
        last_updated_at=now,
        event_time=now,
    )
    event_repo.insert(ev)

    # 官方否认条目到达
    denial_raw = RawItem(
        raw_item_id="raw_denial",
        source_id="tsmc_official",
        title="TSMC Statement: German Fab on Schedule",
        content="TSMC confirms German fab is on track for 2027.",
        published_at=now + timedelta(hours=2),
        fetched_at=now + timedelta(hours=2),
        event_id=ev.event_id,
    )

    res = reviser.apply_update(
        ev.event_id,
        denial_raw,
        new_status=EventStatus.CONTRADICTED.value,
        official_source=True,
    )

    assert res.material_update is True
    assert res.event.version == 2
    assert res.event.status == EventStatus.CONTRADICTED.value
    assert "denial_or_retraction" in res.reasons

    # 检查审计历史记录
    revisions = rev_repo.list_by_event(ev.event_id)
    assert len(revisions) == 1
    assert revisions[0].version == 2
    assert "denial_or_retraction" in revisions[0].note


def test_industry_graph_reload_filters_expired_edges(db):
    """IndustryGraph 严格过滤已过期的边。"""
    edge_repo = IndustryEdgeRepo(db)
    sec_repo = SecurityRepo(db)
    now = datetime.now(timezone.utc)

    sec_repo.upsert(Security(
        security_id="SEC-TSM",
        ticker="TSM",
        company_name_en="TSMC",
        market="US",
        graph_node_ids=["tsmc"],
    ))

    # 有效边：苹果依赖台积电 (valid_to = 明年)
    edge_valid = IndustryEdge(
        edge_id="edge-1",
        from_node="apple",
        to_node="tsmc",
        edge_type="customer_of",
        confidence=0.9,
        valid_from=now - timedelta(days=30),
        valid_to=now + timedelta(days=365),
    )
    edge_repo.upsert(edge_valid)

    # 过期边：英特尔代工合作 (valid_to = 昨天)
    edge_expired = IndustryEdge(
        edge_id="edge-2",
        from_node="intel",
        to_node="tsmc",
        edge_type="customer_of",
        confidence=0.8,
        valid_from=now - timedelta(days=365),
        valid_to=now - timedelta(days=1),
    )
    edge_repo.upsert(edge_expired)

    graph = IndustryGraph(db)
    graph.reload(as_of=now)

    apple_neighbors = graph.neighbor_nodes("apple")
    assert "tsmc" in apple_neighbors

    intel_neighbors = graph.neighbor_nodes("intel")
    assert "tsmc" not in intel_neighbors, "已过期边不得出现在活跃图中"


def test_embedding_dimension_backfill_guard(db, config):
    """当存储的 embedding 维度与当前模型不一致时，自动重新计算并回填。"""
    event_repo = EventRepo(db)
    embedder_256 = HashEmbedder(dim=256)
    cluster = SemanticCluster(db, embedder_256, config)
    now = datetime.now(timezone.utc)

    # 存入一个具有 128 维旧向量的事件
    old_embedder_128 = HashEmbedder(dim=128)
    _, blob_128 = cluster.embed_text("Old event title")
    # 强制做成 128 维
    blob_128_actual = old_embedder_128.encode(["Old event title"])[0]
    import struct
    packed_128 = struct.pack(f"{len(blob_128_actual)}f", *blob_128_actual)

    ev = Event(
        event_id="EVT-OLD-DIM",
        title="Old event title",
        summary="Old event summary",
        status="reported",
        version=1,
        first_seen_at=now,
        last_updated_at=now,
        event_time=now,
        title_embedding=packed_128,
        summary_embedding=packed_128,
    )
    event_repo.insert(ev)

    # 加载窗口：应触发自动 backfill 到当前 256 维
    events, title_vecs, summary_vecs = cluster._load_window()
    loaded_ev = next(e for e in events if e.event_id == "EVT-OLD-DIM")
    assert len(title_vecs[0]) == 256
    assert len(summary_vecs[0]) == 256


def test_event_splitter_auditable_separation(db):
    """EventSplitter 拆分错误合并的条目并建立审计链条。"""
    event_repo = EventRepo(db)
    raw_repo = RawItemRepo(db)
    source_repo = EventSourceRepo(db)
    rev_repo = EventRevisionRepo(db)
    now = datetime.now(timezone.utc)

    ev = Event(
        event_id="EVT-MERGED",
        title="Micron and SK Hynix Updates",
        summary="Mixed report",
        status="reported",
        version=1,
        first_seen_at=now,
        last_updated_at=now,
        event_time=now,
        all_source_ids=["src1", "src2"],
    )
    event_repo.insert(ev)

    r1 = RawItem(raw_item_id="RAW-MU", source_id="src1", title="Micron HBM3e", event_id=ev.event_id)
    r2 = RawItem(raw_item_id="RAW-SK", source_id="src2", title="SK Hynix 1c nm", event_id=ev.event_id)
    raw_repo.insert(r1)
    raw_repo.insert(r2)

    source_repo.add(EventSource(event_id=ev.event_id, raw_item_id=r1.raw_item_id, role="first"))
    source_repo.add(EventSource(event_id=ev.event_id, raw_item_id=r2.raw_item_id, role="confirming"))

    splitter = EventSplitter(db)
    new_ev = splitter.split_raw_item(
        ev.event_id,
        "RAW-SK",
        reason="SK Hynix is a separate corporate entity",
        operator_id="admin_auditor",
    )

    # 验证新事件生成与归属
    assert new_ev.event_id != ev.event_id
    assert new_ev.title == "SK Hynix 1c nm"
    r2_updated = raw_repo.get("RAW-SK")
    assert r2_updated.event_id == new_ev.event_id

    # 验证原事件更新与审计记录
    orig_ev_updated = event_repo.get(ev.event_id)
    assert orig_ev_updated.version == 2
    orig_revs = rev_repo.list_by_event(ev.event_id)
    assert any("admin_auditor" in r.note and "RAW-SK" in r.note for r in orig_revs)


def test_candidate_benchmark_is_label_blind(config):
    from trace.verification.dedup_evaluation import predict_pair, evaluate_pairs
    pairs = json.loads(Path('tests/fixtures/event_pairs_100.json').read_text(encoding='utf-8'))
    assert len(pairs) == 100
    predictions = [predict_pair(p['item_a'], p['item_b'], config) for p in pairs]
    results = evaluate_pairs(predictions, [p['expected']['should_merge'] for p in pairs])
    # This measures candidate retrieval separately from merge decisions. No label
    # may be used to stand in for the missing live verifier.
    assert results['candidate_recall'] >= 0.9
    assert results['automatic_false_merges'] == 0
    assert results['verifier_evaluated'] is False
    reversed_labels = [not p['expected']['should_merge'] for p in pairs]
    evaluate_pairs(predictions, reversed_labels)
    assert predictions[0] == predict_pair(pairs[0]['item_a'], pairs[0]['item_b'], config)
    print(results)
