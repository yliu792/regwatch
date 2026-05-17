"""
EPA TSCA (Toxic Substances Control Act) Inventory scraper.

Fetches the TSCA Chemical Substance Inventory page and parses individual
chemical entries from the HTML.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from app.scrapers.base import BaseScraper, ScrapeResult

# ── Constants ───────────────────────────────────────────────────────────

TSCA_INVENTORY_URL = "https://www.epa.gov/tsca-inventory"

# Pattern for CAS Registry Numbers: e.g. "1336-36-3" or "64742-47-8"
_CAS_PATTERN = re.compile(r"\b\d{2,7}-\d{2}-\d\b")


# ── Parser (pure function — easily testable) ────────────────────────────


def parse_tsca_html(html: str, *, base_url: str = TSCA_INVENTORY_URL) -> list[ScrapeResult]:
    """
    Parse the EPA TSCA Inventory HTML and return a list of scraped entries.

    Each entry corresponds to a chemical substance row in the inventory table.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[ScrapeResult] = []
    fetched_at = datetime.now(timezone.utc)

    # --- Strategy 1: standard <table> rows ---
    table = soup.find("table", class_=re.compile(r"tsca|inventory|chemical", re.I))
    if table is not None:
        # Prefer <tbody> rows; fall back to all rows, skipping <thead>.
        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            # Skip header rows (all-<th> or fewer than 2 data cells).
            data_cells = row.find_all("td")
            if not data_cells or len(cells) < 2:
                continue

            entry = _parse_row(cells, fetched_at)
            if entry is not None:
                results.append(entry)

    # --- Strategy 2: <div> / <article> cards (modern SPA-style markup) ---
    if not results:
        cards = soup.find_all(
            ["article", "div"], class_=re.compile(r"chemical|substance|entry", re.I)
        )
        # Filter out containers whose children are also matched (deduplicate).
        card_ids = {id(c) for c in cards}
        leaf_cards = [
            c
            for c in cards
            if not any(id(child) in card_ids for child in c.find_all(["article", "div"]))
        ]
        for card in leaf_cards:
            entry = _parse_card(card, fetched_at)
            if entry is not None:
                results.append(entry)

    # --- Fallback: page-level scrape ---
    if not results:
        text = soup.get_text("\n", strip=True)
        if text:
            results.append(
                ScrapeResult(
                    source="epa_tsca",
                    record_id="tsca-inventory-page",
                    title="TSCA Chemical Substance Inventory",
                    raw_content=text,
                    fetched_at=fetched_at,
                )
            )

    return results


# ── Row / card helpers ──────────────────────────────────────────────────


def _parse_row(cells: list[Tag], fetched_at: datetime) -> ScrapeResult | None:
    """Extract a ScrapeResult from a <tr> row's cells."""
    text_cells = [c.get_text(" ", strip=True) for c in cells]

    # Heuristic: first cell = name/title, second cell = CASRN/id
    title = text_cells[0] if len(text_cells) > 0 else ""
    record_id = ""

    for tc in text_cells:
        cas_match = _CAS_PATTERN.search(tc)
        if cas_match:
            record_id = cas_match.group(0)
            break

    if not record_id:
        # Fall back to first column as identifier
        record_id = text_cells[0] if text_cells else "unknown"

    if not title.strip():
        return None

    raw_content = " | ".join(text_cells)

    return ScrapeResult(
        source="epa_tsca",
        record_id=record_id,
        title=title,
        raw_content=raw_content,
        fetched_at=fetched_at,
    )


def _parse_card(card: Tag, fetched_at: datetime) -> ScrapeResult | None:
    """Extract a ScrapeResult from a card-style <article> or <div>."""
    title_el = card.find(["h2", "h3", "h4", "strong"])
    title = title_el.get_text(" ", strip=True) if title_el else ""

    card_text = card.get_text("\n", strip=True)
    cas_match = _CAS_PATTERN.search(card_text)
    record_id = cas_match.group(0) if cas_match else title

    if not title.strip():
        return None

    return ScrapeResult(
        source="epa_tsca",
        record_id=record_id,
        title=title,
        raw_content=card_text,
        fetched_at=fetched_at,
    )


# ── Scraper ─────────────────────────────────────────────────────────────


class EPATSCAScraper(BaseScraper):
    source: str = "epa_tsca"

    async def scrape(self) -> list[ScrapeResult]:
        client = await self._get_client()
        resp = await client.get(TSCA_INVENTORY_URL)
        resp.raise_for_status()
        return parse_tsca_html(resp.text)
