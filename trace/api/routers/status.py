"""系统健康状态与运行诊断 API 路由。"""

from __future__ import annotations

from typing import Annotated
from fastapi import APIRouter, Depends

from trace.api.deps import get_app_context
from trace.api.schemas import (
    HealthResponse,
    SourceHealthItem,
    SystemStatusResponse,
)
from trace.app import AppContext
from trace.common.modes import TraceMode
from trace.db.health import SourceHealthRepo, derive_health_status
from trace.db.repositories import RunHistoryRepo, SourceRepo

router = APIRouter(tags=["status"])


@router.get("/health", response_model=HealthResponse)
def get_health(ctx: Annotated[AppContext, Depends(get_app_context)]):
    """获取所有数据源的健康状况（Healthy / Degraded / RateLimited / Broken）。"""
    sources = SourceRepo(ctx.db).list_all()
    health_repo = SourceHealthRepo(ctx.db)
    health_map = {r["source_id"]: r for r in health_repo.all()}

    items: list[SourceHealthItem] = []
    overall_status = "HEALTHY"

    for src in sources:
        h = health_map.get(src.source_id)
        status = derive_health_status(h, enabled=src.enabled)
        if status in ("BROKEN", "DEGRADED"):
            overall_status = "DEGRADED"

        items.append(
            SourceHealthItem(
                source_id=src.source_id,
                source_name=src.source_name,
                status=status,
                last_success_at=h["last_success_at"] if h else None,
                last_failure_at=h["last_failure_at"] if h else None,
                last_error=h["last_error"] if h else None,
                consecutive_failures=h["consecutive_failures"] if h else 0,
            )
        )

    return HealthResponse(status=overall_status, sources=items)


@router.get("/status", response_model=SystemStatusResponse)
def get_system_status(ctx: Annotated[AppContext, Depends(get_app_context)]):
    """获取系统运行指标、最近流水线轮数与回测准确率。"""
    runs = RunHistoryRepo(ctx.db).recent(10)
    accuracy_data = {}
    if hasattr(ctx, "ledger") and ctx.ledger:
        accuracy_data = ctx.ledger.summary()

    return SystemStatusResponse(
        status="OK",
        mode=TraceMode.current(),
        db_path=str(ctx.config.db_path),
        accuracy=accuracy_data,
        recent_runs=runs,
    )

