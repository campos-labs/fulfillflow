"""Known Tracking errors, translated through the common problem envelope."""

from fulfillflow.contracts.problems import ServiceProblemError


class TrackingProblemError(ServiceProblemError):
    """Permanent or resumable sanitized Tracking failure."""


class PayloadTooLargeError(TrackingProblemError):
    """Raised before authentication when the request exceeds its byte limit."""

    def __init__(self) -> None:
        super().__init__(
            status_code=413,
            code="PAYLOAD_TOO_LARGE",
            title="Webhook payload too large",
            detail="The webhook body exceeds the configured byte limit.",
        )


class UnsupportedWebhookMediaTypeError(TrackingProblemError):
    """Raised before authentication for a non-JSON content type."""

    def __init__(self) -> None:
        super().__init__(
            status_code=415,
            code="UNSUPPORTED_MEDIA_TYPE",
            title="Unsupported media type",
            detail="Carrier webhooks require Content-Type application/json.",
        )
