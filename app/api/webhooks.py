"""
Webhooks API — manage subscriber registrations, persisted in the database.

Endpoints:
- ``POST  /webhooks``               — register a new webhook
- ``GET   /webhooks``               — list all registered webhooks
- ``POST  /webhooks/subscribe``     — register (explicit path)
- ``GET   /webhooks/subscriptions`` — list subscriptions
- ``DELETE /webhooks/subscriptions/{id}`` — remove a subscription
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import WebhookRegistration
from app.schemas import WebhookRegistrationCreate, WebhookRegistrationOut

router = APIRouter()


# ── POST /webhooks ──────────────────────────────────────────────────────


@router.post("/webhooks", response_model=WebhookRegistrationOut, status_code=201)
async def create_webhook(
    payload: WebhookRegistrationCreate,
    session: AsyncSession = Depends(get_session),
) -> WebhookRegistration:
    """Register a new webhook subscription."""
    return await _register(payload, session)


# ── GET /webhooks ───────────────────────────────────────────────────────


@router.get("/webhooks", response_model=list[WebhookRegistrationOut])
async def list_webhooks(
    active_only: bool = Query(False, description="Only return active subscriptions"),
    session: AsyncSession = Depends(get_session),
) -> list[WebhookRegistration]:
    """List all registered webhook subscriptions."""
    return await _list_subs(session, active_only)


# ── POST /webhooks/subscribe ────────────────────────────────────────────


@router.post("/webhooks/subscribe", response_model=WebhookRegistrationOut, status_code=201)
async def subscribe(
    payload: WebhookRegistrationCreate,
    session: AsyncSession = Depends(get_session),
) -> WebhookRegistration:
    """Register a new webhook subscription (explicit path)."""
    return await _register(payload, session)


# ── GET /webhooks/subscriptions ─────────────────────────────────────────


@router.get("/webhooks/subscriptions", response_model=list[WebhookRegistrationOut])
async def list_subscriptions(
    active_only: bool = Query(False, description="Only return active subscriptions"),
    session: AsyncSession = Depends(get_session),
) -> list[WebhookRegistration]:
    """List all registered subscriptions (explicit path)."""
    return await _list_subs(session, active_only)


# ── DELETE /webhooks/subscriptions/{id} ─────────────────────────────────


@router.delete("/webhooks/subscriptions/{subscription_id}", status_code=204)
async def unsubscribe(
    subscription_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Remove a webhook subscription."""
    stmt = select(WebhookRegistration).where(WebhookRegistration.id == subscription_id)
    result = await session.execute(stmt)
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    await session.delete(sub)
    await session.commit()


# ── Internal helpers ────────────────────────────────────────────────────


async def _register(
    payload: WebhookRegistrationCreate,
    session: AsyncSession,
) -> WebhookRegistration:
    """Create and persist a WebhookRegistration."""
    registration = WebhookRegistration(
        callback_url=str(payload.callback_url),
        agencies=payload.agencies,
        secret=payload.secret,
    )
    session.add(registration)
    await session.commit()
    await session.refresh(registration)
    return registration


async def _list_subs(
    session: AsyncSession,
    active_only: bool = False,
) -> list[WebhookRegistration]:
    """Load subscriptions ordered by creation date."""
    stmt = select(WebhookRegistration).order_by(WebhookRegistration.created_at.desc())
    if active_only:
        stmt = stmt.where(WebhookRegistration.is_active == True)  # noqa: E712
    result = await session.execute(stmt)
    return list(result.scalars().all())
