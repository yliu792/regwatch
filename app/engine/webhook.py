"""
Webhook dispatcher — deliver diffs to registered subscribers.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timezone

import httpx

from app.models import WebhookRegistration
from app.schemas import DiffResult

logger = logging.getLogger(__name__)


def _sign_payload(secret: str, payload: str) -> str:
    """Return an HMAC-SHA256 hex signature for *payload*."""
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def dispatch(
    diff: DiffResult,
    source: str,
    subscriptions: list[WebhookRegistration],
    *,
    timeout: float = 10.0,
) -> dict[uuid.UUID, bool]:
    """
    Send *diff* to every matching, active subscription.

    Returns a mapping of ``subscription_id → delivery_success``.
    """
    payload = diff.model_dump_json()
    results: dict[uuid.UUID, bool] = {}

    async with httpx.AsyncClient(timeout=timeout) as client:
        for sub in subscriptions:
            if not sub.is_active:
                continue
            if sub.agencies and source not in sub.agencies:
                continue

            headers: dict[str, str] = {"Content-Type": "application/json"}
            if sub.secret:
                headers["X-RegWatch-Signature"] = _sign_payload(sub.secret, payload)

            success = False
            try:
                resp = await client.post(str(sub.callback_url), content=payload, headers=headers)
                success = resp.is_success
                if success:
                    sub.last_delivered_at = datetime.now(timezone.utc)
                else:
                    logger.warning("Webhook to %s returned %d", sub.callback_url, resp.status_code)
            except httpx.RequestError as exc:
                logger.error("Webhook to %s failed: %s", sub.callback_url, exc)

            results[sub.id] = success

    return results
