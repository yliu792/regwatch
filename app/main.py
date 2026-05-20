"""
FastAPI application entry point.

Lifespan manages:
- APScheduler (configurable scrape intervals per source)
- Database engine disposal
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.records import router as records_router
from app.api.scrape import router as scrape_router
from app.api.webhooks import router as webhooks_router
from app.database import engine
from app.scheduler import build_scheduler

logger = logging.getLogger(__name__)

# ── Scheduler (built once at import time so it's ready for lifespan) ────

_scheduler: AsyncIOScheduler | None = build_scheduler()


# ── Lifespan ────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:  # noqa: ARG001
    """Startup / shutdown logic."""
    if _scheduler is not None:
        _scheduler.start()
        logger.info("APScheduler started with %d job(s)", len(_scheduler.get_jobs()))

    yield

    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler shut down")

    await engine.dispose()


# ── App ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="RegWatch",
    description=(
        "Regulatory change watcher — scrape, diff, and dispatch webhooks when agency pages change."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------
# Routers
# --------------------------------------------------
app.include_router(records_router, prefix="/records", tags=["Records"])
app.include_router(webhooks_router, tags=["Webhooks"])
app.include_router(scrape_router, tags=["Scrape"])


@app.get("/health", tags=["Health"])
async def health_check() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/scheduler", tags=["Scheduler"])
async def scheduler_status() -> dict:
    """Return scheduler state and job listing."""
    if _scheduler is None:
        return {"enabled": False, "jobs": []}

    jobs = []
    for job in _scheduler.get_jobs():
        jobs.append(
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
        )
    return {"enabled": True, "running": _scheduler.running, "jobs": jobs}
