"""Authenticated, read-only Notifications HTTP projections."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.contracts.notifications import (
    NotificationCounts,
    NotificationList,
    NotificationProgress,
    NotificationRead,
    NotificationStatus,
)
from fulfillflow.http.dependencies import get_clock, get_session
from fulfillflow.notifications.owned_service import OwnedNotificationService
from fulfillflow.shared import Clock

router = APIRouter(prefix="/internal/v1", include_in_schema=False)
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
ClockDependency = Annotated[Clock, Depends(get_clock)]


@router.get("/notifications", response_model=NotificationList)
async def list_notifications(
    session: SessionDependency,
    clock: ClockDependency,
    status: NotificationStatus | None = None,
    shipment_id: UUID | None = None,
    created_from: AwareDatetime | None = None,
    created_to: AwareDatetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> NotificationList:
    return await OwnedNotificationService(session, clock).list(
        status=status,
        shipment_id=shipment_id,
        created_from=created_from,
        created_to=created_to,
        page=page,
        page_size=page_size,
    )


@router.get("/notifications/{notification_id}", response_model=NotificationRead)
async def get_notification(
    notification_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
) -> NotificationRead:
    return await OwnedNotificationService(session, clock).get(notification_id)


@router.get("/notification-counts", response_model=NotificationCounts)
async def notification_counts(
    session: SessionDependency,
    clock: ClockDependency,
) -> NotificationCounts:
    return await OwnedNotificationService(session, clock).counts()


@router.get("/notification-status/{tracking_event_id}", response_model=NotificationProgress)
async def notification_progress(
    tracking_event_id: UUID,
    session: SessionDependency,
    clock: ClockDependency,
) -> NotificationProgress:
    return await OwnedNotificationService(session, clock).progress(tracking_event_id)
