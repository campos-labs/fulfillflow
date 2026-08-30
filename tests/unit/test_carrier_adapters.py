"""Carrier-specific schema validation and canonical normalization tests."""

from copy import deepcopy
from datetime import datetime

import pytest
from pydantic import ValidationError

from fulfillflow.carriers.public import (
    AlphaCarrierAdapter,
    BetaCarrierAdapter,
    CanonicalShipmentStatus,
    CarrierAdapterNotAllowedError,
    UnknownExternalStatusError,
    normalize_carrier_event,
    resolve_carrier_adapter,
    supported_adapter_keys,
)


def test_alpha_normalizes_documented_payload_without_mutating_it() -> None:
    payload: dict[str, object] = {
        "eventId": "alpha-evt-000001",
        "trackingCode": " alpha000001 ",
        "status": "MOVING",
        "eventDate": "2026-08-28T15:20:00Z",
        "city": " São Bernardo do Campo ",
        "description": " Transferência entre unidades ",
        "supplierExtension": {"kept": "outside-canonical-event"},
    }
    original = deepcopy(payload)

    event = AlphaCarrierAdapter().normalize(payload)

    assert payload == original
    assert event.external_event_id == "alpha-evt-000001"
    assert event.tracking_code == "ALPHA000001"
    assert event.canonical_status is CanonicalShipmentStatus.IN_TRANSIT
    assert event.external_status == "MOVING"
    assert event.occurred_at == datetime.fromisoformat("2026-08-28T15:20:00+00:00")
    assert event.description == "Transferência entre unidades"
    assert event.location == "São Bernardo do Campo"
    assert not hasattr(event, "supplierExtension")


def test_beta_normalizes_documented_nested_payload() -> None:
    event = BetaCarrierAdapter().normalize(
        {
            "id": "beta-evt-000001",
            "tracking_number": " beta000001 ",
            "event": {
                "type": "hub_scan",
                "occurred_at": "2026-08-28T15:20:00-03:00",
                "details": " Recebido no centro de distribuição ",
                "supplier_event_field": True,
            },
            "location": {
                "city": " São Paulo ",
                "state": " sp ",
                "supplier_location_field": 1,
            },
            "supplier_root_field": "ignored",
        }
    )

    assert event.external_event_id == "beta-evt-000001"
    assert event.tracking_code == "BETA000001"
    assert event.canonical_status is CanonicalShipmentStatus.IN_TRANSIT
    assert event.external_status == "hub_scan"
    assert event.occurred_at == datetime.fromisoformat("2026-08-28T15:20:00-03:00")
    assert event.description == "Recebido no centro de distribuição"
    assert event.location == "São Paulo, SP"


@pytest.mark.parametrize(
    ("external_status", "canonical_status"),
    [
        ("CREATED", CanonicalShipmentStatus.POSTED),
        ("MOVING", CanonicalShipmentStatus.IN_TRANSIT),
        ("OUT_FOR_DELIVERY", CanonicalShipmentStatus.OUT_FOR_DELIVERY),
        ("DELIVERED", CanonicalShipmentStatus.DELIVERED),
        ("PROBLEM", CanonicalShipmentStatus.EXCEPTION),
        ("RETURNED", CanonicalShipmentStatus.RETURNED),
    ],
)
def test_alpha_maps_every_documented_status(
    external_status: str,
    canonical_status: CanonicalShipmentStatus,
) -> None:
    event = normalize_carrier_event(
        "alpha",
        {
            "eventId": "alpha-event",
            "trackingCode": "ALPHA001",
            "status": external_status,
            "eventDate": "2026-08-28T15:20:00Z",
        },
    )

    assert event.canonical_status is canonical_status


@pytest.mark.parametrize(
    ("external_status", "canonical_status"),
    [
        ("label_created", CanonicalShipmentStatus.POSTED),
        ("hub_scan", CanonicalShipmentStatus.IN_TRANSIT),
        ("courier_route", CanonicalShipmentStatus.OUT_FOR_DELIVERY),
        ("completed", CanonicalShipmentStatus.DELIVERED),
        ("delivery_issue", CanonicalShipmentStatus.EXCEPTION),
        ("returned_origin", CanonicalShipmentStatus.RETURNED),
    ],
)
def test_beta_maps_every_documented_status(
    external_status: str,
    canonical_status: CanonicalShipmentStatus,
) -> None:
    event = normalize_carrier_event(
        "beta",
        {
            "id": "beta-event",
            "tracking_number": "BETA001",
            "event": {
                "type": external_status,
                "occurred_at": "2026-08-28T15:20:00Z",
            },
        },
    )

    assert event.canonical_status is canonical_status
    assert event.location is None


@pytest.mark.parametrize(
    ("adapter_key", "payload", "external_status"),
    [
        (
            "alpha",
            {
                "eventId": "alpha-event",
                "trackingCode": "ALPHA001",
                "status": "LOST_IN_SPACE",
                "eventDate": "2026-08-28T15:20:00Z",
            },
            "LOST_IN_SPACE",
        ),
        (
            "beta",
            {
                "id": "beta-event",
                "tracking_number": "BETA001",
                "event": {
                    "type": "unknown_scan",
                    "occurred_at": "2026-08-28T15:20:00Z",
                },
            },
            "unknown_scan",
        ),
    ],
)
def test_unknown_external_status_has_stable_typed_error(
    adapter_key: str,
    payload: dict[str, object],
    external_status: str,
) -> None:
    with pytest.raises(UnknownExternalStatusError) as captured:
        normalize_carrier_event(adapter_key, payload)

    assert captured.value.adapter_key == adapter_key
    assert captured.value.external_status == external_status
    assert external_status not in str(captured.value)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "eventId": "alpha-event",
            "trackingCode": "ALPHA001",
            "status": "MOVING",
            "eventDate": "2026-08-28T15:20:00",
        },
        {
            "eventId": " ",
            "trackingCode": "ALPHA001",
            "status": "MOVING",
            "eventDate": "2026-08-28T15:20:00Z",
        },
        {
            "eventId": "alpha-évent",
            "trackingCode": "ALPHA001",
            "status": "MOVING",
            "eventDate": "2026-08-28T15:20:00Z",
        },
        {
            "eventId": " alpha-event",
            "trackingCode": "ALPHA001",
            "status": "MOVING",
            "eventDate": "2026-08-28T15:20:00Z",
        },
        {
            "eventId": "alpha-event",
            "trackingCode": " ",
            "status": "MOVING",
            "eventDate": "2026-08-28T15:20:00Z",
        },
    ],
)
def test_alpha_rejects_naive_timestamp_and_empty_required_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AlphaCarrierAdapter().normalize(payload)


def test_alpha_requires_the_documented_external_field_names() -> None:
    with pytest.raises(ValidationError):
        AlphaCarrierAdapter().normalize(
            {
                "event_id": "alpha-event",
                "tracking_code": "ALPHA001",
                "status": "MOVING",
                "event_date": "2026-08-28T15:20:00Z",
            }
        )


def test_static_allowlist_resolves_only_alpha_and_beta() -> None:
    assert supported_adapter_keys() == frozenset({"alpha", "beta"})
    assert resolve_carrier_adapter("alpha").carrier_code == "carrier-alpha"
    assert resolve_carrier_adapter("beta").carrier_code == "carrier-beta"

    with pytest.raises(CarrierAdapterNotAllowedError) as captured:
        resolve_carrier_adapter("fulfillflow.evil.import_path")

    assert captured.value.adapter_key == "fulfillflow.evil.import_path"
