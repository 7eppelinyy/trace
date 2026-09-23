"""AI 归因问答与金融深度推演 API 路由。"""

from __future__ import annotations

from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from trace.api.security import admit

from trace.api.deps import get_app_context, get_current_user_id
from trace.api.schemas import AskRequest, AskResponse
from trace.app import AppContext
from trace.common.tickers import TickerParseError, normalize_ticker

router = APIRouter(prefix="/ask", tags=["ask"])


@router.post("", response_model=AskResponse)
def ask_question(
    body: AskRequest,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    stream: Annotated[bool, Query(description="是否以 SSE 流式输出")] = False,
):
    """基于本地事实证据库、产业图谱与不可破甲金融大模型的深度推演问答。"""
    if stream:
        raise HTTPException(422, "Streaming is not supported; use the validated JSON response")
    admit(ctx.db, "ask:" + user_id, limit=10, window_seconds=60)
    normalized_ticker = ""
    if body.ticker and body.ticker.strip():
        try:
            norm = normalize_ticker(body.ticker.strip())
            normalized_ticker = norm.ticker
        except TickerParseError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid ticker format: {exc}")

    history_dicts = [m.model_dump() for m in body.history] if body.history else []
    result = ctx.ask_engine.ask(
        ticker=normalized_ticker,
        question=body.question,
        history=history_dicts,
        user_id=user_id,
        event_id=body.event_id,
        event_version=body.event_version,
        mode=body.mode,
    )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Security '{normalized_ticker}' not found in Security Master. Please /watch it first.",
        )

    evidence_ids = [c.event.event_id for c in result.candidates]
    resolved_ticker = result.security.ticker if result.security else normalized_ticker

    return AskResponse(
        ticker=resolved_ticker,
        event_id=body.event_id,
        mode=getattr(result, "mode", body.mode),
        question=body.question,
        answer=result.text,
        claims=getattr(result, "claims", []),
        citations=getattr(result, "citations", []),
        evidence_events=evidence_ids,
        graph_chain=result.graph_chain,
        status=result.status,
        duration_ms=result.duration_ms,
        model_version=getattr(result, "model_version", "legacy_rule_based"),
        prompt_version=getattr(result, "prompt_version", "v1"),
    )
