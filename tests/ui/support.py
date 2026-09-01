"""UI journey helpers that use only normal HTTP interfaces."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from httpx import AsyncClient, Response

from fulfillflow.config import Settings
from fulfillflow.tracking.public import calculate_signature

_CSRF_PATTERN = re.compile(r'name="csrf_token" value="([^"<>]+)"')


def csrf_token(response: Response) -> str:
    match = _CSRF_PATTERN.search(response.text)
    assert match is not None
    return match.group(1)


async def create_order(
    client: AsyncClient,
    *,
    reference: str = "UI-ORDER-0001",
    name: str = "UI Demonstration Recipient",
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/orders",
        json={
            "external_reference": reference,
            "recipient": {
                "name": name,
                "email": "ui-recipient@example.test",
                "postal_code": "09700-000",
                "city": "São Bernardo do Campo",
                "state": "SP",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def confirm_order(client: AsyncClient, order_id: str) -> dict[str, Any]:
    response = await client.post(f"/api/v1/orders/{order_id}/confirm")
    assert response.status_code == 200, response.text
    return response.json()


async def create_shipment(
    client: AsyncClient,
    order_id: str,
    *,
    carrier: str = "carrier-alpha",
    tracking_code: str = "UIALPHA0001",
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/shipments",
        json={
            "order_id": order_id,
            "carrier_code": carrier,
            "tracking_code": tracking_code,
            "estimated_delivery_date": "2026-09-05",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def send_alpha_event(
    client: AsyncClient,
    settings: Settings,
    *,
    event_id: str,
    tracking_code: str,
    external_status: str,
    occurred_at: datetime,
    description: str = "Synthetic UI tracking event",
    extra: dict[str, object] | None = None,
) -> Response:
    payload: dict[str, object] = {
        "eventId": event_id,
        "trackingCode": tracking_code,
        "status": external_status,
        "eventDate": occurred_at.isoformat().replace("+00:00", "Z"),
        "city": "São Bernardo do Campo",
        "description": description,
    }
    payload.update(extra or {})
    raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = str(int(occurred_at.replace(hour=12, minute=0, second=0).timestamp()))
    signature = calculate_signature(
        settings.carrier_alpha_webhook_secret.get_secret_value(),
        timestamp=timestamp,
        event_id=event_id,
        raw_body=raw_body,
    )
    return await client.post(
        "/api/v1/carriers/carrier-alpha/events",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-FulfillFlow-Event-Id": event_id,
            "X-FulfillFlow-Timestamp": timestamp,
            "X-FulfillFlow-Signature": signature,
        },
    )
