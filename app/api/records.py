"""
Records API — list, retrieve, and view change history for regulatory records.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import ChangeEvent, RegulatoryRecord
from app.schemas import ChangeEventOut, RegulatoryRecordBrief, RegulatoryRecordOut

router = APIRouter()


@router.get("/", response_model=list[RegulatoryRecordBrief])
async def list_records(
    source: str | None = Query(None, description="Filter by source slug"),
    status: str | None = Query(None, description="Filter by status (active/stale)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[RegulatoryRecord]:
    """List records, optionally filtered by source and/or status."""
    stmt = (
        select(RegulatoryRecord)
        .order_by(RegulatoryRecord.last_seen.desc())
        .offset(offset)
        .limit(limit)
    )
    if source:
        stmt = stmt.where(RegulatoryRecord.source == source)
    if status:
        stmt = stmt.where(RegulatoryRecord.status == status)
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get("/{record_id}", response_model=RegulatoryRecordOut)
async def get_record(
    record_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> RegulatoryRecord:
    """Retrieve a single record by ID (includes full raw_content)."""
    stmt = select(RegulatoryRecord).where(RegulatoryRecord.id == record_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return record


@router.get("/{record_id}/history", response_model=list[ChangeEventOut])
async def get_record_history(
    record_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[ChangeEvent]:
    """Retrieve the change history for a regulatory record (newest first)."""
    stmt = (
        select(ChangeEvent)
        .where(ChangeEvent.record_id == record_id)
        .order_by(ChangeEvent.detected_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
