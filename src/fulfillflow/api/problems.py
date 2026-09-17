"""Core error translations over the common RFC 9457 HTTP boundary."""

from fastapi import FastAPI, Request
from starlette.responses import Response

from fulfillflow.carriers.public import CarrierNotFoundError
from fulfillflow.http.problems import _problem
from fulfillflow.http.problems import install_problem_handling as install_common_problems
from fulfillflow.orders.public import (
    InvalidOrderTransitionError,
    OrderExternalReferenceConflictError,
    OrderNotConfirmedError,
    OrderNotFoundError,
)
from fulfillflow.shipments.public import (
    InvalidShipmentTransitionError,
    ShipmentNotFoundError,
    ShipmentTrackingCodeConflictError,
)


def install_problem_handling(application: FastAPI) -> None:
    install_common_problems(application)
    for not_found_type in (
        OrderNotFoundError,
        ShipmentNotFoundError,
        CarrierNotFoundError,
    ):
        application.add_exception_handler(not_found_type, _not_found_handler)
    for conflict_type in (
        OrderExternalReferenceConflictError,
        ShipmentTrackingCodeConflictError,
    ):
        application.add_exception_handler(conflict_type, _conflict_handler)
    application.add_exception_handler(
        InvalidOrderTransitionError,
        _invalid_order_transition_handler,
    )
    application.add_exception_handler(
        OrderNotConfirmedError,
        _invalid_order_transition_handler,
    )
    application.add_exception_handler(
        InvalidShipmentTransitionError,
        _invalid_shipment_transition_handler,
    )


async def _not_found_handler(request: Request, exception: Exception) -> Response:
    return _problem(
        request,
        status_code=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail=str(exception),
    )


async def _conflict_handler(request: Request, exception: Exception) -> Response:
    return _problem(
        request,
        status_code=409,
        code="RESOURCE_CONFLICT",
        title="Resource conflict",
        detail=str(exception),
    )


async def _invalid_order_transition_handler(
    request: Request,
    exception: Exception,
) -> Response:
    return _problem(
        request,
        status_code=409,
        code="INVALID_ORDER_TRANSITION",
        title="Invalid order transition",
        detail=str(exception),
    )


async def _invalid_shipment_transition_handler(
    request: Request,
    exception: Exception,
) -> Response:
    return _problem(
        request,
        status_code=409,
        code="INVALID_SHIPMENT_TRANSITION",
        title="Invalid shipment transition",
        detail=str(exception),
    )
