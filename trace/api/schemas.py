"""API 请求与响应的 Pydantic 数据模式。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


class PaginationMeta(BaseModel):
    page: int
    limit: int
    total: int
    has_more: bool


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
    impacted_securities: list[str] = Field(default_factory=list)


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
    key_numbers: list[str] = Field(default_factory=list)
    impacts: list[EventImpactItem] = Field(default_factory=list)
    evidences: list[EventEvidenceItem] = Field(default_factory=list)
    revisions: list[EventRevisionItem] = Field(default_factory=list)


class WatchlistAddRequest(BaseModel):
    ticker: str


class WatchlistEntryItem(BaseModel):
    security_id: str
    ticker: str
    market: str
    company_name_zh: str
    company_name_en: str
    added_at: datetime | None = None
    last_price: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None


class WatchlistResponse(BaseModel):
    items: list[WatchlistEntryItem]
    total: int


class AskRequest(BaseModel):
    ticker: str
    question: str


class AskResponse(BaseModel):
    ticker: str
    question: str
    answer: str
    evidence_events: list[str] = Field(default_factory=list)


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
    status: str
    sources: list[SourceHealthItem]


class SystemStatusResponse(BaseModel):
    status: str
    mode: str
    db_path: str
    uptime: str | None = None
    accuracy: dict[str, Any] = Field(default_factory=dict)
    recent_runs: list[dict[str, Any]] = Field(default_factory=list)
