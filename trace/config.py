"""集中配置加载：settings.yaml + .env。

所有阈值、权重均从 settings.yaml 读取，禁止在业务代码中硬编码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # 可选依赖
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "trace" / "settings.yaml"

# 默认 Provider 与默认模型（Gemini 优先，OpenAI 保持兼容）
DEFAULT_LLM_PROVIDER = "gemini"
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class LLMConfig:
    provider: str = DEFAULT_LLM_PROVIDER
    api_key: str = ""
    base_url: str = ""
    model_event_extractor: str = DEFAULT_GEMINI_MODEL
    model_impact_analyzer: str = DEFAULT_GEMINI_MODEL
    model_same_event_verifier: str = DEFAULT_GEMINI_MODEL
    model_ask: str = DEFAULT_GEMINI_MODEL
    temperature: float = 0.0
    timeout_seconds: float = 60.0
    max_retries: int = 2

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass
class TelegramConfig:
    bot_token: str = ""
    allowed_chat_ids: list[str] = field(default_factory=list)
    default_chat_id: str = ""
    default_user_timezone: str = "Asia/Taipei"

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token)


@dataclass
class SECContactConfig:
    """SEC 公平访问要求：User-Agent 必须是 `公司名 联系邮箱`。"""
    company_name: str = "TraceEventRadar"
    contact_email: str = "research@example.com"

    @property
    def user_agent(self) -> str:
        return f"{self.company_name} {self.contact_email}"


@dataclass
class AppConfig:
    raw: dict[str, Any]
    db_path: Path
    llm: LLMConfig
    telegram: TelegramConfig
    sec_contact: SECContactConfig = field(default_factory=SECContactConfig)

    def get(self, path: str, default: Any = None) -> Any:
        """按点路径读取 settings.yaml 中的配置项。"""
        node: Any = self.raw
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


def load_config(settings_path: Path | None = None) -> AppConfig:
    path = settings_path or (Path(os.environ["TRACE_SETTINGS_PATH"]) if os.environ.get("TRACE_SETTINGS_PATH") else SETTINGS_PATH)
    raw: dict[str, Any] = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    if load_dotenv is not None and os.environ.get('TRACE_LOAD_DOTENV', '1') != '0':
        load_dotenv(PROJECT_ROOT / ".env")

    from trace.common.modes import TraceMode
    TraceMode.validate()

    overrides = {
        "LLM_DAILY_CALL_BUDGET": ("llm", "daily_call_budget", int),
        "LLM_ASK_DAILY_BUDGET": ("llm", "ask_daily_budget", int),
        "LLM_ASK_USER_DAILY_BUDGET": ("llm", "ask_user_daily_budget", int),
        "LLM_PIPELINE_RESERVED_CALLS": ("llm", "pipeline_reserved_calls", int),
        "TRACE_BACKUP_DIR": ("backup", "directory", str),
        "TRACE_BACKUP_KEEP": ("backup", "keep", int),
        "MARKET_QUOTE_CACHE_TTL_SECONDS": ("markets", "quote_cache_ttl_seconds", int),
    }
    for name, (section, key, cast) in overrides.items():
        if os.environ.get(name):
            raw.setdefault(section, {})[key] = cast(os.environ[name])

    db_rel = _env("TRACE_DB_PATH") or raw.get("database", {}).get("path", "data/trace.db")
    db_path = (PROJECT_ROOT / db_rel).resolve()

    llm_raw = raw.get("llm", {})
    models = llm_raw.get("models", {})
    # 环境变量优先于 settings.yaml（API Provider、模型、超时、重试均由环境控制，禁止硬编码）
    # 默认优先 Gemini；LLM_PROVIDER=gemini|openai
    provider = (_env("LLM_PROVIDER", llm_raw.get("provider", DEFAULT_LLM_PROVIDER))
                .strip().lower() or DEFAULT_LLM_PROVIDER)
    if provider == "openai":
        # OpenAI 兼容接口（保持兼容，非必需依赖）
        api_key = _env("OPENAI_API_KEY")
        base_url = _env("OPENAI_BASE_URL") or ""
        env_model = _env("OPENAI_MODEL")
        defaults = models.get("openai", {}) if isinstance(models.get("openai"), dict) else models
        fallback_model = DEFAULT_OPENAI_MODEL
        timeout_env = _env("OPENAI_TIMEOUT_SECONDS", "60")
        retries_env = _env("OPENAI_MAX_RETRIES", "2")
    else:
        # Gemini（默认 Provider）
        api_key = _env("GEMINI_API_KEY")
        base_url = ""
        env_model = _env("GEMINI_MODEL")
        defaults = models.get("gemini", {}) if isinstance(models.get("gemini"), dict) else models
        fallback_model = DEFAULT_GEMINI_MODEL
        timeout_env = _env("GEMINI_TIMEOUT_SECONDS", "60")
        retries_env = _env("GEMINI_MAX_RETRIES", "2")
    default_model = defaults.get("event_extractor", fallback_model)
    llm = LLMConfig(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model_event_extractor=env_model or defaults.get("event_extractor", default_model),
        model_impact_analyzer=env_model or defaults.get("impact_analyzer", default_model),
        model_same_event_verifier=env_model or defaults.get("same_event_verifier", default_model),
        model_ask=env_model or defaults.get("ask", default_model),
        temperature=float(llm_raw.get("temperature", 0.0)),
        timeout_seconds=float(timeout_env or 60),
        max_retries=int(retries_env or 2),
    )

    tg_raw = raw.get("telegram", {})
    telegram = TelegramConfig(
        bot_token=_env("TELEGRAM_BOT_TOKEN"),
        allowed_chat_ids=[
            c.strip()
            for c in _env("TELEGRAM_ALLOWED_CHAT_IDS").split(",")
            if c.strip()
        ],
        default_chat_id=_env("TELEGRAM_DEFAULT_CHAT_ID"),
        default_user_timezone=tg_raw.get("default_user_timezone", "Asia/Taipei"),
    )

    sec_raw = raw.get("sec_contact", {})
    sec_contact = SECContactConfig(
        company_name=_env("SEC_COMPANY_NAME", sec_raw.get("company_name", "TraceEventRadar")),
        contact_email=_env("SEC_CONTACT_EMAIL", sec_raw.get("contact_email", "research@example.com")),
    )

    return AppConfig(raw=raw, db_path=db_path, llm=llm, telegram=telegram,
                     sec_contact=sec_contact)
