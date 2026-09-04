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
    def generate(self, model: str, system_prompt: str, user_prompt: str,
                 schema: dict | None = None) -> str:
        """发送一次生成请求，返回模型输出文本。网络/服务错误直接抛异常。

        schema：结构化输出约束（项目内 JSON Schema 子集）；
        支持的 Provider 应将其下推为 API 级强制（Gemini responseSchema），
        不支持的 Provider 可忽略（由 LLMClient 事后校验兜底）。
        """


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

    def generate(self, model: str, system_prompt: str, user_prompt: str,
                 schema: dict | None = None) -> str:
        assert self._client is not None
        # strict json_schema 要求全部字段 required，与现有 Schema 形态不匹配；
        # 保持 json_object + LLMClient 事后校验兜底
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

    def generate(self, model: str, system_prompt: str, user_prompt: str,
                 schema: dict | None = None) -> str:
        url = self.ENDPOINT.format(model=model)
        generation_config: dict[str, Any] = {
            "temperature": self._temperature,
            # Structured Output：要求模型只输出 JSON 对象
            "responseMimeType": "application/json",
        }
        if schema:
            # API 级结构化约束：字段类型/枚举/必填在解码层强制，
            # 而不是只靠提示词祈祷 + 事后校验
            generation_config["responseSchema"] = _to_gemini_schema(schema)
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [
                {"role": "user", "parts": [{"text": user_prompt}]},
            ],
            "generationConfig": generation_config,
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
    def __init__(self, llm_config, backoff_base: float = 2.0, budget=None):
        self.config = llm_config
        self.backoff_base = backoff_base
        # 每日调用预算（trace.ai.budget.LLMBudget）：事前熔断，None 表示不设护栏
        self.budget = budget
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
    def complete_json(self, model: str, system_prompt: str, user_prompt: str,
                      schema: dict | None = None) -> dict:
        """请求 LLM 返回 JSON 对象（带指数退避重试）。失败抛异常。

        schema 会下推给支持的 Provider 做 API 级结构化约束。
        """
        if not self.available:
            raise LLMUnavailableError(
                self._provider_error
                or f"LLM not configured (provider={getattr(self.config, 'provider', '')})")
        max_retries = int(self.config.max_retries)
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            # 预算检查必须在每次真实请求之前，且重试也算数：
            # 失控场景里绝大部分开销正是来自重试
            if self.budget is not None:
                self.budget.check()
            self.call_count += 1
            if self.budget is not None:
                self.budget.consume(1)
            try:
                text = self._provider.generate(model, system_prompt, user_prompt,
                                               schema=schema)
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
            data = self.complete_json(model, system_prompt, user_prompt,
                                      schema=schema)
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


def _to_gemini_schema(node: dict) -> dict:
    """把项目内 JSON Schema 子集转换为 Gemini responseSchema 结构。

    - type 数组 ["string","null"] → type=STRING + nullable=true
      （Gemini 不接受联合类型；nullable 字段必须拆出来标）
    - type 转大写（Type 枚举名：STRING/NUMBER/INTEGER/BOOLEAN/ARRAY/OBJECT）
    - 丢弃 Gemini 不支持的约束（minLength 等），保留 enum/required/items
    """
    if not isinstance(node, dict):
        return {}
    out: dict[str, Any] = {}
    t = node.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        out["type"] = str((non_null or ["string"])[0]).upper()
        if "null" in t:
            out["nullable"] = True
    elif isinstance(t, str):
        out["type"] = t.upper()
    for key in ("enum", "description"):
        if key in node:
            out[key] = node[key]
    if "items" in node:
        out["items"] = _to_gemini_schema(node["items"])
    if "properties" in node:
        out["properties"] = {k: _to_gemini_schema(v)
                             for k, v in node["properties"].items()}
    if "required" in node:
        out["required"] = list(node["required"])
    return out
