"""可观测性：run_id 上下文 + 敏感信息脱敏。

日志必须能够追踪：
    run_id / source_id / raw_item_id / event_id / event_version /
    security_id / alert_delivery_id

不得在日志中输出：
    Telegram Token / LLM API Key / 完整用户凭据 / 未授权付费正文
"""

from __future__ import annotations

import contextvars
import logging
import re
import sys
import uuid

run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_run_id", default="-")


def new_run_id() -> str:
    rid = f"run-{uuid.uuid4().hex[:10]}"
    run_id_var.set(rid)
    return rid


class RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get("-")
        return True


# 常见凭据模式（Telegram bot token / OpenAI key / 通用 bearer 等）
_SECRET_PATTERNS = [
    re.compile(r"\d{8,10}:AA[0-9a-zA-Z_\-]{30,}"),          # telegram bot token
    re.compile(r"sk-[A-Za-z0-9]{16,}"),                      # openai style key
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"),
]


class SecretRedactionFilter(logging.Filter):
    """把日志消息中的疑似凭据替换为 [REDACTED]。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = msg
            for pat in _SECRET_PATTERNS:
                redacted = pat.sub("[REDACTED]", redacted)
            if redacted != msg:
                record.msg = redacted
                record.args = None
        except Exception:
            pass
        return True


def use_utf8_console() -> None:
    """把 stdout/stderr 切到 UTF-8。

    Windows 控制台默认是 GBK 之类的本地代码页，遇到 CLI 输出里的 emoji
    （摘要的 📊、人工检查队列的 🔎、回测账本的 📈）会直接抛
    UnicodeEncodeError 让命令崩掉 —— 不是显示成乱码，是整条命令失败。
    errors="replace" 保证即使目标编码仍不支持某些字符也只是显示降级。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass        # 被重定向到不支持重配置的流：保持原样


def setup_logging(level: int = logging.INFO) -> None:
    use_utf8_console()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(run_id)s] %(name)s: %(message)s"))
    handler.addFilter(RunIdFilter())
    handler.addFilter(SecretRedactionFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
