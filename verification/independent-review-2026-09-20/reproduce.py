"""Independent local-only audit probes. Uses temporary DBs and fake providers.

Run from repository root: .venv\Scripts\python.exe verification/independent-review-2026-09-20/reproduce.py
This records observed behavior, not a passing product acceptance suite.
"""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from pytest import MonkeyPatch
from fastapi.testclient import TestClient
import trace.config as config_module
import trace.app as app_module
from trace.api.app import create_api_app
from trace.common.modes import TraceMode
from trace.db.repositories import (
    AlertOutboxRepo, ChannelBindingRepo, EventImpactRepo, EventRepo,
    NotificationPreferenceRepo, ProcessingJobRepo, RawItemRepo, UserRepo,
)
from trace.domain.models import AlertOutbox, Event, EventImpact, RawItem
from trace.event_engine.engine import ExtractedEvent
from trace.event_engine.exact_dedup import ExactDedup
from trace.event_engine.normalize import normalize_raw_item
from trace.alerts.delivery_worker import drain_outbox
from trace.alerts.engine import DeliveryReceipt
from trace.ai.budget import LLMBudgetExceededError
from scripts.restore_drill import run_drill

spec = importlib.util.spec_from_file_location("cursor_helpers", ROOT / "tests/test_collector_cursor_safety.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)

observed = {}
original_connect = socket.socket.connect


def guarded_connect(sock, address):
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
        return original_connect(sock, address)
    raise RuntimeError("Independent audit prohibits external network")


with tempfile.TemporaryDirectory(prefix="trace-independent-audit-", ignore_cleanup_errors=True) as tmp:
    with MonkeyPatch.context() as mp:
        mp.setattr(config_module, "load_dotenv", None)
        for key in list(os.environ):
            if key.startswith(("GEMINI_", "OPENAI_", "TELEGRAM_", "ALPACA_", "JIN10_")):
                mp.delenv(key, raising=False)
        mp.setenv("TRACE_MODE", "offline")
        mp.setattr(socket.socket, "connect", guarded_connect)
        config = config_module.load_config()
        config.db_path = Path(tmp) / "audit.db"
        config.raw["embedding"]["provider"] = "hash"
        mp.setattr(app_module, "load_config", lambda *args: config)
        app = app_module.create_app()
        mp.setattr(app.confirmer, "quote", lambda *args, **kwargs: None)
        mp.setattr(app.confirmer, "quotes", lambda *args, **kwargs: {})

        with TestClient(create_api_app(app), raise_server_exceptions=False) as client:
            mp.setenv("TRACE_MODE", "production")
            prod_dev = client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "audit-victim"})
            mp.setenv("TRACE_MODE", "real")
            real_dev = client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "audit-victim"})
            validation = None
            try:
                TraceMode.validate()
            except ValueError as exc:
                validation = str(exc)
            observed["auth_modes"] = {
                "production_dev_grant": prod_dev.status_code,
                "real_dev_grant": real_dev.status_code,
                "real_issued_subject": real_dev.json().get("user_id"),
                "real_is_production": TraceMode.is_production(),
                "real_validation_error": validation,
            }
            mp.setenv("TRACE_MODE", "offline")
            session = client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "audit-owner"}).json()
            headers = {"Authorization": f"Bearer {session['session_token']}"}
            question = client.post("/api/v1/research/questions", headers=headers, json={
                "title": "Private research - not published",
                "hypothesis": "Private acquisition research hypothesis",
                "user_notes": "Private position notes",
            })
            qid = question.json()["question_id"]
            public = client.get(f"/api/v1/research/questions/{qid}/share")
            invalid_evidence = client.post(f"/api/v1/research/questions/{qid}/evidence", headers=headers,
                                           json={"raw_item_id": "RAW-NOT-EXISTING-AUDIT"})
            observed["private_share"] = {
                "created_status": question.status_code,
                "anonymous_share_status": public.status_code,
                "anonymous_hypothesis": public.json().get("hypothesis"),
                "notes_removed": "user_notes" not in public.json(),
                "nonexistent_evidence_status": invalid_evidence.status_code,
                "nonexistent_evidence_saved": "RAW-NOT-EXISTING-AUDIT" in invalid_evidence.json().get("matched_evidence_ids", []),
            }

        now = datetime.now(timezone.utc)
        event = Event(event_id="EVT-INDEPENDENT-AUDIT", title="Audit semiconductor announcement", summary="Audit evidence",
                      first_seen_at=now, last_updated_at=now, event_time=now, first_source_id="src_sec_edgar")
        EventRepo(app.db).insert(event)

        class FakeProvider:
            name = "audit-no-network"
            calls = 0

            def generate_text(self, **kwargs):
                self.calls += 1
                return "audit model answer"

        provider = FakeProvider()
        mp.setattr(app.pipeline.llm, "_provider", provider)
        answer = app.ask_engine.ask(ticker="NVDA", question="Analyze semiconductor revenue", event_id=event.event_id)
        observed["ask_real_budget_integration"] = {"status": answer.status, "fake_provider_calls": provider.calls,
                                                  "budget_used": app.pipeline.budget.used()}
        try:
            app.ask_engine.ask(question="分析美联储降息对股票估值的影响")
            macro_error = None
        except Exception as exc:
            macro_error = f"{type(exc).__name__}: {exc}"
        observed["macro_ask"] = {"exception": macro_error}
        mp.setattr(app.pipeline.llm, "_provider", None)

        # Round-trip the cursor produced by the actual API; no invented cursor format.
        for index in range(2):
            ts = now - timedelta(minutes=index + 1)
            EventRepo(app.db).insert(Event(event_id=f"EVT-AUDIT-PAGE-{index}", title="Pagination fixture",
                                           first_seen_at=ts, last_updated_at=ts, event_time=ts))
        with TestClient(create_api_app(app)) as client:
            first_page = client.get("/api/v1/events", params={"limit": 1}).json()
            page_meta = first_page["pagination"]
            second_page = client.get("/api/v1/events", params={"limit": 1, "cursor": page_meta["cursor"],
                                                               "snapshot_ts": page_meta["snapshot_ts"]}).json()
            observed["cursor_roundtrip"] = {"first_total": page_meta["total"], "first_has_more": page_meta["has_more"],
                                           "second_items": len(second_page["items"]), "cursor": page_meta["cursor"]}

        raw = RawItem(raw_item_id="RAW-AUDIT-V1", source_id="src_sec_edgar", source_item_id="stable-document-id",
                      title="Original guidance", content="Revenue guidance 100", url="https://example.invalid/stable")
        normalize_raw_item(raw)
        RawItemRepo(app.db).insert(raw)
        revised = copy.deepcopy(raw)
        revised.raw_item_id = "RAW-AUDIT-V2"
        revised.content = "Revenue guidance revised to 50"
        normalize_raw_item(revised)
        dup = ExactDedup(app.db).check(revised)
        observed["stable_source_id_correction"] = {"content_changed": raw.content_hash != revised.content_hash,
                                                  "is_duplicate": dup.is_duplicate, "is_revision": dup.is_revision,
                                                  "reason": dup.reason}

        # Fake valid feed with a timestamp: tests parsing, not vendor access.
        from trace.api.routers import market as market_router
        import httpx
        lines = []
        for symbol in ("us.INX", "us.IXIC", "us.DJI", "sh000688"):
            fields = [""] * 33
            fields[3], fields[30], fields[32] = "123.45", "20260918150000", "1.00"
            lines.append(f'v_{symbol}="' + "~".join(fields) + '";')
        fake_response = httpx.Response(200, content="\n".join(lines).encode("gbk"),
                                       request=httpx.Request("GET", "https://example.invalid/quotes"))

        class FakeHTTP:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get(self, *args, **kwargs): return fake_response

        with MonkeyPatch.context() as local_mp:
            local_mp.setattr(market_router.httpx, "Client", FakeHTTP)
            local_mp.setattr(market_router, "_cached_indices", [])
            local_mp.setattr(market_router, "_last_fetch_ts", 0.0)
            index_quotes = market_router.get_market_indices()
            observed["index_timestamp_parsing"] = [{"status": q.status, "market_timestamp": str(q.market_timestamp),
                                                      "as_of": str(q.as_of)} for q in index_quotes]

        pipeline, collector = helpers._wire(app, mp, ["PROCESSING-CRASH"])
        mp.setattr(app.pipeline, "analyze_event", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("simulated interruption")))
        try:
            pipeline.run_once()
        except RuntimeError:
            pass
        jobs_before = app.db.query("SELECT target_id,status FROM processing_job WHERE job_type='stage_b_analyze'")
        recovered = []
        mp.setattr(app.pipeline, "analyze_event", lambda *a, **kw: recovered.append(1) or [])
        pipeline.run_once()
        observed["processing_crash_recovery"] = {
            "jobs_after_crash": [dict(row) for row in jobs_before],
            "recovered_analysis_calls": len(recovered),
            "jobs_after_next_round": [dict(row) for row in app.db.query("SELECT target_id,status FROM processing_job WHERE job_type='stage_b_analyze'")],
        }

        # Positive control: the originally reported blocked-budget path has improved.
        pipeline, collector = helpers._wire(app, mp, ["BUDGET-RECOVERY"])
        mp.setattr(app.pipeline, "analyze_event", lambda *a, **kw: (_ for _ in ()).throw(LLMBudgetExceededError("audit budget pause")))
        first = pipeline.run_once()
        recovered = []
        mp.setattr(app.pipeline, "analyze_event", lambda *a, **kw: recovered.append(1) or [])
        pipeline.run_once()
        observed["blocked_budget_positive_control"] = {"first_status": first.status, "recovered_calls": len(recovered)}

        denial_raw = RawItem(raw_item_id="RAW-AUDIT-DENIAL", source_id="src_sec_edgar", source_item_id="audit-denial",
                             title="Official denial", content="The previously reported deal was denied", url="https://example.invalid/audit")
        denial_extracted = ExtractedEvent(title="Official denial", summary="Deal denied", entities=["NVDA"],
                                         event_type="other", event_status="contradicted")
        decision = app.event_engine._merge_into(EventRepo(app.db).get(event.event_id), denial_raw, denial_extracted)
        observed["official_denial_merge"] = {"extracted_status": "contradicted", "stored_status": decision.event.status}

        UserRepo(app.db).ensure("audit-notify")
        ChannelBindingRepo(app.db).bind("audit-notify", "telegram", "audit-chat")
        outbox = AlertOutboxRepo(app.db)
        outbox.enqueue(AlertOutbox(outbox_id="audit-pending-notice", user_id="audit-notify", channel_type="telegram",
                                  channel_target="audit-chat", event_id=event.event_id, impact_id=None, event_version=1,
                                  alert_type="new_event", idempotency_key=f"audit-notify:{event.event_id}:SEC-US-NVDA:1:new_event:telegram:audit-chat",
                                  content_text="Audit notification"))
        NotificationPreferenceRepo(app.db).set_preference("audit-notify", None, enabled=False)
        sent = []

        def sender(chat_id, text, url):
            sent.append(chat_id)
            return DeliveryReceipt(status="sent", chat_id=chat_id, message_id="audit-fake", response="ok")

        stats = drain_outbox(app.db, sender, is_production=True)
        observed["disable_after_enqueue"] = {"global_preference_enabled": False, "fake_sender_calls": len(sent), "drain_stats": stats}

        # Deliberately corrupt a foreign-key relationship only in our temporary DB.
        app.db.execute("PRAGMA foreign_keys=OFF")
        app.db.execute("""INSERT INTO research_question(question_id,user_id,title,hypothesis,created_at,updated_at)
                          VALUES (?,?,?,?,?,?)""", ("RQ-FK-AUDIT", "missing-audit-user", "audit", "audit", now.isoformat(), now.isoformat()))
        app.db.execute("PRAGMA foreign_keys=ON")
        with contextlib.redirect_stdout(io.StringIO()):
            drill = run_drill(config.db_path, Path(tmp) / "backups", Path(tmp) / "restore.db")
        observed["restore_foreign_key_violation"] = {
            "drill_status": drill["drill_status"],
            "source_fk": drill["source_verification"]["foreign_key_check"],
            "target_fk": drill["target_verification"]["foreign_key_check"],
            "checked_tables": sorted(drill["target_verification"]["table_counts"]),
        }

        proc = subprocess.run([sys.executable, "-m", "trace.main", "--loop"], cwd=ROOT,
                              capture_output=True, text=True, timeout=15)
        observed["compose_runner_command"] = {"exit_code": proc.returncode, "stderr": proc.stderr.strip()}
        app.db.close()

output = Path(__file__).with_name("probe-results.json")
output.write_text(json.dumps(observed, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(observed, indent=2, ensure_ascii=False))
