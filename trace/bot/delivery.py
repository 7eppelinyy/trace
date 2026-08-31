"""Telegram 实际投递（任务书 §10）。

不得仅以 HTTP 200 判断完整成功，必须解析 Telegram 返回结果：
    {"ok": true, "result": {"message_id": ..., "chat": {...}}}

run-once 模式下不启动 long polling，直接用 Bot API sendMessage 投递
并保存回执（telegram_chat_id / telegram_message_id / sent_at）。
"""

from __future__ import annotations

import logging

import httpx

from trace.alerts.engine import DeliveryReceipt

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramDeliveryError(Exception):
    pass


def send_message(bot_token: str, chat_id: str, text: str,
                 timeout: float = 30.0) -> DeliveryReceipt:
    """发送一条消息并解析返回。任何失败都返回 status='failed' 回执。"""
    if not bot_token:
        return DeliveryReceipt(status="failed", response="no_channel",
                               error="TELEGRAM_BOT_TOKEN missing")
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    try:
        resp = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text,
                  "disable_web_page_preview": True},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return DeliveryReceipt(status="failed", response="network_error",
                               error=f"network: {exc}")

    try:
        data = resp.json()
    except ValueError:
        return DeliveryReceipt(status="failed", response="api_error",
                               error=f"non-JSON response (HTTP {resp.status_code})")

    # 必须解析返回体：ok=false 时即使 HTTP 200 也算失败
    if not data.get("ok"):
        desc = data.get("description", "unknown api error")
        return DeliveryReceipt(status="failed", response="api_error",
                               error=f"api: {desc} (HTTP {resp.status_code})")

    result = data.get("result") or {}
    message_id = result.get("message_id")
    result_chat = str((result.get("chat") or {}).get("id", chat_id))
    if message_id is None:
        return DeliveryReceipt(status="failed", response="api_error",
                               error="api: ok=true but no message_id in result")
    return DeliveryReceipt(status="sent", chat_id=result_chat,
                           message_id=str(message_id), response="ok")


def get_me(bot_token: str, timeout: float = 15.0) -> dict | None:
    """getMe：验证 Bot 身份（doctor 用）。失败返回 None。"""
    try:
        resp = httpx.get(f"{TELEGRAM_API}/bot{bot_token}/getMe", timeout=timeout)
        data = resp.json()
        if data.get("ok"):
            return data.get("result")
    except Exception as exc:
        logger.warning("telegram getMe failed: %s", exc)
    return None
