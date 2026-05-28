"""
APScheduler integration — runs scrape jobs on configurable intervals.

Configured via environment variables:

.. code-block:: bash

    REGWATCH_SCHEDULER_ENABLED=true        # master switch (default: true)
    REGWATCH_SCHEDULE_EPA_TSCA=86400       # seconds between EPA TSCA scrapes (default: 24h)
    REGWATCH_SCHEDULE_ECHA_REACH=43200     # seconds between ECHA REACH scrapes (default: 12h)
    REGWATCH_SCHEDULE_CHINA_MEE=21600      # seconds between China MEE scrapes (default: 6h)
"""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.engine.scrape_pipeline import run_scrape_pipeline

load_dotenv()

logger = logging.getLogger(__name__)

# ── Default intervals (seconds) ─────────────────────────────────────────

_DEFAULT_SCHEDULES: dict[str, int] = {
    "epa_tsca": 86400,  # 24 hours
    "echa_reach": 43200,  # 12 hours
    "china_mee": 21600,  # 6 hours
}


def _get_interval(source: str) -> int:
    """Read the interval for *source* from the environment."""
    env_key = f"REGWATCH_SCHEDULE_{source.upper()}"
    val = os.getenv(env_key)
    if val is not None:
        try:
            return int(val)
        except ValueError:
            logger.warning("Invalid %s=%s, using default", env_key, val)
    return _DEFAULT_SCHEDULES.get(source, 86400)


def _is_enabled() -> bool:
    """Check whether the scheduler master switch is on."""
    return os.getenv("REGWATCH_SCHEDULER_ENABLED", "true").lower() not in ("false", "0", "no")


# ── Job runner (called by APScheduler) ──────────────────────────────────


async def _run_scheduled_scrape(source: str) -> None:
    """
    Execute a single scrape+diff+webhook cycle for *source*.

    Creates its own database session — independent of any HTTP request.
    """
    logger.info("Scheduled scrape starting: source=%s", source)
    async with async_session_factory() as session:
        try:
            report = await run_scrape_pipeline(source, session)
            await session.commit()

            logger.info(
                "Scheduled scrape done: source=%s incoming=%d new=%d changed=%d unchanged=%d stale=%d errors=%d webhooks=%s",
                report.source,
                report.total_incoming,
                len(report.new_records),
                len(report.changed_records),
                len(report.unchanged_records),
                len(report.stale_records),
                len(report.errors),
                report.has_changes,
            )
        except Exception:
            await session.rollback()
            logger.exception("Scheduled scrape failed: source=%s", source)


# ── Scheduler builder ───────────────────────────────────────────────────


def build_scheduler() -> AsyncIOScheduler | None:
    """
    Create and configure an :class:`AsyncIOScheduler` with one job per
    registered source.

    Returns ``None`` when ``REGWATCH_SCHEDULER_ENABLED`` is false.
    """
    if not _is_enabled():
        logger.info("Scheduler disabled (REGWATCH_SCHEDULER_ENABLED != true)")
        return None

    scheduler = AsyncIOScheduler()

    for source in sorted(_DEFAULT_SCHEDULES):
        interval = _get_interval(source)
        scheduler.add_job(
            _run_scheduled_scrape,
            trigger=IntervalTrigger(seconds=interval),
            args=[source],
            id=f"scrape-{source}",
            name=f"Scrape {source}",
            replace_existing=True,
        )
        logger.info(
            "Scheduled job: scrape-%s every %d s (≈ %.1f h)",
            source,
            interval,
            interval / 3600,
        )

    return scheduler
