"""用户身份认证与会话凭据管理 API 路由 (F02/T02/N02)。"""

from __future__ import annotations

import secrets
import os
from datetime import datetime, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from trace.api.deps import get_app_context, get_current_user_id
from trace.app import AppContext
from trace.common.modes import TraceMode
from trace.db.repositories import SecurityRepo, UserRepo, UserSessionRepo, WatchlistRepo
from trace.domain.models import WatchlistEntry
from trace.api.security import admit

router = APIRouter(prefix="/auth", tags=["auth"])


class SessionCreateRequest(BaseModel):
    grant_type: str = Field(default="guest", description="凭据类型: guest (匿名访客) 或 dev (开发测试)")
    user_id: str | None = Field(default=None, description="指定用户标识 (仅在开发/测试模式且 grant_type=dev 时有效)")


class SessionRefreshRequest(BaseModel):
    refresh_token: str = Field(..., description="安全刷新凭据")


class SessionRevokeRequest(BaseModel):
    refresh_token: str | None = Field(default=None, description="待撤销的刷新凭据")


class SessionResponse(BaseModel):
    session_token: str
    user_id: str
    expires_at: str
    token_type: str = "Bearer"
    refresh_token: str | None = None


@router.post("/session", response_model=SessionResponse)
def create_session(
    body: SessionCreateRequest,
    request: Request,
    ctx: Annotated[AppContext, Depends(get_app_context)],
):
    """服务端会话签发入口：为客户端签发有期限、可撤销的会话凭据及刷新凭据。"""
    TraceMode.validate()
    is_prod = TraceMode.is_production()
    peer = request.client.host if request.client else "unknown"
    admit(ctx.db, "session-ip:" + peer, limit=20)
    admit(ctx.db, "session-global", limit=500)

    if body.grant_type == "dev":
        if is_prod or os.environ.get("TRACE_ALLOW_DEV_AUTH") != "1":
            raise HTTPException(status_code=403, detail="dev grant_type is prohibited in production mode")
        uid = (body.user_id or "").strip()
        if not uid:
            uid = "dev_" + secrets.token_hex(4)
    elif body.grant_type == "guest":
        # 访客模式：由服务端分配安全主体 ID，避免客户端自声明碰撞或伪造他人 ID
        uid = "usr_" + secrets.token_hex(8)
    elif body.grant_type == "wechat":
        raise HTTPException(501, "WeChat login is not configured; use an explicitly labelled device guest session")
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported grant_type: {body.grant_type}")

    # 幂等注册用户并在短事务中初始化默认核心自选池与刷新凭据
    with ctx.db.transaction(mode="IMMEDIATE"):
        is_new = UserRepo(ctx.db).ensure(uid, ctx.config.telegram.default_user_timezone)
        if is_new:
            sec_repo = SecurityRepo(ctx.db)
            wl_repo = WatchlistRepo(ctx.db)
            defaults = sec_repo.list_watchlist_defaults()
            for d in defaults:
                wl_repo.add(WatchlistEntry(user_id=uid, security_id=d.security_id))

        session_repo = UserSessionRepo(ctx.db)
        refresh_token, family_id = session_repo.issue_refresh_token(user_id=uid, ttl_days=90)
        session = session_repo.create_session(user_id=uid, ttl_days=30, family_id=family_id)

    return SessionResponse(
        session_token=session.session_token,
        refresh_token=refresh_token,
        user_id=session.user_id,
        expires_at=session.expires_at.isoformat(),
        token_type="Bearer",
    )


@router.post("/session/refresh", response_model=SessionResponse)
def refresh_session(
    body: SessionRefreshRequest,
    request: Request,
    ctx: Annotated[AppContext, Depends(get_app_context)],
):
    """会话续期入口：使用刷新凭据轮换获取新会话与新刷新凭据，保持同一 user_id。"""
    TraceMode.validate()
    peer = request.client.host if request.client else "unknown"
    admit(ctx.db, "session-refresh-ip:" + peer, limit=60)
    admit(ctx.db, "session-refresh-global", limit=1000)

    token = (body.refresh_token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing refresh_token")

    session_repo = UserSessionRepo(ctx.db)
    try:
        new_session, new_refresh_token = session_repo.rotate_refresh_token(token)
    except ValueError as exc:
        err = str(exc)
        if err == "refresh_token_replayed":
            raise HTTPException(status_code=401, detail="Refresh token reuse detected; all associated sessions revoked")
        elif err == "refresh_token_expired":
            raise HTTPException(status_code=401, detail="Refresh token expired; please reset device session")
        elif err == "refresh_token_revoked":
            raise HTTPException(status_code=401, detail="Refresh token revoked")
        else:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

    return SessionResponse(
        session_token=new_session.session_token,
        refresh_token=new_refresh_token,
        user_id=new_session.user_id,
        expires_at=new_session.expires_at.isoformat(),
        token_type="Bearer",
    )


@router.delete("/session")
def revoke_session(
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    authorization: Annotated[str | None, Header()] = None,
    body: SessionRevokeRequest | None = None,
):
    """撤销当前会话凭据及关联的刷新凭据。"""
    session_repo = UserSessionRepo(ctx.db)
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        session_repo.revoke_session(token)
    if body and body.refresh_token:
        import hashlib
        h = hashlib.sha256(body.refresh_token.strip().encode()).hexdigest()
        row = ctx.db.query_one("SELECT family_id FROM user_refresh_token WHERE token_hash=?", (h,))
        if row and row["family_id"]:
            session_repo.revoke_refresh_family(row["family_id"])
    return {"ok": True, "message": "Session revoked successfully"}


@router.get("/me")
def get_me(
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """获取当前已认证用户的基本信息。"""
    return {"user_id": user_id, "authenticated": True}
