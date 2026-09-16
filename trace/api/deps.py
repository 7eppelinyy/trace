"""FastAPI 依赖注入与通用上下文获取。"""

from __future__ import annotations

from typing import Annotated
from fastapi import Header, Query, Request

from trace.app import AppContext
from trace.db.repositories import UserRepo


def get_app_context(request: Request) -> AppContext:
    """从 FastAPI app.state 获取全局单例 AppContext。"""
    return request.app.state.ctx


def get_current_user_id(
    request: Request,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    user_id: Annotated[str | None, Query(alias="user_id")] = None,
) -> str:
    """提取当前请求用户标识（兼容 X-User-Id 请求头或 user_id 参数，缺省回退默认用户）。"""
    uid = x_user_id or user_id
    ctx: AppContext = request.app.state.ctx
    if not uid:
        uid = ctx.config.telegram.default_chat_id or "default_user"

    # 幂等确保用户在数据库中存在
    UserRepo(ctx.db).ensure(uid, ctx.config.telegram.default_user_timezone)
    return uid
