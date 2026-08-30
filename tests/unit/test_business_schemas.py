"""HTTP edge normalization and validation tests for business schemas."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from fulfillflow.orders.schemas import OrderCreate
from fulfillflow.shipments.schemas import ShipmentCreate


def test_order_create_normalizes_strings_and_state() -> None:
    payload = OrderCreate.model_validate(
        {
            "external_reference": " ORDER-001 ",
            "recipient": {
                "name": " Demo Recipient ",
                "email": " demo@example.test ",
                "postal_code": " 09700-000 ",
                "city": " Demo City ",
                "state": " sp ",
            },
        }
    )

    assert payload.external_reference == "ORDER-001"
    assert payload.recipient.name == "Demo Recipient"
    assert payload.recipient.email == "demo@example.test"
    assert payload.recipient.state == "SP"


@pytest.mark.parametrize(
    "change",
    [
        {"external_reference": ""},
        {
            "recipient": {
                "name": "",
                "email": "demo@example.test",
                "postal_code": "1",
                "city": "X",
                "state": "SP",
            }
        },
        {
            "recipient": {
                "name": "X",
                "email": "invalid",
                "postal_code": "1",
                "city": "X",
                "state": "SP",
            }
        },
        {
            "recipient": {
                "name": "X",
                "email": "demo@example.test",
                "postal_code": "1",
                "city": "X",
                "state": "S",
            }
        },
        {"unexpected": True},
    ],
)
def test_order_create_rejects_invalid_or_extra_values(change: dict[str, object]) -> None:
    valid: dict[str, object] = {
        "external_reference": "ORDER-001",
        "recipient": {
            "name": "Demo",
            "email": "demo@example.test",
            "postal_code": "1",
            "city": "X",
            "state": "SP",
        },
    }
    valid.update(change)

    with pytest.raises(ValidationError):
        OrderCreate.model_validate(valid)


def test_shipment_create_normalizes_carrier_and_tracking_codes() -> None:
    payload = ShipmentCreate.model_validate(
        {
            "order_id": "00000000-0000-4000-8000-000000000001",
            "carrier_code": " Carrier-Alpha ",
            "tracking_code": " alpha001 ",
        }
    )

    assert payload.order_id == UUID("00000000-0000-4000-8000-000000000001")
    assert payload.carrier_code == "carrier-alpha"
    assert payload.tracking_code == "ALPHA001"


@pytest.mark.parametrize("carrier_code", ["", "has space", "under_score"])
def test_shipment_create_rejects_invalid_carrier_slugs(carrier_code: str) -> None:
    with pytest.raises(ValidationError):
        ShipmentCreate.model_validate(
            {
                "order_id": "00000000-0000-4000-8000-000000000001",
                "carrier_code": carrier_code,
                "tracking_code": "TRACK001",
            }
        )
