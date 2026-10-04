import json
from types import SimpleNamespace

import pytest

from trace.ai.grounded_answer import check_answer, render_answer
from trace.alerts.ask import _is_background_question


class RecordingModel:
    available = True
    config = SimpleNamespace(model_ask='quality-test')
    provider_name = 'test'

    def __init__(self, data):
        self.data = data
        self.payload = None
        self.calls = []

    def complete_json(self, **kwargs):
        self.payload = json.loads(kwargs['user_prompt'])
        self.calls.append(kwargs)
        return self.data


def test_business_comparison_does_not_pull_high_score_news(app, monkeypatch):
    model = RecordingModel({'claims': [{'kind': 'background', 'text': '比较两家的产品，再分析不同存储产品价格变化的影响。'}]})
    app.ask_engine.llm = model
    monkeypatch.setattr(app.ask_engine.impact_repo, 'list_by_securities', lambda *a, **k: pytest.fail('Business comparison must not substitute impact-ranked news'))
    answer = app.ask_engine.ask(ticker='MU', question='美光和闪迪的业务有什么区别，DRAM和NAND涨价分别对谁更有利？', mode='auto')
    assert answer.status == 'ok'
    assert answer.mode == 'background'
    assert answer.candidates == []
    assert answer.graph_chain == []
    assert model.payload['security'] is None
    assert model.payload['evidence'] == {}
    assert {c['ticker'] for c in model.payload['company_context']} == {'MU', 'SNDK'}
    sandisk = next(c for c in model.payload['company_context'] if c['ticker'] == 'SNDK')
    assert '独立上市公司' in sandisk['verified_profile']['facts'][0]
    assert '基础解释' in answer.text
    assert len(model.calls) == 2
    assert 'draft_answer' in model.payload


def test_background_review_result_is_checked_and_used(app):
    class ReviewingModel(RecordingModel):
        def complete_json(self, **kwargs):
            super().complete_json(**kwargs)
            if 'draft_answer' in self.payload:
                return {'claims': [{'kind': 'background', 'text': '复核后的比较结论。'}]}
            return {'claims': [{'kind': 'background', 'text': '未复核草稿。'}]}
    app.ask_engine.llm = ReviewingModel({})
    answer = app.ask_engine.ask(question='DRAM和NAND有什么区别？', mode='auto')
    assert '复核后的比较结论' in answer.text
    assert '未复核草稿' not in answer.text


def test_current_event_question_is_not_general_background():
    assert _is_background_question('DRAM和NAND有什么区别？')
    assert not _is_background_question('美光最新财报和预期有什么区别？')
    assert not _is_background_question('今天美光涨价公告的商业模式是什么意思？')


def test_current_news_without_evidence_does_not_become_hypothesis(app):
    answer = app.ask_engine.ask(question='今天芯片行业发布的最新公告是什么？', mode='auto')
    assert answer.mode == 'evidence_answer'
    assert answer.status == 'insufficient_evidence'
    assert answer.claims == []


def test_empty_model_output_cannot_turn_into_generic_scenario(app):
    app.ask_engine.llm = RecordingModel({'claims': []})
    answer = app.ask_engine.ask(question='假设芯片需求下降，产业链受到什么影响？', mode='scenario')
    assert answer.status == 'invalid_model_output'
    assert answer.claims == []
    assert answer.candidates == []
    assert '供给端传导' not in answer.text
    assert '未生成可信结论' in answer.text


def test_scenario_without_assumptions_is_not_silently_repaired(app):
    app.ask_engine.llm = RecordingModel({'claims': [{'kind': 'scenario', 'text': '芯片需求下降可能减少订单。'}]})
    answer = app.ask_engine.ask(question='假设芯片需求下降，产业链受到什么影响？', mode='scenario')
    assert answer.status == 'invalid_model_output'
    assert answer.claims == []


def test_model_may_explicitly_decline_irrelevant_evidence():
    answer = check_answer({'claims': [], 'insufficient_evidence': True, 'next_checks': ['需要两家公司的产品收入结构。']}, {}, 'evidence_answer')
    assert '证据不足' in render_answer(answer)
    with pytest.raises(ValueError):
        check_answer({'claims': [{'kind': 'background', 'text': '结论'}], 'insufficient_evidence': True}, {}, 'background')


def test_background_cannot_pass_as_verified_fact():
    with pytest.raises(ValueError):
        check_answer({'claims': [{'kind': 'fact', 'text': '缺少来源的事实'}]}, {}, 'background')
    with pytest.raises(ValueError):
        check_answer({'claims': [{'kind': 'background', 'text': '一般解释', 'evidence_id': 'RAW-1'}]}, {}, 'background')


def test_production_without_model_does_not_generate_template(app, monkeypatch):
    monkeypatch.setenv('TRACE_MODE', 'production')
    app.ask_engine.llm = None
    answer = app.ask_engine.ask(question='假设芯片需求下降，产业链受到什么影响？', mode='scenario')
    assert answer.status == 'model_unavailable'
    assert answer.claims == []
    assert answer.graph_chain == []
    assert '供给端传导' not in answer.text
