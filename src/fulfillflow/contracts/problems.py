"""Sanitized application error values shared by the two HTTP boundaries."""


class ServiceProblemError(Exception):
    """An explicitly safe problem, never a raw infrastructure exception."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        title: str,
        detail: str,
        reject_inbox: bool = False,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.title = title
        self.detail = detail
        self.reject_inbox = reject_inbox
        super().__init__(detail)


class RemoteServiceUnavailableError(ServiceProblemError):
    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            code="SERVICE_UNAVAILABLE",
            title="Service unavailable",
            detail="An internal service could not complete the request.",
        )


class EventIdentityConflictError(ServiceProblemError):
    def __init__(self) -> None:
        super().__init__(
            status_code=409,
            code="EVENT_ID_PAYLOAD_CONFLICT",
            title="Carrier event payload conflict",
            detail="The event identity was already used with different content.",
        )
