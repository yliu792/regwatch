"""
Abstract base scraper — every agency scraper inherits from this.
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ScrapeResult:
    """Normalised result from a scraper run — one per regulatory entry."""

    source: str
    record_id: str  # source-specific identifier (e.g. CASRN, doc number)
    title: str
    raw_content: str
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def content_hash(self) -> str:
        """SHA-256 hex digest of *raw_content*."""
        return hashlib.sha256(self.raw_content.encode("utf-8")).hexdigest()


class BaseScraper(ABC):
    """Every regulatory scraper must implement ``scrape()``."""

    source: str = ""  # Override in subclass — e.g. "epa_tsca"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    @abstractmethod
    async def scrape(self) -> list[ScrapeResult]:
        """Fetch and parse the agency's regulatory page(s).  Returns parsed entries."""
        ...
