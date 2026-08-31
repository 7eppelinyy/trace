# Cost Estimate — Trace on Google Cloud

## Resources created
| Resource | Spec | Always Free eligible | Monthly cost |
|---|---|---|---|
| Compute Engine VM `trace-vm` | e2-micro (shared vCPU, 1 GB RAM), us-central1-a | Yes (e2-micro in free region) | $0.00 |
| Boot disk | 30 GB standard persistent | Yes (30 GB pd-standard free) | $0.00 |
| Egress (network) | outbound < Always Free cap | Yes (1 GB free/region/mo; Trace traffic is tiny) | $0.00 |

## What was NOT created (cost avoiders)
- No GPU
- No Load Balancer
- No Cloud SQL
- No GKE / Cloud Run / App Engine
- No static IP reservation (ephemeral IP only)

## Total estimated monthly cost
**$0.00** — entirely within Google Cloud Always Free tier.

## DeepSeek / Telegram API costs
- DeepSeek API: billed per token by DeepSeek (external, not GCP). Idempotency ensures repeated runs cost ~0 (0 LLM calls when no new items). Initial bootstrap one-time ~408 calls.
- Telegram Bot API: free.

## Upgrade contingency (NOT triggered)
If e2-micro were measured unstable, upgrade candidate = e2-small (~$7–10/mo sustained in us-central1). Measured resource usage (306/958 MB RAM, 9% disk, near-idle CPU) shows e2-micro is adequate, so no upgrade and no recurring charge.
