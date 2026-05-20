"""
Webhook dispatcher — deliver ChangeEvents to registered subscribers.

The :class:`WebhookDispatcher` loads active subscriptions from the database,
POSTs each ChangeEvent as JSON to matching webhook URLs, and marks events as
``dispatched = True`` on success.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChangeEvent, WebhookRegistration

logger = logging.getLogger(__name__)

# ── Public payload shape (sent to subscribers) ──────────────────────────

_WEBHOOK_PAYLOAD_VERSION = "1"


def _sign_payload(secret: str, payload: str) -> str:
    """Return an HMAC-SHA256 hex signature for *payload*."""
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _build_payload(event: ChangeEvent, source: str) -> dict:
    """Build the JSON-serializable webhook payload for a single ChangeEvent."""
    return {
        "version": _WEBHOOK_PAYLOAD_VERSION,
        "event_id": str(event.id),
        "source": source,
        "record_id": str(event.record_id),
        "previous_hash": event.previous_hash,
        "new_hash": event.new_hash,
        "diff_summary": event.diff_summary,
        "detected_at": event.detected_at.isoformat(),
    }


# ── Dispatcher ──────────────────────────────────────────────────────────


class WebhookDispatcher:
    """
    Loads active :class:`WebhookRegistration` rows for a given source,
    POSTs each :class:`ChangeEvent` to every matching subscriber, and
    marks successfully delivered events as ``dispatched = True``.

    Usage::

        dispatcher = WebhookDispatcher(session)
        results = await dispatcher.dispatch_all(report.changes, source)
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def dispatch_all(
        self,
        events: list[ChangeEvent],
        source: str,
        *,
        timeout: float = 10.0,
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        """
        Dispatch every *event* to every matching, active subscription.

        Returns a mapping of ``subscription_id → [event_ids]`` for
        successfully delivered events.
        """
        if not events:
            return {}

        subscriptions = await self._load_active_subscriptions(source)
        if not subscriptions:
            logger.debug("No active webhook subscriptions for source=%s", source)
            return {}

        results: dict[uuid.UUID, list[uuid.UUID]] = {}
        dispatched_event_ids: set[uuid.UUID] = set()

        async with httpx.AsyncClient(timeout=timeout) as client:
            for event in events:
                payload = _build_payload(event, source)
                payload_json: str | None = None  # lazily serialized

                for sub in subscriptions:
                    if not sub.is_active:
                        continue
                    if sub.agencies and source not in sub.agencies:
                        continue

                    if payload_json is None:
                        payload_json = _serialise(payload)

                    success = await self._post_one(client, sub, payload_json)
                    if success:
                        results.setdefault(sub.id, []).append(event.id)
                        dispatched_event_ids.add(event.id)
                        sub.last_delivered_at = datetime.now(timezone.utc)

        # Mark successfully dispatched events.
        if dispatched_event_ids:
            for event in events:
                if event.id in dispatched_event_ids:
                    event.dispatched = True
            await self._session.flush()

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_active_subscriptions(self, source: str) -> list[WebhookRegistration]:
        """
        Load subscriptions that are active and either have no agency filter
        or explicitly include *source*.
        """
        stmt = select(WebhookRegistration).where(
            WebhookRegistration.is_active == True  # noqa: E712
        )
        result = await self._session.execute(stmt)
        all_subs = result.scalars().all()

        # Filter in Python — an empty agencies list means "all agencies".
        return [s for s in all_subs if not s.agencies or source in s.agencies]

    async def _post_one(
        self,
        client: httpx.AsyncClient,
        sub: WebhookRegistration,
        payload_json: str,
    ) -> bool:
        """POST *payload_json* to a single subscriber.  Returns True on 2xx."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if sub.secret:
            headers["X-RegWatch-Signature"] = _sign_payload(sub.secret, payload_json)

        try:
            resp = await client.post(str(sub.callback_url), content=payload_json, headers=headers)
            if resp.is_success:
                logger.info(
                    "Webhook delivered: sub=%s event_count=1 status=%d",
                    sub.id,
                    resp.status_code,
                )
                return True
            else:
                logger.warning("Webhook to %s returned %d", sub.callback_url, resp.status_code)
                return False
        except httpx.RequestError as exc:
            logger.error("Webhook to %s failed: %s", sub.callback_url, exc)
            return False


def _serialise(payload: dict) -> str:
    """Fast JSON serialization without Pydantic overhead."""
    import json

    return json.dumps(payload, default=str)
