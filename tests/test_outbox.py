"""渠道绑定与可靠投递 Outbox 测试 (T06 / F06)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from trace.alerts.delivery_worker import drain_outbox
from trace.alerts.engine import DeliveryReceipt
from trace.db.repositories import AlertDeliveryRepo, AlertOutboxRepo, ChannelBindingRepo, UserRepo
from trace.domain.models import AlertOutbox


def test_channel_binding_management(db):
    """测试渠道绑定创建、查询与退订撤销。"""
    repo = ChannelBindingRepo(db)
    user_id = "usr_test_123"

    # 1. 绑定 Telegram
    binding = repo.bind(user_id, "telegram", "chat_999")
    assert binding.is_active is True
    assert binding.channel_target == "chat_999"

    # 2. 查询活跃绑定
    active = repo.list_active(user_id)
    assert len(active) == 1
    assert active[0].channel_target == "chat_999"

    # 3. 退订 / 撤销绑定
    ok = repo.deactivate(user_id, "telegram", "chat_999")
    assert ok is True
    assert len(repo.list_active(user_id)) == 0


def test_unbound_user_not_dispatched_to_telegram(app):
    """未绑定 Telegram 的纯小程序/Web 用户绝对不能被冒然当做 Telegram chat_id 投递。"""
    binding_repo = ChannelBindingRepo(app.db)
    outbox_repo = AlertOutboxRepo(app.db)

    # 注册一个纯微信/小程序用户 usr_abc
    UserRepo(app.db).ensure("usr_abc")
    # 确认没有默认绑定
    assert len(binding_repo.list_active("usr_abc")) == 0


def test_outbox_idempotency_and_atomic_claim(db):
    """测试 Outbox 幂等入队与基于租约的原子竞争认领。"""
    repo = AlertOutboxRepo(db)

    item1 = AlertOutbox(
        outbox_id="out_1",
        user_id="u_1",
        channel_type="telegram",
        channel_target="chat_1",
        event_id="e_1",
        impact_id="imp_1",
        event_version=1,
        alert_type="new_event",
        idempotency_key="u_1:e_1:s_1:1:new_event:telegram:chat_1",
        content_text="Hello alert",
    )

    # 1. 首次入队 -> 成功
    assert repo.enqueue(item1) is True

    # 2. 相同幂等键再次入队 -> 被忽略，返回 False
    item_dup = AlertOutbox(
        outbox_id="out_2",
        user_id="u_1",
        channel_type="telegram",
        channel_target="chat_1",
        event_id="e_1",
        impact_id="imp_1",
        event_version=1,
        alert_type="new_event",
        idempotency_key="u_1:e_1:s_1:1:new_event:telegram:chat_1",
        content_text="Duplicate alert",
    )
    assert repo.enqueue(item_dup) is False

    # 3. Worker 1 认领任务（租约 60 秒）
    claimed_1 = repo.claim_batch(limit=10, lease_seconds=60)
    assert len(claimed_1) == 1
    assert claimed_1[0].outbox_id == "out_1"
    assert claimed_1[0].status == "sending"

    # 4. Worker 2 同时尝试认领 -> 租约生效中，无法认领同一任务
    claimed_2 = repo.claim_batch(limit=10, lease_seconds=60)
    assert len(claimed_2) == 0

    # 5. 模拟租约超时（超过 60 秒），任务自动可被重新认领
    past_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    db.execute("UPDATE alert_outbox SET lease_until = ? WHERE outbox_id='out_1'", (past_time,))

    claimed_3 = repo.claim_batch(limit=10, lease_seconds=60)
    assert claimed_3 == []
    assert repo.get("out_1").status == "ambiguous"


def test_drain_outbox_successful_delivery(db):
    """测试 Outbox 队列顺利完成发送与回执落库。"""
    binding_repo = ChannelBindingRepo(db)
    from trace.db.repositories import EventRepo
    from trace.domain.models import Event
    EventRepo(db).insert(Event(event_id="e_ok",title="Fixture",first_source_id="src_sec_edgar"))
    UserRepo(db).ensure("user_a")
    binding_repo.bind("user_a", "telegram", "chat_a")

    outbox_repo = AlertOutboxRepo(db)
    outbox_repo.enqueue(AlertOutbox(
        outbox_id="out_ok_1",
        user_id="user_a",
        channel_type="telegram",
        channel_target="chat_a",
        event_id="e_ok",
        impact_id="imp_ok",
        event_version=1,
        alert_type="new_event",
        idempotency_key="user_a:e_ok:sec_a:1:new:telegram:chat_a",
        content_text="Alert msg",
    ))

    sent_messages = []

    def mock_sender(chat_id, text, url):
        sent_messages.append((chat_id, text))
        return DeliveryReceipt(status="sent", chat_id=chat_id, message_id="msg_101", response="ok")

    # 执行排空
    stats = drain_outbox(db, mock_sender)
    assert stats["claimed"] == 1
    assert stats["sent"] == 1
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "chat_a"

    # 检查状态
    job = outbox_repo.get("out_ok_1")
    assert job.status == "sent"

    # 检查 alert_delivery 回执
    deliv = db.query_one("SELECT * FROM alert_delivery WHERE telegram_chat_id='chat_a'")
    assert deliv is not None
    assert deliv["telegram_message_id"] == "msg_101"
    assert deliv["status"] == "sent"


def test_drain_outbox_403_permanent_failure_deactivates_binding(db):
    """测试 403 / Bot Blocked 致命错误自动撤销渠道绑定并标记永久失败。"""
    binding_repo = ChannelBindingRepo(db)
    from trace.db.repositories import EventRepo
    from trace.domain.models import Event
    EventRepo(db).insert(Event(event_id="e_b",title="Fixture",first_source_id="src_sec_edgar"))
    UserRepo(db).ensure("user_b")
    binding_repo.bind("user_b", "telegram", "chat_blocked")

    outbox_repo = AlertOutboxRepo(db)
    outbox_repo.enqueue(AlertOutbox(
        outbox_id="out_blocked_1",
        user_id="user_b",
        channel_type="telegram",
        channel_target="chat_blocked",
        event_id="e_b",
        impact_id="imp_b",
        event_version=1,
        alert_type="new_event",
        idempotency_key="user_b:e_b:sec_b:1:new:telegram:chat_blocked",
        content_text="Alert msg",
    ))

    def mock_blocked_sender(chat_id, text, url):
        return DeliveryReceipt(status="failed", chat_id=chat_id, error="Forbidden: bot was blocked by the user", response="bot_blocked")

    stats = drain_outbox(db, mock_blocked_sender)
    assert stats["failed"] == 1

    # 渠道绑定应被置为失效
    active = binding_repo.list_active("user_b", "telegram")
    assert len(active) == 0

    # 任务被标记为 failed
    job = outbox_repo.get("out_blocked_1")
    assert job.status == "failed"


def test_drain_outbox_suppressed_when_binding_deactivated_before_send(db):
    """测试在入队后、发送前如果用户撤销绑定，任务将被自动抑制 (suppressed)，保护用户隐私。"""
    binding_repo = ChannelBindingRepo(db)
    UserRepo(db).ensure("user_c")
    binding_repo.bind("user_c", "telegram", "chat_c")

    outbox_repo = AlertOutboxRepo(db)
    outbox_repo.enqueue(AlertOutbox(
        outbox_id="out_c_1",
        user_id="user_c",
        channel_type="telegram",
        channel_target="chat_c",
        event_id="e_c",
        impact_id="imp_c",
        event_version=1,
        alert_type="new_event",
        idempotency_key="user_c:e_c:sec_c:1:new:telegram:chat_c",
        content_text="Private research alert",
    ))

    # 用户在发送前退订 / 撤销渠道
    binding_repo.deactivate("user_c", "telegram", "chat_c")

    calls = []
    stats = drain_outbox(db, lambda chat, text, url: calls.append(chat))
    assert stats["claimed"] == 1
    assert stats["suppressed"] == 1
    assert stats["sent"] == 0
    assert len(calls) == 0, "撤销绑定的用户不得收到任何消息"

    job = outbox_repo.get("out_c_1")
    assert job.status == "suppressed"
