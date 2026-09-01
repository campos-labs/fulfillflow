"""Strict HTML form and query validation at the web boundary."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.datastructures import FormData, UploadFile
from starlette.requests import ClientDisconnect
from starlette.types import Message

from fulfillflow.notifications.public import NotificationStatus
from fulfillflow.orders.public import OrderStatus
from fulfillflow.orders.schemas import OrderCreate
from fulfillflow.shipments.public import ShipmentStatus
from fulfillflow.shipments.schemas import ShipmentCreate
from fulfillflow.tracking.public import InboxStatus

MAX_FORM_BODY_BYTES = 32_768


@dataclass(frozen=True, slots=True)
class FormValues:
    """Single-valued submitted fields plus the CSRF token."""

    fields: dict[str, str]
    csrf_token: str | None


class FormBoundaryError(ValueError):
    """A sanitized structural form error that is safe to render."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class _QueryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def empty_string_is_none(cls, value: object) -> object:
        return None if value == "" else value


class OrderListQuery(_QueryModel):
    status: OrderStatus | None = None
    external_reference: str | None = Field(default=None, max_length=64)
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None
    page: int = Field(default=1, ge=1)


class ShipmentListQuery(_QueryModel):
    status: ShipmentStatus | None = None
    carrier_code: str | None = Field(default=None, max_length=32)
    order_external_reference: str | None = Field(default=None, max_length=64)
    tracking_code: str | None = Field(default=None, max_length=80)
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None
    page: int = Field(default=1, ge=1)


class ShipmentNewQuery(_QueryModel):
    order_id: UUID | None = None


class InboxListQuery(_QueryModel):
    carrier_code: str | None = Field(default=None, max_length=32)
    status: InboxStatus | None = None
    external_event_id: str | None = Field(default=None, max_length=128)
    received_from: AwareDatetime | None = None
    received_to: AwareDatetime | None = None
    page: int = Field(default=1, ge=1)


class NotificationListQuery(_QueryModel):
    status: NotificationStatus | None = None
    shipment_id: UUID | None = None
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None
    page: int = Field(default=1, ge=1)


async def read_strict_form(
    request: Request,
    *,
    fields: Collection[str],
) -> FormValues:
    """Read one small form while rejecting files, extras and duplicate fields."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            parsed_length = int(content_length)
        except ValueError:
            raise FormBoundaryError("The submitted form has an invalid length.") from None
        if parsed_length < 0:
            raise FormBoundaryError("The submitted form has an invalid length.")
        if parsed_length > MAX_FORM_BODY_BYTES:
            raise FormBoundaryError("The submitted form is too large.")

    raw_body = await _read_limited_body(request)
    submitted = await _parse_validated_form(
        request,
        raw_body,
        max_files=0,
        max_fields=len(fields) + 16,
        max_part_size=MAX_FORM_BODY_BYTES,
    )
    allowed = set(fields) | {"csrf_token"}
    if set(submitted) - allowed:
        raise FormBoundaryError("The form contains unexpected fields.")

    values: dict[str, str] = {}
    for name in fields:
        occurrences = submitted.getlist(name)
        if len(occurrences) > 1:
            raise FormBoundaryError("The form contains duplicate fields.")
        if not occurrences:
            values[name] = ""
            continue
        value = occurrences[0]
        if isinstance(value, UploadFile):
            raise FormBoundaryError("File uploads are not accepted.")
        values[name] = str(value)

    csrf_values = submitted.getlist("csrf_token")
    if len(csrf_values) != 1 or isinstance(csrf_values[0], UploadFile):
        csrf_token = None
    else:
        csrf_token = str(csrf_values[0])
    return FormValues(values, csrf_token)


async def _read_limited_body(request: Request) -> bytes:
    """Read the ASGI stream once and stop retaining data at the configured boundary."""
    chunks: list[bytes] = []
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_FORM_BODY_BYTES:
                raise FormBoundaryError("The submitted form is too large.")
            if chunk:
                chunks.append(chunk)
    except ClientDisconnect:
        raise FormBoundaryError("The form submission was interrupted.") from None
    return b"".join(chunks)


async def _parse_validated_form(
    request: Request,
    raw_body: bytes,
    *,
    max_files: int,
    max_fields: int,
    max_part_size: int,
) -> FormData:
    """Parse only the bounded bytes without consuming the original request stream again."""
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    buffered_request = Request(request.scope, receive=receive)
    return await buffered_request.form(
        max_files=max_files,
        max_fields=max_fields,
        max_part_size=max_part_size,
    )


def validate_order_form(values: Mapping[str, str]) -> OrderCreate:
    """Apply the existing public Order input schema to flat HTML fields."""
    return OrderCreate.model_validate(
        {
            "external_reference": values.get("external_reference", ""),
            "recipient": {
                "name": values.get("recipient_name", ""),
                "email": values.get("recipient_email", ""),
                "postal_code": values.get("recipient_postal_code", ""),
                "city": values.get("recipient_city", ""),
                "state": values.get("recipient_state", ""),
            },
        }
    )


def validate_shipment_form(values: Mapping[str, str]) -> ShipmentCreate:
    """Apply the existing public Shipment input schema to flat HTML fields."""
    estimated = values.get("estimated_delivery_date", "").strip() or None
    return ShipmentCreate.model_validate(
        {
            "order_id": values.get("order_id", ""),
            "carrier_code": values.get("carrier_code", ""),
            "tracking_code": values.get("tracking_code", ""),
            "estimated_delivery_date": estimated,
        }
    )


def validation_messages(error: ValidationError) -> dict[str, list[str]]:
    """Translate Pydantic locations to deterministic flat form field errors."""
    messages: dict[str, list[str]] = {}
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        location = tuple(str(part) for part in item["loc"])
        field = _flat_field(location)
        messages.setdefault(field, []).append(str(item["msg"]))
    return messages


def parse_query[QueryT: _QueryModel](
    model: type[QueryT],
    values: Mapping[str, str],
) -> QueryT:
    """Validate a single-valued query projection with no hidden attributes."""
    return model.model_validate(dict(values))


def query_values(request: Request, allowed: Collection[str]) -> dict[str, str]:
    """Reject repeated query keys and ignore no unknown operational filters."""
    extras = set(request.query_params) - set(allowed)
    if extras:
        raise FormBoundaryError("The query contains unexpected fields.")
    result: dict[str, str] = {}
    for name in allowed:
        occurrences = request.query_params.getlist(name)
        if len(occurrences) > 1:
            raise FormBoundaryError("The query contains duplicate fields.")
        if occurrences:
            result[name] = occurrences[0]
    return result


def aware_datetime(value: AwareDatetime | None) -> datetime | None:
    """Narrow Pydantic's aware datetime annotation for service dataclasses."""
    return value if isinstance(value, datetime) else None


def _flat_field(location: tuple[str, ...]) -> str:
    if len(location) == 2 and location[0] == "recipient":
        return f"recipient_{location[1]}"
    return location[-1] if location else "form"
