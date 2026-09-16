"""事件流相关 API 路由。"""

from __future__ import annotations

from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query

from trace.api.deps import get_app_context
from trace.api.schemas import (
    EventDetailResponse,
    EventEvidenceItem,
    EventImpactItem,
    EventListResponse,
    EventRevisionItem,
    EventSummaryItem,
    PaginationMeta,
)
from trace.app import AppContext
from trace.db.repositories import (
    EventImpactRepo,
    EventRepo,
    EventRevisionRepo,
    EventSourceRepo,
    RawItemRepo,
    SecurityRepo,
)

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=EventListResponse)
def list_events(
    ctx: Annotated[AppContext, Depends(get_app_context)],
    market: Annotated[str | None, Query(description="筛选市场：US 或 CN 或 US,CN")] = None,
    min_score: Annotated[float | None, Query(description="最低评分过滤，如 6.0")] = None,
    page: Annotated[int, Query(ge=1, description="页码")] = 1,
    limit: Annotated[int, Query(ge=1, le=100, description="每页条数")] = 20,
):
    """获取重大事件列表（支持市场与评分过滤，支持分页）。"""
    event_repo = EventRepo(ctx.db)
    impact_repo = EventImpactRepo(ctx.db)
    sec_repo = SecurityRepo(ctx.db)

    # 查出最近更新的事件列表
    all_events = event_repo.recent(hours=24 * 30, limit=1000)

    # 批量预加载证券表以便做市场判定
    all_secs = {s.security_id: s for s in sec_repo.list_all()}

    target_markets = {m.strip().upper() for m in market.split(",")} if market else None

    filtered_items: list[EventSummaryItem] = []
    for ev in all_events:
        impacts = impact_repo.list_by_event(ev.event_id)
        max_score = max((imp.final_score for imp in impacts), default=0.0)

        if min_score is not None and max_score < min_score:
            continue

        impacted_secs = [imp.security_id for imp in impacts]
        if target_markets:
            sec_markets = {all_secs[s].market for s in impacted_secs if s in all_secs}
            if not sec_markets.intersection(target_markets):
                continue

        filtered_items.append(
            EventSummaryItem(
                event_id=ev.event_id,
                title=ev.title,
                summary=ev.summary,
                event_type=ev.event_type,
                status=ev.status,
                version=ev.version,
                first_seen_at=ev.first_seen_at,
                last_updated_at=ev.last_updated_at,
                first_source_id=ev.first_source_id,
                primary_source_id=ev.primary_source_id,
                max_score=round(max_score, 1),
                impacts_count=len(impacts),
                impacted_securities=impacted_secs,
            )
        )

    total = len(filtered_items)
    start = (page - 1) * limit
    end = start + limit
    page_items = filtered_items[start:end]

    return EventListResponse(
        items=page_items,
        pagination=PaginationMeta(
            page=page,
            limit=limit,
            total=total,
            has_more=end < total,
        ),
    )


@router.get("/{event_id}", response_model=EventDetailResponse)
def get_event_detail(
    event_id: str,
    ctx: Annotated[AppContext, Depends(get_app_context)],
):
    """获取单个重大事件的完整全景信息（包含证据链溯源、受影响标的、产业路径与版本历史）。"""
    ev = EventRepo(ctx.db).get(event_id)
    if not ev:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found")

    impacts = EventImpactRepo(ctx.db).list_by_event(event_id)
    ev_sources = EventSourceRepo(ctx.db).list_by_event(event_id)
    revisions = EventRevisionRepo(ctx.db).list_by_event(event_id)
    raw_repo = RawItemRepo(ctx.db)

    evidences: list[EventEvidenceItem] = []
    for es in ev_sources:
        raw = raw_repo.get(es.raw_item_id)
        if raw:
            evidences.append(
                EventEvidenceItem(
                    raw_item_id=raw.raw_item_id,
                    source_id=raw.source_id,
                    title=raw.title,
                    url=raw.url,
                    published_at=raw.published_at,
                    role=es.role,
                )
            )

    impact_items = [
        EventImpactItem(
            impact_id=imp.impact_id,
            security_id=imp.security_id,
            direction=imp.direction,
            directness=imp.directness,
            magnitude=imp.magnitude,
            persistence=imp.persistence,
            confidence=imp.confidence,
            reason=imp.reason,
            industry_path=imp.industry_path,
            final_score=imp.final_score,
            base_score=imp.base_score,
            market_confirmation=imp.market_confirmation,
            analysis_mode=imp.analysis_mode,
            market_data_mode=imp.market_data_mode,
        )
        for imp in impacts
    ]

    rev_items = [
        EventRevisionItem(
            revision_id=r.revision_id,
            version=r.version,
            revision_type=r.revision_type,
            material_update=r.material_update,
            note=r.note,
            created_at=r.created_at,
        )
        for r in revisions
    ]

    return EventDetailResponse(
        event_id=ev.event_id,
        title=ev.title,
        summary=ev.summary,
        event_type=ev.event_type,
        status=ev.status,
        version=ev.version,
        first_seen_at=ev.first_seen_at,
        last_updated_at=ev.last_updated_at,
        event_time=ev.event_time,
        language=ev.language,
        first_source_id=ev.first_source_id,
        primary_source_id=ev.primary_source_id,
        material_update=ev.material_update,
        key_numbers=ev.key_numbers or [],
        impacts=impact_items,
        evidences=evidences,
        revisions=rev_items,
    )
