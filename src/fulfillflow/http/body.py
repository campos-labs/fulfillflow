"""Bound request bytes before decoding or forwarding external content."""

from fastapi import Request

from fulfillflow.contracts.problems import ServiceProblemError


def payload_too_large() -> ServiceProblemError:
    return ServiceProblemError(
        status_code=413,
        code="PAYLOAD_TOO_LARGE",
        title="Webhook payload too large",
        detail="The webhook body exceeds the configured byte limit.",
    )


async def read_limited_body(request: Request, maximum_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum_bytes:
                raise payload_too_large()
        except ValueError:
            pass
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_bytes:
            raise payload_too_large()
        body.extend(chunk)
    return bytes(body)
