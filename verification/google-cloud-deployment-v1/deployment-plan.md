# Deployment Plan — Trace on Google Cloud (Always Free)

## Decision summary
Shortest / stable / low-cost / long-term-maintainable path:
**Compute Engine e2-micro (Always Free) + Ubuntu 22.04 LTS + Python venv + systemd.**
Rejected: GKE, Kubernetes, Cloud Run, Cloud SQL, GPU, LB (unnecessary cost/complexity).

## Region & cost decision
- Free regions checked: us-central1 (preferred), us-west1, us-east1.
- Selected `us-central1-a`: primary free region, stable connectivity to SEC / NVIDIA IR / DeepSeek / Telegram verified by direct connection (no proxy).
- e2-micro: Always Free (non-preemptible, shared vCPU, 1 GB RAM).
- Disk: 30 GB standard persistent (free tier limit).
- Estimated monthly cost: **$0**. Upgrade to e2-small (~$10+/mo) only if measurements prove e2-micro unstable — measurements show it is stable, so no upgrade.

## Deployment steps executed
1. Enable `compute.googleapis.com`; project `trace-prod-77`; billing attached.
2. Create/confirm `trace-vm` (e2-micro, us-central1-a, 30 GB PD, Ubuntu 22.04, non-preemptible).
3. Package code (`trace/`, `requirements.txt`, `pytest.ini`, seed data) into zip; scp to VM.
4. Extract to `/opt/trace`; install `python3.10-venv`, `unzip`.
5. Create venv; install locked deps **excluding `sentence-transformers`** (too heavy for 1 GB RAM; hash embedder fallback).
6. Inject secrets: scp `.env` → `/opt/trace/.env`, chmod 600. Server `.env` has **no proxy keys** (local `127.0.0.1:10808` not inherited).
7. Verify direct connectivity: SEC 200, NVIDIA IR 200, cninfo 200, DeepSeek 200, Telegram 200.
8. Install systemd unit `trace-run.service` (enable + start).

## Scheduling semantics (unchanged)
- `trace.main run` = `Pipeline.run_forever(poll_seconds=60)`: one full `run_once()` per minute, sleep between rounds — not a tight loop.
- Per-source cadence is enforced inside collectors via cursors/rate limits; business alert logic unchanged.
- Dedup guarantees repeated items consume zero LLM calls.

## Operational commands
```bash
sudo systemctl status trace-run
sudo journalctl -u trace-run -f
sudo systemctl restart trace-run
```

## Re-deploy (update) procedure
1. Locally: rebuild zip of code (never include `.env`), `gcloud compute scp` to VM.
2. On VM: `unzip -oq` into `/opt/trace`, `sudo systemctl restart trace-run`.
3. DB/cursors/health persist in `/opt/trace/data/trace.db` (persistent disk).
