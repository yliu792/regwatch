"""
Scrape API — trigger a scrape + diff pipeline for a regulatory source.

After diffing, any new ChangeEvents are dispatched to registered webhooks.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.engine.scrape_pipeline import run_scrape_pipeline
from app.schemas import DiffReportOut, ScrapeRequest, ScrapeResponse

router = APIRouter()


# ── Endpoint ─────────────────────────────────────────────────────────────


@router.post("/scrape", response_model=ScrapeResponse)
async def trigger_scrape(
    payload: ScrapeRequest,
    session: AsyncSession = Depends(get_session),
) -> ScrapeResponse:
    """Run a scraper for the given source, pipe results through DiffEngine, and commit."""
    try:
        report = await run_scrape_pipeline(payload.source, session)
        await session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline failed for source '{payload.source}': {exc}",
        ) from exc

    return ScrapeResponse(
        ok=True,
        report=DiffReportOut(
            source=report.source,
            total_incoming=report.total_incoming,
            new_count=len(report.new_records),
            changed_count=len(report.changed_records),
            unchanged_count=len(report.unchanged_records),
            stale_count=len(report.stale_records),
            error_count=len(report.errors),
            has_changes=report.has_changes,
            change_ids=[e.id for e in report.changes],
        ),
    )
