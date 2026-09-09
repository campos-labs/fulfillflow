"""Supported Tracking imports for the API composition layer."""

from fulfillflow.contracts.tracking import CarrierPayloadProjection
from fulfillflow.tracking.adapters import (
    AlphaCarrierAdapter,
    BetaCarrierAdapter,
    CarrierAdapter,
    CarrierAdapterNotAllowedError,
    UnknownExternalStatusError,
    normalize_carrier_event,
    project_known_carrier_payload,
    resolve_carrier_adapter,
    supported_adapter_keys,
)
from fulfillflow.tracking.authentication import (
    WebhookAuthentication,
    calculate_signature,
    compose_signed_payload,
)
from fulfillflow.tracking.carrier_schemas import (
    AlphaCarrierPayload,
    BetaCarrierEvent,
    BetaCarrierLocation,
    BetaCarrierPayload,
    CanonicalCarrierEvent,
    CanonicalShipmentStatus,
)
from fulfillflow.tracking.domain import (
    InboxStatus,
    ShipmentApplicationResult,
    ShipmentStatus,
)
from fulfillflow.tracking.errors import (
    PayloadTooLargeError,
    TrackingProblemError,
    UnsupportedWebhookMediaTypeError,
)
from fulfillflow.tracking.service import (
    CarrierEventNotFoundError,
    CarrierEventView,
    TrackingService,
    WebhookOutcome,
)

__all__ = [
    "AlphaCarrierAdapter",
    "AlphaCarrierPayload",
    "BetaCarrierAdapter",
    "BetaCarrierEvent",
    "BetaCarrierLocation",
    "BetaCarrierPayload",
    "CanonicalCarrierEvent",
    "CanonicalShipmentStatus",
    "CarrierAdapter",
    "CarrierAdapterNotAllowedError",
    "CarrierEventNotFoundError",
    "CarrierEventView",
    "CarrierPayloadProjection",
    "InboxStatus",
    "PayloadTooLargeError",
    "ShipmentApplicationResult",
    "ShipmentStatus",
    "TrackingProblemError",
    "TrackingService",
    "UnknownExternalStatusError",
    "UnsupportedWebhookMediaTypeError",
    "WebhookAuthentication",
    "WebhookOutcome",
    "calculate_signature",
    "compose_signed_payload",
    "normalize_carrier_event",
    "project_known_carrier_payload",
    "resolve_carrier_adapter",
    "supported_adapter_keys",
]
