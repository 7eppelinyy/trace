"""Regression tests at real integration boundaries identified by independent review."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from trace.api.app import create_api_app
from trace.db.repositories import EventRepo
from trace.domain.models import Event
from trace.domain.models import RawItem
from trace.db.repositories import RawItemRepo
import json


def test_invalid_mode_fails_api_startup(app, monkeypatch):
    monkeypatch.setenv('TRACE_MODE', 'real')
    with pytest.raises(ValueError, match='invalid TRACE_MODE'):
        with TestClient(create_api_app(app)):
            pass


def test_development_auth_requires_explicit_switch(app, monkeypatch):
    monkeypatch.delenv('TRACE_ALLOW_DEV_AUTH', raising=False)
    with TestClient(create_api_app(app)) as client:
        response = client.post('/api/v1/auth/session', json={'grant_type': 'dev', 'user_id': 'victim'})
        assert response.status_code == 403


def test_private_research_requires_owner_to_issue_revocable_share(app):
    with TestClient(create_api_app(app)) as client:
        session = client.post('/api/v1/auth/session', json={'grant_type': 'guest'}).json()
        auth = {'Authorization': 'Bearer ' + session['session_token']}
        q = client.post('/api/v1/research/questions', headers=auth,
                        json={'title': 'Private research', 'hypothesis': 'Private hypothesis', 'user_notes': 'Secret notes'}).json()
        path = '/api/v1/research/questions/' + q['question_id'] + '/share'
        assert client.get(path).status_code == 404
        share = client.post(path, headers=auth, json={'expires_in_days': 1})
        assert share.status_code == 200
        token = share.json()['share_token']
        public = client.get(path, params={'token': token})
        assert public.status_code == 200
        assert 'user_notes' not in public.json()
        client.patch('/api/v1/research/questions/' + q['question_id'], headers=auth,
                     json={'hypothesis': 'Later private change'})
        assert client.get(path, params={'token': token}).json()['hypothesis'] == 'Private hypothesis'
        assert client.delete(path, headers=auth).status_code == 200
        assert client.get(path, params={'token': token}).status_code == 404


def test_generated_cursor_round_trips_all_pages(app):
    now = datetime.now(timezone.utc)
    for i in range(3):
        EventRepo(app.db).insert(Event(event_id=f'EVT-page-{i}', title='Cursor fixture', first_source_id='src_sec_edgar',
                                      first_seen_at=now, last_updated_at=now - timedelta(minutes=i)))
    with TestClient(create_api_app(app)) as client:
        params = {'limit': 1}
        seen = []
        for _ in range(3):
            result = client.get('/api/v1/events', params=params)
            assert result.status_code == 200
            data = result.json()
            assert len(data['items']) == 1
            seen.append(data['items'][0]['event_id'])
            params.update(cursor=data['pagination']['cursor'], snapshot_ts=data['pagination']['snapshot_ts'])
        assert len(set(seen)) == 3


def test_real_ask_client_reserves_once_and_validates_evidence(app, monkeypatch):
    now = datetime.now(timezone.utc)
    ev = Event(event_id='EVT-ask-contract', title='NVDA revenue guidance', first_seen_at=now, last_updated_at=now)
    EventRepo(app.db).insert(ev)
    RawItemRepo(app.db).insert(RawItem(raw_item_id='RAW-contract', event_id=ev.event_id,
        source_id='src_sec_edgar', title=ev.title, content='Revenue guidance is 100.', url='https://example.invalid/evidence'))
    calls = []
    class Provider:
        def generate(self, *args, **kwargs):
            calls.append(1)
            return json.dumps({'claims': [{'kind': 'fact', 'text': 'Revenue guidance is 100.',
                'evidence_id': 'RAW-contract', 'quote': 'Revenue guidance is 100.'}], 'next_checks': []})
    monkeypatch.setattr(app.pipeline.llm, '_provider', Provider())
    monkeypatch.setattr(app.confirmer, 'quote', lambda *a, **kw: None)
    answer = app.ask_engine.ask(ticker='NVDA', question='分析英伟达营收指引', event_id=ev.event_id, user_id='usr_audit')
    assert answer.status == 'ok'
    assert len(calls) == 1
    assert app.pipeline.budget.used() == 1
    assert answer.claims[0]['evidence_ids'] == ['RAW-contract']


def test_macro_question_without_ticker_is_valid(app):
    answer = app.ask_engine.ask(question='分析美联储降息对股票估值的影响')
    assert answer.status == 'insufficient_evidence'


def test_answer_rejects_fabricated_numbers_and_quotes():
    from trace.ai.grounded_answer import check_answer
    evidence = {'r': {'text': 'Revenue is 100.'}}
    with pytest.raises(ValueError):
        check_answer({'claims': [{'kind': 'fact', 'text': 'Revenue is 200.', 'quote': 'Revenue is 200.', 'evidence_id': 'r'}]}, evidence, 'evidence_answer')
    with pytest.raises(ValueError):
        check_answer({'claims': [{'kind': 'inference', 'text': 'Profit grows 300%', 'quote': 'Revenue is 100.',
                                 'evidence_id': 'r', 'assumptions': ['sales hold']}]}, evidence, 'evidence_answer')
