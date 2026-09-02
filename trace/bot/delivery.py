"""Telegram 实际投递（任务书 §10）。

不得仅以 HTTP 200 判断完整成功，必须解析 Telegram 返回结果：
    {"ok": true, "result": {"message_id": ..., "chat": {...}}}

run-once 模式下不启动 long polling，直接用 Bot API sendMessage 投递
并保存回执（telegram_chat_id / telegram_message_id / sent_at）。

发送纪律：
    - 同一 chat 之间保持最小间隔（Telegram 约 1 msg/s/chat），
      一轮触发多条提醒时避免可预期的 429；
    - 收到 429 时按响应中的 retry_after 等待后重试一次，
      而不是把消息标 failed 丢给下一轮。
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from trace.alerts.engine import DeliveryReceipt

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

_PER_CHAT_MIN_INTERVAL = 1.05     # 秒；Telegram per-chat 限制 ~1 msg/s
_MAX_RETRY_AFTER = 30.0           # 429 retry_after 超过该值不再等待

_last_send_at: dict[str, float] = {}
_send_lock = threading.Lock()


def _throttle(chat_id: str) -> None:
    """同一 chat 的连续发送之间保持最小间隔。"""
    with _send_lock:
        wait = (_last_send_at.get(chat_id, 0.0) + _PER_CHAT_MIN_INTERVAL
                - time.monotonic())
        _last_send_at[chat_id] = time.monotonic()
    if wait > 0:
        time.sleep(wait)


def reset_rate_limit_state() -> None:
    """清空限速状态（测试隔离用）。"""
    with _send_lock:
        _last_send_at.clear()


class TelegramDeliveryError(Exception):
    pass


def _post_send_message(url: str, chat_id: str, text: str,
                       timeout: float) -> httpx.Response:
    return httpx.post(
        url,
        json={"chat_id": chat_id, "text": text,
              "disable_web_page_preview": True},
        timeout=timeout,
    )


def send_message(bot_token: str, chat_id: str, text: str,
                 timeout: float = 30.0) -> DeliveryReceipt:
    """发送一条消息并解析返回。任何失败都返回 status='failed' 回执。"""
    if not bot_token:
        return DeliveryReceipt(status="failed", response="no_channel",
                               error="TELEGRAM_BOT_TOKEN missing")
    _throttle(str(chat_id))
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    try:
        resp = _post_send_message(url, chat_id, text, timeout)
        # 429：按服务端给的 retry_after 等待后重试一次
        if resp.status_code == 429:
            retry_after = _retry_after_seconds(resp)
            if retry_after is not None:
                logger.warning("telegram 429 for chat %s, retry after %.1fs",
                               chat_id, retry_after)
                _throttle(str(chat_id))
                time.sleep(retry_after)
                resp = _post_send_message(url, chat_id, text, timeout)
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


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """从 429 响应体解析 retry_after（秒）；缺失或超上限返回 None。"""
    try:
        params = (resp.json().get("parameters") or {})
        retry_after = float(params.get("retry_after") or 0)
    except Exception:
        return None
    if retry_after <= 0 or retry_after > _MAX_RETRY_AFTER:
        return None
    return min(retry_after + 0.5, _MAX_RETRY_AFTER)


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
