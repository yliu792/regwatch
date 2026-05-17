"""
Unit tests for the EPA TSCA Inventory parser.

Covers:
- Table-row parsing (strategy 1)
- Card-style layout parsing (strategy 2)
- Fallback page-level scraping
- CAS Registry Number extraction
- Edge cases (empty HTML, no CAS numbers, missing fields)
- HTTP-level integration via mocked httpx transport
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
import pytest

from app.scrapers.base import ScrapeResult
from app.scrapers.epa_tsca import _CAS_PATTERN, parse_tsca_html

# ── Fixtures ────────────────────────────────────────────────────────────

# Simulates a realistic EPA TSCA Inventory table with 3 chemical entries.
SAMPLE_TABLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>TSCA Chemical Substance Inventory | US EPA</title></head>
<body>
  <main>
    <h1>TSCA Chemical Substance Inventory</h1>
    <table class="tsca-inventory-table">
      <thead>
        <tr>
          <th>Chemical Name</th>
          <th>CAS Registry Number</th>
          <th>UVCB</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>Benzene</td>
          <td>71-43-2</td>
          <td>No</td>
        </tr>
        <tr>
          <td>Formaldehyde</td>
          <td>50-00-0</td>
          <td>No</td>
        </tr>
        <tr>
          <td>Polychlorinated biphenyls (PCBs)</td>
          <td>1336-36-3</td>
          <td>Yes</td>
        </tr>
      </tbody>
    </table>
  </main>
</body>
</html>
"""

# Simulates a modern card-style layout (as used by SPA-backed agency pages).
SAMPLE_CARDS_HTML = """
<!DOCTYPE html>
<html>
<body>
  <div class="chemical-card">
    <article class="substance-entry">
      <h2>1,4-Dioxane</h2>
      <p>CAS Registry Number: 123-91-1</p>
      <p>Status: Active</p>
    </article>
    <article class="substance-entry">
      <h2>Tetrachloroethylene (Perchloroethylene)</h2>
      <p>CASRN 127-18-4</p>
      <p>Status: Active</p>
    </article>
  </div>
</body>
</html>
"""

# No table or cards — should trigger the fallback.
SAMPLE_PLAIN_HTML = """
<html>
<body>
  <p>The TSCA Inventory contains thousands of chemical substances regulated under the Toxic Substances Control Act.</p>
</body>
</html>
"""

SAMPLE_EMPTY_HTML = "<html><body></body></html>"

# A single row with a CAS number embedded in descriptive text (not a dedicated column).
SAMPLE_ROW_WITH_EMBEDDED_CAS = """
<html><body>
<table class="tsca">
  <tr><td>Substance A — also known as Methyl ethyl ketone</td><td>Property data</td><td>See also CAS 78-93-3 for reference</td></tr>
</table>
</body></html>
"""

# A row with no CAS number anywhere.
SAMPLE_ROW_NO_CAS = """
<html><body>
<table class="tsca">
  <tr><td>Unknown Substance</td><td>Pending Review</td></tr>
</table>
</body></html>
"""


# ── CAS pattern tests ───────────────────────────────────────────────────


class TestCASPattern:
    """Low-level regex tests for CAS Registry Number extraction."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("71-43-2", "71-43-2"),
            ("50-00-0", "50-00-0"),
            ("1336-36-3", "1336-36-3"),
            ("CAS Number: 123-91-1", "123-91-1"),
            ("substance 64742-47-8 is", "64742-47-8"),
            ("no cas here", None),
            ("123-45-6789", None),  # check digit too long
            ("12-34-5", "12-34-5"),  # 2 digits is a valid CAS first segment
            ("1234567-89-0", "1234567-89-0"),  # 7 digits is a valid CAS first segment
        ],
    )
    def test_cas_extraction(self, text: str, expected: str | None) -> None:
        match = _CAS_PATTERN.search(text)
        if expected is None:
            assert match is None
        else:
            assert match is not None
            assert match.group(0) == expected


# ── Table-row parsing tests ─────────────────────────────────────────────


class TestTableParsing:
    """Parse EPA TSCA entries from standard <table> markup."""

    def test_parses_all_rows(self) -> None:
        results = parse_tsca_html(SAMPLE_TABLE_HTML)

        assert len(results) == 3

        # Entry 1 — Benzene
        assert results[0].source == "epa_tsca"
        assert results[0].record_id == "71-43-2"
        assert results[0].title == "Benzene"
        assert "71-43-2" in results[0].raw_content
        assert "No" in results[0].raw_content

        # Entry 2 — Formaldehyde
        assert results[1].record_id == "50-00-0"
        assert results[1].title == "Formaldehyde"

        # Entry 3 — PCBs
        assert results[2].record_id == "1336-36-3"
        assert "Polychlorinated biphenyls" in results[2].title

    def test_content_hash_is_stable(self) -> None:
        """Same content → same hash across calls."""
        results_a = parse_tsca_html(SAMPLE_TABLE_HTML)
        results_b = parse_tsca_html(SAMPLE_TABLE_HTML)

        assert len(results_a) == len(results_b)
        for ra, rb in zip(results_a, results_b):
            assert ra.content_hash == rb.content_hash

    def test_content_hash_differs_for_different_content(self) -> None:
        """Different content → different hash."""
        r1 = parse_tsca_html(SAMPLE_TABLE_HTML)
        r2 = parse_tsca_html(SAMPLE_CARDS_HTML)
        assert r1[0].content_hash != r2[0].content_hash

    def test_fetched_at_is_recent(self) -> None:
        results = parse_tsca_html(SAMPLE_TABLE_HTML)
        now = datetime.now(timezone.utc)
        for r in results:
            delta = (now - r.fetched_at).total_seconds()
            assert 0 <= delta < 10  # within 10 seconds

    def test_embedded_cas_extraction(self) -> None:
        """CAS number found in a non-dedicated column."""
        results = parse_tsca_html(SAMPLE_ROW_WITH_EMBEDDED_CAS)
        assert len(results) == 1
        assert results[0].record_id == "78-93-3"

    def test_row_without_cas_falls_back_to_name(self) -> None:
        """No CAS → record_id falls back to the title column text."""
        results = parse_tsca_html(SAMPLE_ROW_NO_CAS)
        assert len(results) == 1
        assert results[0].record_id == "Unknown Substance"


# ── Card-style parsing tests ────────────────────────────────────────────


class TestCardParsing:
    """Parse EPA TSCA entries from card-style markup."""

    def test_parses_all_cards(self) -> None:
        results = parse_tsca_html(SAMPLE_CARDS_HTML)

        assert len(results) == 2

        assert results[0].source == "epa_tsca"
        assert results[0].record_id == "123-91-1"
        assert results[0].title == "1,4-Dioxane"

        assert results[1].record_id == "127-18-4"
        assert "Tetrachloroethylene" in results[1].title


# ── Fallback tests ──────────────────────────────────────────────────────


class TestFallback:
    """When no structured markup is found, fall back to page-level scrape."""

    def test_plain_page_yields_single_result(self) -> None:
        results = parse_tsca_html(SAMPLE_PLAIN_HTML)

        assert len(results) == 1
        assert results[0].source == "epa_tsca"
        assert results[0].record_id == "tsca-inventory-page"
        assert "TSCA Chemical Substance Inventory" in results[0].title
        assert "Toxic Substances Control Act" in results[0].raw_content

    def test_empty_page_yields_no_results(self) -> None:
        results = parse_tsca_html(SAMPLE_EMPTY_HTML)
        assert results == []

    def test_blank_string_yields_no_results(self) -> None:
        results = parse_tsca_html("")
        assert results == []


# ── HTTP-level integration tests ────────────────────────────────────────


class TestHTTPIntegration:
    """Test the full scraper flow with mocked HTTP transport."""

    @pytest.mark.asyncio
    async def test_scrape_with_mocked_http(self) -> None:
        """The EPATSCAScraper.scrape() method fetches and parses HTML."""
        from app.scrapers.epa_tsca import EPATSCAScraper

        # Build mock transport that returns our sample table HTML.
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=SAMPLE_TABLE_HTML.encode("utf-8"))

        transport = httpx.MockTransport(_handler)
        client = httpx.AsyncClient(transport=transport)
        scraper = EPATSCAScraper(client=client)

        results = await scraper.scrape()

        assert len(results) == 3
        assert results[0].record_id == "71-43-2"
        assert results[1].record_id == "50-00-0"
        assert results[2].record_id == "1336-36-3"

        await client.aclose()

    @pytest.mark.asyncio
    async def test_scrape_http_error_propagates(self) -> None:
        """HTTP errors should raise normally."""
        from app.scrapers.epa_tsca import EPATSCAScraper

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, content=b"Service Unavailable")

        transport = httpx.MockTransport(_handler)
        client = httpx.AsyncClient(transport=transport)
        scraper = EPATSCAScraper(client=client)

        with pytest.raises(httpx.HTTPStatusError):
            await scraper.scrape()

        await client.aclose()

    @pytest.mark.asyncio
    async def test_scrape_with_card_layout(self) -> None:
        """Mock a response that uses the card layout."""
        from app.scrapers.epa_tsca import EPATSCAScraper

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=SAMPLE_CARDS_HTML.encode("utf-8"))

        transport = httpx.MockTransport(_handler)
        client = httpx.AsyncClient(transport=transport)
        scraper = EPATSCAScraper(client=client)

        results = await scraper.scrape()
        assert len(results) == 2
        assert results[0].record_id == "123-91-1"
        assert results[1].record_id == "127-18-4"

        await client.aclose()
