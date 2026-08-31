"""AI Structured Output 校验、Evidence ID 完整性、生产模式门禁测试。

任务书 §7/§8/§14：
    - LLM Structured Output 校验（Stage A / Stage B）
    - Evidence ID 完整性（编造的 id 必须被丢弃）
    - production 模式禁止 Mock / 伪分析
    - Schema 校验失败必须重试，重试后仍失败进入人工检查状态
"""

from __future__ import annotations

import pytest

from trace.ai.llm_client import LLMClient, _extract_json
from trace.ai.schemas import (
    EVENT_EXTRACT_SCHEMA,
    IMPACT_ANALYZER_SCHEMA,
    SchemaValidationError,
    check_evidence_ids,
    validate,
)
from trace.domain.models import RawItem
from trace.common.ids import raw_item_id


class _FakeLLMConfig:
    def __init__(self, max_retries: int = 1):
        self.provider = "gemini"
        self.enabled = True
        self.api_key = ""
        self.timeout_seconds = 1
        self.max_retries = max_retries
        self.temperature = 0.0
        self.model_event_extractor = "test-model"
        self.model_impact_analyzer = "test-model"


# ---------------------------------------------------------------------------
# Structured Output 校验
# ---------------------------------------------------------------------------

def _valid_stage_a() -> dict:
    return {
        "title": "Micron raises DRAM prices",
        "summary": "Micron announced a DRAM contract price increase.",
        "entities": ["Micron"],
        "facts": ["Micron raised DRAM prices"],
        "event_type": "pricing",
        "event_status": "official_confirmed",
        "key_numbers": ["+10%"],
        "occurred_at": "2026-08-20T00:00:00Z",
        "uncertainties": [],
        "evidence_ids": ["RAW-1"],
    }


def test_stage_a_schema_valid_output_passes():
    validate(_valid_stage_a(), EVENT_EXTRACT_SCHEMA)


def test_stage_a_schema_rejects_missing_required():
    data = _valid_stage_a()
    del data["evidence_ids"]
    with pytest.raises(SchemaValidationError):
        validate(data, EVENT_EXTRACT_SCHEMA)


def test_stage_a_schema_rejects_bad_enum():
    data = _valid_stage_a()
    data["event_status"] = "confirmed_forever"      # 非法枚举
    with pytest.raises(SchemaValidationError):
        validate(data, EVENT_EXTRACT_SCHEMA)


def test_stage_a_schema_rejects_empty_title():
    data = _valid_stage_a()
    data["title"] = ""
    with pytest.raises(SchemaValidationError):
        validate(data, EVENT_EXTRACT_SCHEMA)


def _valid_stage_b_impact() -> dict:
    return {
        "security_ticker": "MU",
        "direction": "bullish",
        "impact_relation": "direct",
        "directness": "direct",
        "magnitude": 7.0,
        "persistence": 6.0,
        "reason": "DRAM price increase is a direct margin driver.",
        "confidence": 0.8,
        "supporting_evidence_ids": ["RAW-1"],
    }


def test_stage_b_schema_valid_output_passes():
    validate({"impacts": [_valid_stage_b_impact()]}, IMPACT_ANALYZER_SCHEMA)


def test_stage_b_schema_rejects_out_of_range_confidence():
    imp = _valid_stage_b_impact()
    imp["confidence"] = 1.5                          # > 1
    with pytest.raises(SchemaValidationError):
        validate({"impacts": [imp]}, IMPACT_ANALYZER_SCHEMA)


def test_stage_b_schema_rejects_bad_direction():
    imp = _valid_stage_b_impact()
    imp["direction"] = "buy"                         # 非法方向
    with pytest.raises(SchemaValidationError):
        validate({"impacts": [imp]}, IMPACT_ANALYZER_SCHEMA)


def test_stage_b_schema_requires_impacts_key():
    with pytest.raises(SchemaValidationError):
        validate({}, IMPACT_ANALYZER_SCHEMA)


# ---------------------------------------------------------------------------
# Evidence ID 完整性
# ---------------------------------------------------------------------------

def test_check_evidence_ids_drops_fabricated():
    known = {"RAW-1", "RAW-2"}
    out = check_evidence_ids(["RAW-1", "RAW-99", "RAW-2"], known)
    assert out == ["RAW-1", "RAW-2"]


def test_check_evidence_ids_all_fabricated_gives_empty():
    assert check_evidence_ids(["FAKE-1", "FAKE-2"], {"RAW-1"}) == []


def test_check_evidence_ids_empty_input():
    assert check_evidence_ids([], {"RAW-1"}) == []


# ---------------------------------------------------------------------------
# LLM JSON 提取（带 markdown 包裹）
# ---------------------------------------------------------------------------

def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_markdown_wrapped():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_prefix_text():
    assert _extract_json('Here is the result: {"a": 2} thanks') == {"a": 2}


def test_extract_json_no_object_raises():
    with pytest.raises(ValueError):
        _extract_json("no json here")


# ---------------------------------------------------------------------------
# LLM Schema 校验重试
# ---------------------------------------------------------------------------

class _ScriptedLLM(LLMClient):
    """按脚本返回数据的假 LLM 客户端。"""

    def __init__(self, outputs: list):
        super().__init__(_FakeLLMConfig(max_retries=len(outputs)))
        self._outputs = outputs
        self.call_count = 0

    def complete_json(self, model, system_prompt, user_prompt):
        out = self._outputs[min(self.call_count, len(self._outputs) - 1)]
        self.call_count += 1
        if isinstance(out, Exception):
            raise out
        return out


def test_schema_validation_retries_then_succeeds():
    """校验失败 → 重试 → 通过：返回有效数据。"""
    client = _ScriptedLLM([{"title": "bad"}, _valid_stage_a()])
    data = client.complete_json_validated(
        "m", "sys", "user", EVENT_EXTRACT_SCHEMA)
    assert data["title"] == "Micron raises DRAM prices"
    assert client.call_count == 2


def test_schema_validation_exhausted_raises():
    """重试后仍失败：抛 SchemaValidationError（上层进人工检查）。"""
    client = _ScriptedLLM([{"title": "bad"}, {"title": "still bad"}])
    with pytest.raises(SchemaValidationError):
        client.complete_json_validated("m", "sys", "user", EVENT_EXTRACT_SCHEMA)


def _raw_item() -> RawItem:
    return RawItem(
        raw_item_id=raw_item_id(), source_id="src_sec_edgar",
        title="Micron raises DRAM prices",
        published_at=None, language="en",
        content="Micron announced a DRAM contract price increase.")


# ---------------------------------------------------------------------------
# 生产模式门禁（任务书 §7）
# ---------------------------------------------------------------------------

def test_production_mode_no_llm_extractor_raises(monkeypatch):
    """production 模式无 LLM：EventExtractor 必须抛 LLMUnavailableError，不生成伪分析。"""
    from trace.ai.extractor import EventExtractor
    from trace.ai.schemas import LLMUnavailableError

    monkeypatch.setenv("TRACE_MODE", "production")
    client = LLMClient(_FakeLLMConfig())
    assert client.available is False
    extractor = EventExtractor(client, _FakeLLMConfig())
    with pytest.raises(LLMUnavailableError):
        extractor.extract(_raw_item())


def test_production_mode_no_llm_analyzer_raises(monkeypatch):
    """production 模式无 LLM：ImpactAnalyzer 必须抛 LLMUnavailableError。"""
    from trace.ai.analyzer import ImpactAnalyzer, directness_score
    from trace.ai.schemas import LLMUnavailableError
    from trace.db.repositories import SecurityRepo
    from trace.domain.models import Event
    from trace.graph.industry_graph import GraphHit

    monkeypatch.setenv("TRACE_MODE", "production")
    analyzer = ImpactAnalyzer(LLMClient(_FakeLLMConfig()), _FakeLLMConfig())
    # 构造一个假的 GraphHit（只要有候选就会尝试分析）
    from trace.domain.models import Security
    hit = GraphHit(security=Security(security_id="s1", market="US", ticker="MU"),
                   hops=1, path=["mu"], edge_types=[])
    with pytest.raises(LLMUnavailableError):
        analyzer.analyze(Event(event_id="e1"), [hit], [_raw_item()])


def test_offline_mode_no_llm_degrades_with_marker(monkeypatch):
    """offline 模式无 LLM：允许降级但必须标记 rule_based_degraded。"""
    from trace.ai.extractor import EventExtractor
    from trace.common.modes import ANALYSIS_MODE_RULE_DEGRADED

    monkeypatch.setenv("TRACE_MODE", "offline")
    client = LLMClient(_FakeLLMConfig())
    extractor = EventExtractor(client, _FakeLLMConfig())
    item = _raw_item()
    out = extractor.extract(item)
    assert extractor.last_mode == ANALYSIS_MODE_RULE_DEGRADED
    assert any("rule_based_degraded" in u for u in (out.uncertainties or []))
    # 规则兜底只能使用传入的 Evidence，不允许编造
    assert out.evidence_ids == [item.raw_item_id]


def test_offline_analyzer_rule_degraded_low_confidence(monkeypatch):
    """offline 规则兜底：方向不确定 + 低置信度（不夸大）。"""
    from trace.ai.analyzer import ImpactAnalyzer
    from trace.domain.models import Security
    from trace.graph.industry_graph import GraphHit

    monkeypatch.setenv("TRACE_MODE", "offline")
    analyzer = ImpactAnalyzer(LLMClient(_FakeLLMConfig()), _FakeLLMConfig())
    hit = GraphHit(security=Security(security_id="s1", market="US", ticker="MU"),
                   hops=1, path=["mu"], edge_types=[])
    out = analyzer.analyze(None, [hit], [_raw_item()])
    assert len(out) == 1
    assert out[0]["direction"] == "uncertain"
    assert out[0]["confidence"] < 0.5
    assert analyzer.last_mode == "rule_based_degraded"
