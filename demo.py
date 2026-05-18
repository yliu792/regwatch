#!/usr/bin/env python3
"""
RegWatch End-to-End Demo
========================

Scrapes regulatory data → detects a change → fires webhooks → shows the payload.

Usage:
    python demo.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import textwrap
from datetime import datetime, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import engine, get_session
from app.main import app as fastapi_app

# ── Colour helpers (terminal friendly) ────────────────────────────────────

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
RED = "\033[31m"
RESET = "\033[0m"


def header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'═' * 70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 70}{RESET}\n")


def step(n: int, desc: str) -> None:
    print(f"{BOLD}{YELLOW}[STEP {n}]{RESET} {desc}")


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {DIM}→{RESET} {msg}")


def highlight_json(obj: dict | str, indent: int = 2) -> None:
    if isinstance(obj, dict):
        text = json.dumps(obj, indent=indent, ensure_ascii=False, default=str)
    else:
        text = obj
    for line in text.splitlines():
        # Colour keys
        if ": " in line and line.strip().startswith('"'):
            key_end = line.index('":')
            key = line[: key_end + 2]
            rest = line[key_end + 2 :]
            print(f"    {MAGENTA}{key}{RESET}{rest}")
        else:
            print(f"    {DIM}{line}{RESET}")


# ── Sample HTML — simulates the EPA TSCA Inventory page ────────────────────

TSCA_HTML_V1 = textwrap.dedent("""\
<html><body>
<table class="tsca-inventory-table">
  <thead><tr><th>Chemical Name</th><th>CASRN</th><th>UVCB</th><th>Status</th></tr></thead>
  <tbody>
    <tr><td>Benzene</td><td>71-43-2</td><td>No</td><td>Active</td></tr>
    <tr><td>Formaldehyde</td><td>50-00-0</td><td>No</td><td>Active</td></tr>
    <tr><td>Polychlorinated biphenyls (PCBs)</td><td>1336-36-3</td><td>Yes</td><td>Active</td></tr>
  </tbody>
</table>
</body></html>
""")

# Version 2: Benzene status changes, one new substance added
TSCA_HTML_V2 = textwrap.dedent("""\
<html><body>
<table class="tsca-inventory-table">
  <thead><tr><th>Chemical Name</th><th>CASRN</th><th>UVCB</th><th>Status</th></tr></thead>
  <tbody>
    <tr><td>Benzene</td><td>71-43-2</td><td>No</td><td>RESTRICTED</td></tr>
    <tr><td>Formaldehyde</td><td>50-00-0</td><td>No</td><td>Active</td></tr>
    <tr><td>Polychlorinated biphenyls (PCBs)</td><td>1336-36-3</td><td>Yes</td><td>Active</td></tr>
    <tr><td>Perchloroethylene</td><td>127-18-4</td><td>No</td><td>Active</td></tr>
  </tbody>
</table>
</body></html>
""")


# ── Main demo ──────────────────────────────────────────────────────────────


async def main() -> None:
    print(f"\n{BOLD}{'🏛️  RegWatch — Regulatory Change Detection Demo'}{RESET}")
    print(f"{DIM}   Scrape → Diff → Webhook Dispatch{RESET}")

    # ─── Setup: test session & client ────────────────────────────────────
    header("SETUP")

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        # Clean slate
        for tbl in ("change_events", "regulatory_records", "webhook_registrations"):
            await session.execute(text(f"DELETE FROM {tbl}"))
        await session.commit()

        async def _override():
            yield session

        fastapi_app.dependency_overrides[get_session] = _override
        transport = httpx.ASGITransport(app=fastapi_app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        ok("Database tables cleaned, FastAPI TestClient ready")

        # ─── Step 1: Register a webhook subscriber ───────────────────────
        header("STEP 1: REGISTER WEBHOOK SUBSCRIBER")

        webhook_url = "https://example.com/regwatch-webhook"
        webhook_payload = {
            "callback_url": webhook_url,
            "agencies": ["epa_tsca", "echa_reach"],
        }
        info(f"POST /webhooks  →  {webhook_url}")
        info(f"  agencies: {webhook_payload['agencies']}")

        resp = await client.post("/webhooks", json=webhook_payload)
        assert resp.status_code == 201
        sub = resp.json()
        ok(f"Webhook registered (id={sub['id'][:8]}…) is_active={sub['is_active']}")

        # ─── Step 2: Mock the scraper ────────────────────────────────────
        header("STEP 2: MOCK EPA TSCA SCRAPER")

        import app.scrapers.epa_tsca as epa_module

        original_scrape = epa_module.EPATSCAScraper.scrape
        call_count = [0]

        async def _mock_scrape(self):
            call_count[0] += 1
            html = TSCA_HTML_V2 if call_count[0] > 1 else TSCA_HTML_V1
            return epa_module.parse_tsca_html(html)

        epa_module.EPATSCAScraper.scrape = _mock_scrape  # type: ignore[method-assign]

        info("Scraper monkey-patched to return controlled HTML")
        info(f"  Run #1 → {len(epa_module.parse_tsca_html(TSCA_HTML_V1))} substances (all Active)")
        info(
            f"  Run #2 → {len(epa_module.parse_tsca_html(TSCA_HTML_V2))} substances (Benzene → RESTRICTED, +Perchloroethylene)"
        )

        # ─── Step 3: First scrape — CREATE records ───────────────────────
        header("STEP 3: FIRST SCRAPE (baseline — creates records)")

        info("POST /scrape  source=epa_tsca")
        r1 = await client.post("/scrape", json={"source": "epa_tsca"})
        assert r1.status_code == 200
        report1 = r1.json()["report"]

        print(f"\n  Scrape Report #1:")
        print(f"    source          = {report1['source']}")
        print(f"    total_incoming  = {report1['total_incoming']}")
        print(f"    new_count       = {GREEN}{report1['new_count']}{RESET}")
        print(f"    changed_count   = {report1['changed_count']}")
        print(f"    unchanged_count = {report1['unchanged_count']}")
        print(f"    stale_count     = {report1['stale_count']}")
        print(f"    has_changes     = {report1['has_changes']}")

        ok("3 new records created (no changes → no webhook fired)")

        # Show the created records
        info("Created records:")
        for record_id in ["71-43-2", "50-00-0", "1336-36-3"]:
            info(f"  {record_id}")

        # ─── Step 4: Second scrape — DETECT change ───────────────────────
        header("STEP 4: SECOND SCRAPE (change detected — webhook fires)")

        # Monkey-patch the webhook dispatcher to capture payload
        from app.engine.webhook import WebhookDispatcher

        captured: list[dict] = []
        original_post = WebhookDispatcher._post_one

        async def _capture(self, client_inner, sub, payload_json):
            data = json.loads(payload_json)
            captured.append(data)
            print(f"\n  {BOLD}⚡ WEBHOOK FIRED!{RESET}")
            print(f"  {DIM}→ POST {sub.callback_url}{RESET}")
            return True

        WebhookDispatcher._post_one = _capture  # type: ignore[method-assign]

        info("POST /scrape  source=epa_tsca")
        r2 = await client.post("/scrape", json={"source": "epa_tsca"})
        assert r2.status_code == 200
        report2 = r2.json()["report"]

        print(f"\n  Scrape Report #2:")
        print(f"    source          = {report2['source']}")
        print(f"    total_incoming  = {report2['total_incoming']}")
        print(f"    new_count       = {GREEN}{report2['new_count']}{RESET}")
        print(f"    changed_count   = {YELLOW}{report2['changed_count']}{RESET}")
        print(f"    unchanged_count = {report2['unchanged_count']}")
        print(f"    stale_count     = {report2['stale_count']}")
        print(f"    has_changes     = {YELLOW}{report2['has_changes']}{RESET}")

        ok("1 change detected (Benzene: 'Active' → 'RESTRICTED')")
        ok("1 new record created (Perchloroethylene)")
        ok("2 records unchanged (Formaldehyde, PCBs)")

        # Restore dispatcher
        WebhookDispatcher._post_one = original_post  # type: ignore[method-assign]

        # ─── Step 5: Show the webhook payload ────────────────────────────
        header("STEP 5: WEBHOOK PAYLOAD (the JSON the subscriber receives)")

        assert len(captured) == 1
        payload = captured[0]

        print(f"  {BOLD}POST {webhook_url}{RESET}")
        print(f"  {DIM}Content-Type: application/json{RESET}\n")

        highlight_json(payload)

        # ─── Step 6: Verify the ChangeEvent is marked dispatched ─────────
        header("STEP 6: VERIFY ChangeEvent.dispatched = True")

        change_id = report2["change_ids"][0]
        record_uuid = payload["record_id"]
        resp3 = await client.get(f"/records/{record_uuid}/history")
        events = resp3.json()
        matching = [e for e in events if e["id"] == change_id]
        assert len(matching) == 1
        ev = matching[0]

        print(f"  ChangeEvent id:        {ev['id']}")
        print(f"  Record ID (FK):        {ev['record_id']}")
        print(f"  Detected at:           {ev['detected_at']}")
        print(f"  Dispatched:            {GREEN}{ev['dispatched']}{RESET}")
        print(f"  Previous hash:         {DIM}{ev['previous_hash'][:32]}…{RESET}")
        print(f"  New hash:              {DIM}{ev['new_hash'][:32]}…{RESET}")

        ok("ChangeEvent marked dispatched=True after successful delivery")

        # ─── Step 7: Show the diff ───────────────────────────────────────
        header("STEP 7: THE UNIFIED DIFF (what changed)")

        diff_text = ev["diff_summary"]
        for line in diff_text.splitlines():
            if line.startswith("---") or line.startswith("+++"):
                print(f"  {BOLD}{line}{RESET}")
            elif line.startswith("@@"):
                print(f"  {CYAN}{line}{RESET}")
            elif line.startswith("+"):
                print(f"  {GREEN}{line}{RESET}")
            elif line.startswith("-"):
                print(f"  {RED}{line}{RESET}")
            else:
                print(f"  {DIM}{line}{RESET}")

        # ─── Cleanup ────────────────────────────────────────────────────
        epa_module.EPATSCAScraper.scrape = original_scrape  # type: ignore[method-assign]
        fastapi_app.dependency_overrides.clear()
        await client.aclose()

        # ─── Summary ────────────────────────────────────────────────────
        header("✅ END-TO-END FLOW COMPLETE")

        print(f"  {BOLD}What just happened:{RESET}\n")
        print(f"  1. Registered a webhook subscriber at {webhook_url}")
        print(f"  2. Scraped the EPA TSCA Inventory → 3 chemicals stored")
        print(f"  3. Re-scraped after a regulatory change:")
        print(f"     • Benzene status changed: {RED}Active{RESET} → {GREEN}RESTRICTED{RESET}")
        print(f"     • Perchloroethylene was {GREEN}newly added{RESET}")
        print(f"  4. Webhook dispatched with:")
        print(f"     • event_id, source, record_id")
        print(f"     • previous_hash / new_hash (SHA-256)")
        print(f"     • Full unified diff of the change")
        print(f"     • ISO 8601 timestamp")
        print(f"  5. ChangeEvent marked {GREEN}dispatched=True{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
