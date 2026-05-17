"""
SQLAlchemy ORM models.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import functions


class Base(DeclarativeBase):
    pass


# ── RegulatoryRecord ───────────────────────────────────────────────────


class RegulatoryRecord(Base):
    """
    A regulatory document or page entry scraped from an agency source.

    Each record is uniquely identified by the combination of *source* and
    *record_id* (a source-specific identifier such as a CASRN, document
    number, or internal agency ID).
    """

    __tablename__ = "regulatory_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source = Column(String, index=True, nullable=False)  # "epa_tsca", "echa_reach", "china_mee"
    record_id = Column(
        String, index=True, nullable=False
    )  # source-specific identifier (e.g. CASRN)
    title = Column(String, nullable=False, default="")
    raw_content = Column(Text, nullable=False, default="")
    content_hash = Column(String(64), nullable=False)  # SHA-256 of normalized content
    status = Column(String, nullable=False, default="active")  # "active" or "stale"
    first_seen = Column(DateTime(timezone=True), default=datetime.now(timezone.utc), nullable=False)
    last_seen = Column(
        DateTime(timezone=True),
        default=datetime.now(timezone.utc),
        onupdate=datetime.now(timezone.utc),
        nullable=False,
    )

    changes = relationship("ChangeEvent", back_populates="record", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("source", "record_id", name="uq_regulatory_record_source_record_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<RegulatoryRecord(id={self.id!r}, source={self.source!r},"
            f" record_id={self.record_id!r}, status={self.status!r})>"
        )


# ── ChangeEvent ────────────────────────────────────────────────────────


class ChangeEvent(Base):
    """
    A detected change for a single regulatory record.

    Created by the diff engine whenever a new scrape yields a different
    ``content_hash`` than the previously stored value.
    """

    __tablename__ = "change_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    record_id = Column(
        UUID(as_uuid=True), ForeignKey("regulatory_records.id"), nullable=False, index=True
    )
    previous_hash = Column(String(64), nullable=False)
    new_hash = Column(String(64), nullable=False)
    diff_summary = Column(Text, nullable=False, default="")
    detected_at = Column(
        DateTime(timezone=True), default=datetime.now(timezone.utc), nullable=False
    )
    dispatched = Column(Boolean, nullable=False, default=False)

    record = relationship("RegulatoryRecord", back_populates="changes")

    def __repr__(self) -> str:
        return (
            f"<ChangeEvent(id={self.id!r}, record_id={self.record_id!r},"
            f" dispatched={self.dispatched})>"
        )


# ── WebhookRegistration ────────────────────────────────────────────────


class WebhookRegistration(Base):
    """
    A subscriber that receives webhook callbacks when changes are detected.
    """

    __tablename__ = "webhook_registrations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    callback_url = Column(String(2048), nullable=False)
    agencies = Column(ARRAY(String), nullable=False, default=[])
    secret = Column(String(256), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc), nullable=False)
    last_delivered_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<WebhookRegistration(id={self.id!r},"
            f" url={self.callback_url!r}, active={self.is_active})>"
        )
