"""自选标的 Watchlist API 路由。"""

from __future__ import annotations

from typing import Annotated, Any
from fastapi import APIRouter, Depends, HTTPException, Query

from trace.api.deps import get_app_context, get_current_user_id
from trace.api.schemas import (
    WatchlistAddRequest,
    WatchlistAliasUpdateRequest,
    WatchlistEntryItem,
    WatchlistResponse,
    WatchlistSearchItem,
    WatchlistSearchResponse,
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
    entries = wl_repo.list_entries_by_user(user_id)
    if not entries:
        return WatchlistResponse(items=[], total=0)

    entry_map = {e.security_id: e for e in entries}
    valid_secs = [sec_repo.get(e.security_id) for e in entries]
    valid_secs = [s for s in valid_secs if s is not None]

    # 批量并行/分组获取最新行情快照
    reqs = [(s.market, s.ticker, s.security_id) for s in valid_secs]
    quote_map = ctx.confirmer.quotes(reqs)

    items: list[WatchlistEntryItem] = []
    for sec in valid_secs:
        entry = entry_map.get(sec.security_id)
        user_alias = entry.user_alias if entry and entry.user_alias else None

        if sec.is_watchlist_default:
            tier = "realtime_monitored"
        elif sec.is_context_universe:
            tier = "context_universe"
        elif sec.status == "verified":
            tier = "extended_universe"
        else:
            tier = "unverified_candidate"

        quote = quote_map.get((sec.market, sec.ticker))
        last_price = quote.last_price if quote else None
        prev_close = quote.prev_close if quote else None
        change_pct = quote.change_pct_from_prev() if quote else None
        change_pct_15m = quote.change_pct_15m if quote else None

        quality = quote.quality if quote else "unavailable"
        if quality in ("invalid_timestamp", "unavailable"):
            last_price = None
            change_pct = None
            change_pct_15m = None

        items.append(
            WatchlistEntryItem(
                security_id=sec.security_id,
                ticker=sec.ticker,
                market=sec.market,
                company_name_zh=sec.company_name_zh,
                company_name_en=sec.company_name_en,
                user_alias=user_alias,
                status=sec.status,
                coverage_tier=tier,
                last_price=last_price,
                prev_close=prev_close,
                change_pct=round(change_pct, 2) if change_pct is not None else None,
                change_pct_15m=round(change_pct_15m, 2) if change_pct_15m is not None else None,
                market_timestamp=quote.market_timestamp if quote else None,
                fetched_at=quote.fetched_at if quote else None,
                source=quote.source if quote else "",
                currency=quote.currency if quote else ("CNY" if sec.market == "CN" else "USD"),
                is_delayed=quote.is_delayed if quote else False,
                change_basis=quote.change_basis if quote else "prev_close",
                quality=quality,
            )
        )

    return WatchlistResponse(items=items, total=len(items))


def _search_tencent_smartbox(query: str) -> list[Security]:
    """通过腾讯金融 Smartbox 接口获取全市场标的（支持 A 股、美股全部代码/拼音/中文名）。"""
    import urllib.request
    import urllib.parse

    try:
        encoded_q = urllib.parse.quote(query.encode("utf-8"))
        url = f"https://smartbox.gtimg.cn/s3/?t=all&q={encoded_q}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 TraceEventRadar/0.2"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            raw = resp.read().decode("gbk", errors="ignore")
        if '"' not in raw:
            return []
        content = raw.split('"')[1]
        if not content or content == "N":
            return []

        results: list[Security] = []
        for row in content.split("^"):
            parts = row.split("~")
            if len(parts) < 3:
                continue
            m_code, sym, name = parts[0].lower(), parts[1], parts[2]
            if m_code == "sh":
                ticker = f"{sym}.SH"
                market = "CN"
                exchange = "SSE"
            elif m_code == "sz":
                ticker = f"{sym}.SZ"
                market = "CN"
                exchange = "SZSE"
            elif m_code == "us":
                # F23: 保留完整 share class (如 BRK.B / BRK-B), 勿用 split(".")[0] 截断
                norm_sym = sym.strip().upper().replace("-", ".")
                ticker = norm_sym
                market = "US"
                # 不盲目写 NASDAQ，仅对已知标的填入交易所，未知保持为空
                known_ex = {"TSM": "NYSE", "IBM": "NYSE"}
                exchange = known_ex.get(ticker, "")
            else:
                continue

            sec = Security(
                security_id=f"SEC-{market}-{ticker}",
                market=market,
                exchange=exchange,
                ticker=ticker,
                company_name_zh=name,
                company_name_en=ticker,
                status="unverified",
            )
            results.append(sec)
        return results
    except Exception:
        return []


@router.get("/search", response_model=WatchlistSearchResponse)
def search_watchlist(
    q: Annotated[str, Query(max_length=50, description="搜索关键词：股票代码、中文名、英文名")],
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """根据关键词模糊搜索标的主数据，支持代码、中文名与拼音/英文（先主数据后外部补充）。"""
    query_str = (q or "").strip()[:50]
    if not query_str:
        return WatchlistSearchResponse(items=[], total=0)

    sec_repo = SecurityRepo(ctx.db)
    all_secs = sec_repo.list_all()

    q_lower = query_str.lower()
    matched: list[Security] = []
    matched_tickers: set[str] = set()

    # 1. 优先本地主数据精确匹配 ticker
    for s in all_secs:
        if s.ticker.lower() == q_lower:
            matched.append(s)
            matched_tickers.add(s.ticker)

    # 2. 本地主数据中文/英文/代码模糊匹配
    for s in all_secs:
        if s.ticker in matched_tickers:
            continue
        if s.company_name_zh and query_str in s.company_name_zh:
            matched.append(s)
            matched_tickers.add(s.ticker)
        elif s.company_name_en and q_lower in s.company_name_en.lower():
            matched.append(s)
            matched_tickers.add(s.ticker)
        elif q_lower in s.ticker.lower():
            matched.append(s)
            matched_tickers.add(s.ticker)

    # 3. 仅当本地主数据较少时（< 5 条），才进行外部联想补充（避免慢网络阻塞）
    if len(matched) < 5:
        external_secs = _search_tencent_smartbox(query_str)
        for ext in external_secs:
            if ext.ticker not in matched_tickers:
                matched.append(ext)
                matched_tickers.add(ext.ticker)

    # 4. 若仍未命中，但符合合规 Ticker 格式，允许动态生成未核验候选 (F23)
    if not matched:
        try:
            norm = normalize_ticker(query_str)
            dyn_sec = Security(
                security_id=f"SEC-{norm.market}-{norm.ticker}",
                market=norm.market,
                exchange=norm.exchange,
                ticker=norm.ticker,
                company_name_zh=norm.ticker,
                company_name_en=norm.ticker,
                status="unverified",
            )
            matched.append(dyn_sec)
        except TickerParseError:
            pass

    top_secs = matched[:15]

    wl_repo = WatchlistRepo(ctx.db)
    user_watched_ids = set(wl_repo.list_by_user(user_id))

    reqs = [(s.market, s.ticker, s.security_id) for s in top_secs]
    try:
        quote_map = ctx.confirmer.quotes(reqs) if reqs else {}
    except Exception:
        quote_map = {}

    items: list[WatchlistSearchItem] = []
    for s in top_secs:
        quote = quote_map.get((s.market, s.ticker))
        last_price = quote.last_price if quote else None
        change_pct = quote.change_pct_from_prev() if quote else None

        if s.is_watchlist_default:
            tier = "realtime_monitored"
        elif s.is_context_universe:
            tier = "context_universe"
        elif s.status == "verified":
            tier = "extended_universe"
        else:
            tier = "unverified_candidate"

        quality = quote.quality if quote else "unavailable"
        chg_15m = quote.change_pct_15m if quote else None
        if quality in ("invalid_timestamp", "unavailable"):
            last_price = None
            change_pct = None
            chg_15m = None

        items.append(
            WatchlistSearchItem(
                security_id=s.security_id,
                ticker=s.ticker,
                market=s.market,
                company_name_zh=s.company_name_zh or s.ticker,
                company_name_en=s.company_name_en or s.ticker,
                status=s.status,
                coverage_tier=tier,
                last_price=last_price,
                change_pct=round(change_pct, 2) if change_pct is not None else None,
                change_pct_15m=round(chg_15m, 2) if chg_15m is not None else None,
                is_watched=(s.security_id in user_watched_ids),
                market_timestamp=quote.market_timestamp if quote else None,
                fetched_at=quote.fetched_at if quote else None,
                source=quote.source if quote else "",
                currency=quote.currency if quote else ("CNY" if s.market == "CN" else "USD"),
                is_delayed=quote.is_delayed if quote else False,
                change_basis=quote.change_basis if quote else "prev_close",
                quality=quality,
            )
        )

    return WatchlistSearchResponse(items=items, total=len(items))


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
    user_alias = body.user_alias or body.company_name_zh or ""

    if sec is None:
        sec = Security(
            security_id=f"SEC-{norm.market}-{norm.ticker}",
            market=norm.market,
            exchange=norm.exchange,
            ticker=norm.ticker,
            company_name_zh=norm.ticker,
            company_name_en=norm.ticker,
            graph_node_ids=[norm.ticker.lower()],
            status="unverified",
        )
        sec_repo.upsert(sec)
        ctx.graph.reload()
    # F30: 绝不覆盖全局 Security.company_name_zh，保护共享主数据不受污染！

    WatchlistRepo(ctx.db).add(WatchlistEntry(
        user_id=user_id,
        security_id=sec.security_id,
        user_alias=user_alias,
    ))

    quote = ctx.confirmer.quote(sec.market, sec.ticker, security_id=sec.security_id)
    last_price = quote.last_price if quote else None
    prev_close = quote.prev_close if quote else None
    change_pct = quote.change_pct_from_prev() if quote else None

    if sec.is_watchlist_default:
        tier = "realtime_monitored"
    elif sec.is_context_universe:
        tier = "context_universe"
    elif sec.status == "verified":
        tier = "extended_universe"
    else:
        tier = "unverified_candidate"

    return WatchlistEntryItem(
        security_id=sec.security_id,
        ticker=sec.ticker,
        market=sec.market,
        company_name_zh=sec.company_name_zh,
        company_name_en=sec.company_name_en,
        user_alias=user_alias or None,
        status=sec.status,
        coverage_tier=tier,
        last_price=last_price,
        prev_close=prev_close,
        change_pct=round(change_pct, 2) if change_pct is not None else None,
    )


@router.put("/{ticker}/alias", response_model=dict[str, Any])
def update_watchlist_alias(
    ticker: str,
    body: WatchlistAliasUpdateRequest,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """为自选标的设置个人专属别名，不污染系统公共主数据 (F30)。"""
    try:
        norm = normalize_ticker(ticker)
    except TickerParseError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ticker format: {exc}")

    sec_repo = SecurityRepo(ctx.db)
    sec = sec_repo.get_by_ticker(norm.ticker)
    if not sec:
        raise HTTPException(status_code=404, detail="Security not found")

    wl_repo = WatchlistRepo(ctx.db)
    entry = wl_repo.get_entry(user_id, sec.security_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Security not in user watchlist")

    wl_repo.set_user_alias(user_id, sec.security_id, body.user_alias)
    return {"ok": True, "ticker": norm.ticker, "user_alias": body.user_alias}


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


@router.delete("", response_model=dict[str, Any])
def clear_watchlist(
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """清空当前用户的全部自选标的。"""
    cleared = WatchlistRepo(ctx.db).clear(user_id)
    return {"ok": True, "cleared_count": cleared}


@router.post("/reset", response_model=WatchlistResponse)
def reset_watchlist(
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """重置当前用户的自选池为官方默认核心标的池。"""
    wl_repo = WatchlistRepo(ctx.db)
    sec_repo = SecurityRepo(ctx.db)
    wl_repo.clear(user_id)
    defaults = sec_repo.list_watchlist_defaults()
    for d in defaults:
        wl_repo.add(WatchlistEntry(user_id=user_id, security_id=d.security_id))
    return get_watchlist(ctx=ctx, user_id=user_id)

