from datetime import datetime, timezone
import os
from typing import Annotated
from fastapi import Depends, Header, HTTPException, Query, Request

from trace.app import AppContext


def get_app_context(request: Request) -> AppContext:
    """从 FastAPI app.state 获取全局单例 AppContext。"""
    if not hasattr(request.app.state, "ctx") or request.app.state.ctx is None:
        from trace.app import create_app
        request.app.state.ctx = create_app()
    return request.app.state.ctx


def get_current_user_id(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    user_id: Annotated[str | None, Query(alias="user_id")] = None,
) -> str:
    """提取并验证当前请求主体身份凭据 (F02/T02)。

    1. 优先校验 Authorization: Bearer <session_token>。
    2. 验证服务端会话存在、未过期且未被撤销。
    3. 对象级授权检查：若客户端声称了不同的 X-User-Id 或 query 参数，严禁越权操作（拒绝 A 凭据冒用 B 身份）。
    4. 缺失凭据或无效凭据严格返回 401 Unauthorized。
    """
    ctx: AppContext = request.app.state.ctx

    session_token = None
    if authorization and authorization.startswith("Bearer "):
        session_token = authorization[7:].strip()

    if session_token:
        from trace.db.repositories import UserSessionRepo
        session = UserSessionRepo(ctx.db).get_session(session_token)
        if not session or session.is_revoked:
            raise HTTPException(
                status_code=401,
                detail="Invalid or revoked session token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        now = datetime.now(timezone.utc)
        if session.expires_at and session.expires_at < now:
            raise HTTPException(
                status_code=401,
                detail="Session token has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # 对象授权校验：若请求同时声明了 X-User-Id 或 user_id 且与会话属主不一致，严格拒绝越权
        if any(uid and uid.strip() != session.user_id for uid in (x_user_id, user_id)):
            raise HTTPException(
                status_code=403,
                detail="Forbidden: claimed identity does not match authenticated subject",
            )
        return session.user_id

    # 未携带 Bearer 凭据时：受保护接口拒绝匿名访问
    raise HTTPException(
        status_code=401,
        detail="Authentication required: missing or invalid Authorization Bearer session token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_admin(user_id: Annotated[str, Depends(get_current_user_id)]) -> str:
    allowed = {s.strip() for s in os.environ.get("TRACE_ADMIN_USER_IDS", "").split(',') if s.strip()}
    if user_id not in allowed:
        raise HTTPException(403, "Administrator access required")
    return user_id
