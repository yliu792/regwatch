"""
Tests that assert ChangeEvent correctness given a known DB state.

Pattern:
1. Directly seed RegulatoryRecords into the DB (not via DiffEngine)
2. Feed new ScrapeResults to DiffEngine.process()
3. Assert every property of each produced ChangeEvent:
   - previous_hash / new_hash match expected values
   - diff_summary is a valid unified diff with correct markers
   - record_id FK points to the right RegulatoryRecord
   - dispatched defaults to False
   - detected_at is within a recent window
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.database
from app.engine.diff_engine import DiffEngine
from app.models import ChangeEvent, RegulatoryRecord
from app.scrapers.base import ScrapeResult

_TEST_SOURCE = "epa_tsca"

# ── Fixture ─────────────────────────────────────────────────────────────


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
    factory = async_sessionmaker(app.database.engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        for tbl in ("change_events", "regulatory_records", "webhook_registrations"):
            await s.execute(text(f"DELETE FROM {tbl}"))
        await s.commit()
        yield s
        await s.rollback()


# ── Helpers ─────────────────────────────────────────────────────────────


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_result(record_id: str, title: str, raw_content: str) -> ScrapeResult:
    return ScrapeResult(
        source=_TEST_SOURCE,
        record_id=record_id,
        title=title,
        raw_content=raw_content,
        fetched_at=datetime.now(timezone.utc),
    )


async def _seed(session: AsyncSession, **records: str) -> dict[str, RegulatoryRecord]:
    """
    Insert RegulatoryRecords directly.

    Usage:  await _seed(session, benzene="old benzene content")
    Returns: {"benzene": <RegulatoryRecord>}
    """
    result: dict[str, RegulatoryRecord] = {}
    for record_id, raw_content in records.items():
        rec = RegulatoryRecord(
            source=_TEST_SOURCE,
            record_id=record_id,
            title=record_id.title(),
            raw_content=raw_content,
            content_hash=_hash(raw_content),
            status="active",
        )
        session.add(rec)
        result[record_id] = rec
    await session.flush()
    return result


async def _fetch_event(session: AsyncSession, event_id: uuid.UUID) -> ChangeEvent:
    stmt = select(ChangeEvent).where(ChangeEvent.id == event_id)
    return (await session.execute(stmt)).scalar_one()


# ── Single record, single-line change ───────────────────────────────────


class TestSingleRecordChange:
    @pytest.mark.asyncio
    async def test_previous_hash_matches_seeded_content(self, session):
        """previous_hash = SHA-256 of the stored record's raw_content."""
        stored = await _seed(session, benzene="Benzene is a hydrocarbon.")

        engine = DiffEngine(session)
        report = await engine.process(
            [_make_result("benzene", "Benzene", "Benzene is a carcinogen.")]
        )

        assert len(report.changes) == 1
        event = report.changes[0]
        expected_prev = _hash("Benzene is a hydrocarbon.")
        assert event.previous_hash == expected_prev
        assert report.changed_records[0].id == stored["benzene"].id

    @pytest.mark.asyncio
    async def test_new_hash_matches_incoming_content(self, session):
        """new_hash = SHA-256 of the incoming ScrapeResult's raw_content."""
        await _seed(session, benzene="Old text.")

        engine = DiffEngine(session)
        report = await engine.process([_make_result("benzene", "Benzene", "New text here.")])

        expected_new = _hash("New text here.")
        assert report.changes[0].new_hash == expected_new

    @pytest.mark.asyncio
    async def test_hashes_are_different_when_content_differs(self, session):
        """previous_hash != new_hash when content changes."""
        await _seed(session, benzene="v1")

        engine = DiffEngine(session)
        report = await engine.process([_make_result("benzene", "Benzene", "v2")])

        event = report.changes[0]
        assert event.previous_hash != event.new_hash
        assert event.previous_hash == _hash("v1")
        assert event.new_hash == _hash("v2")


# ── Diff summary format ─────────────────────────────────────────────────


class TestDiffSummaryFormat:
    @pytest.mark.asyncio
    async def test_diff_has_unified_diff_markers(self, session):
        """diff_summary contains ---, +++, @@ markers."""
        await _seed(session, benzene="line one\nline two\nline three")

        engine = DiffEngine(session)
        report = await engine.process(
            [_make_result("benzene", "Benzene", "line one\nline two MODIFIED\nline three")]
        )

        diff = report.changes[0].diff_summary
        assert "--- previous" in diff
        assert "+++ current" in diff
        assert "@@" in diff

    @pytest.mark.asyncio
    async def test_diff_shows_added_lines(self, session):
        """Added lines prefixed with '+'."""
        await _seed(session, benzene="A\nB\nC")

        engine = DiffEngine(session)
        report = await engine.process([_make_result("benzene", "Benzene", "A\nB\nC\nD")])

        diff = report.changes[0].diff_summary
        assert "+D" in diff

    @pytest.mark.asyncio
    async def test_diff_shows_removed_lines(self, session):
        """Removed lines prefixed with '-'."""
        await _seed(session, benzene="A\nB\nC")

        engine = DiffEngine(session)
        report = await engine.process([_make_result("benzene", "Benzene", "A\nC")])

        diff = report.changes[0].diff_summary
        assert "-B" in diff

    @pytest.mark.asyncio
    async def test_multi_line_diff_context(self, session):
        """Multi-line change shows surrounding context."""
        await _seed(
            session,
            benzene=("## Benzene\nCAS: 71-43-2\nStatus: Active\nNotes: None\nUpdated: 2024\n"),
        )
        engine = DiffEngine(session)
        report = await engine.process(
            [
                _make_result(
                    "benzene",
                    "Benzene",
                    (
                        "## Benzene\n"
                        "CAS: 71-43-2\n"
                        "Status: Restricted\n"
                        "Notes: TSCA Section 6\n"
                        "Updated: 2025\n"
                    ),
                )
            ]
        )
        diff = report.changes[0].diff_summary
        assert "## Benzene" in diff
        assert "-Status: Active" in diff
        assert "+Status: Restricted" in diff
        assert "+Notes: TSCA Section 6" in diff
        assert "-Updated: 2024" in diff
        assert "+Updated: 2025" in diff


# ── Record FK integrity ─────────────────────────────────────────────────


class TestRecordForeignKey:
    @pytest.mark.asyncio
    async def test_event_record_id_points_to_correct_regulatory_record(self, session):
        """ChangeEvent.record_id FK references the updated RegulatoryRecord."""
        stored = await _seed(session, benzene="old")

        engine = DiffEngine(session)
        report = await engine.process([_make_result("benzene", "Benzene", "new")])

        event = report.changes[0]
        assert event.record_id == stored["benzene"].id
        assert event.record_id == report.changed_records[0].id


    @pytest.mark.asyncio
    async def test_change_event_persisted_and_retrievable(self, session):
        """ChangeEvent is flushed and can be re-queried by ID."""
        await _seed(session, benzene="old content")

        engine = DiffEngine(session)
        report = await engine.process([_make_result("benzene", "Benzene", "new content")])

        event_id = report.changes[0].id
        fetched = await _fetch_event(session, event_id)
        assert fetched.previous_hash == _hash("old content")
        assert fetched.new_hash == _hash("new content")
        assert fetched.diff_summary == report.changes[0].diff_summary
        assert fetched.record_id == report.changed_records[0].id

    @pytest.mark.asyncio
    async def test_multiple_events_reference_same_record(self, session):
        """Two sequential changes → two ChangeEvents, both referencing the same record."""
        stored = await _seed(session, benzene="v1")

        engine = DiffEngine(session)
        r1 = await engine.process([_make_result("benzene", "Benzene", "v2")])
        r2 = await engine.process([_make_result("benzene", "Benzene", "v3")])

        assert r1.changes[0].record_id == stored["benzene"].id
        assert r2.changes[0].record_id == stored["benzene"].id
        assert r1.changes[0].record_id == r2.changes[0].record_id


# ── Defaults and metadata ───────────────────────────────────────────────


class TestChangeEventDefaults:
    @pytest.mark.asyncio
    async def test_dispatched_defaults_to_false(self, session):
        """Newly created ChangeEvent has dispatched=False."""
        await _seed(session, benzene="old")

        engine = DiffEngine(session)
        report = await engine.process([_make_result("benzene", "Benzene", "new")])

        assert report.changes[0].dispatched is False

    @pytest.mark.asyncio
    async def test_detected_at_is_recent(self, session):
        """detected_at is within a few seconds of now."""
        await _seed(session, benzene="old")

        before = datetime.now(timezone.utc)
        engine = DiffEngine(session)
        report = await engine.process([_make_result("benzene", "Benzene", "new")])
        after = datetime.now(timezone.utc)

        dt = report.changes[0].detected_at
        assert abs((dt - before).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_record_content_hash_updated_after_change(self, session):
        """After a change, the RegulatoryRecord's content_hash reflects the new content."""
        await _seed(session, benzene="original")

        engine = DiffEngine(session)
        report = await engine.process([_make_result("benzene", "Benzene", "revised")])

        record = report.changed_records[0]
        assert record.content_hash == _hash("revised")
        assert record.content_hash == report.changes[0].new_hash

    @pytest.mark.asyncio
    async def test_record_status_stays_active_after_change(self, session):
        """A changed record retains status='active'."""
        await _seed(session, benzene="old")

        engine = DiffEngine(session)
        report = await engine.process([_make_result("benzene", "Benzene", "new")])

        assert report.changed_records[0].status == "active"


# ── No-change scenarios ─────────────────────────────────────────────────


class TestNoChangeEventProduced:
    @pytest.mark.asyncio
    async def test_identical_content_produces_no_change_event(self, session):
        """Same content → no ChangeEvent."""
        await _seed(session, benzene="identical content")

        engine = DiffEngine(session)
        report = await engine.process([_make_result("benzene", "Benzene", "identical content")])

        assert len(report.changes) == 0
        assert len(report.unchanged_records) == 1

    @pytest.mark.asyncio
    async def test_new_record_produces_no_change_event(self, session):
        """Brand-new record → no ChangeEvent created."""
        engine = DiffEngine(session)
        report = await engine.process([_make_result("newone", "New Substance", "content")])

        assert len(report.changes) == 0
        assert len(report.new_records) == 1


# ── Multi-record mixed scenarios ────────────────────────────────────────


class TestMultiRecordMixed:
    @pytest.mark.asyncio
    async def test_two_changes_three_unchanged(self, session):
        """5 records seeded, 2 changed, 3 unchanged → 2 ChangeEvents."""
        await _seed(
            session,
            A="content A",
            B="content B",
            C="content C",
            D="content D",
            E="content E",
        )
        engine = DiffEngine(session)
        report = await engine.process(
            [
                _make_result("A", "A", "content A"),
                _make_result("B", "B", "content B MODIFIED"),
                _make_result("C", "C", "content C"),
                _make_result("D", "D", "content D MODIFIED"),
                _make_result("E", "E", "content E"),
            ]
        )

        assert len(report.changes) == 2
        assert len(report.unchanged_records) == 3
        assert len(report.stale_records) == 0

        events = {(e.previous_hash, e.new_hash) for e in report.changes}
        assert (_hash("content B"), _hash("content B MODIFIED")) in events
        assert (_hash("content D"), _hash("content D MODIFIED")) in events

    @pytest.mark.asyncio
    async def test_changes_and_stale_in_same_run(self, session):
        """Seed 4 records; incoming has 3 (one changed, two same, one new, one missing)."""
        await _seed(session, W="w", X="x", Y="y", Z="z")

        engine = DiffEngine(session)
        report = await engine.process(
            [
                _make_result("W", "W", "w"),
                _make_result("X", "X", "x MODIFIED"),
                _make_result("Y", "Y", "y"),
                _make_result("N", "N", "new entry"),
                # Z is missing → stale
            ]
        )

        assert len(report.changes) == 1
        assert len(report.stale_records) == 1
        assert report.stale_records[0].record_id == "Z"

        event = report.changes[0]
        assert event.previous_hash == _hash("x")
        assert event.new_hash == _hash("x MODIFIED")

    @pytest.mark.asyncio
    async def test_each_change_event_has_unique_id(self, session):
        """Multiple ChangeEvents in one run → each has a distinct UUID."""
        await _seed(session, A="a1", B="b1")

        engine = DiffEngine(session)
        report = await engine.process(
            [
                _make_result("A", "A", "a2"),
                _make_result("B", "B", "b2"),
            ]
        )

        assert len(report.changes) == 2
        assert report.changes[0].id != report.changes[1].id
        assert isinstance(report.changes[0].id, uuid.UUID)
