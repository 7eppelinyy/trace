"""假设跟踪与用户反馈 API 路由 (T16 / F31 / F32)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from trace.api.deps import get_app_context, get_current_user_id, require_admin
from trace.db.shares import ResearchShareRepo
from trace.db.repositories import RawItemRepo
from pydantic import BaseModel, Field
from trace.api.schemas import (
    AlertFeedbackCreateRequest,
    AlertFeedbackItem,
    AlertFeedbackSummaryResponse,
    CreateResearchQuestionRequest,
    ResearchExportResponse,
    ResearchQuestionDraftRequest,
    ResearchQuestionDraftResponse,
    ResearchQuestionItem,
    ResearchQuestionListResponse,
    ResearchQuestionShareResponse,
    UpdateResearchQuestionRequest,
)
from trace.app import AppContext
from trace.common.ids import alert_feedback_id, research_question_id
from trace.db.repositories import (
    AlertFeedbackRepo,
    EventImpactRepo,
    EventRepo,
    ResearchQuestionRepo,
)
from trace.domain.models import AlertFeedback, ResearchQuestion, ResearchQuestionState

router = APIRouter(prefix="/research", tags=["research"])


def _to_question_item(q: ResearchQuestion) -> ResearchQuestionItem:
    return ResearchQuestionItem(
        question_id=q.question_id,
        user_id=q.user_id,
        event_id=q.event_id,
        security_id=q.security_id,
        title=q.title,
        hypothesis=q.hypothesis,
        supporting_conditions=q.supporting_conditions,
        contradicting_conditions=q.contradicting_conditions,
        next_check_at=q.next_check_at,
        state=q.state,
        user_notes=q.user_notes,
        matched_evidence_ids=q.matched_evidence_ids,
        created_at=q.created_at,
        updated_at=q.updated_at,
        revision=q.revision,
    )


def _to_feedback_item(fb: AlertFeedback) -> AlertFeedbackItem:
    return AlertFeedbackItem(
        feedback_id=fb.feedback_id,
        user_id=fb.user_id,
        event_id=fb.event_id,
        rating=fb.rating,
        security_id=fb.security_id,
        reason=fb.reason,
        created_at=fb.created_at,
    )


@router.post("/questions/draft", response_model=ResearchQuestionDraftResponse)
def draft_research_question(
    req: ResearchQuestionDraftRequest,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """基于事件与传导影响自动生成研究假设草案 (F31)。

    提取事实要点、推导支持/反证条件与预估下一验证窗口。
    """
    from trace.common.source_policy import event_permitted
    event = EventRepo(ctx.db).get(req.event_id)
    if not event or not event_permitted(ctx.db, req.event_id, "display"):
        raise HTTPException(status_code=404, detail="Event not found")

    title = f"验证: {event.title[:50]}"
    hypothesis = event.summary or event.title

    supporting_conditions: list[str] = []
    contradicting_conditions: list[str] = [
        "【待用户确认的建议】请填写：什么可观察证据会推翻你的假设？（如官方澄清反转、订单取消或业绩不及预期）"
    ]

    # 若指定或关联了标的，从 EventImpact 提取推断依据
    impacts = EventImpactRepo(ctx.db).list_by_event(event.event_id)
    target_impact = None
    if req.security_id:
        target_impact = next((imp for imp in impacts if imp.security_id == req.security_id), None)
    elif impacts:
        target_impact = impacts[0]

    if target_impact:
        supporting_conditions.append(
            f"【待用户确认的建议】标的 {target_impact.security_id} 传导方向 ({target_impact.direction}) 是否得到后续财报或业务数据印证"
        )
        if target_impact.reason:
            supporting_conditions.append(f"【待用户确认的建议】传导逻辑是否生效: {target_impact.reason[:80]}")
    else:
        supporting_conditions.append("【待用户确认的建议】请填写：什么可观察证据会支持你的假设？")

    # 默认 7 天后作为首个核验观察窗口（标注为待用户确认的建议日期）
    next_check = datetime.now(timezone.utc) + timedelta(days=7)

    return ResearchQuestionDraftResponse(
        event_id=event.event_id,
        security_id=req.security_id or (target_impact.security_id if target_impact else None),
        title=title,
        hypothesis=hypothesis,
        supporting_conditions=supporting_conditions,
        contradicting_conditions=contradicting_conditions,
        next_check_at=next_check,
    )


@router.post("/questions", response_model=ResearchQuestionItem)
def create_research_question(
    req: CreateResearchQuestionRequest,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """保存并开启一条新的假设跟踪 (F31)。"""
    if not req.title.strip() or not req.hypothesis.strip():
        raise HTTPException(status_code=400, detail="Title and hypothesis cannot be empty")

    if req.event_id:
        from trace.common.source_policy import event_permitted
        ev = EventRepo(ctx.db).get(req.event_id)
        if not ev:
            raise HTTPException(status_code=404, detail=f"Event {req.event_id} not found")
        if not event_permitted(ctx.db, req.event_id, "display"):
            raise HTTPException(status_code=403, detail="Referenced event cannot be displayed under current source policy")

    now = datetime.now(timezone.utc)
    qid = research_question_id()

    q = ResearchQuestion(
        question_id=qid,
        user_id=user_id,
        title=req.title.strip(),
        hypothesis=req.hypothesis.strip(),
        event_id=req.event_id,
        security_id=req.security_id,
        supporting_conditions=req.supporting_conditions,
        contradicting_conditions=req.contradicting_conditions,
        next_check_at=req.next_check_at,
        state=ResearchQuestionState.TRACKING.value,
        user_notes=req.user_notes or "",
        matched_evidence_ids=[],
        created_at=now,
        updated_at=now,
    )

    ResearchQuestionRepo(ctx.db).insert(q)
    return _to_question_item(q)


@router.get("/questions", response_model=ResearchQuestionListResponse)
def list_research_questions(
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    state: Annotated[str | None, Query(description="状态过滤: tracking / confirmed / falsified / archived")] = None,
    security_id: Annotated[str | None, Query(description="标的过滤")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """获取当前用户的假设跟踪列表 (严格租户隔离，支持分页与真实 total/has_more)。"""
    repo = ResearchQuestionRepo(ctx.db)
    total = repo.count_by_user(user_id=user_id, state=state, security_id=security_id)
    questions = repo.list_by_user(user_id=user_id, state=state, security_id=security_id, limit=limit, offset=offset)
    has_more = (offset + len(questions)) < total
    return ResearchQuestionListResponse(
        items=[_to_question_item(q) for q in questions],
        total=total,
        has_more=has_more,
        offset=offset,
        limit=limit,
    )


@router.get("/questions/{question_id}", response_model=ResearchQuestionItem)
def get_research_question(
    question_id: str,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """获取单条假设跟踪详情 (严格验证属主授权)。"""
    repo = ResearchQuestionRepo(ctx.db)
    q = repo.get(question_id)
    if not q:
        raise HTTPException(status_code=404, detail="Research question not found")
    if q.user_id != user_id:
        raise HTTPException(status_code=403, detail="Forbidden: access to another user's research question is denied")
    return _to_question_item(q)


@router.patch("/questions/{question_id}", response_model=ResearchQuestionItem)
def update_research_question(
    question_id: str,
    req: UpdateResearchQuestionRequest,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """更新假设内容、私有备注或推进状态 (tracking -> confirmed / falsified / archived)。"""
    repo = ResearchQuestionRepo(ctx.db)
    q = repo.get(question_id)
    if not q:
        raise HTTPException(status_code=404, detail="Research question not found")
    if q.user_id != user_id:
        raise HTTPException(status_code=403, detail="Forbidden: access to another user's research question is denied")

    if req.expected_revision is not None and req.expected_revision != q.revision:
        raise HTTPException(409, 'Research has changed; reload before editing')
    if req.state is not None:
        valid_states = {s.value for s in ResearchQuestionState}
        if req.state not in valid_states:
            raise HTTPException(status_code=400, detail=f"Invalid state: {req.state}. Must be one of {valid_states}")
        q.state = req.state

    if req.title is not None:
        q.title = req.title.strip()
    if req.hypothesis is not None:
        q.hypothesis = req.hypothesis.strip()
    if req.supporting_conditions is not None:
        q.supporting_conditions = req.supporting_conditions
    if req.contradicting_conditions is not None:
        q.contradicting_conditions = req.contradicting_conditions
    if req.next_check_at is not None:
        q.next_check_at = req.next_check_at
    if req.user_notes is not None:
        q.user_notes = req.user_notes

    q.updated_at = datetime.now(timezone.utc)
    try:
        repo.update(q)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return _to_question_item(q)


@router.delete("/questions/{question_id}")
def delete_research_question(
    question_id: str,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """删除假设跟踪。"""
    repo = ResearchQuestionRepo(ctx.db)
    q = repo.get(question_id)
    if not q:
        raise HTTPException(status_code=404, detail="Research question not found")
    if q.user_id != user_id:
        raise HTTPException(status_code=403, detail="Forbidden: access to another user's research question is denied")

    ok = repo.delete(question_id, user_id)
    return {"ok": ok, "question_id": question_id}


@router.post("/questions/{question_id}/evidence", response_model=ResearchQuestionItem)
def link_evidence_to_question(
    question_id: str,
    raw_item_id: Annotated[str, Body(embed=True)],
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """向假设手动或半自动关联新证据 (F31: '可能相关，需要复核')。"""
    repo = ResearchQuestionRepo(ctx.db)
    q = repo.get(question_id)
    if not q:
        raise HTTPException(status_code=404, detail="Research question not found")
    if q.user_id != user_id:
        raise HTTPException(status_code=403, detail="Forbidden: access to another user's research question is denied")

    if raw_item_id and raw_item_id not in q.matched_evidence_ids:
        raw = RawItemRepo(ctx.db).get(raw_item_id)
        if raw is None:
            raise HTTPException(404, "Evidence not found")
        from trace.common.source_policy import permitted
        if not permitted(ctx.db, raw.source_id, "display"):
            raise HTTPException(403, "Evidence source is not permitted for display")
        q.matched_evidence_ids.append(raw_item_id)
        q.updated_at = datetime.now(timezone.utc)
        repo.update(q)

    return _to_question_item(q)


@router.get("/questions/{question_id}/share", response_model=ResearchQuestionShareResponse)
def get_public_share_view(
    question_id: str,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    token: str = "",
):
    """Only an owner-issued unexpired capability can access a frozen public snapshot."""
    snapshot = ResearchShareRepo(ctx.db).read(question_id, token) if token else None
    if snapshot is None:
        raise HTTPException(404, "Share not found or expired")
    if snapshot.get("event_id"):
        from trace.common.source_policy import event_permitted
        if not event_permitted(ctx.db, snapshot["event_id"], "display"):
            raise HTTPException(404, "Share not found or expired")
    return ResearchQuestionShareResponse(**snapshot)


class ShareRequest(BaseModel):
    expires_in_days: int = Field(default=7, ge=1, le=30)


@router.post("/questions/{question_id}/share")
def create_public_share(question_id: str, req: ShareRequest,
                        ctx: Annotated[AppContext, Depends(get_app_context)],
                        user_id: Annotated[str, Depends(get_current_user_id)]):
    repo = ResearchQuestionRepo(ctx.db)
    q = repo.get(question_id)
    if not q or q.user_id != user_id:
        raise HTTPException(status_code=404, detail="Research question not found")
    if q.event_id:
        from trace.common.source_policy import event_permitted
        if not event_permitted(ctx.db, q.event_id, "display"):
            raise HTTPException(status_code=403, detail="Referenced event cannot be displayed under current source policy")

    snapshot = ResearchQuestionShareResponse(
        question_id=q.question_id,
        event_id=q.event_id,
        security_id=q.security_id,
        title=q.title,
        hypothesis=q.hypothesis,
        supporting_conditions=q.supporting_conditions,
        contradicting_conditions=q.contradicting_conditions,
        next_check_at=q.next_check_at,
        state=q.state,
        matched_evidence_ids=q.matched_evidence_ids,
        created_at=q.created_at,
        updated_at=q.updated_at,
    ).model_dump(mode="json")
    return ResearchShareRepo(ctx.db).create(question_id, user_id, snapshot, req.expires_in_days)


@router.delete("/questions/{question_id}/share")
def revoke_public_share(question_id: str, ctx: Annotated[AppContext, Depends(get_app_context)],
                        user_id: Annotated[str, Depends(get_current_user_id)]):
    q = ResearchQuestionRepo(ctx.db).get(question_id)
    if not q or q.user_id != user_id:
        raise HTTPException(404, "Research question not found")
    ResearchShareRepo(ctx.db).revoke(question_id, user_id)
    return {"ok": True}


@router.post("/feedback", response_model=AlertFeedbackItem)
def record_alert_feedback(
    req: AlertFeedbackCreateRequest,
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """记录用户对提醒的有效性与原因反馈 (F32: '这条提醒没用' / too_late / irrelevant 等)。"""
    valid_ratings = {"useful", "not_useful", "irrelevant", "too_late", "incorrect_analysis", "duplicate"}
    if req.rating not in valid_ratings:
        raise HTTPException(status_code=400, detail=f"Invalid rating: {req.rating}. Must be one of {valid_ratings}")

    event = EventRepo(ctx.db).get(req.event_id)
    if not event:
        raise HTTPException(status_code=404, detail=f"Event {req.event_id} not found")

    # One updatable opinion per user/event version/security, not unlimited votes.
    import hashlib
    fid = 'AFB-' + hashlib.sha256(f'{user_id}:{event.event_id}:{event.version}:{req.security_id or ""}'.encode()).hexdigest()[:32]
    now = datetime.now(timezone.utc)
    fb = AlertFeedback(
        feedback_id=fid,
        user_id=user_id,
        event_id=req.event_id,
        security_id=req.security_id,
        rating=req.rating,
        reason=req.reason,
        created_at=now,
    )

    with ctx.db.transaction():
        ctx.db.execute('DELETE FROM alert_feedback WHERE feedback_id=? AND user_id=?', (fid,user_id))
        AlertFeedbackRepo(ctx.db).insert(fb)
        ctx.db.execute('UPDATE alert_feedback SET event_version=? WHERE feedback_id=?', (event.version,fid))
    return _to_feedback_item(fb)


@router.get("/feedback/summary", response_model=AlertFeedbackSummaryResponse)
def get_feedback_summary(
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    scope: Annotated[str, Query(description="范围: 'my' 或 'all'")] = "my",
):
    """获取提醒反馈统计摘要 (支持当前用户与系统整体)。"""
    repo = AlertFeedbackRepo(ctx.db)
    if scope not in ("my", "all"):
        raise HTTPException(422, "scope must be my or all")
    if scope == "all":
        require_admin(user_id)
    target_uid = user_id if scope == "my" else None
    stats = repo.summary(user_id=target_uid)
    return AlertFeedbackSummaryResponse(
        total=stats["total"],
        useful_count=stats["useful_count"],
        not_useful_count=stats["not_useful_count"],
        useful_rate=stats["useful_rate"],
        by_rating=stats["by_rating"],
    )


@router.get("/export", response_model=ResearchExportResponse)
def export_user_research(
    ctx: Annotated[AppContext, Depends(get_app_context)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    """导出用户全部研究内容与反馈记录 (满足 G3/T16: 用户可随时导出研究资产)。"""
    q_repo = ResearchQuestionRepo(ctx.db)
    f_repo = AlertFeedbackRepo(ctx.db)

    # A consistent snapshot and no implicit truncation of the user's export.
    with ctx.db.transaction():
        questions = q_repo.list_by_user(user_id=user_id, limit=None)
        feedbacks = f_repo.list_by_user(user_id=user_id, limit=None)

    return ResearchExportResponse(
        user_id=user_id,
        questions=[_to_question_item(q) for q in questions],
        feedbacks=[_to_feedback_item(fb) for fb in feedbacks],
        exported_at=datetime.now(timezone.utc),
    )
