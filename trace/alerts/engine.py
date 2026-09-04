"""Alert Engine：决定谁在什么时候收到什么提醒。

基础逻辑：

    if security in watchlist
    and final_score >= threshold
    and confidence >= minimum_confidence
    and event_not_sent:
        send_alert()

- 默认阈值 final_score >= 7（可配置）
- 用户可用 /alert SNDK 8 独立调整
- 重复提醒有 idempotency key：
  (user_id, event_id, security_id, event_version, alert_type)
- 机器人重启不会把历史消息重新发送（投递记录持久化）
- /mute 期间不推送
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from trace.db.connection import Database
from trace.db.repositories import (
    AlertDeliveryRepo,
    AlertRuleRepo,
    EventRepo,
    SecurityRepo,
    UserRepo,
    WatchlistRepo,
)
from trace.common.ids import alert_delivery_id
from trace.common.observability import run_id_var
from trace.domain.models import AlertDelivery, AlertType, Event, EventImpact

logger = logging.getLogger(__name__)


@dataclass
class AlertDecision:
    should_send: bool
    user_id: str
    event: Event
    impact: EventImpact
    alert_type: str
    reason: str = ""


@dataclass
class DeliveryReceipt:
    """投递回执：不得仅以 HTTP 200 判断，必须解析 Telegram 返回结果。"""
    status: str                              # sent / failed
    chat_id: str | None = None
    message_id: str | None = None
    response: str | None = None              # ok / api_error / network_error / no_channel
    error: str = ""


@dataclass
class BootstrapSuppression:
    """首次同步保护抑制记录：历史事件（早于用户 alert_activation_at
    或超出 freshness_window）不得作为即时 Alert 集中推送。

    这些事件仍保留在 Event DB，可通过 /event、/digest 查询。
    """
    user_id: str
    event: Event
    impact: EventImpact
    alert_type: str
    reason: str                     # freshness_window / before_activation


@dataclass
class AlertBatch:
    decisions: list[AlertDecision] = field(default_factory=list)
    suppressed: int = 0                      # 阈值/静音/幂等去重等原因被抑制的数量
    bootstrap_suppressions: list[BootstrapSuppression] = field(default_factory=list)


class AlertEngine:
    def __init__(self, db: Database, config):
        self.db = db
        self.config = config
        self.default_threshold = float(config.get("alerts.default_threshold", 7.0))
        self.min_confidence = float(config.get("alerts.minimum_confidence", 0.0))
        # 首次同步保护：即时推送的新鲜度窗口（小时）
        self.freshness_window_hours = float(
            config.get("alerts.freshness_window_hours", 168))
        self.user_repo = UserRepo(db)
        self.watch_repo = WatchlistRepo(db)
        self.rule_repo = AlertRuleRepo(db)
        self.delivery_repo = AlertDeliveryRepo(db)
        self.event_repo = EventRepo(db)
        self.security_repo = SecurityRepo(db)

    # ------------------------------------------------------------------
    def _bootstrap_reason(self, user, event: Event) -> str:
        """首次同步保护：历史事件不得作为即时 Alert 集中推送。

        参考时间取事件实际发生时间（≈来源 published_at），缺失时退回
        first_seen_at / last_updated_at。返回 "" 表示允许推送；否则：
            freshness_window     事件发生在新鲜度窗口之外
            before_activation    事件早于该用户的 alert_activation_at

        被抑制的历史事件仍保留在 Event DB，可通过 /event、/digest 查询。
        """
        now = datetime.now(timezone.utc)

        def _aware(dt):
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

        ref = event.event_time or event.first_seen_at or event.last_updated_at
        if ref is not None:
            ref = _aware(ref)
            cutoff = now - timedelta(hours=self.freshness_window_hours)
            if ref < cutoff:
                return "freshness_window"
            activation = getattr(user, "alert_activation_at", None)
            if activation is not None and ref < _aware(activation):
                return "before_activation"
        return ""

    # ------------------------------------------------------------------
    def evaluate(self, event: Event, impacts: list[EventImpact],
                 is_update: bool = False,
                 bypass_freshness: bool = False,
                 resend_allowed: bool = True) -> AlertBatch:
        """评估事件的投递决策。

        bypass_freshness 仅供验收回放（replay-event --acceptance-test）使用：
        允许绕过首次同步保护的 freshness/activation 检查，其余门禁
        （阈值 / 置信度 / 静音 / 幂等）保持真实，不得放宽。

        resend_allowed=False：事件确有实质更新（version 已 +1，审计轨迹保留），
        但修订原因不在 alerts.revision_resend_rules 白名单内 —— 不再推送。
        这是让那份配置真正生效的地方；此前无论什么原因都会推。
        """
        batch = AlertBatch()
        alert_type = AlertType.EVENT_UPDATE.value if is_update else AlertType.NEW_EVENT.value

        if is_update and not resend_allowed:
            batch.suppressed += sum(
                len(self._target_users(imp.security_id)) for imp in impacts)
            logger.info("event %s updated but resend not allowed by "
                        "revision_resend_rules: %d impacts suppressed",
                        event.event_id, len(impacts))
            return batch

        for impact in impacts:
            for user_id in self._target_users(impact.security_id):
                user = self.user_repo.get(user_id)
                if user is None:
                    continue
                if user.muted_until and user.muted_until > datetime.now(timezone.utc):
                    batch.suppressed += 1
                    continue
                threshold = self._threshold_for(user_id, impact.security_id)
                if impact.final_score < threshold:
                    batch.suppressed += 1
                    continue
                if impact.confidence < self.min_confidence:
                    batch.suppressed += 1
                    continue
                bootstrap_reason = ("" if bypass_freshness
                                    else self._bootstrap_reason(user, event))
                if bootstrap_reason:
                    batch.bootstrap_suppressions.append(BootstrapSuppression(
                        user_id=user_id, event=event, impact=impact,
                        alert_type=alert_type, reason=bootstrap_reason))
                    continue
                if self.delivery_repo.already_sent(
                        user_id, event.event_id, impact.security_id,
                        event.version, alert_type):
                    batch.suppressed += 1
                    continue
                batch.decisions.append(AlertDecision(
                    should_send=True, user_id=user_id, event=event, impact=impact,
                    alert_type=alert_type))
        return batch

    def _target_users(self, security_id: str) -> list[str]:
        """订阅该证券的用户 + 订阅 all 的用户（单条 SQL，避免逐用户查询）。"""
        rows = self.db.query(
            """SELECT user_id FROM watchlist WHERE security_id=?
               UNION
               SELECT user_id FROM alert_rule WHERE security_id=?""",
            (security_id, AlertRuleRepo.ALL_SENTINEL))
        return sorted(r["user_id"] for r in rows)

    def _threshold_for(self, user_id: str, security_id: str) -> float:
        t = self.rule_repo.get_threshold(user_id, security_id)
        if t is not None:
            return t
        t = self.rule_repo.get_threshold(user_id, None)   # all 规则
        return t if t is not None else self.default_threshold

    # ------------------------------------------------------------------
    def mark_bootstrap_suppressed(self, suppression: BootstrapSuppression) -> None:
        """首次同步保护：历史事件抑制记录（显式标记，不宣称投递成功）。"""
        self.delivery_repo.record_bootstrap_suppressed(AlertDelivery(
            delivery_id=alert_delivery_id(),
            user_id=suppression.user_id,
            event_id=suppression.event.event_id,
            security_id=suppression.impact.security_id,
            event_version=suppression.event.version,
            alert_type=suppression.alert_type,
            final_score=suppression.impact.final_score,
            sent_at=datetime.now(timezone.utc),
            status="suppressed",
            response_status=f"bootstrap_suppressed:{suppression.reason}",
            run_id=run_id_var.get("-"),
            bootstrap_suppressed=True,
        ))
        logger.info("bootstrap suppressed: user=%s event=%s reason=%s",
                    suppression.user_id, suppression.event.event_id,
                    suppression.reason)

    def mark_sent(self, decision: AlertDecision,
                  receipt: DeliveryReceipt | None = None) -> None:
        """投递成功后写入回执（含 telegram_chat_id / telegram_message_id）。"""
        self.delivery_repo.record(AlertDelivery(
            delivery_id=alert_delivery_id(),
            user_id=decision.user_id,
            event_id=decision.event.event_id,
            security_id=decision.impact.security_id,
            event_version=decision.event.version,
            alert_type=decision.alert_type,
            final_score=decision.impact.final_score,
            sent_at=datetime.now(timezone.utc),
            status="sent",
            telegram_chat_id=(receipt.chat_id if receipt else None) or decision.user_id,
            telegram_message_id=receipt.message_id if receipt else None,
            response_status=receipt.response if receipt else None,
            run_id=run_id_var.get("-"),
        ))

    def mark_failed(self, decision: AlertDecision, receipt: DeliveryReceipt) -> None:
        """投递失败：记录 status=failed（不占用幂等键，允许下一轮重试）。"""
        self.delivery_repo.record_failed(AlertDelivery(
            delivery_id=alert_delivery_id(),
            user_id=decision.user_id,
            event_id=decision.event.event_id,
            security_id=decision.impact.security_id,
            event_version=decision.event.version,
            alert_type=decision.alert_type,
            final_score=decision.impact.final_score,
            sent_at=datetime.now(timezone.utc),
            status="failed",
            response_status=receipt.response,
            run_id=run_id_var.get("-"),
        ))
        logger.error("alert delivery failed user=%s event=%s (%s): %s",
                     decision.user_id, decision.event.event_id,
                     receipt.response, receipt.error)
