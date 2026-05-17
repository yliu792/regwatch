"""
Webhooks API — manage subscriber registrations, persisted in the database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import WebhookRegistration
from app.schemas import WebhookRegistrationCreate, WebhookRegistrationOut

router = APIRouter()


@router.post("/subscribe", response_model=WebhookRegistrationOut, status_code=201)
async def subscribe(
    payload: WebhookRegistrationCreate,
    session: AsyncSession = Depends(get_session),
) -> WebhookRegistration:
    """Register a new webhook subscription."""
    registration = WebhookRegistration(
        callback_url=str(payload.callback_url),
        agencies=payload.agencies,
        secret=payload.secret,
    )
    session.add(registration)
    await session.commit()
    await session.refresh(registration)
    return registration


@router.get("/subscriptions", response_model=list[WebhookRegistrationOut])
async def list_subscriptions(
    active_only: bool = False,
    session: AsyncSession = Depends(get_session),
) -> list[WebhookRegistration]:
    """List all registered subscriptions."""
    stmt = select(WebhookRegistration).order_by(WebhookRegistration.created_at.desc())
    if active_only:
        stmt = stmt.where(WebhookRegistration.is_active == True)  # noqa: E712
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.delete("/subscriptions/{subscription_id}", status_code=204)
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
