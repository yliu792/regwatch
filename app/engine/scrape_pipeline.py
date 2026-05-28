"""
Scrape pipeline — shared between the HTTP endpoint and the scheduler.

Orchestrates: lookup scraper → scrape → diff → webhook dispatch.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.diff_engine import DiffEngine, DiffReport
from app.engine.webhook import WebhookDispatcher

logger = logging.getLogger(__name__)

# ── Scraper registry ────────────────────────────────────────────────────

_scraper_registry: dict[str, type] = {}


def register_scraper(source: str, scraper_cls: type) -> None:
    """Register a scraper class for a source slug."""
    _scraper_registry[source] = scraper_cls


def _get_scraper(source: str):
    """Look up and instantiate a scraper by source slug."""
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


# ── Pipeline ────────────────────────────────────────────────────────────


async def run_scrape_pipeline(source: str, session: AsyncSession) -> DiffReport:
    """
    Run the full scrape → diff → webhook pipeline for *source*.

    The caller is responsible for committing or rolling back the session.

    Returns the :class:`DiffReport` so callers can build API responses
    or log summaries.
    """
    scraper = _get_scraper(source)

    try:
        results = await scraper.scrape()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Scraper failed for source '{source}': {exc}",
        ) from exc

    if not results:
        return DiffReport(source=source, total_incoming=0)

    try:
        engine = DiffEngine(session)
        report = await engine.process(results)

        if report.has_changes:
            dispatcher = WebhookDispatcher(session)
            await dispatcher.dispatch_all(report.changes, source)

        return report
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"DiffEngine failed for source '{source}': {exc}",
        ) from exc
