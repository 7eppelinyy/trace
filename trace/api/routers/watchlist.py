"""自选标的 Watchlist API 路由。"""

from __future__ import annotations

from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException

from trace.api.deps import get_app_context, get_current_user_id
from trace.api.schemas import (
    WatchlistAddRequest,
    WatchlistEntryItem,
    WatchlistResponse,
)
from trace.app import AppContext
from trace.common.tickers import TickerParseError, normalize_ticker
from trace.db.repositories import SecurityRepo, WatchlistRepo
from trace.domain.models import Security, WatchlistEntry

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


@router.get("", response_model=WatchlistResponse)
def get_watchlist(
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """获取当前用户的自选股列表及最新行情。"""
    wl_repo = WatchlistRepo(ctx.db)
    sec_repo = SecurityRepo(ctx.db)
    sec_ids = wl_repo.list_by_user(user_id)

    items: list[WatchlistEntryItem] = []
    for sid in sec_ids:
        sec = sec_repo.get(sid)
        if not sec:
            continue
        # 获取行情快照（如有）
        quote = ctx.confirmer.quote(sec.market, sec.ticker, security_id=sec.security_id)
        last_price = quote.last_price if quote else None
        prev_close = quote.prev_close if quote else None
        change_pct = quote.change_pct_from_prev() if quote else None

        items.append(
            WatchlistEntryItem(
                security_id=sec.security_id,
                ticker=sec.ticker,
                market=sec.market,
                company_name_zh=sec.company_name_zh,
                company_name_en=sec.company_name_en,
                last_price=last_price,
                prev_close=prev_close,
                change_pct=round(change_pct, 2) if change_pct is not None else None,
            )
        )

    return WatchlistResponse(items=items, total=len(items))


@router.post("", response_model=WatchlistEntryItem)
def add_watchlist(
    body: WatchlistAddRequest,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """将标的加入自选（支持标准美股代码及 6 位 A 股代码规范）。"""
    try:
        norm = normalize_ticker(body.ticker)
    except TickerParseError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ticker format: {exc}")

    sec_repo = SecurityRepo(ctx.db)
    sec = sec_repo.get_by_ticker(norm.ticker)
    if sec is None:
        sec = Security(
            security_id=f"SEC-{norm.market}-{norm.ticker}",
            market=norm.market,
            exchange=norm.exchange,
            ticker=norm.ticker,
            graph_node_ids=[norm.ticker.lower()],
        )
        sec_repo.upsert(sec)
        ctx.graph.reload()

    WatchlistRepo(ctx.db).add(WatchlistEntry(user_id=user_id, security_id=sec.security_id))

    quote = ctx.confirmer.quote(sec.market, sec.ticker, security_id=sec.security_id)
    last_price = quote.last_price if quote else None
    prev_close = quote.prev_close if quote else None
    change_pct = quote.change_pct_from_prev() if quote else None

    return WatchlistEntryItem(
        security_id=sec.security_id,
        ticker=sec.ticker,
        market=sec.market,
        company_name_zh=sec.company_name_zh,
        company_name_en=sec.company_name_en,
        last_price=last_price,
        prev_close=prev_close,
        change_pct=round(change_pct, 2) if change_pct is not None else None,
    )


@router.delete("/{ticker}")
def remove_watchlist(
    ticker: str,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """从自选中移除标的。"""
    try:
        norm = normalize_ticker(ticker)
    except TickerParseError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ticker format: {exc}")

    sec_repo = SecurityRepo(ctx.db)
    sec = sec_repo.get_by_ticker(norm.ticker)
    if sec:
        WatchlistRepo(ctx.db).remove(user_id, sec.security_id)

    return {"ok": True, "ticker": norm.ticker}
