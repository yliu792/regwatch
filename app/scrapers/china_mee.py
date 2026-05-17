"""
China MEE (Ministry of Ecology and Environment) scraper.
"""

from __future__ import annotations

from app.scrapers.base import BaseScraper, ScrapeResult


class ChinaMEEScraper(BaseScraper):
    source: str = "china_mee"

    async def scrape(self) -> list[ScrapeResult]:
        client = await self._get_client()
        url = "https://www.mee.gov.cn/"
        resp = await client.get(url)
        resp.raise_for_status()

        # TODO: parse actual page content
        return [
            ScrapeResult(
                source=self.source,
                record_id="mee-homepage",
                title="MEE Homepage",
                raw_content=resp.text,
            )
        ]
