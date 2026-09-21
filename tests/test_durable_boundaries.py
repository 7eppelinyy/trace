"""Fault injection through production repositories, engines and worker policies."""
from datetime import datetime, timedelta, timezone
import pytest

from trace.db.repositories import UserRepo, ChannelBindingRepo, NotificationPreferenceRepo, AlertOutboxRepo, RawItemRepo, EventRepo
from trace.db.jobs import ProcessingJobRepo
from trace.domain.models import AlertOutbox, RawItem
from trace.event_engine.normalize import normalize_raw_item
from trace.event_engine.exact_dedup import ExactDedup
from trace.event_engine.engine import ExtractedEvent
from trace.alerts.delivery_worker import drain_outbox
from trace.pipeline import Pipeline
from trace.collectors.base import CollectResult


def test_job_lease_fencing_and_recovery(db):
    repo = ProcessingJobRepo(db)
    job = repo.create_or_update('stage_a_extract', 'raw')
    first = repo.claim(job.job_id)
    assert first and repo.claim(job.job_id) is None
    db.execute('UPDATE processing_job SET lease_until=? WHERE job_id=?',
               ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),job.job_id))
    second = repo.claim(job.job_id)
    assert second and first.lease_owner != second.lease_owner
    with pytest.raises(RuntimeError, match='lease lost'):
        repo.finish(first, 'completed')
    repo.finish(second, 'completed')
    assert repo.get(job.job_id).status == 'completed'


def test_stage_b_error_recovers_without_new_collection(app, monkeypatch):
    item = RawItem(raw_item_id='RAW-fault', source_id='src_sec_edgar', source_item_id='fault',
                   title='Micron guidance', content='Revenue grows', url='https://example.invalid/fault',
                   published_at=datetime.now(timezone.utc))
    monkeypatch.setattr(app.collectors, 'run_all', lambda **kw: ([item], [CollectResult(source_ids=['src_sec_edgar'])]))
    monkeypatch.setattr(app.pipeline.extractor, 'extract', lambda raw: ExtractedEvent(
        title=raw.title, summary=raw.content, entities=['Micron'], event_type='guidance', event_status='reported'))
    monkeypatch.setattr(app.pipeline, 'analyze_event', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('fault')))
    pipeline = Pipeline(app)
    pipeline.run_once()
    job = ProcessingJobRepo(app.db).list_pending('stage_b_analyze')[0]
    assert job.status == 'failed'
    monkeypatch.setattr(app.collectors, 'run_all', lambda **kw: ([], []))
    calls = []
    monkeypatch.setattr(app.pipeline, 'analyze_event', lambda *a, **kw: calls.append(1) or [])
    pipeline.run_once()
    assert calls == [1]
    assert ProcessingJobRepo(app.db).get(job.job_id).status == 'succeeded_empty'
    assert app.db.query_one('SELECT COUNT(*) AS n FROM analysis_run')['n'] == 1


def test_disabled_after_enqueue_never_sends_and_expiry_is_ambiguous(db):
    UserRepo(db).ensure('usr_a')
    ChannelBindingRepo(db).bind('usr_a', 'telegram', 'test-chat')
    repo = AlertOutboxRepo(db)
    item = AlertOutbox(outbox_id='out-a', user_id='usr_a', channel_type='telegram', channel_target='test-chat',
                      event_id='e', impact_id=None, event_version=1, alert_type='new_event', idempotency_key='a',content_text='private')
    repo.enqueue(item)
    NotificationPreferenceRepo(db).set_preference('usr_a',None,enabled=False)
    calls = []
    stats = drain_outbox(db, lambda *a: calls.append(1), is_production=True)
    assert stats['suppressed'] == 1 and not calls
    assert repo.get('out-a').status == 'suppressed'
    item.outbox_id, item.idempotency_key = 'out-b', 'b'
    repo.enqueue(item)
    leased = repo.claim_batch()[0]
    db.execute('UPDATE alert_outbox SET lease_until=? WHERE outbox_id=?',
               ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),leased.outbox_id))
    assert repo.claim_batch() == []
    assert repo.get(leased.outbox_id).status == 'ambiguous'
    with pytest.raises(RuntimeError,match='lease lost'):
        repo.finish(leased,'sent')


def test_stable_document_revision_preserves_both_versions_and_denial(app):
    now = datetime.now(timezone.utc)
    raw = RawItem(raw_item_id='RAW-original', source_id='src_sec_edgar', source_item_id='stable',
                  title='Micron guidance', content='Revenue 100', url='https://example.invalid/stable', published_at=now)
    first = app.event_engine.ingest(raw,ExtractedEvent(title=raw.title, summary=raw.content,entities=['Micron'],
                                                     event_type='guidance',event_status='reported'))
    changed = RawItem(raw_item_id='RAW-correction', source_id=raw.source_id, source_item_id=raw.source_item_id,
                      title='Micron denies guidance', content='Guidance withdrawn', url=raw.url, published_at=now)
    normalize_raw_item(changed)
    match = ExactDedup(app.db).check(changed)
    assert match.is_revision and not match.is_duplicate
    decision = app.event_engine.ingest(changed,ExtractedEvent(title=changed.title,summary=changed.content,entities=['Micron'],
                                      event_type='guidance',event_status='retracted'), target_event_id=match.existing_event_id)
    assert decision.event.event_id == first.event.event_id
    assert decision.event.status == 'retracted'
    assert len(RawItemRepo(app.db).list_by_event(first.event.event_id)) == 2
    jobs = app.db.query("SELECT input_version FROM processing_job WHERE job_type='stage_b_analyze' ORDER BY input_version")
    assert [j['input_version'] for j in jobs] == [1,2]


def test_failed_notification_intent_transaction_keeps_analysis_retryable(app, monkeypatch):
    raw = RawItem(raw_item_id='RAW-atomic', source_id='src_sec_edgar',source_item_id='atomic',title='Micron',content='Growth',url='https://example.invalid/atomic')
    event = app.event_engine.ingest(raw,ExtractedEvent(title=raw.title,summary=raw.content,entities=['Micron'],
                                   event_type='guidance',event_status='reported')).event
    monkeypatch.setattr(app.collectors,'run_all',lambda **kw:([],[]))
    monkeypatch.setattr(app.pipeline,'analyze_event',lambda *a,**kw:[])
    monkeypatch.setattr(app.alert_engine,'evaluate',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('intent write fault')))
    Pipeline(app).run_once()
    assert app.db.query_one('SELECT COUNT(*) AS n FROM analysis_run')['n'] == 0
    assert ProcessingJobRepo(app.db).get_by_target('stage_b_analyze',event.event_id).status == 'failed'
