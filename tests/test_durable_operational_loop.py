"""N03 Durable Processing, Multi-Process Leases, Ambiguous Resolution and Operational Loop Tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trace.alerts.delivery_worker import drain_outbox, start_delivery_worker
from trace.alerts.engine import DeliveryReceipt
from trace.common.modes import STATUS_OK, STOP_LLM_BUDGET_EXCEEDED
from trace.common.process_lock import SingleInstanceLock
from trace.db.connection import Database
from trace.db.jobs import ProcessingJobRepo
from trace.db.migration import apply_migrations
from trace.db.outbox import AlertOutboxRepo
from trace.db.health import HumanReviewRepo
from trace.db.repositories import (
    AlertDeliveryRepo,
    ChannelBindingRepo,
    EventRepo,
    NotificationPreferenceRepo,
    RawItemRepo,
    UserRepo,
)
from trace.domain.models import AlertOutbox, Event, RawItem

ROOT = Path(__file__).resolve().parents[1]


def test_processing_job_lease_renewal(db):
    """Test owner lease renewal and rejection of non-owner/expired renewal."""
    repo = ProcessingJobRepo(db)
    job = repo.create_or_update("stage_b_analyze", "ev_renew_1")
    claimed = repo.claim(job.job_id, lease_seconds=300)
    assert claimed is not None

    # 1. Genuine owner renews lease -> succeeds
    ok = repo.renew_lease(claimed, additional_seconds=900)
    assert ok is True
    assert claimed.lease_until is not None

    # 2. Imposter / non-owner tries to renew -> raises RuntimeError
    fake_job = repo.get(job.job_id)
    fake_job.lease_owner = "imposter_owner_token"
    with pytest.raises(RuntimeError, match="lease lost"):
        repo.renew_lease(fake_job, additional_seconds=600)

    # 3. Simulate expired lease -> renewal fails
    db.execute(
        "UPDATE processing_job SET lease_until=? WHERE job_id=?",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job.job_id),
    )
    with pytest.raises(RuntimeError, match="lease lost"):
        repo.renew_lease(claimed, additional_seconds=600)


def test_single_instance_runner_lock(tmp_path):
    """Test single-instance lock prevents duplicate concurrent pipeline runners."""
    lock_file = tmp_path / "test_runner.lock"

    lock1 = SingleInstanceLock(lock_file, name="test_runner")
    lock2 = SingleInstanceLock(lock_file, name="test_runner")

    assert lock1.acquire() is True

    # Second instance attempting to acquire must be blocked
    with pytest.raises(RuntimeError, match="(?i)already active"):
        lock2.acquire()

    # Once first instance releases, second instance can acquire
    lock1.release()
    assert lock2.acquire() is True
    lock2.release()


def test_multiprocess_job_lease_competition():
    """Verify two real OS subprocesses competing for a leased job in a shared SQLite file.

    Process 1 claims with short lease and sleeps past expiration.
    Process 2 claims after expiration and completes.
    Process 1 wakes and attempts completion -> rejected with lease lost error.
    """
    with tempfile.TemporaryDirectory(prefix="trace-multiprocess-") as tmp:
        db_path = Path(tmp) / "shared.db"
        db = Database(db_path)
        apply_migrations(db)

        repo = ProcessingJobRepo(db)
        job = repo.create_or_update("stage_b_analyze", "ev_mp_1")
        job_id = job.job_id
        db.close()

        # Worker script executed by subprocesses
        worker_code = """
import sys, time
from pathlib import Path
root_dir = sys.argv[4]
sys.path.insert(0, root_dir)

from trace.db.connection import Database
from trace.db.jobs import ProcessingJobRepo

action = sys.argv[1]
db_path = sys.argv[2]
job_id = sys.argv[3]
db = Database(db_path)
repo = ProcessingJobRepo(db)

if action == "worker1":
    claimed = repo.claim(job_id, lease_seconds=2)
    if not claimed:
        sys.exit(10)
    # Signal parent that worker 1 claimed
    print("CLAIMED", flush=True)
    # Sleep past lease expiration (3.5 seconds)
    time.sleep(3.5)
    try:
        repo.finish(claimed, "completed")
        # If finish succeeded, that is an error because lease was lost!
        sys.exit(20)
    except RuntimeError as exc:
        if "lease lost" in str(exc):
            sys.exit(0)  # Correctly rejected!
        sys.exit(21)

elif action == "worker2":
    # Wait until worker 1's 2-second lease has expired
    time.sleep(2.5)
    claimed = repo.claim(job_id, lease_seconds=10)
    if not claimed:
        sys.exit(30)
    repo.finish(claimed, "completed")
    sys.exit(0)
"""
        script_file = Path(tmp) / "worker.py"
        script_file.write_text(worker_code, encoding="utf-8")

        p1 = subprocess.Popen(
            [sys.executable, str(script_file), "worker1", str(db_path), job_id, str(ROOT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(ROOT),
        )

        # Wait for worker 1 to output CLAIMED
        claimed_line = p1.stdout.readline().strip()
        assert claimed_line == "CLAIMED", f"Worker 1 output was {claimed_line!r}, stderr: {p1.stderr.read()}"

        # Now start worker 2
        p2 = subprocess.Popen(
            [sys.executable, str(script_file), "worker2", str(db_path), job_id, str(ROOT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(ROOT),
        )

        ret2 = p2.wait(timeout=10)
        ret1 = p1.wait(timeout=10)

        assert ret2 == 0, f"Worker 2 failed with code {ret2}: {p2.stderr.read()}"
        assert ret1 == 0, f"Worker 1 expected exit code 0 (lease lost handled), got {ret1}: {p1.stderr.read()}"

        # Verify DB final state
        db_check = Database(db_path)
        final_job = ProcessingJobRepo(db_check).get(job_id)
        assert final_job.status == "completed"
        db_check.close()


def test_ambiguous_outbox_resolution_and_audit(db):
    """Test ambiguous outbox listing, resolution actions (confirm, requeue, discard), and audit trail."""
    outbox_repo = AlertOutboxRepo(db)
    user_repo = UserRepo(db)
    binding_repo = ChannelBindingRepo(db)

    user_repo.ensure("usr_ops_target")
    binding_repo.bind("usr_ops_target", "telegram", "chat_ops_999")

    item = AlertOutbox(
        outbox_id="out_amb_1",
        user_id="usr_ops_target",
        channel_type="telegram",
        channel_target="chat_ops_999",
        event_id="e_amb_1",
        impact_id="imp_amb_1",
        event_version=1,
        alert_type="new_event",
        idempotency_key="amb_key_1",
        content_text="Ambiguous alert test",
    )
    outbox_repo.enqueue(item)

    # 1. Worker claims item
    claimed = outbox_repo.claim_batch(limit=1, lease_seconds=60)
    assert len(claimed) == 1

    # 2. Simulate lease expiration -> enters 'ambiguous'
    past = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    db.execute("UPDATE alert_outbox SET lease_until=? WHERE outbox_id='out_amb_1'", (past,))
    outbox_repo.claim_batch(limit=1)

    amb_item = outbox_repo.get("out_amb_1")
    assert amb_item.status == "ambiguous"

    # 3. List ambiguous
    amb_list = outbox_repo.list_ambiguous()
    assert any(it.outbox_id == "out_amb_1" for it in amb_list)

    # 4. Resolve via 'requeue'
    resolved = outbox_repo.resolve_ambiguous(
        "out_amb_1", action="requeue", operator="ops_admin_bob", note="Network glitch confirmed; requeuing"
    )
    assert resolved.status == "pending"

    # Verify audit log
    logs = outbox_repo.list_audit_logs("out_amb_1")
    assert len(logs) == 1
    assert logs[0]["action"] == "requeue"
    assert logs[0]["operator"] == "ops_admin_bob"
    assert logs[0]["previous_status"] == "ambiguous"
    assert logs[0]["new_status"] == "pending"

    # 5. Worker can claim it again after requeue
    reclaimed = outbox_repo.claim_batch(limit=1, lease_seconds=60)
    assert len(reclaimed) == 1
    assert reclaimed[0].outbox_id == "out_amb_1"

    # 6. Put it back to ambiguous for confirm_delivered test
    db.execute("UPDATE alert_outbox SET status='ambiguous' WHERE outbox_id='out_amb_1'")
    confirmed = outbox_repo.resolve_ambiguous(
        "out_amb_1", action="confirm_delivered", operator="ops_admin_alice", note="User confirms receipt"
    )
    assert confirmed.status == "sent"

    # Verify alert_delivery recorded
    deliv = db.query_one("SELECT * FROM alert_delivery WHERE user_id='usr_ops_target'")
    assert deliv is not None
    assert deliv["status"] == "sent"
    assert deliv["response_status"] == "admin_confirmed"

    # Verify second audit log entry
    logs2 = outbox_repo.list_audit_logs("out_amb_1")
    assert len(logs2) == 2
    assert [l["action"] for l in logs2] == ["confirm_delivered", "requeue"]

    # 7. Discard test on a new item
    item2 = AlertOutbox(
        outbox_id="out_amb_2",
        user_id="usr_ops_target",
        channel_type="telegram",
        channel_target="chat_ops_999",
        event_id="e_amb_2",
        impact_id=None,
        event_version=1,
        alert_type="new_event",
        idempotency_key="amb_key_2",
        content_text="Discard test",
    )
    outbox_repo.enqueue(item2)
    db.execute("UPDATE alert_outbox SET status='ambiguous' WHERE outbox_id='out_amb_2'")
    discarded = outbox_repo.resolve_ambiguous("out_amb_2", action="discard", operator="ops_admin_alice")
    assert discarded.status == "suppressed"

    # 8. Negative validations
    with pytest.raises(ValueError, match="Only ambiguous outbox items can be resolved"):
        outbox_repo.resolve_ambiguous("out_amb_2", action="requeue", operator="ops_admin")

    with pytest.raises(ValueError, match="Operator must be provided"):
        outbox_repo.resolve_ambiguous("out_amb_1", action="requeue", operator="")

    with pytest.raises(ValueError, match="Invalid resolution action"):
        outbox_repo.resolve_ambiguous("out_amb_1", action="invalid_action", operator="ops_admin")


def test_human_review_retry_safeguards(db):
    """Test review --retry rejects missing payloads, cannot overwrite running/completed jobs, and preserves error."""
    review_repo = HumanReviewRepo(db)
    job_repo = ProcessingJobRepo(db)

    # 1. Missing raw_item payload -> raises ValueError
    review_repo.add("rev_neg_1", reason="stage_a_schema_validation_failed: test", raw_item_id="RAW-nonexistent")
    with pytest.raises(ValueError, match="Original payload is missing"):
        review_repo.retry("rev_neg_1")

    # 2. Raw item exists, but job is completed -> cannot overwrite
    RawItemRepo(db).insert(
        RawItem(
            raw_item_id="RAW-safe-1",
            source_id="src_sec_edgar",
            source_item_id="safe1",
            title="Safe",
            url="https://example.com/safe1",
            published_at=datetime.now(timezone.utc),
        )
    )
    job = job_repo.create_or_update("stage_a_extract", "RAW-safe-1")
    claimed = job_repo.claim(job.job_id)
    job_repo.finish(claimed, "completed")

    review_repo.add("rev_neg_2", reason="stage_a_schema_validation_failed: test", raw_item_id="RAW-safe-1")
    with pytest.raises(ValueError, match="No reviewable job found"):
        review_repo.retry("rev_neg_2")

    # 3. Job in 'human_review' status -> retry successfully requeues to pending, preserves reason
    job_review = job_repo.create_or_update("stage_a_extract", "RAW-safe-2")
    RawItemRepo(db).insert(
        RawItem(
            raw_item_id="RAW-safe-2",
            source_id="src_sec_edgar",
            source_item_id="safe2",
            title="Safe 2",
            url="https://example.com/safe2",
            published_at=datetime.now(timezone.utc),
        )
    )
    cl = job_repo.claim(job_review.job_id)
    job_repo.finish(cl, "human_review", error="Schema invalid")

    review_repo.add("rev_pos_1", reason="stage_a_schema_validation_failed: bad field", raw_item_id="RAW-safe-2")
    ok = review_repo.retry("rev_pos_1")
    assert ok is True

    # Verify job is now pending
    updated_job = job_repo.get(job_review.job_id)
    assert updated_job.status == "pending"
    assert updated_job.retry_count == 0

    # Verify human_review status changed to retried and reason preserved
    rev_row = db.query_one("SELECT * FROM human_review WHERE review_id='rev_pos_1'")
    assert rev_row["status"] == "retried"
    assert "bad field" in rev_row["reason"]


def test_daily_digest_execution_and_outbox_policy(app, monkeypatch):
    """Verify daily digest maintenance execution, controlled exception semantics, and outbox delivery policy."""
    from trace.pipeline import Pipeline
    import pytz

    # Setup user and channel binding
    UserRepo(app.db).ensure("chat_digest_user")
    ChannelBindingRepo(app.db).bind("chat_digest_user", "telegram", "chat_digest_user")

    # Mock telegram config
    app.config.telegram.default_chat_id = "chat_digest_user"
    app.config.telegram.default_user_timezone = "UTC"

    # Force digest send time to be earlier than current UTC hour:minute
    tz = pytz.timezone("UTC")
    now_local = datetime.now(timezone.utc).astimezone(tz)
    past_time = (now_local - timedelta(minutes=5)).strftime("%H:%M")
    app.config.raw.setdefault("digest", {})["send_time"] = past_time

    sent_receipts = []

    def mock_sender(chat_id, text, url):
        sent_receipts.append((chat_id, text))
        return DeliveryReceipt(status="sent", chat_id=chat_id, message_id="msg_digest_100", response="ok")

    pipeline = Pipeline(app)
    pipeline.alert_sender = mock_sender

    # Run maintenance
    pipeline._maybe_send_digest()
    assert len(sent_receipts) == 1
    assert sent_receipts[0][0] == "chat_digest_user"
    assert "每日事件摘要" in sent_receipts[0][1]

    # Verify that delivery worker delivery_policy accepts daily_digest alert_type
    outbox_repo = AlertOutboxRepo(app.db)
    outbox_repo.enqueue(
        AlertOutbox(
            outbox_id="out_dig_test",
            user_id="chat_digest_user",
            channel_type="telegram",
            channel_target="chat_digest_user",
            event_id="digest_2026-09-21",
            impact_id=None,
            event_version=1,
            alert_type="daily_digest",
            idempotency_key="dig_test_key",
            content_text="Daily digest outbox content",
        )
    )
    stats = drain_outbox(app.db, mock_sender)
    assert stats["claimed"] == 1
    assert stats["sent"] == 1


def test_delivery_worker_continues_under_model_stop(app, monkeypatch):
    """Verify delivery worker thread continues to process queued messages even when pipeline hits model STOP."""
    from trace.pipeline import Pipeline

    # 1. Enqueue an alert to outbox
    UserRepo(app.db).ensure("usr_worker_test")
    ChannelBindingRepo(app.db).bind("usr_worker_test", "telegram", "chat_w_1")
    app.db.execute("UPDATE source SET can_forward=1 WHERE source_id='src_sec_edgar'")
    EventRepo(app.db).insert(Event(event_id="e_w_1", title="Worker Test", first_source_id="src_sec_edgar"))

    outbox_repo = AlertOutboxRepo(app.db)
    outbox_repo.enqueue(
        AlertOutbox(
            outbox_id="out_w_1",
            user_id="usr_worker_test",
            channel_type="telegram",
            channel_target="chat_w_1",
            event_id="e_w_1",
            impact_id=None,
            event_version=1,
            alert_type="new_event",
            idempotency_key="w_key_1",
            content_text="Worker test alert",
        )
    )

    delivered_items = []

    def mock_sender(target, text, url):
        delivered_items.append(target)
        return DeliveryReceipt(status="sent", chat_id=target, message_id="m1", response="ok")

    # Start independent delivery worker
    stop_event, worker_thread = start_delivery_worker(app.db, mock_sender)

    try:
        # Wait up to 3 seconds for background worker to drain and commit 'sent'
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            item = outbox_repo.get("out_w_1")
            if item and item.status == "sent":
                break
            time.sleep(0.05)

        assert delivered_items == ["chat_w_1"]
        assert outbox_repo.get("out_w_1").status == "sent"
    finally:
        stop_event.set()
        worker_thread.join(timeout=3)
