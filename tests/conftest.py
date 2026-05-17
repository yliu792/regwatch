"""
Pytest configuration — works around asyncpg + Python 3.14 event-loop issues
by re-creating the database engine with pool_pre_ping and a short recycle.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine

load_dotenv()


@pytest.fixture()
async def _test_engine():
    """Session-scoped test engine with aggressive connection recycling."""
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://regwatch:regwatch@192.168.64.3:5432/regwatch",
    )
    engine = create_async_engine(
        database_url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=0,
    )
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
def _patch_engine(monkeypatch, _test_engine):
    """Replace app.database.engine with the test engine."""
    import app.database

    monkeypatch.setattr(app.database, "engine", _test_engine)
