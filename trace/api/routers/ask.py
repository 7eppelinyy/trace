"""AI 归因问答 API 路由。"""

from __future__ import annotations

import asyncio
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from trace.api.deps import get_app_context
from trace.api.schemas import AskRequest, AskResponse
from trace.app import AppContext
from trace.common.tickers import TickerParseError, normalize_ticker

router = APIRouter(prefix="/ask", tags=["ask"])


@router.post("", response_model=AskResponse)
def ask_question(
    body: AskRequest,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    stream: Annotated[bool, Query(description="是否以 SSE 流式输出")] = False,
):
    """基于本地事实证据库与产业图谱的重大事件归因问答。"""
    try:
        norm = normalize_ticker(body.ticker)
    except TickerParseError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ticker format: {exc}")

    result = ctx.ask_engine.ask(norm.ticker, body.question)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Security '{norm.ticker}' not found in Security Master. Please /watch it first.",
        )

    if stream:
        async def event_generator():
            # 按词模拟逐字流式打字输出（前端体验类似 ChatGPT）
            chunks = result.text.split("\n")
            for chunk in chunks:
                yield f"data: {chunk}\n\n"
                await asyncio.sleep(0.03)
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    evidence_ids = [c.event.event_id for c in result.candidates]
    return AskResponse(
        ticker=norm.ticker,
        question=body.question,
        answer=result.text,
        evidence_events=evidence_ids,
    )
