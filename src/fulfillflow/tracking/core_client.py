"""Tracking's only access to Core: authenticated HTTP and immutable wire DTOs."""

from urllib.parse import quote, urlencode
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from fulfillflow.contracts.core import CarrierRead
from fulfillflow.contracts.problems import RemoteServiceUnavailableError
from fulfillflow.http.internal import ServiceClient, raise_for_service_problem

_CARRIER = TypeAdapter[CarrierRead | None](CarrierRead | None)
_CARRIERS = TypeAdapter[list[CarrierRead]](list[CarrierRead])


class CoreClient(ServiceClient):
    async def find_carrier(self, code: str, *, active_only: bool = False) -> CarrierRead | None:
        response = await self.request(
            "GET",
            f"/internal/v1/carriers/{quote(code, safe='')}",
            params={"active_only": str(active_only).lower()},
        )
        raise_for_service_problem(response)
        try:
            return _CARRIER.validate_json(response.content)
        except ValidationError as exc:
            raise RemoteServiceUnavailableError from exc

    async def carriers_by_ids(self, ids: set[UUID]) -> dict[UUID, CarrierRead]:
        if not ids:
            return {}
        query = urlencode([("ids", str(identifier)) for identifier in sorted(ids)])
        response = await self.request("GET", f"/internal/v1/carriers?{query}")
        raise_for_service_problem(response)
        try:
            carriers = _CARRIERS.validate_json(response.content)
        except ValidationError as exc:
            raise RemoteServiceUnavailableError from exc
        if {carrier.id for carrier in carriers} != ids:
            raise RemoteServiceUnavailableError
        return {carrier.id: carrier for carrier in carriers}

    async def require_shipment(self, shipment_id: UUID) -> None:
        response = await self.request("GET", f"/internal/v1/shipments/{shipment_id}")
        raise_for_service_problem(response)
        try:
            if TypeAdapter(UUID).validate_json(response.content) != shipment_id:
                raise RemoteServiceUnavailableError
        except ValidationError as exc:
            raise RemoteServiceUnavailableError from exc
