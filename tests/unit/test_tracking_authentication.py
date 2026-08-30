"""Raw-byte Carrier webhook authentication tests."""

from datetime import UTC, datetime

import pytest

from fulfillflow.tracking.authentication import (
    InvalidWebhookSignatureError,
    StaleWebhookTimestampError,
    WebhookAuthentication,
    authenticate_webhook,
    calculate_signature,
    compose_signed_payload,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
TIMESTAMP = str(int(NOW.timestamp()))
EVENT_ID = "alpha-evt-unicode"
SECRET = "distinct-alpha-unit-secret"


def test_signature_covers_exact_whitespace_and_unicode_bytes() -> None:
    raw_body = '{\n  "city": "São Paulo", "emoji": "🚚"\n}'.encode()
    compact_body = '{"city":"São Paulo","emoji":"🚚"}'.encode()
    signature = calculate_signature(
        SECRET,
        timestamp=TIMESTAMP,
        event_id=EVENT_ID,
        raw_body=raw_body,
    )

    assert compose_signed_payload(
        timestamp=TIMESTAMP,
        event_id=EVENT_ID,
        raw_body=raw_body,
    ).endswith(raw_body)
    authenticate_webhook(
        WebhookAuthentication(EVENT_ID, TIMESTAMP, signature),
        secret=SECRET,
        raw_body=raw_body,
        now=NOW,
        tolerance_seconds=300,
    )
    with pytest.raises(InvalidWebhookSignatureError):
        authenticate_webhook(
            WebhookAuthentication(EVENT_ID, TIMESTAMP, signature),
            secret=SECRET,
            raw_body=compact_body,
            now=NOW,
            tolerance_seconds=300,
        )


@pytest.mark.parametrize(
    ("event_id", "timestamp", "signature"),
    [
        ("", TIMESTAMP, "sha256=" + "0" * 64),
        ("alpha événement", TIMESTAMP, "sha256=" + "0" * 64),
        (" alpha-event", TIMESTAMP, "sha256=" + "0" * 64),
        ("alpha-event ", TIMESTAMP, "sha256=" + "0" * 64),
        ("alpha\tevent", TIMESTAMP, "sha256=" + "0" * 64),
        ("x" * 129, TIMESTAMP, "sha256=" + "0" * 64),
        (EVENT_ID, f" {TIMESTAMP}", "sha256=" + "0" * 64),
        (EVENT_ID, "9" * 5_000, "sha256=" + "0" * 64),
        (EVENT_ID, TIMESTAMP, "0" * 64),
        (EVENT_ID, TIMESTAMP, "sha256=not-hex"),
    ],
)
def test_malformed_authentication_values_are_rejected(
    event_id: str,
    timestamp: str,
    signature: str,
) -> None:
    with pytest.raises(InvalidWebhookSignatureError):
        authenticate_webhook(
            WebhookAuthentication(event_id, timestamp, signature),
            secret=SECRET,
            raw_body=b"{}",
            now=NOW,
            tolerance_seconds=300,
        )


@pytest.mark.parametrize("offset", [-301, 301])
def test_timestamp_outside_window_is_rejected(offset: int) -> None:
    timestamp = str(int(NOW.timestamp()) + offset)
    signature = calculate_signature(
        SECRET,
        timestamp=timestamp,
        event_id=EVENT_ID,
        raw_body=b"{}",
    )

    with pytest.raises(StaleWebhookTimestampError):
        authenticate_webhook(
            WebhookAuthentication(EVENT_ID, timestamp, signature),
            secret=SECRET,
            raw_body=b"{}",
            now=NOW,
            tolerance_seconds=300,
        )


@pytest.mark.parametrize("offset", [-300, 300])
def test_timestamp_at_window_boundary_is_accepted(offset: int) -> None:
    timestamp = str(int(NOW.timestamp()) + offset)
    signature = calculate_signature(
        SECRET,
        timestamp=timestamp,
        event_id=EVENT_ID,
        raw_body=b"{}",
    )

    authenticate_webhook(
        WebhookAuthentication(EVENT_ID, timestamp, signature),
        secret=SECRET,
        raw_body=b"{}",
        now=NOW,
        tolerance_seconds=300,
    )


def test_signature_uses_the_carrier_specific_secret() -> None:
    signature = calculate_signature(
        SECRET,
        timestamp=TIMESTAMP,
        event_id=EVENT_ID,
        raw_body=b"{}",
    )

    with pytest.raises(InvalidWebhookSignatureError):
        authenticate_webhook(
            WebhookAuthentication(EVENT_ID, TIMESTAMP, signature),
            secret="different-beta-unit-secret",
            raw_body=b"{}",
            now=NOW,
            tolerance_seconds=300,
        )
