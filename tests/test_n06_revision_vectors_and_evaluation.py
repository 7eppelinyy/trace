"""N06 专项测试：事件修订、向量版本与模型身份隔离、及独立评测保真。

测试覆盖：
1. 重放同一 source_item_id 内容变化：两份 RawItem 均完整保留，且关联同一 Event 的新版本。
2. Pipeline.run_once 真实入口验证：修订目标沿 Durable Job input_json 精准传递，包括超出 72 小时聚类窗口的历史事件。
3. 官方否认、撤回与恢复确认状态机：非权威源不能覆盖官方否认；官方确认可使否认事件恢复确认，且记录 recovery_to_confirmed。
4. 文本变更清空/重算向量与模型身份跟踪：同维不同模型、同模型不同 revision 均触发向量重算；模型身份持久化在 DB。
5. Label-blind 评测隔离与 100 对测试集完整性：SHA256 校验、预测器无标签泄漏、30 自动合并、70 待审核、无盲目声称端到端准确率。
"""

from __future__ import annotations

import hashlib
import json
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from trace.ai.extractor import ExtractedEvent
from trace.config import load_config
from trace.db.connection import Database
from trace.db.migration import apply_migrations
from trace.db.repositories import EventRepo, EventRevisionRepo, RawItemRepo, SourceRepo
from trace.domain.models import Event, EventStatus, RawItem, Source
from trace.event_engine.embeddings import Embedder, HashEmbedder
from trace.event_engine.engine import EventEngine
from trace.event_engine.exact_dedup import ExactDedup
from trace.event_engine.normalize import normalize_raw_item
from trace.event_engine.revision import EventReviser
from trace.event_engine.semantic_cluster import SemanticCluster
from trace.pipeline import Pipeline
from trace.verification.dedup_evaluation import evaluate_pairs, predict_pair


# ---------------------------------------------------------------------------
# 1. 重放同一 source_item_id 内容变更测试
# ---------------------------------------------------------------------------

def test_replay_same_source_item_id_retains_two_raw_items_and_advances_version(app):
    """验证同一 source_item_id 文本变化时，两份 RawItem 记录均保留并关联同一 Event 的新版本。"""
    db = app.db
    engine = app.event_engine
    raw_repo = RawItemRepo(db)
    ev_repo = EventRepo(db)
    rev_repo = EventRevisionRepo(db)
    source_repo = SourceRepo(db)
    now = datetime.now(timezone.utc)

    db.execute("UPDATE source SET can_display=1, enabled=1 WHERE source_id='src_sec_edgar'")

    # 1. 模拟初次采集条目
    item_v1 = RawItem(
        raw_item_id="raw_doc_v1",
        source_id="src_sec_edgar",
        source_item_id="0001018724-26-000001",
        title="Amazon Announces Q3 2026 Operating Income $15B",
        content="Amazon today announced Q3 2026 operating income of 15 billion USD.",
        published_at=now,
        fetched_at=now,
    )
    normalize_raw_item(item_v1)
    
    extracted_v1 = ExtractedEvent(
        title=item_v1.title,
        summary=item_v1.content,
        entities=["Amazon"],
        event_type="earnings",
        event_status="official_confirmed",
        key_numbers=["15 billion USD"],
    )

    dec_v1 = engine.ingest(item_v1, extracted_v1)
    assert dec_v1.action == "created"
    e_id = dec_v1.event.event_id
    ev_1 = ev_repo.get(e_id)
    assert ev_1.version == 1

    # 2. 模拟同一 source_item_id 发出更正/修订版本（数字修正为 18B）
    item_v2 = RawItem(
        raw_item_id="raw_doc_v2",
        source_id="src_sec_edgar",
        source_item_id="0001018724-26-000001",
        title="Amazon Announces Q3 2026 Operating Income $18B (Amended)",
        content="Amazon today filed an amendment: Q3 2026 operating income was 18 billion USD.",
        published_at=now + timedelta(hours=1),
        fetched_at=now + timedelta(hours=1),
    )
    normalize_raw_item(item_v2)

    extracted_v2 = ExtractedEvent(
        title=item_v2.title,
        summary=item_v2.content,
        entities=["Amazon"],
        event_type="earnings",
        event_status="official_confirmed",
        key_numbers=["18 billion USD"],
    )

    dec_v2 = engine.ingest(item_v2, extracted_v2)
    assert dec_v2.action == "revised"
    assert dec_v2.event.event_id == e_id
    assert dec_v2.event.version == 2

    # 3. 验证数据库中两份 RawItem 完整保留且均关联至同一个 event_id
    stored_v1 = raw_repo.get("raw_doc_v1")
    stored_v2 = raw_repo.get("raw_doc_v2")
    assert stored_v1 is not None
    assert stored_v2 is not None
    assert stored_v1.raw_item_id != stored_v2.raw_item_id
    assert stored_v1.source_item_id == stored_v2.source_item_id == "0001018724-26-000001"
    assert stored_v1.event_id == e_id
    assert stored_v2.event_id == e_id
    assert "15B" in stored_v1.title
    assert "18B" in stored_v2.title

    # 4. 验证修订审计历史
    revs = rev_repo.list_by_event(e_id)
    assert len(revs) == 1
    assert revs[0].version == 2
    assert "key_number_changed" in revs[0].note or "document_corrected" in revs[0].note


# ---------------------------------------------------------------------------
# 2. Pipeline.run_once 入口级修订目标穿透测试（含超 72 小时旧事件）
# ---------------------------------------------------------------------------

def test_pipeline_run_once_revision_propagation_past_72h_window(app, monkeypatch):
    """验证从 Pipeline.run_once 入口摄取时，超过 72 小时聚类窗口的历史事件仍能正确修订。"""
    db = app.db
    ev_repo = EventRepo(db)
    raw_repo = RawItemRepo(db)
    source_repo = SourceRepo(db)

    # 确保存储和展示权限开启
    source_repo.upsert(
        Source(
            source_id="src_historical",
            source_name="Historical SEC Source",
            source_type="official",
            enabled=True,
            can_fetch=True,
            can_store=True,
            can_display=True,
        )
    )

    # 1. 在 10 天前（远超 72h 窗口）创建初始历史事件与原始记录
    ten_days_ago = datetime.now(timezone.utc) - timedelta(days=10)
    old_raw = RawItem(
        raw_item_id="raw_old_10d",
        source_id="src_historical",
        source_item_id="FILING-OLD-001",
        title="Initial Filing: Expansion in Dresden",
        canonical_url="https://sec.gov/filings/old_001.htm",
        content="Company plans initial plant investment of 5B.",
        published_at=ten_days_ago,
        fetched_at=ten_days_ago,
        event_id="EVT-HISTORICAL-10D",
    )
    normalize_raw_item(old_raw)
    raw_repo.insert(old_raw)

    old_ev = Event(
        event_id="EVT-HISTORICAL-10D",
        title=old_raw.title,
        summary=old_raw.content,
        first_seen_at=ten_days_ago,
        last_updated_at=ten_days_ago,
        status=EventStatus.OFFICIAL_CONFIRMED.value,
        version=1,
        first_source_id="src_historical",
    )
    ev_repo.insert(old_ev)

    # 2. 模拟采集器在当前轮次采集到该文档的内容修订版本（URL 相同或 source_item_id 相同，内容更新为 8B）
    now = datetime.now(timezone.utc)
    amended_item = RawItem(
        raw_item_id="raw_amended_now",
        source_id="src_historical",
        source_item_id="FILING-OLD-001",
        title="Amended Filing: Expansion in Dresden to 8B",
        canonical_url="https://sec.gov/filings/old_001.htm",
        content="Company amended Dresden plant investment from 5B to 8B.",
        published_at=now,
        fetched_at=now,
    )
    normalize_raw_item(amended_item)

    # Mock 采集器返回此条目
    class FakeCollectorResult:
        status = "ok"
        keyword_filtered = 0
        source_ids = ["src_historical"]

    monkeypatch.setattr(app.collectors, "run_all", lambda **kw: ([amended_item], [FakeCollectorResult()]))
    monkeypatch.setattr(app.collectors, "commit_cursors", lambda: 1)
    monkeypatch.setattr(app.collectors, "discard_cursors", lambda: 0)

    # 3. 触发 Pipeline.run_once 完整闭环
    pipeline = Pipeline(app)
    summary = pipeline.run_once()

    # 4. 验证统计指标与数据库穿透效果
    assert summary.raw_items_new >= 1
    assert summary.events_revised >= 1

    # 验证原事件被成功修订至版本 2，且即使超过 72h 也未被误拆成两个事件！
    updated_ev = ev_repo.get("EVT-HISTORICAL-10D")
    assert updated_ev.version == 2
    assert "8B" in updated_ev.title
    assert updated_ev.material_update is True

    # 验证新 raw_item 同样落库且关联至老事件
    stored_amended = raw_repo.get("raw_amended_now")
    assert stored_amended is not None
    assert stored_amended.event_id == "EVT-HISTORICAL-10D"


# ---------------------------------------------------------------------------
# 3. 官方否认、撤回与恢复确认状态机测试
# ---------------------------------------------------------------------------

def test_status_machine_denial_retraction_and_recovery(app):
    """验证官方否认优先于媒体报道、非官方来源不能覆盖官方否认、官方确认可恢复确认。"""
    db = app.db
    ev_repo = EventRepo(db)
    raw_repo = RawItemRepo(db)
    source_repo = SourceRepo(db)
    reviser = EventReviser(db, app.config)
    now = datetime.now(timezone.utc)

    source_repo.upsert(
        Source(source_id="src_rumor_media", source_name="Media", source_type="financial_media", authority_level="financial_media", enabled=True, can_fetch=True, can_store=True, can_display=True)
    )
    source_repo.upsert(
        Source(source_id="src_company_ir", source_name="IR", source_type="official", authority_level="official_company", enabled=True, can_fetch=True, can_store=True, can_display=True)
    )

    # 1. 初始传闻事件 (status=reported)
    ev = Event(
        event_id="EVT-FAB-RUMOR",
        title="Rumor: Fab Construction Delayed",
        summary="Media reports fab delay.",
        status=EventStatus.REPORTED.value,
        version=1,
        first_seen_at=now,
        last_updated_at=now,
        first_source_id="src_rumor_media",
    )
    ev_repo.insert(ev)

    # 2. 官方否认到达 (status -> contradicted)
    denial_raw = RawItem(
        raw_item_id="raw_denial_stmt",
        source_id="src_company_ir",
        title="Official Statement: Rumor Denied",
        content="Company denies delay rumors; timeline remains intact.",
        published_at=now + timedelta(minutes=10),
        fetched_at=now + timedelta(minutes=10),
        event_id=ev.event_id,
    )
    raw_repo.insert(denial_raw)

    res_denial = reviser.apply_update(
        ev.event_id,
        denial_raw,
        new_status=EventStatus.CONTRADICTED.value,
        official_source=True,
    )
    assert res_denial.material_update is True
    assert res_denial.event.status == EventStatus.CONTRADICTED.value
    assert res_denial.event.version == 2
    assert "denial_or_retraction" in res_denial.reasons

    # 3. 普通媒体再次声称确有其事 (非官方来源试图推翻官方否认 -> 必须被状态机拒绝)
    unofficial_raw = RawItem(
        raw_item_id="raw_media_repeat",
        source_id="src_rumor_media",
        title="Anonymous Source Insists Fab Delayed",
        content="Insiders insist delay is real.",
        published_at=now + timedelta(minutes=20),
        fetched_at=now + timedelta(minutes=20),
        event_id=ev.event_id,
    )
    raw_repo.insert(unofficial_raw)

    # 在 EventEngine._merge_into 规则下：若当前为 contradicted，非 official 来源无法恢复
    engine = app.event_engine
    dec_media = engine._merge_into(
        res_denial.event,
        unofficial_raw,
        ExtractedEvent(title=unofficial_raw.title, summary=unofficial_raw.content, entities=["TSMC"], event_type="capacity", event_status="reported"),
    )
    # 状态严格保持 contradicted，没有被媒体污染
    assert dec_media.event.status == EventStatus.CONTRADICTED.value

    # 4. 后续官方权威澄清恢复确认 (status -> official_confirmed)
    recovery_raw = RawItem(
        raw_item_id="raw_official_recovery",
        source_id="src_company_ir",
        title="Official Clarification: All Regulatory Approvals Secured",
        content="CEO confirms all conditions cleared.",
        published_at=now + timedelta(hours=2),
        fetched_at=now + timedelta(hours=2),
        event_id=ev.event_id,
    )
    raw_repo.insert(recovery_raw)

    dec_recovery = engine._merge_into(
        dec_media.event,
        recovery_raw,
        ExtractedEvent(title=recovery_raw.title, summary=recovery_raw.content, entities=["TSMC"], event_type="capacity", event_status="official_confirmed"),
    )
    assert dec_recovery.event.status == EventStatus.OFFICIAL_CONFIRMED.value
    assert dec_recovery.event.version == 3
    assert dec_recovery.action == "revised"
    # 确认恢复原因包含 recovery_to_confirmed 且在复推白名单内
    assert "recovery_to_confirmed" in dec_recovery.reason
    assert dec_recovery.resend_allowed is True


# ---------------------------------------------------------------------------
# 4. 向量版本、模型身份与文本变更失效测试
# ---------------------------------------------------------------------------

class CustomMockEmbedder(Embedder):
    """测试用具：具有指定 model_name、dim 与 revision 的测试 Embedder。"""
    def __init__(self, model_name: str, dim: int = 256, revision: str = "v1"):
        self.model_name = model_name
        self.dim = dim
        self.revision = revision
        self.is_degraded = True

    def encode(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            # 简单可复现伪向量
            val = float(len(t))
            out.append([val] * self.dim)
        return out


def test_embedding_invalidation_on_title_summary_change(app):
    """验证 title/summary 更新后原向量立即失效并重算，embedding_identity 保持一致。"""
    db = app.db
    ev_repo = EventRepo(db)
    source_repo = SourceRepo(db)
    cluster = app.event_engine.cluster
    now = datetime.now(timezone.utc)

    source_repo.upsert(
        Source(source_id="src_sec", source_name="SEC", source_type="official", authority_level="official_company", enabled=True, can_fetch=True, can_store=True, can_display=True)
    )

    # 1. 新建事件，记录初始向量
    tv_orig, blob_orig = cluster.embed_text("Original Title A")
    sv_orig, sblob_orig = cluster.embed_text("Original Summary A")
    ev = Event(
        event_id="EVT-VEC-TEST",
        title="Original Title A",
        summary="Original Summary A",
        first_seen_at=now,
        last_updated_at=now,
        version=1,
        title_embedding=blob_orig,
        summary_embedding=sblob_orig,
        embedding_model=cluster.embedding_identity,
    )
    ev_repo.insert(ev)

    # 验证 DB 中存有 embedding_model
    fetched = ev_repo.get("EVT-VEC-TEST")
    assert fetched.embedding_model == cluster.embedding_identity

    # 2. 通过 reviser 修改 title
    reviser = app.event_engine.reviser
    raw_amend = RawItem(raw_item_id="raw_mod_title", source_id="src_sec", title="New Revised Title B")
    res = reviser.apply_update(ev.event_id, raw_amend, new_title="New Revised Title B", document_changed=True)
    
    # 验证更新文本后，旧向量已在内存和 DB 中被清空
    assert res.event.title_embedding is None
    assert res.event.embedding_model is None
    db_row = ev_repo.get("EVT-VEC-TEST")
    assert db_row.title_embedding is None
    assert db_row.embedding_model is None

    # 3. 通过 EventEngine._merge_into 重新计算并回填新向量
    engine = app.event_engine
    dec = engine._merge_into(
        ev,
        raw_amend,
        ExtractedEvent(title="New Revised Title B", summary="Original Summary A", entities=["Company"], event_type="earnings", event_status="reported"),
        document_changed=True,
    )
    assert dec.event.title_embedding is not None
    assert dec.event.embedding_model == cluster.embedding_identity
    # 确认新向量不同于旧向量！
    assert dec.event.title_embedding != blob_orig


def test_embedding_model_identity_mismatch_triggers_recalculation(db, app):
    """测试同维不同模型（同为 256 维但 model_name 不同）在 _load_window 时自动触发重新计算。"""
    ev_repo = EventRepo(db)
    now = datetime.now(timezone.utc)

    # 模型 1 (model_alpha, 256维)
    emb_alpha = CustomMockEmbedder("model_alpha", dim=256, revision="v1")
    identity_alpha = f"CustomMockEmbedder:model_alpha:v1:256"

    # 生成由模型 1 计算的向量
    packed_alpha = struct.pack("256f", *([1.0] * 256))
    ev = Event(
        event_id="EVT-ALPHA",
        title="Title Embedded by Alpha",
        summary="Summary Embedded by Alpha",
        first_seen_at=now,
        last_updated_at=now,
        version=1,
        title_embedding=packed_alpha,
        summary_embedding=packed_alpha,
        embedding_model=identity_alpha,
    )
    ev_repo.insert(ev)

    # 模拟系统切换到模型 2 (model_beta, 依然为 256 维，但模型语义完全不同)
    emb_beta = CustomMockEmbedder("model_beta", dim=256, revision="v1")
    cluster_beta = SemanticCluster(db, emb_beta, app.config)
    assert cluster_beta.embedding_identity == "CustomMockEmbedder:model_beta:v1:256"
    assert cluster_beta.embedding_identity != identity_alpha

    # 窗口加载：虽然维度同为 256，但因 embedding_model identity 不匹配，必须触发自动重算！
    events, title_vecs, summary_vecs = cluster_beta._load_window()
    assert len(events) == 1
    # 验证 DB 中的 embedding_model 被自动更新为 beta
    updated_ev = ev_repo.get("EVT-ALPHA")
    assert updated_ev.embedding_model == cluster_beta.embedding_identity


def test_same_model_different_revision_triggers_recalculation(db, app):
    """测试同模型不同 revision（如 v1 升级到 v2）在 _load_window 时触发重算。"""
    ev_repo = EventRepo(db)
    now = datetime.now(timezone.utc)

    emb_v1 = CustomMockEmbedder("bge_m3", dim=128, revision="v1")
    identity_v1 = "CustomMockEmbedder:bge_m3:v1:128"
    packed_v1 = struct.pack("128f", *([0.5] * 128))

    ev = Event(
        event_id="EVT-REV-V1",
        title="Title V1",
        summary="Summary V1",
        first_seen_at=now,
        last_updated_at=now,
        version=1,
        title_embedding=packed_v1,
        summary_embedding=packed_v1,
        embedding_model=identity_v1,
    )
    ev_repo.insert(ev)

    # 升级到 revision v2
    emb_v2 = CustomMockEmbedder("bge_m3", dim=128, revision="v2")
    cluster_v2 = SemanticCluster(db, emb_v2, app.config)

    cluster_v2._load_window()
    updated = ev_repo.get("EVT-REV-V1")
    assert updated.embedding_model == cluster_v2.embedding_identity
    assert "v2" in updated.embedding_model


# ---------------------------------------------------------------------------
# 5. Label-blind 评测隔离与 100 对测试集完整性验证
# ---------------------------------------------------------------------------

def test_100_pair_fixture_checksum_and_label_blind_contract(config):
    """验证 100 对测试集哈希保真、预测器严格 label-blind、且评测指标诚实复现。"""
    fixture_path = Path("tests/fixtures/event_pairs_100.json")
    assert fixture_path.exists(), "tests/fixtures/event_pairs_100.json must exist in repository"

    # 1. 校验 SHA256 指纹，确保测试集未被篡改（跨平台规范化 LF 换行符）
    data_bytes = fixture_path.read_bytes()
    expected_lf_sha256 = "a618eed239a2800b6bd50c6aa0d379b7ee0b2d3f8f91af97502b63bb7f75e432"
    actual_lf_sha256 = hashlib.sha256(data_bytes.replace(b"\r\n", b"\n")).hexdigest()
    assert actual_lf_sha256 == expected_lf_sha256, f"Fixture SHA256 mismatch! Got: {actual_lf_sha256}"

    pairs = json.loads(data_bytes.decode("utf-8"))
    assert len(pairs) == 100

    # 2. 验证 predict_pair 接口的 label-blind 隔离契约：
    # 函数签名只接收文档 item_a 和 item_b，绝对不接收包含 expected 标签的 pair 对象！
    predictions = [predict_pair(p["item_a"], p["item_b"], config) for p in pairs]
    assert len(predictions) == 100

    # 3. 统计无 verifier 情况下的原始评测指标（与规划书 §9.8 严格对齐）
    labels = [p["expected"]["should_merge"] for p in pairs]
    results = evaluate_pairs(predictions, labels)

    assert results["samples"] == 100
    assert results["candidate_recall"] == 1.0
    assert results["automatic_false_merges"] == 0
    assert results["automatic_correct_merges"] == 30
    assert results["needs_verifier"] == 70
    assert results["verifier_evaluated"] is False
    assert results["verifier_status"] == "pending_real_verifier"

    # 4. 验证标签反转对抗：标签仅影响最终打分，绝不改变模型预测输出
    reversed_labels = [not y for y in labels]
    res_rev = evaluate_pairs(predictions, reversed_labels)
    # 反转标签后，原本正确的 30 个合并现在变成了 30 个 false_merges，说明标签严格只在评分函数生效
    assert res_rev["automatic_false_merges"] == 30

    # 5. 验证可选 Verifier 运行入口
    def perfect_mock_verifier(doc_a, doc_b):
        # 模拟接入真实 Verifier 时的评估接口
        return True

    res_with_verifier = evaluate_pairs(predictions, labels, verifier_fn=perfect_mock_verifier, pairs=pairs)
    assert res_with_verifier["verifier_evaluated"] is True
    assert res_with_verifier["verifier_status"] == "evaluated"
    assert res_with_verifier["verifier_correct_merges"] == 35  # 65 总正例 - 30 自动 = 35 由 verifier 正确召回
    assert res_with_verifier["verifier_false_merges"] == 35   # 35 负例均被 mock 判为 True
