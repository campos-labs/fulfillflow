"""Opaque credentials and transaction participants for Core access."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fulfillflow.access.tables import audit, sessions, users
from fulfillflow.shared import Clock, new_uuid

Role = Literal["viewer", "operator"]


class AccessDeniedError(PermissionError):
    """Sanitized access failure without credential material."""


class AuthenticationRequiredError(AccessDeniedError):
    """No currently valid local session."""


class OperationForbiddenError(AccessDeniedError):
    """Authenticated identity lacks the required permission."""


@dataclass(frozen=True)
class Identity:
    user_id: UUID
    role: Role
    expires_at: datetime


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token(token: str) -> str:
    return hmac.new(token.encode(), b"fulfillflow-csrf-v2", hashlib.sha256).hexdigest()


class AccessService:
    """Participate in caller-owned transactions; never commit independently."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self.session = session
        self.clock = clock

    async def authenticate(self, token: str, *, mutation: bool = False) -> Identity:
        digest = token_hash(token)
        user_id = await self.session.scalar(
            select(sessions.c.user_id).where(sessions.c.token_hash == digest)
        )
        if user_id is None:
            raise AuthenticationRequiredError("Session is absent or expired")
        # Discover without a lock, then lock the owner before reading the session again.
        query = select(users).where(users.c.id == user_id)
        if mutation:
            query = query.with_for_update(read=True)
        user = (await self.session.execute(query)).mappings().one_or_none()
        expiry = await self.session.scalar(
            select(sessions.c.expires_at).where(
                sessions.c.token_hash == digest,
                sessions.c.user_id == user_id,
            )
        )
        if user is None or not user["enabled"] or expiry is None or expiry <= self.clock.now():
            raise AuthenticationRequiredError("Session is absent or expired")
        if mutation and user["role"] != "operator":
            raise OperationForbiddenError("Operation is not permitted")
        return Identity(user_id, user["role"], expiry)

    async def login(self, issuer: str, subject: str, *, lifetime: int = 28800) -> str:
        if lifetime <= 0:
            raise ValueError("Session lifetime must be positive")
        user = (
            (
                await self.session.execute(
                    select(users)
                    .where(
                        users.c.issuer == issuer,
                        users.c.subject == subject,
                    )
                    .with_for_update(read=True)
                )
            )
            .mappings()
            .one_or_none()
        )
        if user is None or not user["enabled"]:
            raise OperationForbiddenError("Identity is not enabled")
        token = secrets.token_urlsafe(32)
        now = self.clock.now()
        await self.session.execute(
            insert(sessions).values(
                token_hash=token_hash(token),
                user_id=user["id"],
                created_at=now,
                expires_at=now + timedelta(seconds=lifetime),
            )
        )
        return token

    async def logout(self, token: str) -> None:
        digest = token_hash(token)
        user_id = await self.session.scalar(
            select(sessions.c.user_id).where(
                sessions.c.token_hash == digest,
            )
        )
        if user_id is not None:
            await self.session.execute(
                select(users.c.id)
                .where(
                    users.c.id == user_id,
                )
                .with_for_update()
            )
            await self.session.execute(delete(sessions).where(sessions.c.token_hash == digest))

    async def administer(
        self,
        *,
        issuer: str,
        subject: str,
        display_name: str | None = None,
        role: Role | None = None,
        enabled: bool | None = None,
        executor: str,
        reason: str,
        revoke_sessions: bool = False,
    ) -> UUID:
        if not all(value.strip() for value in (issuer, subject, executor, reason)):
            raise ValueError("Identity, executor and reason are required")
        if role is not None and role not in ("viewer", "operator"):
            raise ValueError("Invalid role")
        if display_name is not None and not display_name.strip():
            raise ValueError("Display name must not be blank")
        now = self.clock.now()
        created = None
        if enabled is True and role is not None and display_name:
            created = await self.session.scalar(
                pg_insert(users)
                .values(
                    id=new_uuid(),
                    issuer=issuer,
                    subject=subject,
                    display_name=display_name,
                    role=role,
                    enabled=True,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=[users.c.issuer, users.c.subject])
                .returning(users.c.id)
            )
        user = (
            (
                await self.session.execute(
                    select(users)
                    .where(
                        users.c.issuer == issuer,
                        users.c.subject == subject,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if user is None:
            raise ValueError("New identities require explicit enable, role and name")
        new_role = role if role is not None else user["role"]
        new_enabled = enabled if enabled is not None else user["enabled"]
        new_name = display_name if display_name is not None else user["display_name"]
        user_id = user["id"]
        await self.session.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(
                role=new_role,
                enabled=new_enabled,
                display_name=new_name,
            )
        )
        if not new_enabled or revoke_sessions:
            await self.session.execute(delete(sessions).where(sessions.c.user_id == user_id))
        await self.session.execute(
            insert(audit).values(
                id=new_uuid(),
                user_id=user_id,
                executor=executor.strip(),
                reason=reason.strip(),
                action=(
                    "enable"
                    if created or enabled is True
                    else "block"
                    if enabled is False
                    else "revoke_sessions"
                    if revoke_sessions
                    else "change_access"
                ),
                occurred_at=now,
                old_role=None if created else user["role"],
                new_role=new_role,
                old_enabled=None if created else user["enabled"],
                new_enabled=new_enabled,
            )
        )
        return cast(UUID, user_id)
