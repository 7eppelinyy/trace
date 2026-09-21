"""API 请求与响应的 Pydantic 数据模式。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field


class PaginationMeta(BaseModel):
    consistency: str = "first_seen_membership_latest_content"
    page: int
    limit: int
    total: int
    has_more: bool
    snapshot_ts: str | None = None
    cursor: str | None = None
    next_cursor: str | None = None


class EventImpactItem(BaseModel):
    impact_id: str
    security_id: str
    direction: str
    directness: str
    magnitude: float
    persistence: float
    confidence: float
    reason: str
    industry_path: str
    final_score: float
    base_score: float
    market_confirmation: float
    analysis_mode: str
    market_data_mode: str


class EventEvidenceItem(BaseModel):
    raw_item_id: str
    source_id: str
    title: str
    url: str
    published_at: datetime | None = None
    role: str = "supporting"


class EventRevisionItem(BaseModel):
    revision_id: str
    version: int
    revision_type: str
    material_update: bool
    note: str
    created_at: datetime | None = None


class EventSecurityChip(BaseModel):
    ticker: str
    name: str = ""
    change_pct: float | None = None          # 日涨跌幅
    change_pct_15m: float | None = None      # 15分钟涨跌幅 (与日涨跌分离，F15)
    price: float | None = None
    market_timestamp: datetime | None = None
    fetched_at: datetime | None = None
    source: str = ""
    currency: str = "USD"
    is_delayed: bool = False
    change_basis: str = "prev_close"
    quality: str = "real"


class EventSummaryItem(BaseModel):
    event_id: str
    title: str
    summary: str
    event_type: str
    status: str
    version: int
    first_seen_at: datetime | None = None
    last_updated_at: datetime | None = None
    first_source_id: str | None = None
    primary_source_id: str | None = None
    max_score: float = 0.0
    impacts_count: int = 0
    transmission_depth: int = 1
    directness: str = "direct"
    impacted_securities: list[str] = Field(default_factory=list)
    securities: list[EventSecurityChip] = Field(default_factory=list)


class IndexQuoteItem(BaseModel):
    name: str
    code: str
    price: float | None = None
    change_pct: float | None = None
    status: str = "real"                # real / cached / delayed / stale / unavailable / mock / unknown_timestamp / invalid_timestamp
    as_of: datetime | None = None
    market_timestamp: datetime | None = None
    fetched_at: datetime | None = None
    source: str = ""
    currency: str = "USD"
    is_delayed: bool = False
    change_basis: str = "prev_close"
    quality: str = "real"


class EventListResponse(BaseModel):
    items: list[EventSummaryItem]
    pagination: PaginationMeta


class EventDetailResponse(BaseModel):
    event_id: str
    title: str
    summary: str
    event_type: str
    status: str
    version: int
    first_seen_at: datetime | None = None
    last_updated_at: datetime | None = None
    event_time: datetime | None = None
    language: str = ""
    first_source_id: str | None = None
    primary_source_id: str | None = None
    material_update: bool = False
    transmission_depth: int = 1
    key_numbers: list[str] = Field(default_factory=list)
    impacts: list[EventImpactItem] = Field(default_factory=list)
    evidences: list[EventEvidenceItem] = Field(default_factory=list)
    revisions: list[EventRevisionItem] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    next_checks: list[str] = Field(default_factory=list)
    securities: list[EventSecurityChip] = Field(default_factory=list)


class WatchlistAddRequest(BaseModel):
    ticker: str
    company_name_zh: str = ""
    user_alias: str = ""


class WatchlistAliasUpdateRequest(BaseModel):
    user_alias: str = ""


class WatchlistEntryItem(BaseModel):
    security_id: str
    ticker: str
    market: str
    company_name_zh: str
    company_name_en: str
    user_alias: str | None = None
    status: str = "verified"
    coverage_tier: str = "realtime_monitored"
    added_at: datetime | None = None
    last_price: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    change_pct_15m: float | None = None
    market_timestamp: datetime | None = None
    fetched_at: datetime | None = None
    source: str = ""
    currency: str = "USD"
    is_delayed: bool = False
    change_basis: str = "prev_close"
    quality: str = "real"


class WatchlistResponse(BaseModel):
    items: list[WatchlistEntryItem]
    total: int


class WatchlistSearchItem(BaseModel):
    security_id: str
    ticker: str
    market: str
    company_name_zh: str = ""
    company_name_en: str = ""
    status: str = "verified"
    coverage_tier: str = "realtime_monitored"
    last_price: float | None = None
    change_pct: float | None = None
    change_pct_15m: float | None = None
    is_watched: bool = False
    market_timestamp: datetime | None = None
    fetched_at: datetime | None = None
    source: str = ""
    currency: str = "USD"
    is_delayed: bool = False
    change_basis: str = "prev_close"
    quality: str = "real"


class WatchlistSearchResponse(BaseModel):
    items: list[WatchlistSearchItem]
    total: int


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=2000)


class AskRequest(BaseModel):
    ticker: str = ""
    event_id: str | None = None
    event_version: int | None = None
    mode: Literal["evidence_answer", "scenario"] = "evidence_answer"
    question: str = Field(..., min_length=1, max_length=1000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=10)


class AskResponse(BaseModel):
    ticker: str = ""
    event_id: str | None = None
    mode: str = "evidence_answer"
    question: str
    answer: str
    claims: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    evidence_events: list[str] = Field(default_factory=list)
    graph_chain: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "ok"
    duration_ms: int = 0
    model_version: str = "legacy_rule_based"
    prompt_version: str = "v1"


class DigestResponse(BaseModel):
    digest_id: str | None = None
    date_str: str
    content_markdown: str
    sent_at: datetime | None = None


class SourceHealthItem(BaseModel):
    source_id: str
    source_name: str
    status: str
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0


class HealthResponse(BaseModel):
    status: str                                           # HEALTHY / DEGRADED / UNHEALTHY
    db_status: str = "OK"                                 # OK / ERROR
    db_latency_ms: float = 0.0                            # Database roundtrip latency in ms
    sources_status: str = "OK"                            # OK / STALE / ERROR
    sources_stale_count: int = 0                          # Sources without success in > 1h
    llm_budget_used_pct: float | None = None              # Budget percentage consumed today
    outbox_pending_count: int = 0                         # Pending outbox items
    diagnostics: list[str] = Field(default_factory=list)  # Actionable troubleshooting hints
    sources: list[SourceHealthItem] = Field(default_factory=list)


class ReadyResponse(BaseModel):
    ready: bool
    status: str
    details: dict[str, Any] = Field(default_factory=dict)


class SystemStatusResponse(BaseModel):
    status: str
    mode: str
    db_path: str
    uptime: str | None = None
    accuracy: dict[str, Any] = Field(default_factory=dict)
    recent_runs: list[dict[str, Any]] = Field(default_factory=list)
    business_health: dict[str, Any] = Field(default_factory=dict)


class PreferenceItem(BaseModel):
    preference_id: str
    user_id: str
    security_id: str | None = None
    threshold: float = 7.0
    enabled: bool = True
    quiet_start: str | None = None
    quiet_end: str | None = None
    channel: str = "all"
    revision: int = 1
    updated_at: datetime | None = None


class PreferencesResponse(BaseModel):
    global_preference: PreferenceItem
    overrides: list[PreferenceItem] = Field(default_factory=list)


class UpdatePreferenceRequest(BaseModel):
    security_id: str | None = None
    threshold: float = Field(default=7.0,ge=1,le=10)
    enabled: bool = True
    quiet_start: str | None = Field(default=None,pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    quiet_end: str | None = Field(default=None,pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    channel: Literal['all','telegram','wechat'] = "all"
    expected_revision: int | None = None


# ---------------------------------------------------------------------------
# 假设跟踪与用户反馈模式 (T16)
# ---------------------------------------------------------------------------

class ResearchQuestionDraftRequest(BaseModel):
    event_id: str
    security_id: str | None = None


class ResearchQuestionDraftResponse(BaseModel):
    event_id: str
    security_id: str | None = None
    title: str
    hypothesis: str
    supporting_conditions: list[str] = Field(default_factory=list)
    contradicting_conditions: list[str] = Field(default_factory=list)
    next_check_at: datetime | None = None


class CreateResearchQuestionRequest(BaseModel):
    event_id: str | None = None
    security_id: str | None = None
    title: str = Field(min_length=1,max_length=200)
    hypothesis: str = Field(min_length=1,max_length=3000)
    supporting_conditions: list[str] = Field(default_factory=list)
    contradicting_conditions: list[str] = Field(default_factory=list)
    next_check_at: datetime | None = None
    user_notes: str = Field(default='',max_length=10000)


class UpdateResearchQuestionRequest(BaseModel):
    title: str | None = Field(default=None,min_length=1,max_length=200)
    hypothesis: str | None = Field(default=None,min_length=1,max_length=3000)
    supporting_conditions: list[str] | None = None
    contradicting_conditions: list[str] | None = None
    next_check_at: datetime | None = None
    state: str | None = None
    user_notes: str | None = Field(default=None,max_length=10000)
    expected_revision: int | None = Field(default=None,ge=1)


class ResearchQuestionItem(BaseModel):
    question_id: str
    user_id: str
    event_id: str | None = None
    security_id: str | None = None
    title: str
    hypothesis: str
    supporting_conditions: list[str] = Field(default_factory=list)
    contradicting_conditions: list[str] = Field(default_factory=list)
    next_check_at: datetime | None = None
    state: str = "tracking"
    user_notes: str = ""
    matched_evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    revision: int = 1


class ResearchQuestionShareResponse(BaseModel):
    """公开分享视图：严格剥离个人私有备注 (F31/T16 验收标准)。"""
    question_id: str
    event_id: str | None = None
    security_id: str | None = None
    title: str
    hypothesis: str
    supporting_conditions: list[str] = Field(default_factory=list)
    contradicting_conditions: list[str] = Field(default_factory=list)
    next_check_at: datetime | None = None
    state: str = "tracking"
    matched_evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ResearchQuestionListResponse(BaseModel):
    items: list[ResearchQuestionItem]
    total: int
    has_more: bool = False
    offset: int = 0
    limit: int = 100


class AlertFeedbackCreateRequest(BaseModel):
    event_id: str
    rating: str
    security_id: str | None = None
    reason: str = ""


class AlertFeedbackItem(BaseModel):
    feedback_id: str
    user_id: str
    event_id: str
    rating: str
    security_id: str | None = None
    reason: str = ""
    created_at: datetime | None = None


class AlertFeedbackSummaryResponse(BaseModel):
    total: int
    useful_count: int
    not_useful_count: int
    useful_rate: float
    by_rating: dict[str, int] = Field(default_factory=dict)


class ResearchExportResponse(BaseModel):
    user_id: str
    questions: list[ResearchQuestionItem] = Field(default_factory=list)
    feedbacks: list[AlertFeedbackItem] = Field(default_factory=list)
    exported_at: datetime


class ResolveAmbiguousRequest(BaseModel):
    action: Literal["confirm_delivered", "requeue", "discard"]
    note: str = ""


class AmbiguousOutboxItem(BaseModel):
    outbox_id: str
    user_id: str
    channel_type: str
    channel_target: str
    event_id: str
    event_version: int
    alert_type: str
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

