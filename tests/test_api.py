"""
API integration tests using FastAPI's TestClient.

Uses dependency overrides to inject a shared test session so all
endpoint calls within a single test share the same DB transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.database as db
from app.database import get_session
from app.main import app as fastapi_app
from app.models import RegulatoryRecord

# ── Fixture ─────────────────────────────────────────────────────────────


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Yield an async httpx client, each test gets a fresh DB session via dependency override."""
    factory = async_sessionmaker(db.engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        # Clean tables.
        for tbl in ("change_events", "regulatory_records", "webhook_registrations"):
            await s.execute(text(f"DELETE FROM {tbl}"))
        await s.commit()

        # Override the get_session dependency.
        async def _override():
            yield s

        fastapi_app.dependency_overrides[get_session] = _override

        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

        fastapi_app.dependency_overrides.clear()
        await s.rollback()


# ── Helpers ─────────────────────────────────────────────────────────────


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _seed(session: AsyncSession, **records: str) -> dict[str, RegulatoryRecord]:
    result: dict[str, RegulatoryRecord] = {}
    for record_id, raw_content in records.items():
        rec = RegulatoryRecord(
            source="epa_tsca",
            record_id=record_id,
            title=record_id,
            raw_content=raw_content,
            content_hash=_hash(raw_content),
            status="active",
        )
        session.add(rec)
        result[record_id] = rec
    await session.flush()
    return result


# ── Health ──────────────────────────────────────────────────────────────


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ── Records — list and detail ───────────────────────────────────────────


class TestRecordsList:
    @pytest.mark.asyncio
    async def test_list_empty(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/records/")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_returns_seeded_records(self, client: httpx.AsyncClient) -> None:
        # Seed via the override session.
        override = fastapi_app.dependency_overrides[get_session]
        async for session in override():
            await _seed(session, Benzene="content-B", Formaldehyde="content-F")
            await session.commit()

        resp = await client.get("/records/")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert {r["record_id"] for r in data} == {"Benzene", "Formaldehyde"}
        assert "raw_content" not in data[0]

    @pytest.mark.asyncio
    async def test_list_filter_by_source(self, client: httpx.AsyncClient) -> None:
        override = fastapi_app.dependency_overrides[get_session]
        async for session in override():
            session.add(
                RegulatoryRecord(
                    source="epa_tsca",
                    record_id="A",
                    title="A",
                    raw_content="a",
                    content_hash=_hash("a"),
                    status="active",
                )
            )
            session.add(
                RegulatoryRecord(
                    source="echa_reach",
                    record_id="B",
                    title="B",
                    raw_content="b",
                    content_hash=_hash("b"),
                    status="active",
                )
            )
            await session.commit()

        resp = await client.get("/records/?source=epa_tsca")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["source"] == "epa_tsca"

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, client: httpx.AsyncClient) -> None:
        override = fastapi_app.dependency_overrides[get_session]
        async for session in override():
            session.add(
                RegulatoryRecord(
                    source="epa_tsca",
                    record_id="A",
                    title="A",
                    raw_content="a",
                    content_hash=_hash("a"),
                    status="active",
                )
            )
            session.add(
                RegulatoryRecord(
                    source="epa_tsca",
                    record_id="B",
                    title="B",
                    raw_content="b",
                    content_hash=_hash("b"),
                    status="stale",
                )
            )
            await session.commit()

        resp = await client.get("/records/?status=stale")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["record_id"] == "B"


class TestRecordDetail:
    @pytest.mark.asyncio
    async def test_get_record_returns_full_data(self, client: httpx.AsyncClient) -> None:
        override = fastapi_app.dependency_overrides[get_session]
        async for session in override():
            stored = await _seed(session, Benzene="raw benzene content")
            await session.commit()
            rec_id = str(stored["Benzene"].id)

        resp = await client.get(f"/records/{rec_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["record_id"] == "Benzene"
        assert data["raw_content"] == "raw benzene content"

    @pytest.mark.asyncio
    async def test_get_record_404(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/records/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404


# ── Records — history ───────────────────────────────────────────────────


class TestRecordHistory:
    @pytest.mark.asyncio
    async def test_history_returns_change_events(self, client: httpx.AsyncClient) -> None:
        from app.engine.diff_engine import DiffEngine
        from app.scrapers.base import ScrapeResult

        def R(rid, raw):
            return ScrapeResult(
                source="epa_tsca",
                record_id=rid,
                title=rid,
                raw_content=raw,
                fetched_at=datetime.now(timezone.utc),
            )

        override = fastapi_app.dependency_overrides[get_session]
        async for session in override():
            eng = DiffEngine(session)
            await eng.process([R("Benzene", "v1")])
            await eng.process([R("Benzene", "v2")])
            await session.commit()
            stmt = select(RegulatoryRecord).where(RegulatoryRecord.record_id == "Benzene")
            rec = (await session.execute(stmt)).scalar_one()
            rec_id = str(rec.id)

        resp = await client.get(f"/records/{rec_id}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["new_hash"] == _hash("v2")
        assert data[0]["dispatched"] is False
        assert "diff_summary" in data[0]

    @pytest.mark.asyncio
    async def test_history_empty_for_no_changes(self, client: httpx.AsyncClient) -> None:
        from app.engine.diff_engine import DiffEngine
        from app.scrapers.base import ScrapeResult

        def R(rid, raw):
            return ScrapeResult(
                source="epa_tsca",
                record_id=rid,
                title=rid,
                raw_content=raw,
                fetched_at=datetime.now(timezone.utc),
            )

        override = fastapi_app.dependency_overrides[get_session]
        async for session in override():
            eng = DiffEngine(session)
            await eng.process([R("Benzene", "v1")])
            await session.commit()
            stmt = select(RegulatoryRecord).where(RegulatoryRecord.record_id == "Benzene")
            rec = (await session.execute(stmt)).scalar_one()
            rec_id = str(rec.id)

        resp = await client.get(f"/records/{rec_id}/history")
        assert resp.status_code == 200
        assert resp.json() == []


# ── Scrape endpoint ─────────────────────────────────────────────────────


_SAMPLE_TSCA_HTML = """
<html><body>
<table class="tsca-inventory-table">
  <thead><tr><th>Chemical Name</th><th>CASRN</th><th>UVCB</th></tr></thead>
  <tbody>
    <tr><td>Benzene</td><td>71-43-2</td><td>No</td></tr>
    <tr><td>Formaldehyde</td><td>50-00-0</td><td>No</td></tr>
  </tbody>
</table>
</body></html>
"""


class TestScrapeEndpoint:
    @pytest.mark.asyncio
    async def test_scrape_unknown_source(self, client: httpx.AsyncClient) -> None:
        resp = await client.post("/scrape", json={"source": "unknown_agency"})
        assert resp.status_code == 400
        assert "Unknown source" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_scrape_epa_tsca_with_mocked_html(self, client: httpx.AsyncClient) -> None:
        import app.scrapers.epa_tsca as epa_module

        original = epa_module.EPATSCAScraper.scrape

        async def _mock(self):
            return epa_module.parse_tsca_html(_SAMPLE_TSCA_HTML)

        epa_module.EPATSCAScraper.scrape = _mock  # type: ignore[method-assign]

        try:
            resp = await client.post("/scrape", json={"source": "epa_tsca"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            report = data["report"]
            assert report["source"] == "epa_tsca"
            assert report["total_incoming"] == 2
            assert report["new_count"] == 2
            assert report["changed_count"] == 0
            assert report["has_changes"] is False
        finally:
            epa_module.EPATSCAScraper.scrape = original  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_scrape_then_diff_detects_change(self, client: httpx.AsyncClient) -> None:
        import app.scrapers.epa_tsca as epa_module

        original = epa_module.EPATSCAScraper.scrape
        html_v2 = _SAMPLE_TSCA_HTML.replace(
            "<td>Benzene</td><td>71-43-2</td><td>No</td>",
            "<td>Benzene</td><td>71-43-2</td><td>RESTRICTED</td>",
        )
        count = [0]

        async def _mock(self):
            count[0] += 1
            return epa_module.parse_tsca_html(html_v2 if count[0] > 1 else _SAMPLE_TSCA_HTML)

        epa_module.EPATSCAScraper.scrape = _mock  # type: ignore[method-assign]

        try:
            r1 = await client.post("/scrape", json={"source": "epa_tsca"})
            assert r1.json()["report"]["new_count"] == 2

            r2 = await client.post("/scrape", json={"source": "epa_tsca"})
            report = r2.json()["report"]
            assert report["changed_count"] == 1
            assert report["has_changes"] is True
            assert len(report["change_ids"]) == 1
        finally:
            epa_module.EPATSCAScraper.scrape = original  # type: ignore[method-assign]


# ── Webhooks — CRUD ─────────────────────────────────────────────────────


class TestWebhooks:
    @pytest.mark.asyncio
    async def test_subscribe_and_list(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            "/webhooks/subscribe",
            json={
                "callback_url": "https://example.com/hook",
                "agencies": ["epa_tsca"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["callback_url"] == "https://example.com/hook"
        assert data["is_active"] is True
        assert "secret" not in data

        resp2 = await client.get("/webhooks/subscriptions")
        assert resp2.status_code == 200
        assert len(resp2.json()) == 1

    @pytest.mark.asyncio
    async def test_unsubscribe(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            "/webhooks/subscribe",
            json={
                "callback_url": "https://example.com/hook",
                "agencies": [],
            },
        )
        sub_id = resp.json()["id"]

        resp2 = await client.delete(f"/webhooks/subscriptions/{sub_id}")
        assert resp2.status_code == 204

        resp3 = await client.get("/webhooks/subscriptions")
        assert resp3.json() == []

    @pytest.mark.asyncio
    async def test_unsubscribe_404(self, client: httpx.AsyncClient) -> None:
        resp = await client.delete("/webhooks/subscriptions/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404
