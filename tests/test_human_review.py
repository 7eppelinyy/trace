"""人工检查队列测试。

Schema 校验重试后仍然失败的样本会写进 human_review 表，但长期没有任何
CLI 或 Bot 命令能看到 —— 只进不出。这是唯一能看出 prompt / 模型输出退化的
信号（也是 few-shot 语料来源），必须可见、可归类、可标记处理。
"""

from __future__ import annotations

from datetime import datetime, timezone

from trace.ai.schemas import SchemaValidationError
from trace.collectors.base import CollectResult
from trace.common.ids import raw_item_id, revision_id
from trace.db.health import HumanReviewRepo
from trace.domain.models import RawItem
from trace.pipeline import Pipeline


def _add(db, reason: str, *, event_id=None, raw_item_id_=None) -> str:
    rid = revision_id()
    HumanReviewRepo(db).add(rid, reason=reason, event_id=event_id,
                            raw_item_id=raw_item_id_)
    return rid


# ---------------------------------------------------------------------------
# 仓库层
# ---------------------------------------------------------------------------

def test_pending_count_and_ordering(db):
    repo = HumanReviewRepo(db)
    assert repo.pending_count() == 0
    _add(db, "stage_a_schema_validation_failed: missing title")
    _add(db, "stage_b_schema_validation_failed: bad direction")
    assert repo.pending_count() == 2
    assert len(repo.pending(limit=1)) == 1


def test_reason_breakdown_groups_by_failure_kind(db):
    """reason 带具体字段详情，必须按冒号前的类型归并才看得出趋势。"""
    _add(db, "stage_a_schema_validation_failed: missing title")
    _add(db, "stage_a_schema_validation_failed: missing summary")
    _add(db, "stage_b_schema_validation_failed: bad direction")

    breakdown = dict(HumanReviewRepo(db).reason_breakdown())
    assert breakdown == {"stage_a_schema_validation_failed": 2,
                         "stage_b_schema_validation_failed": 1}


def test_resolve_marks_single_item(db):
    repo = HumanReviewRepo(db)
    rid = _add(db, "stage_a_schema_validation_failed: x")
    _add(db, "stage_b_schema_validation_failed: y")

    assert repo.resolve(rid) is True
    assert repo.pending_count() == 1
    assert repo.resolve(rid) is False          # 已处理过：不重复计数
    assert repo.resolve("NOPE") is False


def test_resolve_all(db):
    repo = HumanReviewRepo(db)
    _add(db, "a: 1")
    _add(db, "b: 2")
    assert repo.resolve_all() == 2
    assert repo.pending_count() == 0
    assert repo.resolve_all() == 0


def test_render_empty_and_populated(db):
    repo = HumanReviewRepo(db)
    assert "没有待处理项" in repo.render()

    _add(db, "stage_a_schema_validation_failed: missing title",
         raw_item_id_="RAW-9")
    text = repo.render()
    assert "待处理: 1 条" in text
    assert "stage_a_schema_validation_failed" in text
    assert "RAW-9" in text


def test_cli_output_survives_non_utf8_console():
    """CLI 输出里的 emoji（📊 摘要 / 🔎 队列 / 📈 回测）不得让命令崩掉。

    Windows 控制台默认 GBK：直接 print 这些字符会抛 UnicodeEncodeError，
    整条命令失败（不是显示成乱码）。setup_logging 会把 stdout 切到 UTF-8。
    """
    import io
    import sys

    from trace.common.observability import use_utf8_console

    original = sys.stdout
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
    try:
        use_utf8_console()
        print("🔎 人工检查队列 📊 📈")          # 切换前会 UnicodeEncodeError
        sys.stdout.flush()
    finally:
        sys.stdout = original


def test_render_truncates_long_queue(db):
    for i in range(15):
        _add(db, f"stage_a_schema_validation_failed: field {i}")
    text = HumanReviewRepo(db).render(limit=10)
    assert "待处理: 15 条" in text
    assert "另有 5 条未显示" in text


# ---------------------------------------------------------------------------
# 流水线：校验失败必须进队列且不进 Alert 链路
# ---------------------------------------------------------------------------

def test_stage_a_failure_lands_in_review_queue(app, monkeypatch):
    """Stage A 校验失败：进人工检查、不建事件、不进 Alert 链路。"""
    pipeline = Pipeline(app)
    now = datetime.now(timezone.utc)
    item = RawItem(raw_item_id=raw_item_id(), source_id="src_reuters",
                   source_item_id="HR1", title="Micron 扩产",
                   url="https://h.test/1", published_at=now,
                   language="zh", content="Micron 扩产")
    monkeypatch.setattr(app.collectors, "run_all",
                        lambda **kw: ([item], [CollectResult(source_ids=["src_reuters"])]))
    monkeypatch.setattr(app.pipeline.extractor, "extract",
                        lambda _i: (_ for _ in ()).throw(
                            SchemaValidationError("event_type not in enum")))

    summary = pipeline.run_once()

    assert summary.human_review == 1
    assert summary.events_created == 0
    assert app.db.query("SELECT * FROM event_impact") == []

    repo = HumanReviewRepo(app.db)
    assert repo.pending_count() == 1
    row = repo.pending()[0]
    assert row["raw_item_id"] == item.raw_item_id
    assert "event_type not in enum" in row["reason"]
    assert "stage_a_schema_validation_failed" in repo.render()
