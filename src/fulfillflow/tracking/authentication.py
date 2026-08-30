"""Raw-byte webhook authentication without parsing supplier JSON."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime

_TIMESTAMP_PATTERN = re.compile(r"^[0-9]+$")
_SIGNATURE_PATTERN = re.compile(r"^sha256=([0-9a-fA-F]{64})$")
_EVENT_ID_PATTERN = re.compile(r"^[!-~]{1,128}$", flags=re.ASCII)


class InvalidWebhookSignatureError(ValueError):
    """Raised for missing or malformed authentication material."""


class StaleWebhookTimestampError(ValueError):
    """Raised when the signed timestamp is outside the replay window."""


@dataclass(frozen=True, slots=True)
class WebhookAuthentication:
    """Exact header values covered by the HMAC."""

    event_id: str
    timestamp: str
    signature: str


def compose_signed_payload(*, timestamp: str, event_id: str, raw_body: bytes) -> bytes:
    """Compose the protocol payload without rewriting any signed value."""
    return timestamp.encode("ascii") + b"." + event_id.encode("ascii") + b"." + raw_body


def calculate_signature(
    secret: str,
    *,
    timestamp: str,
    event_id: str,
    raw_body: bytes,
) -> str:
    """Return the protocol's prefixed HMAC-SHA256 signature."""
    digest = hmac.new(
        secret.encode("utf-8"),
        compose_signed_payload(timestamp=timestamp, event_id=event_id, raw_body=raw_body),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def authenticate_webhook(
    authentication: WebhookAuthentication,
    *,
    secret: str,
    raw_body: bytes,
    now: datetime,
    tolerance_seconds: int,
) -> None:
    """Verify protocol syntax, replay window and HMAC over the original bytes."""
    event_id = authentication.event_id
    timestamp = authentication.timestamp
    signature = authentication.signature

    if not _EVENT_ID_PATTERN.fullmatch(event_id):
        raise InvalidWebhookSignatureError(
            "Webhook event ID must contain only visible ASCII characters."
        )
    if not _TIMESTAMP_PATTERN.fullmatch(timestamp):
        raise InvalidWebhookSignatureError("Webhook timestamp must be Unix seconds.")
    signature_match = _SIGNATURE_PATTERN.fullmatch(signature)
    if signature_match is None:
        raise InvalidWebhookSignatureError("Webhook signature is missing or invalid.")

    try:
        timestamp_seconds = int(timestamp)
    except ValueError as exc:
        raise InvalidWebhookSignatureError("Webhook timestamp must be Unix seconds.") from exc
    now_seconds = now.timestamp()
    if not (
        now_seconds - tolerance_seconds <= timestamp_seconds <= now_seconds + tolerance_seconds
    ):
        raise StaleWebhookTimestampError("Webhook timestamp is outside the accepted window.")

    expected = calculate_signature(
        secret,
        timestamp=timestamp,
        event_id=event_id,
        raw_body=raw_body,
    )
    supplied = f"sha256={signature_match.group(1).lower()}"
    if not hmac.compare_digest(expected, supplied):
        raise InvalidWebhookSignatureError("Webhook signature is missing or invalid.")
