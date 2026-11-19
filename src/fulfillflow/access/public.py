"""Public technical access contract used by transaction coordinators."""

from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.access.service import AccessService
from fulfillflow.shared import Clock


async def authorize_mutation(session: AsyncSession, clock: Clock) -> None:
    """Recheck HTTP authority under a shared owner lock in the business transaction.

    Trusted local workers do not have browser credentials. The HTTP boundary always
    binds an opaque token, and never uses this absence as an HTTP bypass.
    """
    token = session.info.get("access_token")
    if token is not None:
        await AccessService(session, clock).authenticate(token, mutation=True)
