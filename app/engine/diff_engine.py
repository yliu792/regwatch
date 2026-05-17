"""
Diff engine — compare incoming scraped records against stored records.

Handles three cases per incoming record:
1. **New record** → INSERT with status ``"active"``
2. **Hash unchanged** → update ``last_seen``, keep ``"active"``
3. **Hash changed** → create ``ChangeEvent`` + update stored record

Also detects records that exist in the DB but are **missing** from the
incoming batch and marks them ``"stale"`` (they were removed upstream).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.diff import compute_diff
from app.models import ChangeEvent, RegulatoryRecord
from app.scrapers.base import ScrapeResult

logger = logging.getLogger(__name__)


# ── Result types ────────────────────────────────────────────────────────


@dataclass
class DiffReport:
    """Summary produced by :meth:`DiffEngine.process`."""

    source: str
    total_incoming: int
    new_records: list[RegulatoryRecord] = field(default_factory=list)
    changed_records: list[RegulatoryRecord] = field(default_factory=list)
    unchanged_records: list[RegulatoryRecord] = field(default_factory=list)
    stale_records: list[RegulatoryRecord] = field(default_factory=list)
    changes: list[ChangeEvent] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return len(self.changes) > 0

    @property
    def total_affected(self) -> int:
        return (
            len(self.new_records)
            + len(self.changed_records)
            + len(self.unchanged_records)
            + len(self.stale_records)
        )


# ── Engine ──────────────────────────────────────────────────────────────


class DiffEngine:
    """
    Compares incoming :class:`ScrapeResult` objects against persisted
    :class:`RegulatoryRecord` rows using content hashing.

    Usage::

        async with async_session_factory() as session:
            engine = DiffEngine(session)
            report = await engine.process(scrape_results)
            await session.commit()
            # optionally dispatch webhooks for report.changes
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process(self, results: list[ScrapeResult]) -> DiffReport:
        """
        Run the full diff pipeline.

        The caller is responsible for committing the session afterward so
        that webhook dispatch or other side effects can share the
        transaction.
        """
        if not results:
            return DiffReport(source="", total_incoming=0)

        # ---- 1. De-duplicate incoming by (source, record_id) ----------
        deduped: dict[tuple[str, str], ScrapeResult] = {}
        for r in results:
            deduped[(r.source, r.record_id)] = r
        unique_results = list(deduped.values())

        source = unique_results[0].source
        report = DiffReport(source=source, total_incoming=len(unique_results))

        # ---- 2. Load all stored records for this source --------------
        existing_map = await self._load_all_for_source(source)

        # ---- 3. Process each incoming result -------------------------
        incoming_keys: set[tuple[str, str]] = set()

        for incoming in unique_results:
            key = (incoming.source, incoming.record_id)
            incoming_keys.add(key)

            try:
                stored = existing_map.get(key)
                if stored is None:
                    # Case 1 — New record → INSERT
                    record = await self._handle_new(incoming)
                    report.new_records.append(record)
                elif stored.content_hash == incoming.content_hash:
                    # Case 2 — No change → update last_seen
                    record = await self._handle_unchanged(stored)
                    report.unchanged_records.append(record)
                else:
                    # Case 3 — Hash changed → ChangeEvent + update
                    record, event = await self._handle_changed(stored, incoming)
                    report.changed_records.append(record)
                    report.changes.append(event)
            except Exception:
                logger.exception(
                    "DiffEngine error for source=%s record_id=%s",
                    incoming.source,
                    incoming.record_id,
                )
                report.errors.append({"source": incoming.source, "record_id": incoming.record_id})

        # ---- 4. Mark missing records as stale ------------------------
        stale = await self._mark_stale(source, incoming_keys)
        report.stale_records = stale

        return report

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_all_for_source(self, source: str) -> dict[tuple[str, str], RegulatoryRecord]:
        """Load every stored record for *source* into a lookup dict."""
        stmt = select(RegulatoryRecord).where(RegulatoryRecord.source == source)
        result = await self._session.execute(stmt)
        return {(r.source, r.record_id): r for r in result.scalars().all()}

    async def _handle_new(self, incoming: ScrapeResult) -> RegulatoryRecord:
        """Case 1 — INSERT a new record."""
        record = RegulatoryRecord(
            source=incoming.source,
            record_id=incoming.record_id,
            title=incoming.title,
            raw_content=incoming.raw_content,
            content_hash=incoming.content_hash,
            status="active",
            first_seen=incoming.fetched_at,
            last_seen=incoming.fetched_at,
        )
        self._session.add(record)
        await self._session.flush()
        logger.info("New record: source=%s record_id=%s", record.source, record.record_id)
        return record

    async def _handle_unchanged(self, stored: RegulatoryRecord) -> RegulatoryRecord:
        """Case 2 — No hash change; update last_seen."""
        stored.last_seen = datetime.now(timezone.utc)
        stored.status = "active"
        await self._session.flush()
        logger.debug(
            "Unchanged: id=%s record_id=%s hash=%s…",
            stored.id,
            stored.record_id,
            stored.content_hash[:16],
        )
        return stored

    async def _handle_changed(
        self, stored: RegulatoryRecord, incoming: ScrapeResult
    ) -> tuple[RegulatoryRecord, ChangeEvent]:
        """Case 3 — Content hash differs; compute diff and create ChangeEvent."""
        diff_result = compute_diff(
            record_id=stored.id,
            old_content=stored.raw_content,
            new_content=incoming.raw_content,
        )

        # Persist the ChangeEvent (diff_result is None only when hashes
        # match, which we already excluded — but guard anyway).
        if diff_result is None:
            # Should not happen, but treat as unchanged.
            return await self._handle_unchanged(stored), None  # type: ignore[return-value]

        event = ChangeEvent(
            record_id=stored.id,
            previous_hash=diff_result.previous_hash,
            new_hash=diff_result.new_hash,
            diff_summary=diff_result.diff_summary,
        )
        self._session.add(event)

        # Update the stored record to reflect new content.
        stored.title = incoming.title
        stored.raw_content = incoming.raw_content
        stored.content_hash = incoming.content_hash
        stored.last_seen = incoming.fetched_at
        stored.status = "active"

        await self._session.flush()
        logger.info(
            "Changed: id=%s record_id=%s old_hash=%s… new_hash=%s…",
            stored.id,
            stored.record_id,
            event.previous_hash[:16],
            event.new_hash[:16],
        )
        return stored, event

    async def _mark_stale(
        self, source: str, incoming_keys: set[tuple[str, str]]
    ) -> list[RegulatoryRecord]:
        """
        Mark stored records as ``"stale"`` when they are not present in
        the incoming batch (i.e. they were removed from the upstream source).
        """
        # Find records for this source whose key is NOT in incoming_keys.
        stmt = select(RegulatoryRecord).where(
            RegulatoryRecord.source == source,
            RegulatoryRecord.status == "active",
        )
        result = await self._session.execute(stmt)
        all_active = result.scalars().all()

        stale_candidates = [r for r in all_active if (r.source, r.record_id) not in incoming_keys]
        if not stale_candidates:
            return []

        ids = [r.id for r in stale_candidates]
        await self._session.execute(
            update(RegulatoryRecord)
            .where(RegulatoryRecord.id.in_(ids))
            .values(status="stale", last_seen=datetime.now(timezone.utc))
        )
        await self._session.flush()

        logger.info("Marked %d record(s) as stale for source=%s", len(ids), source)
        return stale_candidates
