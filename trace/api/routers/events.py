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
    EventSecurityChip,
    EventSummaryItem,
    PaginationMeta,
)
from trace.app import AppContext
from trace.common.cursors import encode_cursor, decode_cursor
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
    snapshot_ts: Annotated[str | None, Query(description="稳定快照时间戳，避免下翻页漂移")] = None,
    cursor: Annotated[str | None, Query(description="游标 last_updated_at:event_id")] = None,
):
    """获取重大事件列表（SQL 级下推筛选与稳定分页，O(1) 查询开销）。"""
    event_repo = EventRepo(ctx.db)
    impact_repo = EventImpactRepo(ctx.db)
    sec_repo = SecurityRepo(ctx.db)

    if cursor:
        try:
            decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    # 1. SQL 级下推分页与定界查询（无静默截断）
    page_events, total, eff_snapshot_ts = event_repo.paginate_events(
        page=page,
        limit=limit,
        market=market,
        min_score=min_score,
        snapshot_ts=snapshot_ts,
        cursor=cursor,
        enforce_display=True,
    )

    page_event_ids = [e.event_id for e in page_events]

    # 2. 批量加载当前页的 impacts（单条 SQL，消除 N+1）
    impacts_by_event = impact_repo.list_by_events(page_event_ids)

    # 3. 批量预加载证券字典与行情快照
    all_secs = {s.security_id: s for s in sec_repo.list_all()}
    needed_sids = {
        imp.security_id
        for eid in page_event_ids
        for imp in impacts_by_event.get(eid, [])
    }
    needed_secs = [all_secs[sid] for sid in needed_sids if sid in all_secs]
    reqs = [(s.market, s.ticker, s.security_id) for s in needed_secs]

    # 慢行情解耦 (N05)：事件列表接口仅读取内存/本地快照有时限的已缓存行情 (cached_quotes)，
    # 绝不阻塞在外部行情的同步网络请求上。未命中缓存的标的留空，由详情或独立接口按需加载。
    try:
        quote_map = ctx.confirmer.cached_quotes(reqs) if reqs else {}
    except Exception:
        quote_map = {}

    page_items: list[EventSummaryItem] = []
    for ev in page_events:
        impacts = impacts_by_event.get(ev.event_id, [])
        max_score = max((imp.final_score for imp in impacts), default=0.0)

        impacted_secs = [imp.security_id for imp in impacts]
        has_indirect = any(imp.directness in ("indirect", "conditional") for imp in impacts)
        has_2hop = any("->" in (imp.industry_path or "") and imp.industry_path.count("->") >= 2 for imp in impacts)
        transmission_depth = 3 if has_2hop else (2 if has_indirect else (1 if impacts else 0))
        directness = "indirect" if has_indirect else ("direct" if impacts else "none")

        chips: list[EventSecurityChip] = []
        for sid in impacted_secs:
            sec_obj = all_secs.get(sid)
            if sec_obj:
                quote = quote_map.get((sec_obj.market, sec_obj.ticker))
                chg = quote.change_pct_from_prev() if quote else None
                chg_15m = quote.change_pct_15m if quote else None
                quality = quote.quality if quote else "unavailable"
                chips.append(
                    EventSecurityChip(
                        ticker=sec_obj.ticker,
                        name=sec_obj.company_name_zh or sec_obj.company_name_en or sec_obj.ticker,
                        change_pct=round(chg, 2) if chg is not None else None,
                        change_pct_15m=round(chg_15m, 2) if chg_15m is not None else None,
                        price=quote.last_price if quote else None,
                        market_timestamp=quote.market_timestamp if quote else None,
                        fetched_at=quote.fetched_at if quote else None,
                        source=quote.source if quote else "",
                        currency=quote.currency if quote else ("CNY" if sec_obj.market == "CN" else "USD"),
                        is_delayed=quote.is_delayed if quote else False,
                        change_basis=quote.change_basis if quote else "prev_close",
                        quality=quality,
                    )
                )

        page_items.append(
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
                transmission_depth=transmission_depth,
                directness=directness,
                impacted_securities=impacted_secs,
                securities=chips,
            )
        )

    has_more = (len(page_events) == limit) if cursor else (page * limit < total)
    next_cursor = None
    if page_events and has_more:
        last_ev = page_events[-1]
        ts_str = last_ev.first_seen_at.isoformat() if last_ev.first_seen_at else ""
        next_cursor = encode_cursor(ts_str, last_ev.event_id)

    return EventListResponse(
        items=page_items,
        pagination=PaginationMeta(
            page=page,
            limit=limit,
            total=total,
            has_more=has_more,
            snapshot_ts=eff_snapshot_ts,
            cursor=next_cursor,
            next_cursor=next_cursor,
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
    from trace.common.source_policy import event_permitted
    if not event_permitted(ctx.db,event_id,'display'):
        raise HTTPException(404, 'Event not available for display')

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

    sec_repo = SecurityRepo(ctx.db)
    all_secs = {s.security_id: s for s in sec_repo.list_all()}
    
    chips: list[EventSecurityChip] = []
    impacted_sids = [imp.security_id for imp in impacts]
    needed_secs = [all_secs[sid] for sid in impacted_sids if sid in all_secs]
    reqs = [(s.market, s.ticker, s.security_id) for s in needed_secs]
    quote_map = ctx.confirmer.quotes(reqs) if reqs else {}

    for s in needed_secs:
        quote = quote_map.get((s.market, s.ticker))
        chg = quote.change_pct_from_prev() if quote else None
        chg_15m = quote.change_pct_15m if quote else None
        quality = quote.quality if quote else "unavailable"
        chips.append(
            EventSecurityChip(
                ticker=s.ticker,
                name=s.company_name_zh or s.company_name_en or s.ticker,
                change_pct=round(chg, 2) if chg is not None else None,
                change_pct_15m=round(chg_15m, 2) if chg_15m is not None else None,
                price=quote.last_price if quote else None,
                market_timestamp=quote.market_timestamp if quote else None,
                fetched_at=quote.fetched_at if quote else None,
                source=quote.source if quote else "",
                currency=quote.currency if quote else ("CNY" if s.market == "CN" else "USD"),
                is_delayed=quote.is_delayed if quote else False,
                change_basis=quote.change_basis if quote else "prev_close",
                quality=quality,
            )
        )

    has_indirect = any(imp.directness in ("indirect", "conditional") for imp in impacts)
    has_2hop = any("->" in (imp.industry_path or "") and imp.industry_path.count("->") >= 2 for imp in impacts)
    transmission_depth = 3 if has_2hop else (2 if has_indirect else (1 if impacts else 0))

    claims = []
    if ev.summary:
        claims.append({
            "claim_id": "C_fact_1",
            "kind": "inference",
            "text": "自动提取摘要（请核对原文）：" + ev.summary,
            "evidence_ids": [es.raw_item_id for es in ev_sources],
        })
    for idx, kn in enumerate(ev.key_numbers or [], 1):
        claims.append({
            "claim_id": f"C_fact_num_{idx}",
            "kind": "inference",
            "text": f"待核对的提取数字：{kn}",
            "evidence_ids": [es.raw_item_id for es in ev_sources],
        })
    for idx, imp in enumerate(impacts, 1):
        if imp.reason:
            sec_name = all_secs[imp.security_id].ticker if imp.security_id in all_secs else imp.security_id
            claims.append({
                "claim_id": f"C_inf_{idx}",
                "kind": "inference",
                "text": f"{sec_name}：{imp.reason}",
                "assumptions": [],
                "counter_evidence": [],
                "evidence_ids": imp.evidence_ids,
            })

    uncertainties = []
    if not ev.key_numbers:
        uncertainties.append("当前结构化结果没有关键数字；这不代表原文没有披露，请核对来源。")
    if ev.status not in ("confirmed", "official_confirmed"):
        uncertainties.append("当前记录未标为官方确认，需核对报道、否认或撤回状态。")
    uncertainties.append("产业关系本身不能证明本次事件已产生财务影响。")

    next_checks = [
        "跟踪相关标的下一期季度财报与管理层业绩指引（Guidance）",
        "核查 SEC 8-K / 交易所法定披露系统是否发布正式公告或澄清说明",
        "监控高频产业链进出口数据与重点厂商月度出货统计",
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
        transmission_depth=transmission_depth,
        key_numbers=ev.key_numbers or [],
        impacts=impact_items,
        evidences=evidences,
        revisions=rev_items,
        claims=claims,
        uncertainties=uncertainties,
        next_checks=next_checks,
        securities=chips,
    )
