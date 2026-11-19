"""One-use, browser-bound OIDC state, consumed before external token exchange."""

import secrets
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import Column, DateTime, String, Table, delete, insert
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.access.service import AuthenticationRequiredError, token_hash
from fulfillflow.db.base import Base
from fulfillflow.shared import Clock

flows = Table(
    "access_login_flows",
    Base.metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("state", JSONB, nullable=False),
)


async def save_flow(session: AsyncSession, clock: Clock, state: dict[str, Any]) -> str:
    handle = secrets.token_urlsafe(32)
    async with session.begin():
        await session.execute(delete(flows).where(flows.c.expires_at <= clock.now()))
        await session.execute(
            insert(flows).values(
                token_hash=token_hash(handle),
                expires_at=clock.now() + timedelta(minutes=10),
                state=state,
            )
        )
    return handle


async def consume_flow(session: AsyncSession, clock: Clock, handle: str) -> dict[str, Any]:
    async with session.begin():
        row = (
            (
                await session.execute(
                    delete(flows)
                    .where(
                        flows.c.token_hash == token_hash(handle),
                    )
                    .returning(flows.c.expires_at, flows.c.state)
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None or row["expires_at"] <= clock.now():
        raise AuthenticationRequiredError("Login expired or already used")
    return cast(dict[str, Any], row["state"])
