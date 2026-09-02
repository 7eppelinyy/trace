"""可插拔 LLM Provider 架构测试（Gemini 默认 / OpenAI 兼容）。

任务要求：
    - LLM_PROVIDER=gemini|openai，默认优先 Gemini
    - 新增配置项 GEMINI_API_KEY / GEMINI_MODEL
    - Structured Output / Schema 校验 / 重试 在 Gemini 下同样工作
    - OpenAI 保持兼容但不作为必需依赖
"""

from __future__ import annotations

import json

import pytest

from trace.ai.llm_client import GeminiProvider, LLMClient, OpenAIProvider
from trace.ai.schemas import LLMUnavailableError, SchemaValidationError
from trace.config import load_config


class _LLMCfg:
    def __init__(self, provider="gemini", api_key="test-key", max_retries=2):
        self.provider = provider
        self.api_key = api_key
        self.base_url = ""
        self.temperature = 0.0
        self.timeout_seconds = 1
        self.max_retries = max_retries

    @property
    def enabled(self):
        return bool(self.api_key)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _FakeHTTP:
    """记录请求并返回预置响应的假 httpx.Client。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._responses.pop(0)


def _gemini_payload(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


# ---------------------------------------------------------------------------
# Provider 选择（可插拔）
# ---------------------------------------------------------------------------

def test_client_selects_gemini_by_default():
    client = LLMClient(_LLMCfg(provider="gemini"))
    assert client.available
    assert client.provider_name == "gemini"
    assert isinstance(client._provider, GeminiProvider)


def test_client_selects_openai_when_configured():
    client = LLMClient(_LLMCfg(provider="openai"))
    assert client.available
    assert client.provider_name == "openai"
    assert isinstance(client._provider, OpenAIProvider)


def test_client_unknown_provider_unavailable():
    client = LLMClient(_LLMCfg(provider="anthropic"))
    assert not client.available
    with pytest.raises(LLMUnavailableError):
        client.complete_json("m", "sys", "user")


def test_client_no_key_unavailable():
    client = LLMClient(_LLMCfg(api_key=""))
    assert not client.available


# ---------------------------------------------------------------------------
# Gemini Structured Output / 请求格式
# ---------------------------------------------------------------------------

def test_gemini_generate_request_format():
    provider = GeminiProvider(_LLMCfg())
    fake = _FakeHTTP([_FakeResponse(200, _gemini_payload('{"same_event": true}'))])
    provider._http = fake

    text = provider.generate("gemini-2.0-flash", "SYS", "USER")

    assert json.loads(text) == {"same_event": True}
    call = fake.calls[0]
    assert call["url"].endswith("/models/gemini-2.0-flash:generateContent")
    assert call["headers"]["x-goog-api-key"] == "test-key"
    body = call["json"]
    assert body["systemInstruction"]["parts"][0]["text"] == "SYS"
    assert body["contents"][0]["parts"][0]["text"] == "USER"
    # Structured Output：必须要求 JSON 输出
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_gemini_generate_pushes_response_schema():
    """传入 schema 时必须下推为 API 级 responseSchema（类型/必填/枚举强制）。"""
    provider = GeminiProvider(_LLMCfg())
    fake = _FakeHTTP([_FakeResponse(200, _gemini_payload('{"title": "ok"}'))])
    provider._http = fake
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "occurred_at": {"type": ["string", "null"]},
            "event_type": {"type": "string", "enum": ["earnings", "pricing"]},
        },
        "required": ["title"],
    }
    provider.generate("m", "s", "u", schema=schema)

    rs = fake.calls[0]["json"]["generationConfig"]["responseSchema"]
    assert rs["type"] == "OBJECT"
    assert rs["required"] == ["title"]
    assert rs["properties"]["title"]["type"] == "STRING"
    # 联合类型拆为 nullable；Gemini 不支持的约束被丢弃
    assert rs["properties"]["occurred_at"]["type"] == "STRING"
    assert rs["properties"]["occurred_at"]["nullable"] is True
    assert rs["properties"]["event_type"]["enum"] == ["earnings", "pricing"]
    assert "minLength" not in rs["properties"]["title"]


def test_client_passes_schema_down_to_provider():
    """complete_json_validated 必须把 schema 透传给 Provider。"""
    client = LLMClient(_LLMCfg(provider="gemini", max_retries=1))
    fake = _FakeHTTP([_FakeResponse(200, _gemini_payload('{"title": "ok"}'))])
    client._provider._http = fake
    schema = {"type": "object", "properties": {"title": {"type": "string"}},
              "required": ["title"]}
    data = client.complete_json_validated("m", "sys", "user", schema)
    assert data == {"title": "ok"}
    body = fake.calls[0]["json"]
    assert body["generationConfig"]["responseSchema"]["type"] == "OBJECT"


def test_gemini_generate_merges_multiple_parts():
    provider = GeminiProvider(_LLMCfg())
    provider._http = _FakeHTTP([_FakeResponse(
        200, {"candidates": [{"content": {"parts": [
            {"text": '{"a"'}, {"text": ': 1}'},
        ]}}]})])
    assert json.loads(provider.generate("m", "s", "u")) == {"a": 1}


def test_gemini_generate_http_error_raises():
    provider = GeminiProvider(_LLMCfg())
    provider._http = _FakeHTTP([_FakeResponse(429, None, "rate limited")])
    with pytest.raises(RuntimeError):
        provider.generate("m", "s", "u")


def test_gemini_generate_unexpected_body_raises():
    provider = GeminiProvider(_LLMCfg())
    provider._http = _FakeHTTP([_FakeResponse(200, {"candidates": []})])
    with pytest.raises(RuntimeError):
        provider.generate("m", "s", "u")


# ---------------------------------------------------------------------------
# Gemini 下网络重试 / Schema 校验重试
# ---------------------------------------------------------------------------

def test_gemini_network_retry_then_success():
    """第一次网络失败 → 重试 → 成功（重试能力与 OpenAI 一致）。"""
    client = LLMClient(_LLMCfg(provider="gemini", max_retries=2))
    client._provider._http = _FakeHTTP([
        _FakeResponse(500, None, "server error"),
        _FakeResponse(200, _gemini_payload('{"a": 1}')),
    ])
    assert client.complete_json("m", "sys", "user") == {"a": 1}


def test_gemini_network_retry_exhausted_raises():
    client = LLMClient(_LLMCfg(provider="gemini", max_retries=1))
    client._provider._http = _FakeHTTP([
        _FakeResponse(500, None, "err"),
        _FakeResponse(500, None, "err"),
    ])
    with pytest.raises(RuntimeError):
        client.complete_json("m", "sys", "user")


_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string", "minLength": 1}},
    "required": ["title"],
}


def test_gemini_schema_validation_retry_then_success():
    """Gemini：Schema 校验失败 → 反馈错误重试 → 通过。"""
    client = LLMClient(_LLMCfg(provider="gemini", max_retries=2))
    client._provider._http = _FakeHTTP([
        _FakeResponse(200, _gemini_payload('{"other": 1}')),
        _FakeResponse(200, _gemini_payload('{"title": "ok"}')),
    ])
    data = client.complete_json_validated("m", "sys", "user", _SCHEMA)
    assert data == {"title": "ok"}
    # 第二次请求的 user prompt 必须包含校验错误反馈
    assert "不符合要求" in client._provider._http.calls[1]["json"]["contents"][0]["parts"][0]["text"]


def test_gemini_schema_validation_exhausted_raises():
    client = LLMClient(_LLMCfg(provider="gemini", max_retries=1))
    client._provider._http = _FakeHTTP([
        _FakeResponse(200, _gemini_payload('{"bad": 1}')),
        _FakeResponse(200, _gemini_payload('{"still_bad": 2}')),
    ])
    with pytest.raises(SchemaValidationError):
        client.complete_json_validated("m", "sys", "user", _SCHEMA)


# ---------------------------------------------------------------------------
# 配置加载：LLM_PROVIDER / GEMINI_API_KEY / GEMINI_MODEL
# ---------------------------------------------------------------------------

def test_config_default_provider_is_gemini(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    cfg = load_config()
    assert cfg.llm.provider == "gemini"
    assert not cfg.llm.enabled
    assert cfg.llm.model_event_extractor.startswith("gemini-")


def test_config_gemini_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gm-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    cfg = load_config()
    assert cfg.llm.provider == "gemini"
    assert cfg.llm.enabled
    assert cfg.llm.api_key == "gm-key"
    assert cfg.llm.model_event_extractor == "gemini-2.5-pro"
    assert cfg.llm.model_impact_analyzer == "gemini-2.5-pro"
    assert cfg.llm.model_same_event_verifier == "gemini-2.5-pro"


def test_config_openai_compat(monkeypatch):
    """OpenAI 保持兼容：显式 LLM_PROVIDER=openai 时使用 OPENAI_* 配置。"""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "oa-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("GEMINI_API_KEY", "gm-key")
    cfg = load_config()
    assert cfg.llm.provider == "openai"
    assert cfg.llm.api_key == "oa-key"          # 不取 Gemini key
    assert cfg.llm.model_event_extractor == "gpt-4o"


def test_config_openai_missing_key_not_required(monkeypatch):
    """OpenAI 非必需依赖：缺 OPENAI_API_KEY 不报错，仅 enabled=False。"""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    cfg = load_config()
    assert cfg.llm.provider == "openai"
    assert not cfg.llm.enabled
