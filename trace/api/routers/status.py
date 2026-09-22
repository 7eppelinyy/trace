"""系统健康状态与运行诊断 API 路由 (T14 / F21)。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from trace.api.deps import get_app_context, require_admin
from trace.api.schemas import (
    AmbiguousOutboxItem,
    HealthResponse,
    ReadyResponse,
    ResolveAmbiguousRequest,
    SourceHealthItem,
    SystemStatusResponse,
)
from trace.app import AppContext
from trace.common.modes import TraceMode
from trace.db.health import SourceHealthRepo, derive_health_status
from trace.db.repositories import RunHistoryRepo, SourceRepo

router = APIRouter(tags=["status"])


def _parse_iso(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


@router.get("/health", response_model=HealthResponse)
def get_health(ctx: Annotated[AppContext, Depends(get_app_context)]):
    """业务健康检查 (F21)：细分 healthy / degraded / unhealthy 与排查诊断建议。"""
    diagnostics: list[str] = []
    overall_status = "HEALTHY"

    # 1. 数据库连通性与往返延迟
    t0 = time.perf_counter()
    db_status = "OK"
    try:
        row = ctx.db.query_one("SELECT 1;")
        db_ok = (row and row[0] == 1)
        db_latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        if not db_ok:
            db_status = "ERROR"
            overall_status = "UNHEALTHY"
            diagnostics.append(f"Database probe returned unexpected result: {row[0] if row else 'null'}")
    except Exception as exc:
        db_status = "ERROR"
        db_latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        overall_status = "UNHEALTHY"
        diagnostics.append(f"Database query failed: {type(exc).__name__}")

    # 2. 数据源健康状态与抓取新鲜度检查 (若所有启用源 > 1 小时未成功，标记 DEGRADED)
    sources = SourceRepo(ctx.db).list_all()
    health_repo = SourceHealthRepo(ctx.db)
    health_map = {r["source_id"]: r for r in health_repo.all()}

    items: list[SourceHealthItem] = []
    now_utc = datetime.now(timezone.utc)
    stale_threshold = now_utc - timedelta(hours=1)

    enabled_sources = [s for s in sources if s.enabled]
    has_any_recent_success = False
    stale_count = 0

    for src in sources:
        h = health_map.get(src.source_id)
        status = derive_health_status(h, enabled=src.enabled)
        if status in ("BROKEN", "DEGRADED") and overall_status == "HEALTHY":
            overall_status = "DEGRADED"

        succ_dt = _parse_iso(h["last_success_at"]) if h and h["last_success_at"] else None
        if src.enabled:
            if succ_dt and succ_dt >= stale_threshold:
                has_any_recent_success = True
            else:
                stale_count += 1

        items.append(
            SourceHealthItem(
                source_id=src.source_id,
                source_name=src.source_name,
                status=status,
                last_success_at=succ_dt,
                last_failure_at=_parse_iso(h["last_failure_at"]) if h and h["last_failure_at"] else None,
                last_error=None,  # Raw provider failures belong in authenticated diagnostics/logs.
                consecutive_failures=h["consecutive_failures"] if h else 0,
            )
        )

    if enabled_sources and not has_any_recent_success:
        sources_status = "STALE"
        diagnostics.append("All enabled sources have no successful fetch within the last 1 hour")
        if overall_status == "HEALTHY":
            overall_status = "DEGRADED"
    elif enabled_sources and stale_count > len(enabled_sources) / 2:
        sources_status = "DEGRADED"
        diagnostics.append(f"{stale_count}/{len(enabled_sources)} enabled sources are stale (>1h without success)")
        if overall_status == "HEALTHY":
            overall_status = "DEGRADED"
    else:
        sources_status = "OK"

    # 3. LLM 预算消耗比例检查
    llm_budget_used_pct = None
    if hasattr(ctx, "pipeline") and hasattr(ctx.pipeline, "budget"):
        b = ctx.pipeline.budget
        used = b.used()
        daily_limit = b.daily_limit
        if daily_limit > 0:
            llm_budget_used_pct = round((used / daily_limit) * 100.0, 1)
            if used >= daily_limit:
                diagnostics.append(f"LLM call budget exhausted for today ({used}/{daily_limit})")
                if overall_status == "HEALTHY":
                    overall_status = "DEGRADED"

    # 4. Outbox 待投递积压检查
    outbox_pending_count = 0
    try:
        row = ctx.db.query_one("SELECT COUNT(*) AS cnt FROM alert_outbox WHERE status='pending'")
        outbox_pending_count = (row["cnt"] or 0) if row else 0
        if outbox_pending_count > 50:
            diagnostics.append(f"Alert outbox backlog high ({outbox_pending_count} pending items)")
            if overall_status == "HEALTHY":
                overall_status = "DEGRADED"
    except Exception:
        pass

    return HealthResponse(
        status=overall_status,
        db_status=db_status,
        db_latency_ms=db_latency_ms,
        sources_status=sources_status,
        sources_stale_count=stale_count,
        llm_budget_used_pct=llm_budget_used_pct,
        outbox_pending_count=outbox_pending_count,
        diagnostics=diagnostics,
        sources=items,
    )


@router.get("/ready", response_model=ReadyResponse)
def get_ready(ctx: Annotated[AppContext, Depends(get_app_context)]):
    """服务就绪探针 (Readiness Probe)：验证数据库可达、基础主数据与 migration 完整。"""
    details: dict[str, Any] = {}
    ready = True

    # 1. 数据库连通
    try:
        ctx.db.query_one("SELECT 1")
        details["database"] = "reachable"
    except Exception as exc:
        details["database"] = f"unreachable: {exc}"
        ready = False

    # 2. 迁移版本检查
    try:
        migrations = ctx.db.query("SELECT version FROM schema_version")
        details["applied_migrations_count"] = len(migrations)
        from trace.db.migration import MIGRATIONS_DIR
        expected = {p.stem for p in MIGRATIONS_DIR.glob('*.sql')}
        applied = {r['version'] for r in migrations}
        if applied != expected:
            details["schema_status"] = "incomplete_migrations"
            ready = False
        else:
            details["schema_status"] = "up_to_date"
    except Exception as exc:
        details["schema_status"] = f"error: {exc}"
        ready = False

    # 3. 基础种子主数据检查
    try:
        sec_cnt = ctx.db.query_one("SELECT COUNT(*) AS n FROM security")
        cnt = sec_cnt["n"] if sec_cnt else 0
        details["securities_count"] = cnt
        if cnt == 0:
            details["seeds_status"] = "missing_securities"
            ready = False
        else:
            details["seeds_status"] = "loaded"
    except Exception as exc:
        details["seeds_status"] = f"error: {exc}"
        ready = False

    status_code = 200 if ready else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "ready": ready,
            "status": "READY" if ready else "NOT_READY",
            "details": details,
        },
    )


@router.get("/live")
def get_live():
    """轻量存活探针 (Liveness Probe)：只要 HTTP 进程存活响应即返回 200。"""
    return {
        "status": "ALIVE",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/status", response_model=SystemStatusResponse)
def get_system_status(ctx: Annotated[AppContext, Depends(get_app_context)],
                      admin: Annotated[str, Depends(require_admin)]):
    """获取系统运行指标、最近流水线轮数与回测准确率（受管理员身份保护）。"""
    from pathlib import Path
    runs = RunHistoryRepo(ctx.db).recent(10)
    accuracy_data = {}
    if hasattr(ctx, "ledger") and ctx.ledger:
        accuracy_data = ctx.ledger.summary()

    sanitized_db = Path(ctx.config.db_path).name

    # 业务深度健康诊断 (敏感运维指标)
    oldest_job = ctx.db.query_one(
        "SELECT created_at FROM processing_job WHERE status IN ('pending', 'processing') ORDER BY created_at ASC LIMIT 1"
    )
    oldest_age_sec = None
    if oldest_job and oldest_job["created_at"]:
        try:
            oldest_dt = datetime.fromisoformat(oldest_job["created_at"].replace("Z", "+00:00"))
            oldest_age_sec = round((datetime.now(timezone.utc) - oldest_dt).total_seconds(), 1)
        except Exception:
            pass

    failed_jobs = ctx.db.query_one("SELECT COUNT(*) AS n FROM processing_job WHERE status='failed' OR retry_count >= max_retries")
    ambiguous_outbox = ctx.db.query_one("SELECT COUNT(*) AS n FROM alert_outbox WHERE status='ambiguous'")
    latest_raw = ctx.db.query_one("SELECT MAX(fetched_at) AS max_fetch FROM raw_item")
    from trace.db.backup import latest_backup
    lb = latest_backup(ctx.config.get("backup.directory", "data/backups"))

    business_health = {
        "processing_oldest_age_seconds": oldest_age_sec,
        "processing_failed_count": (failed_jobs["n"] or 0) if failed_jobs else 0,
        "outbox_ambiguous_count": (ambiguous_outbox["n"] or 0) if ambiguous_outbox else 0,
        "latest_source_data_at": latest_raw["max_fetch"] if latest_raw else None,
        "latest_backup": str(lb.name) if lb else None,
    }

    return SystemStatusResponse(
        status=get_health(ctx).status,
        mode=TraceMode.current(),
        db_path=sanitized_db,
        accuracy=accuracy_data,
        recent_runs=runs,
        business_health=business_health,
    )



@router.get("/admin/outbox/ambiguous", response_model=list[AmbiguousOutboxItem])
def list_ambiguous_outbox(
    ctx: Annotated[AppContext, Depends(get_app_context)],
    admin: Annotated[str, Depends(require_admin)],
    limit: int = 100,
):
    """List ambiguous outbox items requiring administrative review."""
    from trace.db.repositories import AlertOutboxRepo
    repo = AlertOutboxRepo(ctx.db)
    items = repo.list_ambiguous(limit=limit)
    return [
        AmbiguousOutboxItem(
            outbox_id=i.outbox_id,
            user_id=i.user_id,
            channel_type=i.channel_type,
            channel_target=i.channel_target,
            event_id=i.event_id,
            event_version=i.event_version,
            alert_type=i.alert_type,
            last_error=i.last_error,
            created_at=i.created_at,
            updated_at=i.updated_at,
        )
        for i in items
    ]


@router.post("/admin/outbox/ambiguous/{outbox_id}/resolve")
def resolve_ambiguous_outbox(
    outbox_id: str,
    payload: ResolveAmbiguousRequest,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    admin: Annotated[str, Depends(require_admin)],
):
    """Resolve ambiguous outbox item with audit trail."""
    from fastapi import HTTPException
    from trace.db.repositories import AlertOutboxRepo
    repo = AlertOutboxRepo(ctx.db)
    try:
        resolved = repo.resolve_ambiguous(
            outbox_id=outbox_id,
            action=payload.action,
            operator=admin,
            note=payload.note,
        )
        return {
            "status": "ok",
            "outbox_id": resolved.outbox_id,
            "new_status": resolved.status,
            "last_error": resolved.last_error,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/admin/outbox/audit")
def list_outbox_audit_logs(
    ctx: Annotated[AppContext, Depends(get_app_context)],
    admin: Annotated[str, Depends(require_admin)],
    outbox_id: str | None = None,
    limit: int = 100,
):
    """List outbox operational resolution audit logs."""
    from trace.db.repositories import AlertOutboxRepo
    repo = AlertOutboxRepo(ctx.db)
    return repo.list_audit_logs(outbox_id=outbox_id, limit=limit)
