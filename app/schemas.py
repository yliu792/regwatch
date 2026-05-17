"""
Pydantic schemas (request / response shapes).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

# ── RegulatoryRecord ──────────────────────────────────────────────────


class RegulatoryRecordOut(BaseModel):
    """Full record including raw_content payload."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    record_id: str
    title: str
    content_hash: str
    raw_content: str
    status: str
    first_seen: datetime
    last_seen: datetime


class RegulatoryRecordBrief(BaseModel):
    """Lightweight record (no raw_content)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    record_id: str
    title: str
    content_hash: str
    status: str
    first_seen: datetime
    last_seen: datetime


# ── ChangeEvent ───────────────────────────────────────────────────────


class ChangeEventOut(BaseModel):
    """A detected change for a regulatory record."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    record_id: uuid.UUID
    previous_hash: str
    new_hash: str
    diff_summary: str
    detected_at: datetime
    dispatched: bool


class ChangeEventBrief(BaseModel):
    """Lightweight change event (no diff_summary)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    record_id: uuid.UUID
    previous_hash: str
    new_hash: str
    detected_at: datetime
    dispatched: bool


class DiffResult(BaseModel):
    """Result from the diff engine (before persistence)."""

    record_id: uuid.UUID
    previous_hash: str
    new_hash: str
    diff_summary: str


# ── DiffReport ───────────────────────────────────────────────────────


class DiffReportOut(BaseModel):
    """Serializable summary returned by the diff engine."""

    source: str
    total_incoming: int
    new_count: int = 0
    changed_count: int = 0
    unchanged_count: int = 0
    stale_count: int = 0
    error_count: int = 0
    has_changes: bool = False
    change_ids: list[uuid.UUID] = Field(default_factory=list)


# ── Scrape ────────────────────────────────────────────────────────────


class ScrapeRequest(BaseModel):
    """Payload to trigger a scrape + diff run for a regulatory source."""

    source: str = Field(..., description="Source slug, e.g. 'epa_tsca', 'echa_reach'")


class ScrapeResponse(BaseModel):
    """Result of a scrape + diff run."""

    ok: bool
    report: DiffReportOut | None = None
    error: str | None = None


# ── WebhookRegistration ───────────────────────────────────────────────


class WebhookRegistrationCreate(BaseModel):
    """Payload to register a new webhook subscriber."""

    callback_url: HttpUrl
    agencies: list[str] = Field(default_factory=list)
    secret: str | None = None


class WebhookRegistrationOut(BaseModel):
    """Exposed webhook subscription (secret is never returned)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    callback_url: str
    agencies: list[str]
    is_active: bool
    created_at: datetime
    last_delivered_at: datetime | None = None
