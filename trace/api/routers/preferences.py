"""提醒偏好与免打扰设置 API 路由 (T10 / F10 / F27)。"""

from __future__ import annotations

from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status

from trace.api.deps import get_app_context, get_current_user_id
from trace.api.schemas import (
    PreferenceItem,
    PreferencesResponse,
    UpdatePreferenceRequest,
)
from trace.app import AppContext
from trace.db.repositories import NotificationPreferenceRepo
from trace.domain.models import NotificationPreference

router = APIRouter(prefix="/preferences", tags=["preferences"])


def _to_schema(pref: NotificationPreference) -> PreferenceItem:
    return PreferenceItem(
        preference_id=pref.preference_id,
        user_id=pref.user_id,
        security_id=pref.security_id,
        threshold=pref.threshold,
        enabled=pref.enabled,
        quiet_start=pref.quiet_start,
        quiet_end=pref.quiet_end,
        channel=pref.channel,
        revision=pref.revision,
        updated_at=pref.updated_at,
    )


@router.get("", response_model=PreferencesResponse)
def get_preferences(
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """获取当前用户的全局提醒偏好及单标的覆盖配置。"""
    repo = NotificationPreferenceRepo(ctx.db)
    global_pref = repo.get_effective_preference(user_id, None)
    all_prefs = repo.list_by_user(user_id)
    overrides = [p for p in all_prefs if p.security_id is not None]

    return PreferencesResponse(
        global_preference=_to_schema(global_pref),
        overrides=[_to_schema(p) for p in overrides],
    )


@router.put("", response_model=PreferenceItem)
def update_preference(
    req: UpdatePreferenceRequest,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """设置全局或单标的提醒偏好，支持并发版本冲突检查 (expected_revision)。"""
    repo = NotificationPreferenceRepo(ctx.db)
    try:
        updated = repo.set_preference(
            user_id=user_id,
            security_id=req.security_id,
            threshold=req.threshold,
            enabled=req.enabled,
            quiet_start=req.quiet_start,
            quiet_end=req.quiet_end,
            channel=req.channel,
            expected_revision=req.expected_revision,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    return _to_schema(updated)
