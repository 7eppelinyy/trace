# 接手状态与基线记录 (N00)

- **记录时间**：2026-09-21T03:17:04.671459+00:00
- **Git HEAD**：`f27252b93d6328eecafa62f6a23a88b46dd54628`
- **接手快照**：`.acceptance/product-hardening-takeover-20260921111704.zip`
- **文件清单哈希**：`verification/product-hardening/next-pass/source-manifest.json` (261 文件)

## 工作区状态
```text
M .env.example
 M .github/workflows/ci.yml
 M design/miniprogram_homepage_preview.html
 M requirements.txt
 M tests/conftest.py
 M tests/test_api.py
 M tests/test_cn_market_data.py
 M tests/test_collector_cursor_safety.py
 M tests/test_digest_timezone.py
 M tests/test_event_engine.py
 M tests/test_forecast_ledger.py
 M tests/test_human_review.py
 M tests/test_llm_budget.py
 M tests/test_market_confirmation.py
 M tests/test_market_data.py
 M tests/test_ops.py
 M tests/test_perf_paths.py
 M tests/test_pipeline.py
 M tests/test_revision_resend.py
 M trace/ai/budget.py
 M trace/ai/llm_client.py
 M trace/ai/pipeline.py
 M trace/alerts/ask.py
 M trace/alerts/digest.py
 M trace/alerts/engine.py
 M trace/alerts/template.py
 M trace/api/app.py
 M trace/api/deps.py
 M trace/api/routers/ask.py
 M trace/api/routers/digest.py
 M trace/api/routers/events.py
 M trace/api/routers/status.py
 M trace/api/routers/watchlist.py
 M trace/api/schemas.py
 M trace/app.py
 M trace/bot/delivery.py
 M trace/collectors/base.py
 M trace/collectors/market_data/alpaca.py
 M trace/collectors/market_data/base.py
 M trace/collectors/market_data/cn.py
 M trace/collectors/market_data/confirmation.py
 M trace/common/hashing.py
 M trace/common/ids.py
 M trace/common/tickers.py
 M trace/config.py
 M trace/data/seed.py
 M trace/data/seed_securities.yaml
 M trace/data/seed_sources.yaml
 M trace/db/backup.py
 M trace/db/connection.py
 M trace/db/health.py
 M trace/db/migration.py
 M trace/db/repositories.py
 M trace/domain/__init__.py
 M trace/domain/models.py
 M trace/event_engine/embeddings.py
 M trace/event_engine/engine.py
 M trace/event_engine/exact_dedup.py
 M trace/event_engine/revision.py
 M trace/event_engine/semantic_cluster.py
 M trace/feedback/ledger.py
 M trace/graph/industry_graph.py
 M trace/main.py
 M trace/market_time/calendar.py
 M trace/market_time/holidays.yaml
 M trace/pipeline.py
 M trace/settings.yaml
?? .agent/
?? Dockerfile
?? design/miniprogram_ask_preview.html
?? design/miniprogram_event_detail_preview.html
?? design/miniprogram_watchlist_preview.html
?? docker-compose.yml
?? docs/
?? miniprogram/
?? project.config.json
?? project.private.config.json
?? requirements-core.lock
?? requirements-core.txt
?? scratch/
?? scripts/backup.ps1
?? scripts/backup.sh
?? scripts/db_admin.py
?? scripts/restore.ps1
?? scripts/restore.sh
?? scripts/restore_drill.py
?? scripts/verify_local_deployment.py
?? tests/fixtures/
?? tests/test_ask_grounding.py
?? tests/test_authz.py
?? tests/test_budget_concurrency.py
?? tests/test_dedup_and_revision.py
?? tests/test_durable_boundaries.py
?? tests/test_guardrails.py
?? tests/test_hardening_boundaries.py
?? tests/test_health_and_deployment.py
?? tests/test_hypothesis_and_user_validation.py
?? tests/test_immutable_forecast_snapshots.py
?? tests/test_isolation.py
?? tests/test_market_semantics_and_calendar.py
?? tests/test_outbox.py
?? tests/test_pagination_and_query_governance.py
?? tests/test_preferences_and_digest.py
?? tests/test_processing_recovery.py
?? tests/test_research_integration.py
?? tests/test_restore_safety.py
?? tests/test_universe_and_sources_governance.py
?? trace/ai/grounded_answer.py
?? trace/ai/guardrails.py
?? trace/alerts/delivery_worker.py
?? trace/api/routers/auth.py
?? trace/api/routers/market.py
?? trace/api/routers/preferences.py
?? trace/api/routers/research.py
?? trace/api/security.py
?? trace/collectors/market_data/time_quality.py
?? trace/common/cursors.py
?? trace/common/source_policy.py
?? trace/db/jobs.py
?? trace/db/migrations/0014_user_session.sql
?? trace/db/migrations/0015_processing_job.sql
?? trace/db/migrations/0016_channel_outbox.sql
?? trace/db/migrations/0017_notification_preferences.sql
?? trace/db/migrations/0018_event_query_indexes.sql
?? trace/db/migrations/0019_market_rescore_schedule.sql
?? trace/db/migrations/0020_immutable_forecast_snapshots.sql
?? trace/db/migrations/0021_security_and_source_governance.sql
?? trace/db/migrations/0022_hypothesis_tracking_and_feedback.sql
?? trace/db/migrations/0023_private_shares_and_request_limits.sql
?? trace/db/migrations/0024_llm_usage_scope.sql
?? trace/db/migrations/0025_durable_versions_and_leases.sql
?? trace/db/migrations/0026_forecast_provenance.sql
?? trace/db/migrations/0027_research_and_embedding_versions.sql
?? trace/db/outbox.py
?? trace/db/shares.py
?? trace/event_engine/repair.py
?? trace/event_engine/split.py
?? trace/verification/dedup_evaluation.py
?? verification/independent-review-2026-09-20/
?? verification/product-hardening/
?? verification/product-readiness/
```

## Diff 统计
```text
.env.example                                 |  102 +-
 .github/workflows/ci.yml                     |   16 +-
 design/miniprogram_homepage_preview.html     | 1734 ++++++++++++++++++++------
 requirements.txt                             |   38 +-
 tests/conftest.py                            |   44 +
 tests/test_api.py                            |  174 ++-
 tests/test_cn_market_data.py                 |    4 +-
 tests/test_collector_cursor_safety.py        |   16 +-
 tests/test_digest_timezone.py                |    2 +-
 tests/test_event_engine.py                   |    8 +-
 tests/test_forecast_ledger.py                |   13 +-
 tests/test_human_review.py                   |    1 +
 tests/test_llm_budget.py                     |    2 +
 tests/test_market_confirmation.py            |   65 +-
 tests/test_market_data.py                    |    9 +-
 tests/test_ops.py                            |    5 +
 tests/test_perf_paths.py                     |    8 +-
 tests/test_pipeline.py                       |   16 +-
 tests/test_revision_resend.py                |    6 +-
 trace/ai/budget.py                           |   61 +-
 trace/ai/llm_client.py                       |  116 +-
 trace/ai/pipeline.py                         |   82 +-
 trace/alerts/ask.py                          |  520 +++++++-
 trace/alerts/digest.py                       |    7 +-
 trace/alerts/engine.py                       |   38 +-
 trace/alerts/template.py                     |    2 +
 trace/api/app.py                             |   18 +-
 trace/api/deps.py                            |   69 +-
 trace/api/routers/ask.py                     |   60 +-
 trace/api/routers/digest.py                  |    3 +-
 trace/api/routers/events.py                  |  171 ++-
 trace/api/routers/status.py                  |  181 ++-
 trace/api/routers/watchlist.py               |  285 ++++-
 trace/api/schemas.py                         |  235 +++-
 trace/app.py                                 |    8 +-
 trace/bot/delivery.py                        |    8 +-
 trace/collectors/base.py                     |    2 +-
 trace/collectors/market_data/alpaca.py       |  148 ++-
 trace/collectors/market_data/base.py         |   17 +-
 trace/collectors/market_data/cn.py           |   42 +-
 trace/collectors/market_data/confirmation.py |  177 ++-
 trace/common/hashing.py                      |    6 +-
 trace/common/ids.py                          |   13 +
 trace/common/tickers.py                      |   19 +-
 trace/config.py                              |   24 +-
 trace/data/seed.py                           |    7 +
 trace/data/seed_securities.yaml              |   39 +
 trace/data/seed_sources.yaml                 |  174 +++
 trace/db/backup.py                           |  129 +-
 trace/db/connection.py                       |    9 +-
 trace/db/health.py                           |   34 +
 trace/db/migration.py                        |   24 +-
 trace/db/repositories.py                     | 1112 +++++++++++++++--
 trace/domain/__init__.py                     |    6 +
 trace/domain/models.py                       |  214 +++-
 trace/event_engine/embeddings.py             |   12 +
 trace/event_engine/engine.py                 |   68 +-
 trace/event_engine/exact_dedup.py            |   53 +-
 trace/event_engine/revision.py               |   17 +-
 trace/event_engine/semantic_cluster.py       |   71 +-
 trace/feedback/ledger.py                     |  303 +++--
 trace/graph/industry_graph.py                |   14 +-
 trace/main.py                                |    8 +-
 trace/market_time/calendar.py                |  104 +-
 trace/market_time/holidays.yaml              |  128 +-
 trace/pipeline.py                            |  343 ++---
 trace/settings.yaml                          |    2 +
 67 files changed, 6337 insertions(+), 1109 deletions(-)
```

## 独立复核对应关系
- **R01 / RV01, RV02** -> N02 (会话续期、生产门禁、分享快照与缓存隔离)
- **R02 / RV03** -> N04 (真实问答路径、模型统一预算、负例与降级保护)
- **R03 / RV04** -> N03 (作业持久化租约、崩溃恢复与 Outbox 投递闭环)
- **R04 / RV05, RV06** -> N06 (同文档更正修订、向量版本跟踪与 label-blind 评测)
- **R05 / RV07** -> N10 (依赖锁定、干净环境执行与可重现依赖)
- **R06 / RV08, RV09** -> N05 (Quote 质量传递、慢行情解耦、稳定分页游标)
- **R07 / RV10** -> N09 (历史数据迁移、外键恢复与全表哈希灾备演练)
- **R08 / RV11, RV12** -> N07 (来源四能力硬门禁、发送旁路审计与真实授权登记)
- **R09 / RV13** -> N08 (研究假设端到端闭环、列表真实分页与完整导出)
- **R10 / RV14** -> N11 (需求复核矩阵、纠偏报告与交付包)
