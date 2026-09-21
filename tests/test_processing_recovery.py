"""持久化处理作业与人审恢复机制测试 (T03 / F03 / Probe 11.2)。"""

from __future__ import annotations

from datetime import datetime, timezone
import pytest

from trace.ai.budget import LLMBudgetExceededError
from trace.ai.schemas import SchemaValidationError
from trace.collectors.base import BaseCollector
from trace.common.ids import raw_item_id
from trace.common.modes import STATUS_OK, STOP_LLM_BUDGET_EXCEEDED
from trace.db.health import CursorRepo, HumanReviewRepo
from trace.db.repositories import EventRepo, ProcessingJobRepo, RawItemRepo
from trace.domain.models import RawItem
from trace.event_engine.engine import ExtractedEvent
from trace.pipeline import Pipeline


CURSOR_SOURCE = "src_digitimes"


class _StubCollector(BaseCollector):
    collector_type = "rss"

    def __init__(self, db, config, item_ids: list[str]):
        super().__init__(db, config)
        self._item_ids = item_ids

    @property
    def handled_source_ids(self) -> set[str]:
        return {CURSOR_SOURCE}

    def collect(self) -> list[RawItem]:
        cursor = self.cursor_repo.get(CURSOR_SOURCE)
        seen = set(cursor.get("seen_item_ids", []))
        now = datetime.now(timezone.utc)
        items = []
        for iid in self._item_ids:
            if iid in seen:
                continue
            seen.add(iid)
            items.append(RawItem(
                raw_item_id=raw_item_id(), source_id=CURSOR_SOURCE,
                source_item_id=iid, title=f"Micron 公告 {iid}",
                url=f"https://s.test/{iid}", published_at=now,
                language="zh", content=f"Micron 重要业务变动详情 {iid}"))
        self.cursor_repo.set(CURSOR_SOURCE, {"seen_item_ids": sorted(seen)})
        return items


def _wire_test_env(app, monkeypatch, item_ids: list[str]) -> tuple[Pipeline, _StubCollector]:
    collector = _StubCollector(app.db, app.config, item_ids)
    monkeypatch.setattr(app.collectors, "_collectors", [collector])
    monkeypatch.setattr(app.pipeline.extractor, "extract", lambda it: ExtractedEvent(
        title=it.title, summary=it.content or it.title, entities=["Micron"],
        event_type="regulation", event_status="reported",
        event_time=it.published_at))
    return Pipeline(app), collector


def test_probe_11_2_stage_b_recovery_after_budget_stop(app, monkeypatch):
    """Probe 11.2 恢复用例：Stage B 预算耗尽中断后，在无新采集条目的下一轮中能自动恢复。"""
    pipeline, _ = _wire_test_env(app, monkeypatch, ["B-STOP"])

    # 1. 模拟 Stage B 遭遇预算熔断
    monkeypatch.setattr(
        app.pipeline, "analyze_event",
        lambda event, **kw: (_ for _ in ()).throw(LLMBudgetExceededError("budget exhausted in Stage B"))
    )

    # 2. 运行第一轮
    summary1 = pipeline.run_once()
    assert summary1.status == STOP_LLM_BUDGET_EXCEEDED
    assert summary1.cursors_committed == 1, "游标应已正常提交，防止重复采集"
    assert summary1.events_created == 1, "事件已创建"
    assert summary1.events_analyzed == 0, "Stage B 熔断未产出分析"

    # 验证 processing_job 状态被置为 blocked_budget
    job_repo = ProcessingJobRepo(app.db)
    ev_list = EventRepo(app.db).recent(hours=24, limit=5)
    assert len(ev_list) == 1
    ev_id = ev_list[0].event_id
    job = job_repo.get_by_target("stage_b_analyze", ev_id)
    assert job is not None
    assert job.status == "blocked_budget"

    # 3. 预算恢复，还原 analyze_event 为成功替身（返回空 impacts 模拟无影响事件）
    stage_b_calls = []

    def mock_analyze(event, **kw):
        stage_b_calls.append(event.event_id)
        return []  # 合法空 impacts

    monkeypatch.setattr(app.pipeline, "analyze_event", mock_analyze)

    # 4. 运行第二轮：由于游标已提交，采集器无新条目
    summary2 = pipeline.run_once()
    assert summary2.status == STATUS_OK
    assert summary2.raw_items_new == 0, "采集器无新条目"

    # 5. 断言：上一轮遗留的 Stage B 任务被成功恢复并执行
    assert len(stage_b_calls) == 1
    assert stage_b_calls[0] == ev_id
    assert summary2.events_analyzed == 1, "恢复分析的事件记入 events_analyzed"

    # 6. 断言：job 状态被更新为 succeeded_empty，而非永久 pending 或 blocked
    job_after = job_repo.get_by_target("stage_b_analyze", ev_id)
    assert job_after is not None
    assert job_after.status == "succeeded_empty"

    # 7. 再次运行第三轮，断言不会无限重复处理该事件
    stage_b_calls.clear()
    summary3 = pipeline.run_once()
    assert len(stage_b_calls) == 0, "已完成的事件不得重复执行 Stage B"


def test_probe_11_2_human_review_and_raw_item_integrity(app, monkeypatch):
    """Probe 11.2 人审用例：Stage A Schema 校验失败送人工检查时，必须已完整持久化 RawItem。"""
    pipeline, _ = _wire_test_env(app, monkeypatch, ["A-SCHEMA"])

    # 1. 模拟 Stage A 抛出 SchemaValidationError
    monkeypatch.setattr(
        app.pipeline.extractor, "extract",
        lambda it: (_ for _ in ()).throw(SchemaValidationError("invalid json: missing summary"))
    )

    # 2. 运行流水线
    summary = pipeline.run_once()
    assert summary.human_review == 1
    assert summary.cursors_committed == 1

    # 3. 执行探针 SQL 校验人审与 raw_item 关联完整性
    rows = app.db.query(
        """SELECT h.review_id, h.reason, r.raw_item_id, r.title, r.content, r.source_id
           FROM human_review h
           LEFT JOIN raw_item r ON h.raw_item_id = r.raw_item_id"""
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["review_id"]
    assert r["raw_item_id"] is not None, "修复后 r.raw_item_id 绝对不能为 NULL"
    assert "A-SCHEMA" in r["title"]
    assert "Micron 重要业务变动详情" in r["content"]
    assert r["source_id"] == CURSOR_SOURCE
    assert "stage_a_schema_validation_failed" in r["reason"]

    # 4. 验证 HumanReviewRepo.get_with_raw 辅助接口能够完整穿透查看
    rev_repo = HumanReviewRepo(app.db)
    detail = rev_repo.get_with_raw(r["review_id"])
    assert detail is not None
    assert detail["raw_content"] == r["content"]
    assert detail["raw_title"] == r["title"]


def test_stage_a_and_stage_b_job_records_created(app, monkeypatch):
    """验证正常流水线处理过程中，各阶段 ProcessingJob 记录正确生成并流转至 completed。"""
    pipeline, _ = _wire_test_env(app, monkeypatch, ["JOB-1"])
    monkeypatch.setattr(app.pipeline, "analyze_event", lambda ev, **kw: [])

    summary = pipeline.run_once()
    assert summary.events_created == 1
    assert summary.events_analyzed == 1

    job_repo = ProcessingJobRepo(app.db)
    raw_list = app.db.query("SELECT raw_item_id FROM raw_item")
    assert len(raw_list) == 1
    raw_id = raw_list[0]["raw_item_id"]

    ev_list = EventRepo(app.db).recent(hours=24, limit=5)
    ev_id = ev_list[0].event_id

    job_a = job_repo.get_by_target("stage_a_extract", raw_id)
    assert job_a is not None
    assert job_a.status == "completed"

    job_b = job_repo.get_by_target("stage_b_analyze", ev_id)
    assert job_b is not None
    assert job_b.status == "succeeded_empty"


def test_legacy_inconsistency_repair_dry_run_and_execution(app):
    """测试遗留数据与悬空引用的一致性修复扫描器。"""
    from trace.event_engine.repair import scan_and_repair_inconsistencies

    # 1. 制造一条悬空的人工检查（引用了不存在的 raw_item）
    app.db.execute(
        "INSERT INTO human_review (review_id, raw_item_id, reason, created_at) VALUES (?, ?, ?, ?)",
        ("rev_dangling_1", "raw_non_existent", "legacy schema error", "2026-01-01T00:00:00Z")
    )

    # 2. 制造一个孤立且无分析的 Event
    app.db.execute(
        "INSERT INTO event (event_id, title, summary, event_type, status, version, first_seen_at, last_updated_at, event_time, language) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ev_orphan_1", "Orphan Event", "No analysis", "other", "reported", 1, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "zh")
    )

    # 3. Dry-run 扫描
    report_dry = scan_and_repair_inconsistencies(app.db, dry_run=True)
    assert report_dry["dry_run"] is True
    assert len(report_dry["dangling_reviews"]) == 1
    assert report_dry["dangling_reviews"][0]["review_id"] == "rev_dangling_1"
    assert len(report_dry["unanalyzed_events"]) == 1
    assert report_dry["unanalyzed_events"][0]["event_id"] == "ev_orphan_1"
    assert len(report_dry["actions_taken"]) == 0

    # 4. 执行真实修复
    report_real = scan_and_repair_inconsistencies(app.db, dry_run=False)
    assert report_real["dry_run"] is False
    assert len(report_real["actions_taken"]) >= 2

    # 5. 断言悬空人审记录被标记为 legacy_payload_missing 而非伪造补齐
    rev_row = app.db.query_one("SELECT reason FROM human_review WHERE review_id='rev_dangling_1'")
    assert "legacy_payload_missing" in rev_row["reason"]

    # 6. 断言为未分析事件创建了待分析作业
    job_repo = ProcessingJobRepo(app.db)
    job = job_repo.get_by_target("stage_b_analyze", "ev_orphan_1")
    assert job is not None
    assert job.status == "pending"

