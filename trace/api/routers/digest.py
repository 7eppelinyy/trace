"""每日事件与风险摘要 API 路由。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from fastapi import APIRouter, Depends
import pytz

from trace.api.deps import get_app_context, get_current_user_id
from trace.api.schemas import DigestResponse
from trace.app import AppContext
from trace.db.repositories import DailyDigestRepo, UserRepo

router = APIRouter(prefix="/digest", tags=["digest"])


@router.get("/today", response_model=DigestResponse)
def get_today_digest(
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """获取今日美股 + A 股重大事件与风险每日综合摘要。"""
    user = UserRepo(ctx.db).get(user_id)
    tz_name = user.timezone if user else ctx.config.telegram.default_user_timezone
    tz = pytz.timezone(tz_name)
    local_now = datetime.now(timezone.utc).astimezone(tz)
    date_str = local_now.strftime("%Y-%m-%d")

    digest_repo = DailyDigestRepo(ctx.db)
    saved = digest_repo.get(date_str)
    if saved:
        return DigestResponse(
            digest_id=saved.digest_id,
            date_str=saved.date_str,
            content_markdown=saved.content_markdown,
            sent_at=saved.sent_at,
        )

    # 动态生成今日摘要
    digest = ctx.digest_builder.build(date_str=date_str, timezone_name=tz_name)
    return DigestResponse(
        digest_id=digest.digest_id,
        date_str=digest.date_str,
        content_markdown=digest.content_markdown,
        sent_at=digest.sent_at,
    )

