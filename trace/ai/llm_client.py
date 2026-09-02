"""LLM Structured Output 客户端（可插拔 Provider 架构）。

无 API Key 时降级为规则抽取（仅限 test / offline / demo 模式），
保证整条流程离线可运行；有 Key 时使用对应 Provider。

支持 Provider（LLM_PROVIDER 环境变量，默认 gemini）：
    gemini   Google Gemini（默认；GEMINI_API_KEY / GEMINI_MODEL）
    openai   OpenAI 兼容接口（OPENAI_API_KEY / OPENAI_MODEL / OPENAI_BASE_URL）

生产模式（TRACE_MODE=production）：
    缺少 LLM Key 不得生成伪 AI 分析 —— 必须抛出
    LLMUnavailableError，由上层转为 STOP / DEGRADED 状态。

API Provider、模型名称、timeout、重试次数全部来自配置/环境变量，
禁止硬编码。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

from trace.ai.schemas import LLMUnavailableError, SchemaValidationError, validate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider 抽象层：所有 Provider 只负责"发一次请求并取回原始文本"，
# JSON 提取 / 重试 / Schema 校验由 LLMClient 统一控制（与 Provider 无关）。
# ---------------------------------------------------------------------------


class BaseLLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def generate(self, model: str, system_prompt: str, user_prompt: str) -> str:
        """发送一次生成请求，返回模型输出文本。网络/服务错误直接抛异常。"""


class OpenAIProvider(BaseLLMProvider):
    """OpenAI 兼容 Provider（OpenAIProvider 保持兼容，但非必需依赖）。"""

    name = "openai"

    def __init__(self, llm_config):
        self._client = None
        self._temperature = llm_config.temperature
        try:
            from openai import OpenAI
            kwargs: dict[str, Any] = {
                "api_key": llm_config.api_key,
                "timeout": llm_config.timeout_seconds,
                "max_retries": 0,   # 重试由 LLMClient 统一控制
            }
            if llm_config.base_url:
                kwargs["base_url"] = llm_config.base_url
            self._client = OpenAI(**kwargs)
        except Exception as exc:
            logger.warning("openai provider init failed: %s", exc)

    @property
    def ready(self) -> bool:
        return self._client is not None

    def generate(self, model: str, system_prompt: str, user_prompt: str) -> str:
        assert self._client is not None
        resp = self._client.chat.completions.create(
            model=model,
            temperature=self._temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return resp.choices[0].message.content or "{}"


class GeminiProvider(BaseLLMProvider):
    """Google Gemini REST Provider（无 SDK 硬依赖，responseMimeType 强制 JSON）。"""

    name = "gemini"
    ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
                "{model}:generateContent")

    def __init__(self, llm_config):
        self._api_key = llm_config.api_key
        self._temperature = llm_config.temperature
        self._timeout = llm_config.timeout_seconds
        # 网络错误直接抛给 LLMClient 的重试循环
        self._http = httpx.Client(timeout=self._timeout)

    @property
    def ready(self) -> bool:
        return bool(self._api_key)

    def generate(self, model: str, system_prompt: str, user_prompt: str) -> str:
        url = self.ENDPOINT.format(model=model)
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [
                {"role": "user", "parts": [{"text": user_prompt}]},
            ],
            "generationConfig": {
                "temperature": self._temperature,
                # Structured Output：要求模型只输出 JSON 对象
                "responseMimeType": "application/json",
            },
        }
        resp = self._http.post(
            url, json=payload,
            headers={"x-goog-api-key": self._api_key,
                     "Content-Type": "application/json"})
        if resp.status_code != 200:
            raise RuntimeError(
                f"gemini api http {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(
                f"gemini api unexpected response: {json.dumps(data)[:200]}")
        if not text.strip():
            raise RuntimeError("gemini api returned empty content")
        return text


# ---------------------------------------------------------------------------
# LLMClient：统一入口，按 provider 选择实现
# ---------------------------------------------------------------------------

class LLMClient:
    def __init__(self, llm_config, backoff_base: float = 2.0):
        self.config = llm_config
        self.backoff_base = backoff_base
        # 真实 API 调用次数（含网络/Schema 重试消耗的每一次调用）：
        # 任务书 §17 的成本统计必须以此为准，只数成功会低估成本
        self.call_count: int = 0
        self._provider: BaseLLMProvider | None = None
        self._provider_error: str = ""

        if not llm_config.enabled:
            return

        provider_name = (getattr(llm_config, "provider", "") or "gemini").lower()
        if provider_name == "gemini":
            provider: BaseLLMProvider | None = GeminiProvider(llm_config)
        elif provider_name == "openai":
            provider = OpenAIProvider(llm_config)
        else:
            self._provider_error = f"unknown LLM_PROVIDER: {provider_name}"
            logger.warning("unknown LLM_PROVIDER: %s", provider_name)
            return

        if not provider.ready:
            self._provider_error = f"{provider_name} provider not ready"
            logger.warning("llm provider %s not ready", provider_name)
            return
        self._provider = provider

    @property
    def provider_name(self) -> str:
        return self._provider.name if self._provider else ""

    @property
    def available(self) -> bool:
        return self._provider is not None

    def _backoff(self, attempt: int) -> float:
        return min(30.0, self.backoff_base * (2 ** attempt)) + random.uniform(0, 1)

    # ------------------------------------------------------------------
    def complete_json(self, model: str, system_prompt: str, user_prompt: str) -> dict:
        """请求 LLM 返回 JSON 对象（带指数退避重试）。失败抛异常。"""
        if not self.available:
            raise LLMUnavailableError(
                self._provider_error
                or f"LLM not configured (provider={getattr(self.config, 'provider', '')})")
        max_retries = int(self.config.max_retries)
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            self.call_count += 1
            try:
                text = self._provider.generate(model, system_prompt, user_prompt)
                return _extract_json(text)
            except Exception as exc:
                last_exc = exc
                if attempt >= max_retries:
                    break
                wait = self._backoff(attempt)
                logger.warning("LLM call failed (attempt %d/%d): %s — retry in %.1fs",
                               attempt + 1, max_retries + 1, exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"LLM call failed after retries: {last_exc}") from last_exc

    # ------------------------------------------------------------------
    def complete_json_validated(self, model: str, system_prompt: str,
                                user_prompt: str, schema: dict) -> dict:
        """请求并做严格 Schema 校验；校验失败必须重试，重试后仍失败抛异常。

        上层收到 SchemaValidationError 后必须进入人工检查状态，
        不允许进入 Alert Engine。
        """
        max_retries = int(self.config.max_retries)
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            data = self.complete_json(model, system_prompt, user_prompt)
            try:
                validate(data, schema)
                return data
            except SchemaValidationError as exc:
                last_exc = exc
                logger.warning("LLM schema validation failed (attempt %d/%d): %s",
                               attempt + 1, max_retries + 1, exc)
                if attempt >= max_retries:
                    break
                time.sleep(self._backoff(attempt))
                # 把校验错误反馈给模型，要求修正
                user_prompt = (user_prompt + "\n\n你上次的输出不符合要求："
                               + str(exc) + "\n请重新输出符合要求的 JSON。")
        raise SchemaValidationError(
            f"LLM output failed schema validation after retries: {last_exc}") from last_exc


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in LLM output")
    return json.loads(text[start:end + 1])
