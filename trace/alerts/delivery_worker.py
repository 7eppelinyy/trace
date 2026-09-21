"""Delivery is independent of analysis; check current consent before each send."""
from datetime import datetime, timezone
import logging
from trace.alerts.engine import DeliveryReceipt, is_in_quiet_hours
from trace.common.ids import alert_delivery_id
from trace.db.repositories import AlertDeliveryRepo, AlertOutboxRepo, ChannelBindingRepo, NotificationPreferenceRepo, UserRepo
from trace.domain.models import AlertDelivery

logger = logging.getLogger(__name__)


def delivery_policy(db, item):
    bindings = ChannelBindingRepo(db).list_active(item.user_id, item.channel_type)
    if not any(b.channel_target == item.channel_target and b.verified_at for b in bindings):
        return 'suppressed', 'channel_binding_inactive_or_revoked'
    if item.channel_type != 'telegram':
        return 'suppressed', 'channel_not_implemented'
    if item.alert_type != 'daily_digest':
        from trace.common.source_policy import event_permitted
        if not event_permitted(db,item.event_id,'forward'):
            return 'suppressed', 'source_forward_not_permitted'
    user = UserRepo(db).get(item.user_id)
    if user is None:
        return 'suppressed', 'user_missing'
    prefs = NotificationPreferenceRepo(db)
    global_pref = prefs.get(item.user_id, None)
    pref = prefs.get_effective_preference(item.user_id, item.security_id)
    if (global_pref and not global_pref.enabled) or not pref.enabled:
        return 'suppressed', 'notification_disabled'
    if pref.channel not in ('all', item.channel_type):
        return 'suppressed', 'channel_not_selected'
    now = datetime.now(timezone.utc)
    if (user.muted_until and user.muted_until > now) or is_in_quiet_hours(now, user.timezone, pref.quiet_start, pref.quiet_end):
        return 'pending', 'quiet_hours_or_muted'
    if item.security_id:
        subscribed = db.query_one("""SELECT 1 FROM watchlist WHERE user_id=? AND security_id=?
            UNION SELECT 1 FROM alert_rule WHERE user_id=? AND security_id IN (?, '__ALL__')""",
            (item.user_id,item.security_id,item.user_id,item.security_id))
        if not subscribed:
            return 'suppressed', 'subscription_removed'
    if item.final_score is not None and item.final_score < pref.threshold:
        return 'suppressed', 'threshold_changed'
    return None, None


def drain_outbox(db, sender, limit=20, lease_seconds=120, is_production=False):
    repo = AlertOutboxRepo(db)
    stats = {'claimed': 0, 'sent': 0, 'failed': 0, 'suppressed': 0}
    for _ in range(limit):
        items = repo.claim_batch(lease_seconds=lease_seconds)
        if not items: break
        item = items[0]
        stats['claimed'] += 1
        state, reason = delivery_policy(db, item)
        if state:
            # Waiting for user quiet hours is not a failed transport attempt.
            if state == 'pending':
                item.retry_count -= 1
            repo.finish(item, state, reason, retry_after_seconds=60 if state == 'pending' else None)
            stats['suppressed'] += 1
            continue
        if sender is None and is_production:
            repo.finish(item, 'pending', 'production_channel_missing', retry_after_seconds=60)
            stats['failed'] += 1
            continue
        try:
            receipt = sender(item.channel_target, item.content_text, item.content_url) if sender else DeliveryReceipt(status='sent', response='log_only')
        except Exception as exc:
            receipt = DeliveryReceipt(status='ambiguous', response='network_error', error=type(exc).__name__)
        try:
            with db.transaction(mode='IMMEDIATE'):
                if receipt.status == 'sent':
                    repo.finish(item, 'sent')
                    AlertDeliveryRepo(db).record(AlertDelivery(
                        delivery_id=alert_delivery_id(), user_id=item.user_id, event_id=item.event_id,
                        security_id=item.security_id or '', event_version=item.event_version,
                        alert_type=item.alert_type, final_score=item.final_score or 0,
                        sent_at=datetime.now(timezone.utc), status='sent', telegram_chat_id=receipt.chat_id or item.channel_target,
                        telegram_message_id=receipt.message_id, response_status=receipt.response))
                    stats['sent'] += 1
                else:
                    error = (receipt.error or '')[:300]
                    lower = error.lower()
                    if receipt.status == 'ambiguous' or 'timeout' in lower or 'network' in lower:
                        repo.finish(item, 'ambiguous', error)
                    elif receipt.http_status in (400,403) or any(t in lower for t in ('403','blocked','chat not found')):
                        ChannelBindingRepo(db).deactivate(item.user_id, item.channel_type, item.channel_target)
                        repo.finish(item, 'failed', error)
                    else:
                        delay = receipt.retry_after_seconds or min(600, 10 * (2 ** item.retry_count))
                        repo.finish(item, 'pending', error, retry_after_seconds=delay)
                    stats['failed'] += 1
        except RuntimeError:
            logger.warning('Discarding stale delivery completion: %s', item.outbox_id)
            stats['failed'] += 1
    return stats


def start_delivery_worker(db, sender, *, production=False):
    """A dedicated consumer continues while collection or model requests are blocked."""
    import threading
    stop = threading.Event()
    def run():
        try:
            while not stop.is_set():
                try:
                    drain_outbox(db, sender, is_production=production)
                except Exception:
                    logger.exception('Delivery worker iteration failed')
                stop.wait(5)
        finally:
            db.close()
    thread = threading.Thread(target=run, name='trace-delivery', daemon=True)
    thread.start()
    return stop, thread
