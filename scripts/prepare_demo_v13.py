"""Prepare a small repeatable functional demo through the public Core API.

This is not the frozen benchmark dataset loader. It never deletes existing data.
"""

import argparse
import asyncio
import json
import sys
from typing import Any

import httpx

REFERENCE = "V13-FUNCTIONAL-DEMO-0001"
RECIPIENT = {
    "name": "Functional Demo Recipient",
    "email": "v13-demo@example.test",
    "postal_code": "09700-000",
    "city": "São Bernardo do Campo",
    "state": "SP",
}


async def prepare(client: httpx.AsyncClient) -> dict[str, Any]:
    """Resume creation using fixed public keys; reject conflicting existing records."""

    async def request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        return dict(response.json())

    orders = await request("GET", "/api/v1/orders", params={"external_reference": REFERENCE})
    matches = [item for item in orders["items"] if item["external_reference"] == REFERENCE]
    order = (
        matches[0]
        if matches
        else await request(
            "POST", "/api/v1/orders", json={"external_reference": REFERENCE, "recipient": RECIPIENT}
        )
    )
    if order["recipient"] != RECIPIENT or order["status"] == "CANCELLED":
        raise ValueError("The demo Order conflicts with the expected public data.")
    if order["status"] == "CREATED":
        order = await request("POST", f"/api/v1/orders/{order['id']}/confirm")
    shipments = []
    for carrier, code in (("carrier-alpha", "V13ALPHA0001"), ("carrier-beta", "V13BETA0001")):
        expected = {
            "order_id": order["id"],
            "carrier_code": carrier,
            "tracking_code": code,
            "estimated_delivery_date": "2026-09-15",
        }
        found = await request(
            "GET", "/api/v1/shipments", params={"carrier_code": carrier, "tracking_code": code}
        )
        matches = [item for item in found["items"] if item["tracking_code"] == code]
        shipment = (
            matches[0] if matches else await request("POST", "/api/v1/shipments", json=expected)
        )
        if any(shipment[key] != value for key, value in expected.items()):
            raise ValueError("A demo Shipment conflicts with the expected public data.")
        shipments.append({key: shipment[key] for key in ("id", "carrier_code", "tracking_code")})
    return {"order_id": order["id"], "external_reference": REFERENCE, "shipments": shipments}


async def _run(base_url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        return await prepare(client)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    arguments = parser.parse_args()
    try:
        result = asyncio.run(_run(arguments.base_url))
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        # Neither credentials nor the peer's unsanitized response are printed.
        print(f"Functional preparation failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
