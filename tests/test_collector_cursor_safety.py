"""采集游标的崩溃安全测试。

采集器的增量游标（seen_accessions / seen_item_ids / etag …）标记的是
"这条已经处理过，不用再采"。但游标此前在 collect() 里就落库，而 RawItem
的持久化发生在流水线后续阶段 —— 两者之间只要中断（LLM 预算耗尽 STOP、
生产模式缺 Key、进程重启、未捕获异常），这批条目就**既没进库、又被游标
永久跳过**，再也采不回来。对 SEC 8-K 这种一次性事件是不可恢复的丢失。

而预算熔断上线后，"中途 STOP" 从异常路径变成了常规路径。

改成两阶段提交：ingest 循环正常走完才 commit()，中途退出则丢弃。
"""

from __future__ import annotations

from datetime import datetime, timezone

from trace.ai.budget import LLMBudgetExceededError
from trace.collectors.base import BaseCollector, CollectResult
from trace.common.ids import raw_item_id
from trace.common.modes import STOP_LLM_BUDGET_EXCEEDED
from trace.db.health import CursorRepo, StagedCursorRepo
from trace.domain.models import RawItem
from trace.event_engine.engine import ExtractedEvent
from trace.pipeline import Pipeline


# ---------------------------------------------------------------------------
# StagedCursorRepo 本身
# ---------------------------------------------------------------------------

def test_set_does_not_persist_until_commit(db):
    inner = CursorRepo(db)
    staged = StagedCursorRepo(inner)

    staged.set("src_sec_edgar", {"seen_accessions": ["A1"]})
    assert inner.get("src_sec_edgar") == {}          # 还没真正落库
    assert staged.pending_count == 1

    assert staged.commit() == 1
    assert inner.get("src_sec_edgar") == {"seen_accessions": ["A1"]}
    assert staged.pending_count == 0


def test_discard_drops_uncommitted_cursor(db):
    inner = CursorRepo(db)
    staged = StagedCursorRepo(inner)
    staged.set("src_sec_edgar", {"seen_accessions": ["A1"]})

    assert staged.discard() == 1
    assert inner.get("src_sec_edgar") == {}
    assert staged.commit() == 0                      # 已丢弃，无可提交


def test_get_reflects_pending_within_same_round(db):
    """同一轮里 get→set→get 必须自洽（采集器会读回自己刚写的游标）。"""
    staged = StagedCursorRepo(CursorRepo(db))
    staged.set("src_rss", {"etag": "v1"})
    assert staged.get("src_rss") == {"etag": "v1"}


def test_get_returns_a_copy_not_the_pending_object(db):
    """返回副本：调用方就地修改不得污染待提交的游标。"""
    staged = StagedCursorRepo(CursorRepo(db))
    staged.set("src_rss", {"seen": ["a"]})
    got = staged.get("src_rss")
    got["seen"].append("b")
    assert staged.get("src_rss")["seen"] == ["a"]


# ---------------------------------------------------------------------------
# 流水线集成
# ---------------------------------------------------------------------------

CURSOR_SOURCE = "src_digitimes"      # seed 中默认启用（未启用的来源永不运行）


class _StubCollector(BaseCollector):
    """每轮返回尚未"处理过"的条目，并像真实采集器那样推进游标。"""

    collector_type = "rss"

    def __init__(self, db, config, item_ids: list[str]):
        super().__init__(db, config)
        self._item_ids = item_ids
        self.collect_calls = 0

    @property
    def handled_source_ids(self) -> set[str]:
        return {CURSOR_SOURCE}

    def collect(self) -> list[RawItem]:
        self.collect_calls += 1
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
                language="zh", content=f"Micron 公告 {iid}"))
        self.cursor_repo.set(CURSOR_SOURCE, {"seen_item_ids": sorted(seen)})
        return items


def _wire(app, monkeypatch, item_ids: list[str]) -> tuple[Pipeline, _StubCollector]:
    collector = _StubCollector(app.db, app.config, item_ids)
    monkeypatch.setattr(app.collectors, "_collectors", [collector])
    monkeypatch.setattr(app.pipeline.extractor, "extract", lambda it: ExtractedEvent(
        title=it.title, summary=it.title, entities=["Micron"],
        event_type="regulation", event_status="reported",
        event_time=it.published_at))
    monkeypatch.setattr(app.pipeline, "analyze_event", lambda event, **kw: [])
    return Pipeline(app), collector


def test_cursor_committed_after_successful_round(app, monkeypatch):
    pipeline, collector = _wire(app, monkeypatch, ["A1", "A2"])

    summary = pipeline.run_once()
    assert summary.raw_items_new == 2
    assert summary.cursors_committed == 1
    assert CursorRepo(app.db).get(CURSOR_SOURCE) == {"seen_item_ids": ["A1", "A2"]}

    # 第二轮：确实不再重复处理已落库的条目
    assert pipeline.run_once().raw_items_new == 0


def test_aborted_round_does_not_advance_cursor(app, monkeypatch):
    """预算耗尽中途 STOP：游标不得推进，这批条目下一轮必须还能采到。"""
    pipeline, collector = _wire(app, monkeypatch, ["A1", "A2"])
    monkeypatch.setattr(app.pipeline.extractor, "extract",
                        lambda it: (_ for _ in ()).throw(
                            LLMBudgetExceededError("budget exhausted 3000/3000")))

    summary = pipeline.run_once()
    assert summary.status == STOP_LLM_BUDGET_EXCEEDED
    # Cursor advancement is now safe: both raw payloads and extraction jobs are durable.
    assert summary.cursors_committed == 1
    assert CursorRepo(app.db).get(CURSOR_SOURCE) == {"seen_item_ids": ["A1", "A2"]}
    assert len(app.db.query("SELECT * FROM raw_item")) == 2
    assert len(app.db.query("SELECT * FROM processing_job WHERE job_type='stage_a_extract'")) == 2

    # 恢复后重新采集：条目没有丢
    monkeypatch.setattr(app.pipeline.extractor, "extract", lambda it: ExtractedEvent(
        title=it.title, summary=it.title, entities=["Micron"],
        event_type="regulation", event_status="reported",
        event_time=it.published_at))
    recovered = pipeline.run_once()
    assert recovered.raw_items_new == 0
    assert app.db.query_one("SELECT COUNT(*) AS n FROM raw_item WHERE event_id IS NOT NULL")['n'] == 2
    assert app.db.query_one("SELECT COUNT(*) AS n FROM processing_job WHERE job_type='stage_a_extract' AND status='completed'")['n'] == 2
    assert recovered.cursors_committed == 1


def test_pending_cursor_from_aborted_round_is_dropped_next_round(app, monkeypatch):
    """中途退出后，采集器上还挂着未提交的游标。

    下一轮开始必须丢弃它 —— 否则 get() 读到的是"已跳过"的暂存值，
    这批条目照样再也采不到（两阶段提交做了一半等于没做）。
    """
    pipeline, collector = _wire(app, monkeypatch, ["A1"])
    monkeypatch.setattr(app.pipeline.extractor, "extract",
                        lambda it: (_ for _ in ()).throw(
                            LLMBudgetExceededError("budget exhausted")))
    pipeline.run_once()
    assert collector.cursor_repo.pending_count == 0      # 原文与作业持久化后已安全提交

    monkeypatch.setattr(app.pipeline.extractor, "extract", lambda it: ExtractedEvent(
        title=it.title, summary=it.title, entities=["Micron"],
        event_type="regulation", event_status="reported",
        event_time=it.published_at))
    assert pipeline.run_once().events_created == 1


def test_schema_failure_still_commits_cursor(app, monkeypatch):
    """Schema 校验失败的条目进人工检查队列，但 ingest 循环是走完的。

    这类条目必须照常推进游标：重试也会再失败，不推进就等于每轮都拿它
    重烧一次 LLM。与"中途 STOP"的区别正是"有没有走完这一轮"。
    """
    from trace.ai.schemas import SchemaValidationError

    pipeline, collector = _wire(app, monkeypatch, ["A1"])
    monkeypatch.setattr(app.pipeline.extractor, "extract",
                        lambda it: (_ for _ in ()).throw(
                            SchemaValidationError("event_type not in enum")))

    summary = pipeline.run_once()
    assert summary.human_review == 1
    assert summary.cursors_committed == 1
    assert CursorRepo(app.db).get(CURSOR_SOURCE) == {"seen_item_ids": ["A1"]}
    assert pipeline.run_once().human_review == 0, "不得每轮重复送检同一条目"
