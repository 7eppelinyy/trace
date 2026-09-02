"""pytest 共享 fixture：每个测试用独立临时数据库。

测试必须与本地 .env 中的真实凭据完全隔离：
    - 禁止 load_config() 读取 .env（不得注入真实 API Key / Token）
    - 测试一律不得发起真实 LLM / Telegram / 数据源网络调用
需要特定环境语义的测试用 monkeypatch 显式设置。
"""

from __future__ import annotations

import pytest

from trace.config import load_config
from trace.db.connection import Database
from trace.db.migration import apply_migrations
from trace.data.seed import load_all_seeds

# verification/ 是验收证据目录（含模块级执行真实网络请求的一次性脚本），
# 不得被 pytest 收集；默认测试套件必须离线可运行
collect_ignore_glob = ["verification/*"]

# 测试会话内隔离的环境变量名单（真实凭据与 Provider 选择）
_ISOLATED_VARS = (
    "LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
    "OPENAI_TIMEOUT_SECONDS", "OPENAI_MAX_RETRIES",
    "GEMINI_TIMEOUT_SECONDS", "GEMINI_MAX_RETRIES",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_DEFAULT_CHAT_ID", "TELEGRAM_ALLOWED_CHAT_IDS",
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """禁止测试读取 .env 真实凭据；默认离线模式。"""
    monkeypatch.setattr("trace.config.load_dotenv", None)
    for name in _ISOLATED_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRACE_MODE", "offline")
    yield


@pytest.fixture(autouse=True)
def _no_backoff_sleeps(monkeypatch):
    """单元测试不等真实退避：LLM/Telegram 重试逻辑照常执行，sleep 置零；
    并清空 Telegram 投递的按 chat 限速状态（跨测试隔离）。"""
    monkeypatch.setattr("trace.ai.llm_client.time.sleep", lambda s: None)
    monkeypatch.setattr("trace.bot.delivery.time.sleep", lambda s: None)
    from trace.bot import delivery as _delivery
    _delivery.reset_rate_limit_state()
    yield
    _delivery.reset_rate_limit_state()


@pytest.fixture()
def config():
    return load_config()


@pytest.fixture()
def db(tmp_path, config):
    database = Database(tmp_path / "test.db")
    apply_migrations(database)
    load_all_seeds(database)
    yield database
    database.close()


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """独立临时数据库的 AppContext（与 .env 真实凭据完全隔离）。"""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")

    import trace.app as appmod

    def fake_load(path=None):
        cfg = load_config()
        cfg.db_path = tmp_path / "app.db"
        return cfg

    monkeypatch.setattr(appmod, "load_config", fake_load)
    return appmod.create_app()
