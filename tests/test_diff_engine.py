"""
Unit tests for the DiffEngine.

Covers all four cases:
1. New record → INSERT (status="active")
2. Hash unchanged → update last_seen (status stays "active")
3. Hash changed → ChangeEvent created + record updated
4. Missing from batch → marked "stale"
——
Each test gets its own async session and cleans tables first.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# from app.database import engine as _db_engine
import app.database
from app.engine.diff_engine import DiffEngine
from app.models import ChangeEvent, RegulatoryRecord
from app.scrapers.base import ScrapeResult

_TEST_SOURCE = "epa_tsca"

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="function")
def event_loop():
    """Create a fresh event loop for each test (asyncpg + Python 3.14 workaround)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


@pytest.fixture
async def session(event_loop) -> AsyncGenerator[AsyncSession, None]:
    """Yield a fresh session with clean tables, rolled back after each test."""
    # factory = async_sessionmaker(_db_engine, class_=AsyncSession, expire_on_commit=False)
    factory = async_sessionmaker(app.database.engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        for tbl in ("change_events", "regulatory_records", "webhook_registrations"):
            await s.execute(text(f"DELETE FROM {tbl}"))
        await s.commit()
        async with s.begin():
            yield s
            await s.rollback()


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_result(
    record_id: str = "71-43-2",
    title: str = "Benzene",
    raw_content: str | None = None,
    source: str = _TEST_SOURCE,
) -> ScrapeResult:
    return ScrapeResult(
        source=source,
        record_id=record_id,
        title=title,
        raw_content=raw_content or f"Content for {record_id}: {title}",
        fetched_at=datetime.now(timezone.utc),
    )


async def _count_rows(session: AsyncSession, model) -> int:
    stmt = select(model)
    result = await session.execute(stmt)
    return len(result.scalars().all())


# ── Case 1: New records ─────────────────────────────────────────────────


class TestNewRecords:
    @pytest.mark.asyncio
    async def test_single_new_record(self, session):
        engine = DiffEngine(session)
        report = await engine.process([_make_result("71-43-2", "Benzene")])
        assert report.total_incoming == 1
        assert len(report.new_records) == 1
        assert report.new_records[0].status == "active"
        assert report.new_records[0].record_id == "71-43-2"

    @pytest.mark.asyncio
    async def test_multiple_new_records(self, session):
        results = [
            _make_result("71-43-2", "Benzene"),
            _make_result("50-00-0", "Formaldehyde"),
            _make_result("1336-36-3", "PCBs"),
        ]
        engine = DiffEngine(session)
        report = await engine.process(results)
        assert report.total_incoming == 3
        assert len(report.new_records) == 3

    @pytest.mark.asyncio
    async def test_new_records_persisted(self, session):
        engine = DiffEngine(session)
        await engine.process([_make_result("71-43-2", "Benzene")])
        stmt = select(RegulatoryRecord).where(
            RegulatoryRecord.source == _TEST_SOURCE,
            RegulatoryRecord.record_id == "71-43-2",
        )
        result = await session.execute(stmt)
        assert result.scalar_one().status == "active"


# ── Case 2: Unchanged records ───────────────────────────────────────────


class TestUnchangedRecords:
    @pytest.mark.asyncio
    async def test_unchanged_record(self, session):
        engine = DiffEngine(session)
        await engine.process([_make_result("71-43-2", "Benzene")])
        report = await engine.process([_make_result("71-43-2", "Benzene")])
        assert len(report.unchanged_records) == 1
        assert len(report.changes) == 0
        assert await _count_rows(session, RegulatoryRecord) == 1

    @pytest.mark.asyncio
    async def test_unchanged_last_seen_updated(self, session):
        engine = DiffEngine(session)
        await engine.process([_make_result("71-43-2", "Benzene")])
        await asyncio.sleep(0.05)
        report = await engine.process([_make_result("71-43-2", "Benzene")])
        assert report.unchanged_records[0].last_seen > report.unchanged_records[0].first_seen


# ── Case 3: Changed records ─────────────────────────────────────────────


class TestChangedRecords:
    @pytest.mark.asyncio
    async def test_changed_record_creates_change_event(self, session):
        engine = DiffEngine(session)
        await engine.process([_make_result("71-43-2", "Benzene", raw_content="Old content")])
        report = await engine.process(
            [_make_result("71-43-2", "Benzene", raw_content="New content")]
        )
        assert len(report.changes) == 1
        event = report.changes[0]
        assert event.previous_hash != event.new_hash
        assert "Old content" in event.diff_summary
        assert "New content" in event.diff_summary
        assert await _count_rows(session, RegulatoryRecord) == 1
        assert await _count_rows(session, ChangeEvent) == 1

    @pytest.mark.asyncio
    async def test_multiple_changes_accumulate(self, session):
        engine = DiffEngine(session)
        for v in ["v1", "v2", "v3"]:
            await engine.process([_make_result("71-43-2", "Benzene", raw_content=v)])
        assert await _count_rows(session, RegulatoryRecord) == 1
        assert await _count_rows(session, ChangeEvent) == 2

    @pytest.mark.asyncio
    async def test_identical_content_after_change_is_unchanged(self, session):
        engine = DiffEngine(session)
        await engine.process([_make_result("71-43-2", "Benzene", raw_content="v1")])
        await engine.process([_make_result("71-43-2", "Benzene", raw_content="v2")])
        report = await engine.process([_make_result("71-43-2", "Benzene", raw_content="v2")])
        assert len(report.unchanged_records) == 1
        assert len(report.changes) == 0
        assert await _count_rows(session, ChangeEvent) == 1


# ── Case 4: Stale records ───────────────────────────────────────────────


class TestStaleRecords:
    @pytest.mark.asyncio
    async def test_missing_record_marked_stale(self, session):
        engine = DiffEngine(session)
        await engine.process(
            [_make_result("71-43-2", "Benzene"), _make_result("50-00-0", "Formaldehyde")]
        )
        report = await engine.process([_make_result("71-43-2", "Benzene")])
        assert len(report.stale_records) == 1
        assert report.stale_records[0].record_id == "50-00-0"

    @pytest.mark.asyncio
    async def test_no_stale_when_all_present(self, session):
        engine = DiffEngine(session)
        await engine.process(
            [_make_result("71-43-2", "Benzene"), _make_result("50-00-0", "Formaldehyde")]
        )
        report = await engine.process(
            [_make_result("71-43-2", "Benzene"), _make_result("50-00-0", "Formaldehyde")]
        )
        assert len(report.stale_records) == 0

    @pytest.mark.asyncio
    async def test_already_stale_not_double_counted(self, session):
        engine = DiffEngine(session)
        await engine.process(
            [_make_result("71-43-2", "Benzene"), _make_result("50-00-0", "Formaldehyde")]
        )
        await engine.process([_make_result("71-43-2", "Benzene")])
        report = await engine.process([_make_result("71-43-2", "Benzene")])
        assert len(report.stale_records) == 0

    @pytest.mark.asyncio
    async def test_stale_record_revived(self, session):
        engine = DiffEngine(session)
        await engine.process(
            [_make_result("71-43-2", "Benzene"), _make_result("50-00-0", "Formaldehyde")]
        )
        await engine.process([_make_result("71-43-2", "Benzene")])
        report = await engine.process(
            [_make_result("71-43-2", "Benzene"), _make_result("50-00-0", "Formaldehyde")]
        )
        assert len([r for r in report.stale_records if r.record_id == "50-00-0"]) == 0
        assert any(r.record_id == "50-00-0" for r in report.unchanged_records)


# ── Edge cases ───────────────────────────────────────────────────────────


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_incoming(self, session):
        engine = DiffEngine(session)
        report = await engine.process([])
        assert report.total_incoming == 0
        assert not report.has_changes

    @pytest.mark.asyncio
    async def test_duplicate_incoming_deduplicated(self, session):
        engine = DiffEngine(session)
        report = await engine.process(
            [_make_result("71-43-2", "Benzene v1"), _make_result("71-43-2", "Benzene v2")]
        )
        assert report.total_incoming == 1
        assert report.new_records[0].title == "Benzene v2"

    @pytest.mark.asyncio
    async def test_mixed_scenario(self, session):
        engine = DiffEngine(session)
        await engine.process(
            [
                _make_result("A", "Substance A", raw_content="content-A"),
                _make_result("B", "Substance B", raw_content="content-B"),
                _make_result("C", "Substance C", raw_content="content-C"),
            ]
        )
        report = await engine.process(
            [
                _make_result("A", "Substance A", raw_content="content-A"),
                _make_result("B", "Substance B", raw_content="content-B-new"),
                _make_result("D", "Substance D", raw_content="content-D"),
            ]
        )
        assert report.total_incoming == 3
        assert len(report.new_records) == 1
        assert len(report.unchanged_records) == 1
        assert len(report.changed_records) == 1
        assert len(report.changes) == 1
        assert len(report.stale_records) == 1
        assert len(report.errors) == 0
        assert report.new_records[0].record_id == "D"
        assert report.stale_records[0].record_id == "C"
