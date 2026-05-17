"""
FastAPI application entry point.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.records import router as records_router
from app.api.scrape import router as scrape_router
from app.api.webhooks import router as webhooks_router
from app.database import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:  # noqa: ARG001
    """Startup / shutdown logic."""
    # Schema is managed by Alembic migrations — run `alembic upgrade head` before starting.
    yield
    await engine.dispose()


app = FastAPI(
    title="RegWatch",
    description="Regulatory change watcher — scrape, diff, and dispatch webhooks when agency pages change.",
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
app.include_router(webhooks_router, prefix="/webhooks", tags=["Webhooks"])
app.include_router(scrape_router, tags=["Scrape"])


@app.get("/health", tags=["Health"])
async def health_check() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
