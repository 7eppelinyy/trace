from datetime import datetime, timezone
from fastapi.testclient import TestClient
from trace.api.app import create_api_app
from trace.domain.models import Event, RawItem, ResearchQuestion
from trace.db.repositories import EventRepo, ResearchQuestionRepo, RawItemRepo, UserRepo
from trace.event_engine.engine import ExtractedEvent


def test_real_merge_triggers_private_research_followup(app):
    now = datetime.now(timezone.utc)
    raw = RawItem(raw_item_id='r-original',source_id='src_sec_edgar',source_item_id='document',
                  title='Guidance',content='Revenue 100',url='https://example.invalid/one',published_at=now)
    original = app.event_engine.ingest(raw,ExtractedEvent(title=raw.title,summary=raw.content,entities=['NVDA'],
                  event_type='guidance',event_status='reported')).event
    UserRepo(app.db).ensure('usr_research')
    repo = ResearchQuestionRepo(app.db)
    repo.insert(ResearchQuestion(question_id='q',user_id='usr_research',event_id=original.event_id,
                                title='Follow guidance',hypothesis='Demand continues'))
    change = RawItem(raw_item_id='r-new',source_id='src_sec_edgar',source_item_id='document',
                    title='Revised guidance',content='Revenue 50',url=raw.url,published_at=now)
    app.event_engine.ingest(change,ExtractedEvent(title=change.title,summary=change.content,entities=['NVDA'],
                           event_type='guidance',event_status='reported'))
    question = repo.get('q')
    assert question.matched_evidence_ids == ['r-new']
    assert question.state == 'tracking'
    assert question.revision == 2


def test_export_includes_more_than_one_thousand_private_records(app):
    with TestClient(create_api_app(app)) as client:
        session = client.post('/api/v1/auth/session',json={'grant_type':'guest'}).json()
        user = session['user_id']
        with app.db.transaction():
            for i in range(1001):
                ResearchQuestionRepo(app.db).insert(ResearchQuestion(question_id=f'q-{i}',user_id=user,title='Question',hypothesis='Hypothesis'))
        exported = client.get('/api/v1/research/export',headers={'Authorization':'Bearer '+session['session_token']})
        assert exported.status_code == 200
        assert len(exported.json()['questions']) == 1001


def test_source_policy_revocation_hides_event_and_stops_forwarding(app):
    from trace.common.source_policy import event_permitted
    now = datetime.now(timezone.utc)
    EventRepo(app.db).insert(Event(event_id='policy-event',title='Policy fixture',first_source_id='src_sec_edgar',first_seen_at=now,last_updated_at=now))
    with TestClient(create_api_app(app)) as client:
        assert client.get('/api/v1/events/policy-event').status_code == 200
        app.db.execute("UPDATE source SET can_display=0,can_forward=0 WHERE source_id='src_sec_edgar'")
        assert client.get('/api/v1/events/policy-event').status_code == 404
        assert client.get('/api/v1/events').json()['items'] == []
        assert not event_permitted(app.db,'policy-event','forward')
