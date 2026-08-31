"""Event Engine 测试：确定性去重 + 事件创建 + Revision。"""

from datetime import datetime, timezone

from trace.common.ids import raw_item_id
from trace.db.repositories import EventRepo
from trace.domain.models import RawItem
from trace.event_engine.embeddings import HashEmbedder
from trace.event_engine.engine import EventEngine, ExtractedEvent


def _item(title: str, url: str, source_id: str = "src_reuters",
          source_item_id: str | None = None) -> RawItem:
    return RawItem(
        raw_item_id=raw_item_id(),
        source_id=source_id,
        source_item_id=source_item_id,
        title=title,
        url=url,
        published_at=datetime.now(timezone.utc),
        language="en",
        content=title,
    )


def _extracted(title: str) -> ExtractedEvent:
    return ExtractedEvent(
        title=title, summary=title, entities=["NVIDIA"],
        event_type="regulation", event_status="reported",
        event_time=datetime.now(timezone.utc))


def test_exact_dedup_by_source_item_id(db, config):
    engine = EventEngine(db, config, embedder=HashEmbedder())
    item1 = _item("Nvidia faces new chip limits", "https://a.com/1", source_item_id="X1")
    d1 = engine.ingest(item1, _extracted(item1.title))
    assert d1.action == "created"

    item2 = _item("Completely different title", "https://a.com/2", source_item_id="X1")
    d2 = engine.ingest(item2, _extracted(item2.title))
    assert d2.action == "duplicate"
    assert d2.reason == "source_item_id"


def test_exact_dedup_by_title_hash(db, config):
    engine = EventEngine(db, config, embedder=HashEmbedder())
    d1 = engine.ingest(_item("Micron raises DRAM prices", "https://a.com/1"),
                       _extracted("Micron raises DRAM prices"))
    assert d1.action == "created"
    d2 = engine.ingest(_item("MICRON RAISES DRAM PRICES", "https://b.com/2"),
                       _extracted("MICRON RAISES DRAM PRICES"))
    assert d2.action == "duplicate"
    assert d2.reason == "title_hash"


def test_new_event_created(db, config):
    engine = EventEngine(db, config, embedder=HashEmbedder())
    item = _item("BIS publishes new export rule", "https://bis.gov/1",
                 source_id="src_bis")
    d = engine.ingest(item, _extracted(item.title))
    assert d.action == "created"
    ev = EventRepo(db).get(d.event.event_id)
    assert ev is not None
    assert ev.first_source_id == "src_bis"
    assert ev.version == 1


def test_schema_validation_rejects_bad_llm_output():
    from trace.ai.schemas import EVENT_EXTRACT_SCHEMA, SchemaValidationError, validate
    import pytest
    with pytest.raises(SchemaValidationError):
        validate({"title": "x"}, EVENT_EXTRACT_SCHEMA)   # 缺字段
    with pytest.raises(SchemaValidationError):
        validate({"title": "x", "summary": "y", "entities": [],
                  "event_type": "not_a_type", "event_status": "rumor"},
                 EVENT_EXTRACT_SCHEMA)
