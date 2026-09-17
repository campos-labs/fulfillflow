"""Finite authenticated reads from Notifications without allocating Core SQL work."""

from datetime import datetime
from typing import cast
from uuid import UUID

import httpx
from fastapi import Request
from pydantic import BaseModel

from fulfillflow.config import Settings
from fulfillflow.contracts.notifications import (
    NotificationCounts,
    NotificationList,
    NotificationProgress,
    NotificationRead,
    NotificationStatus,
)
from fulfillflow.contracts.problems import RemoteServiceUnavailableError, ServiceProblemError
from fulfillflow.http.internal import (
    ServiceClient,
    query_params,
    raise_for_service_problem,
    trace_headers,
)


class NotificationsClient(ServiceClient):
    async def _read[T: BaseModel](
        self,
        path: str,
        schema: type[T],
        *,
        params: dict[str, str | int] | None = None,
        allow_missing: bool = False,
    ) -> T:
        try:
            response = await self.request("GET", path, params=params)
            media_type = response.headers.get("content-type", "").split(";", maxsplit=1)[0]
            if response.is_success:
                if media_type != "application/json":
                    raise RemoteServiceUnavailableError
                return schema.model_validate_json(response.content)
            if allow_missing and response.status_code == 404:
                if media_type == "application/problem+json":
                    raise_for_service_problem(response)
            raise RemoteServiceUnavailableError
        except ServiceProblemError as error:
            if allow_missing and error.status_code == 404 and error.code == "RESOURCE_NOT_FOUND":
                raise
            raise RemoteServiceUnavailableError from error
        except ValueError as error:
            raise RemoteServiceUnavailableError from error

    async def list(
        self,
        *,
        status: NotificationStatus | None = None,
        shipment_id: UUID | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> NotificationList:
        return await self._read(
            "/internal/v1/notifications",
            NotificationList,
            params=query_params(
                dict(
                    status=status,
                    shipment_id=shipment_id,
                    created_from=created_from,
                    created_to=created_to,
                    page=page,
                    page_size=page_size,
                )
            ),
        )

    async def get(self, notification_id: UUID) -> NotificationRead:
        result = await self._read(
            f"/internal/v1/notifications/{notification_id}", NotificationRead, allow_missing=True
        )
        if result.id != notification_id:
            raise RemoteServiceUnavailableError
        return result

    async def counts(self) -> NotificationCounts:
        return await self._read("/internal/v1/notification-counts", NotificationCounts)

    async def progress(self, event_id: UUID) -> NotificationProgress:
        result = await self._read(
            f"/internal/v1/notification-status/{event_id}", NotificationProgress
        )
        if result.tracking_event_id != event_id:
            raise RemoteServiceUnavailableError
        return result


def get_notifications(request: Request) -> NotificationsClient:
    settings = cast(Settings, request.app.state.settings)
    return NotificationsClient(
        cast(httpx.AsyncClient, request.app.state.notifications_client),
        settings,
        cast(UUID, request.state.request_id),
        trace_headers(request),
        secret=settings.notifications_api_secret,
        timeout_seconds=settings.notifications_http_timeout_seconds,
    )
