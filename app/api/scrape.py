"""
Scrape API — trigger a scrape + diff pipeline for a regulatory source.

After diffing, any new ChangeEvents are dispatched to registered webhooks.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.engine.diff_engine import DiffEngine
from app.engine.webhook import WebhookDispatcher
from app.schemas import DiffReportOut, ScrapeRequest, ScrapeResponse

router = APIRouter()

# ── Scraper registry ────────────────────────────────────────────────────

_scraper_registry: dict[str, type] = {}


def register_scraper(source: str, scraper_cls: type) -> None:
    _scraper_registry[source] = scraper_cls


def _get_scraper(source: str):
    cls = _scraper_registry.get(source)
    if cls is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source '{source}'. Available: {sorted(_scraper_registry)}",
        )
    return cls()


# Import scrapers to populate the registry.
from app.scrapers.china_mee import ChinaMEEScraper  # noqa: E402
from app.scrapers.echa_reach import ECHAReachScraper  # noqa: E402
from app.scrapers.epa_tsca import EPATSCAScraper  # noqa: E402

register_scraper("epa_tsca", EPATSCAScraper)
register_scraper("echa_reach", ECHAReachScraper)
register_scraper("china_mee", ChinaMEEScraper)


# ── Endpoint ─────────────────────────────────────────────────────────────


@router.post("/scrape", response_model=ScrapeResponse)
async def trigger_scrape(
    payload: ScrapeRequest,
    session: AsyncSession = Depends(get_session),
) -> ScrapeResponse:
    """Run a scraper for the given source, pipe results through DiffEngine, and commit."""
    try:
        scraper = _get_scraper(payload.source)
        results = await scraper.scrape()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Scraper failed for source '{payload.source}': {exc}",
        ) from exc

    if not results:
        return ScrapeResponse(
            ok=True, report=DiffReportOut(source=payload.source, total_incoming=0)
        )

    try:
        engine = DiffEngine(session)
        report = await engine.process(results)

        # ── Dispatch webhooks for new ChangeEvents ───────────────────────
        if report.has_changes:
            dispatcher = WebhookDispatcher(session)
            await dispatcher.dispatch_all(report.changes, payload.source)

        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"DiffEngine failed for source '{payload.source}': {exc}",
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
