"""
ECHA REACH (Registration, Evaluation, Authorisation of Chemicals) scraper.

Parses the ECHA website for substance entries and SVHC candidate list items.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from app.scrapers.base import BaseScraper, ScrapeResult

# ── Constants ───────────────────────────────────────────────────────────

ECHA_REACH_URL = "https://echa.europa.eu/regulations/reach/understanding-reach"
ECHA_SVHC_URL = "https://echa.europa.eu/candidate-list-table"

# EC numbers: e.g. "200-753-7", "231-791-2"
_EC_PATTERN = re.compile(r"\b\d{3}-\d{3}-\d\b")


# ── Parser ──────────────────────────────────────────────────────────────


def parse_echa_html(html: str) -> list[ScrapeResult]:
    """
    Parse ECHA REACH-related HTML and return a list of substance entries.

    Handles both standard tables (Candidate List) and card-style layouts.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[ScrapeResult] = []
    fetched_at = datetime.now(timezone.utc)

    # --- Strategy 1: table rows (SVHC Candidate List style) ---
    table = soup.find("table")
    if table is not None:
        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            data_cells = row.find_all("td")
            if not data_cells or len(cells) < 2:
                continue
            entry = _parse_table_row(cells, fetched_at)
            if entry is not None:
                results.append(entry)

    # --- Strategy 2: substance cards ---
    if not results:
        cards = soup.find_all(
            ["article", "div"], class_=re.compile(r"substance|infocard|entry", re.I)
        )
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

    # --- Fallback ---
    if not results:
        text = soup.get_text("\n", strip=True)
        if text:
            results.append(
                ScrapeResult(
                    source="echa_reach",
                    record_id="echa-reach-page",
                    title="REACH Regulation Overview",
                    raw_content=text,
                    fetched_at=fetched_at,
                )
            )

    return results


# ── Row / card helpers ──────────────────────────────────────────────────


def _parse_table_row(cells: list[Tag], fetched_at: datetime) -> ScrapeResult | None:
    """Extract a ScrapeResult from a Candidate List table row."""
    text_cells = [c.get_text(" ", strip=True) for c in cells]

    title = text_cells[0] if text_cells else ""
    if not title.strip():
        return None

    # EC number is typically in one of the cells.
    record_id = ""
    for tc in text_cells:
        ec_match = _EC_PATTERN.search(tc)
        if ec_match:
            record_id = ec_match.group(0)
            break
    if not record_id:
        record_id = title

    raw_content = " | ".join(text_cells)
    return ScrapeResult(
        source="echa_reach",
        record_id=record_id,
        title=title,
        raw_content=raw_content,
        fetched_at=fetched_at,
    )


def _parse_card(card: Tag, fetched_at: datetime) -> ScrapeResult | None:
    """Extract a ScrapeResult from an infocard-style element."""
    title_el = card.find(["h2", "h3", "h4", "strong"])
    title = title_el.get_text(" ", strip=True) if title_el else ""

    card_text = card.get_text("\n", strip=True)
    ec_match = _EC_PATTERN.search(card_text)
    record_id = ec_match.group(0) if ec_match else title

    if not title.strip():
        return None

    return ScrapeResult(
        source="echa_reach",
        record_id=record_id,
        title=title,
        raw_content=card_text,
        fetched_at=fetched_at,
    )


# ── Scraper ─────────────────────────────────────────────────────────────


class ECHAReachScraper(BaseScraper):
    source: str = "echa_reach"

    async def scrape(self) -> list[ScrapeResult]:
        client = await self._get_client()

        # Fetch the SVHC Candidate List first (more structured).
        resp = await client.get(ECHA_SVHC_URL)
        resp.raise_for_status()
        results = parse_echa_html(resp.text)

        # If no structured data found, try the main REACH page.
        if not results or (len(results) == 1 and results[0].record_id == "echa-reach-page"):
            resp2 = await client.get(ECHA_REACH_URL)
            resp2.raise_for_status()
            results = parse_echa_html(resp2.text)

        return results
