"""
China MEE (Ministry of Ecology and Environment) scraper.

Fetches regulatory notices and standards from the MEE website's public
pages and parses individual entries from the HTML.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from app.scrapers.base import BaseScraper, ScrapeResult

# ── Constants ───────────────────────────────────────────────────────────

MEE_BASE_URL = "https://www.mee.gov.cn"
MEE_NOTICES_URL = f"{MEE_BASE_URL}/ywdt/tpxw/"  # Latest news / regulatory notices

# Document number pattern for MEE announcements, e.g. "公告 2024年 第1号" or "环办〔2024〕10号"
_DOCNO_PATTERN = re.compile(
    r"(?:公告|环办|环发|部令|国环规).*?(?:第[一二三四五六七八九十\d]+号|〔\d{4}〕\d+号)",
    re.DOTALL,
)

# Date patterns: "2024-01-15" or "2024年1月15日"
_DATE_PATTERN = re.compile(r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?")


# ── Parser (pure function — easily testable) ────────────────────────────


def parse_mee_html(html: str, *, base_url: str = MEE_NOTICES_URL) -> list[ScrapeResult]:
    """
    Parse the MEE regulatory notices HTML and return a list of scraped entries.

    Handles:
    - List-style layouts (``<ul>/<li>`` with title links)
    - Table rows
    - Card / article layouts
    - Fallback page‑level scraping
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[ScrapeResult] = []
    fetched_at = datetime.now(timezone.utc)

    # --- Strategy 1: list items (most common for Chinese gov portals) ---
    list_items = soup.find_all("li")
    if list_items:
        for li in list_items:
            entry = _parse_list_item(li, fetched_at)
            if entry is not None:
                results.append(entry)

    # --- Strategy 2: table rows ---
    if not results:
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

    # --- Strategy 3: card / article blocks ---
    if not results:
        cards = soup.find_all(
            ["article", "div"], class_=re.compile(r"notice|news|article|entry|content", re.I)
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

    # --- Fallback: page-level scrape ---
    if not results:
        text = soup.get_text("\n", strip=True)
        if text:
            results.append(
                ScrapeResult(
                    source="china_mee",
                    record_id="mee-homepage",
                    title="MEE Homepage",
                    raw_content=text,
                    fetched_at=fetched_at,
                )
            )

    return results


# ── Row / item / card helpers ───────────────────────────────────────────


def _parse_list_item(li: Tag, fetched_at: datetime) -> ScrapeResult | None:
    """Extract a ScrapeResult from a ``<li>`` item (title link + optional date)."""
    link = li.find("a", href=True)
    if link is None:
        return None

    title = link.get_text(" ", strip=True)
    if not title or len(title) < 4:
        return None

    # Build raw_content from link text + any sibling text
    parts = [title]
    for child in li.children:
        text = child.get_text(" ", strip=True) if hasattr(child, "get_text") else ""
        if text and text not in parts:
            parts.append(text)
    raw_content = " | ".join(parts)

    # Try to extract a document number as record_id
    record_id = _extract_docno(raw_content) or _slugify(title)

    return ScrapeResult(
        source="china_mee",
        record_id=record_id,
        title=title,
        raw_content=raw_content,
        fetched_at=fetched_at,
    )


def _parse_table_row(cells: list[Tag], fetched_at: datetime) -> ScrapeResult | None:
    """Extract a ScrapeResult from a table row."""
    text_cells = [c.get_text(" ", strip=True) for c in cells]
    title = text_cells[0] if text_cells else ""
    if not title.strip():
        return None

    raw_content = " | ".join(text_cells)
    record_id = _extract_docno(raw_content) or _slugify(title)

    return ScrapeResult(
        source="china_mee",
        record_id=record_id,
        title=title,
        raw_content=raw_content,
        fetched_at=fetched_at,
    )


def _parse_card(card: Tag, fetched_at: datetime) -> ScrapeResult | None:
    """Extract a ScrapeResult from a card-style element."""
    title_el = card.find(["h2", "h3", "h4", "strong", "a"])
    title = title_el.get_text(" ", strip=True) if title_el else ""
    card_text = card.get_text("\n", strip=True)

    if not title.strip() or len(title) < 4:
        return None

    record_id = _extract_docno(card_text) or _slugify(title)

    return ScrapeResult(
        source="china_mee",
        record_id=record_id,
        title=title,
        raw_content=card_text,
        fetched_at=fetched_at,
    )


# ── Identifier helpers ──────────────────────────────────────────────────


def _extract_docno(text: str) -> str | None:
    """Try to extract a document/announcement number from *text*."""
    match = _DOCNO_PATTERN.search(text)
    if match:
        return match.group(0).strip()
    return None


def _slugify(title: str, max_len: int = 64) -> str:
    """Create a URL‑friendly slug from *title* for use as a fallback record_id."""
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", title.strip()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "mee-entry"


# ── Scraper ─────────────────────────────────────────────────────────────


class ChinaMEEScraper(BaseScraper):
    source: str = "china_mee"

    async def scrape(self) -> list[ScrapeResult]:
        client = await self._get_client()

        # Fetch the latest notices page first (most structured).
        resp = await client.get(MEE_NOTICES_URL)
        resp.raise_for_status()
        results = parse_mee_html(resp.text)

        # If nothing structured was found, try the homepage as fallback.
        if not results:
            resp2 = await client.get(MEE_BASE_URL)
            resp2.raise_for_status()
            results = parse_mee_html(resp2.text)

        return results
