# Trace → Google Cloud 7×24 Deployment Gate V1 — Final Report

- **Date**: 2026-08-26
- **Final Status**: `READY_TRACE_GOOGLE_CLOUD_7X24_DEPLOYMENT_V1`
- **Upstream gate**: `READY_TRACE_SOURCE_COMPLETION_GATE_V1` (frozen, 153 tests passed)
- **Deployment model**: Google Compute Engine (e2-micro, Always Free) + Ubuntu 22.04 LTS + Python venv + systemd — no Kubernetes / GKE / Cloud SQL / GPU / LB

---

## 1. Deployment Architecture

| Item | Value |
|---|---|
| Project | `trace-prod-77` |
| VM | `trace-vm` |
| Zone | `us-central1-a` (Always Free eligible) |
| Machine | `e2-micro` (Always Free: 1 shared vCPU, 1 GB RAM) |
| Disk | 30 GB standard persistent (within 30 GB free tier) |
| Preemptible | `false` (stable long-running service) |
| OS | Ubuntu 22.04 LTS |
| App dir | `/opt/trace` |
| Python | 3.10.12, venv at `/opt/trace/.venv` |
| Process manager | systemd unit `trace-run.service` (Type=simple, Restart=always, RestartSec=30) |
| Scheduling | `run_forever(poll_seconds=60)` — one `run_once()` per minute, not a tight loop |
| Secrets | `/opt/trace/.env` (chmod 600, server-side only) |
| Proxy | none — GCP VM direct-connects to all sources, DeepSeek, Telegram |

### Cost
All resources sit within the **Google Cloud Always Free** tier:
- e2-micro in a free region (us-central1): $0
- 30 GB standard persistent disk: $0 (≤30 GB free)
- No GPU / LB / Cloud SQL / GKE created.
- **Estimated monthly cost: $0.** No upgrade to e2-small needed (see resource evidence below).

---

## 2. Acceptance Results (15/15)

| # | Item | Result | Evidence |
|---|---|---|---|
| 1 | gcloud project & VM status correct | PASS | `vm-config.json`, `vm-status.txt` |
| 2 | Server Python/venv/deps OK | PASS | `python-venv.txt` (153 tests importable) |
| 3 | `doctor` passes | PASS | `doctor.log` — 26 items, 0 failures |
| 4 | Full pytest | PASS | `pytest.log` — **153 passed** |
| 5 | Real `run-once` #1 | PASS | `run-once-1.log` — status=OK, 7/7 sources, 193 events |
| 6 | Idempotency `run-once` #2 | PASS | `run-once-2-idempotency.log` — 0 new events, **0 LLM calls**, 0 alerts |
| 7 | Source Health (all enabled) | PASS | `source-health.json`, doctor — all HEALTHY |
| 8 | DeepSeek real call chain | PASS | 408 real `POST api.deepseek.com` (222 A + 160 B + 26 verifier) |
| 9 | Telegram real delivery | PASS | `telegram-smoke-test.log`, receipt message_id=6 |
| 10 | SQLite/state persistence | PASS | `persistence-proof.txt` — identical before/after reboot |
| 11 | systemd auto-restart | PASS | kill PID 6116 → systemd relaunched 6314 (`restart_test.sh`) |
| 12 | VM reboot boot recovery | PASS | after reset, `trace-run` active/enabled, uptime reset |
| 13 | Bootstrap history not pushed | PASS | 1352 alerts suppressed (freshness_window), 0 sent |
| 14 | No secrets in logs | PASS | `secret-scan.txt` — NO_SECRET_IN_LOGS_OR_JOURNAL |
| 15 | e2-micro resource usage | PASS | `resource-usage.txt` — ~306 MB / 958 MB RAM, 9% disk, 0 swap, low CPU |

---

## 3. Key Verification Highlights

### run-once #1 (initial bootstrap)
```
status: OK
sources_checked=10, succeeded=7, failed=0, disabled=3
raw_items_new=222, events_created=193, events_revised=29, events_analyzed=193
alerts_eligible=0, alerts_sent=0, alerts_suppressed=1352   <- bootstrap suppression
llm_stage_a_calls=222, llm_stage_b_calls=160, llm_verifier_calls=26
```
All 1352 eligible historical alerts were suppressed — no bulk historical push.

### run-once #2 (idempotency)
```
raw_items_new=0, raw_items_duplicate=13, events_created=0
llm_stage_a_calls=0, llm_stage_b_calls=0, llm_verifier_calls=0
```
Duplicate items trigger **zero** extra LLM calls — dedup works as required.

### DeepSeek real chain
408 real DeepSeek API calls during run #1 (Stage A extraction + Stage B analysis + same-event verifier), all `HTTP/1.1 200 OK`. No mock/fabricated output.

### Telegram real delivery
`telegram-test` → real `sendMessage` → `message_id=6`, bot `@traceyyy_bot`, production mode.

### Persistence (before vs after VM reboot)
| table | before | after |
|---|---|---|
| event | 193 | 193 |
| raw_item | 222 | 222 |
| collector_cursor | 8 | 8 |
| source_health | 13 | 13 |

### e2-micro resource usage (real measured)
- RAM: 306 MB used / 958 MB total (506 MB available)
- Swap: 0 MB used (none configured)
- Disk: 2.6 GB / 29 GB (9%)
- CPU: ~97% idle at rest; Trace process RSS ~100 MB
- **Conclusion**: e2-micro runs Trace stably. **No upgrade needed.**

---

## 4. Secrets Handling
- `.env` injected via `scp`, chmod 600, never committed to Git.
- Server `.env` contains **no proxy keys** (verified `NO_PROXY_CONFIG`).
- All reports/logs print only variable **names**, never values.
- Secret scan over journal + all run logs: **NO_SECRET_IN_LOGS_OR_JOURNAL**.

## 5. Known Limitations
1. **sentence-transformers excluded** from server install (memory on e2-micro); embeddings fall back to hash embedder (logged as WARNING, non-fatal). Semantic dedup still functions via LLM verifier.
2. **Micron IR RSS endpoint** returns 404/429 (official endpoint issue); fallback newsroom route used, status HEALTHY. Consistent with Source Completion Gate.
3. **Alpaca / CN market data not connected** — production mode marks market data as `unavailable` (no mock), as required.
4. Bootstrap run consumed a one-time LLM batch (~408 calls) for the 222 initial items; subsequent runs are near-zero cost when no new items appear.

## 6. No Code / Business-Logic Changes
No new sources added, no Source Completion redo, no changes to event semantics, dedup strategy, or alert thresholds. Only deployment artifacts were added under `.deploy/` (setup script, service unit, evidence collectors).

---

**Final state: `READY_TRACE_GOOGLE_CLOUD_7X24_DEPLOYMENT_V1`** — all 15 acceptance items completed on a real Google Cloud VM with Always-Free cost.
