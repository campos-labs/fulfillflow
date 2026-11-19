"""Real PostgreSQL access transactions, including concurrent revocation."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select, text

from fulfillflow.access.service import AccessDeniedError, AccessService, csrf_token, token_hash
from fulfillflow.access.tables import audit, sessions, users


async def provision(database, clock, **overrides):
    async with database.session() as session, session.begin():
        return await AccessService(session, clock).administer(
            **dict(
                issuer="http://identity.test/realms/test",
                subject="operator-1",
                display_name="Synthetic Operator",
                role="operator",
                enabled=True,
                executor="test-administrator",
                reason="Synthetic test preparation",
            )
            | overrides
        )


@pytest.fixture
async def access_database(postgres_database):
    async with postgres_database.session() as session, session.begin():
        for table in (audit, sessions, users):
            await session.execute(delete(table))
    return postgres_database


async def login(database, clock, lifetime=28800):
    async with database.session() as session, session.begin():
        return await AccessService(session, clock).login(
            "http://identity.test/realms/test",
            "operator-1",
            lifetime=lifetime,
        )


async def test_identity_requires_enablement_and_cookie_is_only_stored_as_hash(
    access_database,
    fixed_clock,
):
    with pytest.raises(AccessDeniedError):
        await login(access_database, fixed_clock)
    await provision(access_database, fixed_clock)
    token = await login(access_database, fixed_clock)
    async with access_database.session() as session, session.begin():
        row = (await session.execute(select(sessions))).mappings().one()
        assert row["token_hash"] == token_hash(token) != token
        assert row["expires_at"] == fixed_clock.now() + timedelta(hours=8)
        assert csrf_token(token) != token_hash(token)
        identity = await AccessService(session, fixed_clock).authenticate(token, mutation=True)
        assert identity.role == "operator"


async def test_role_block_logout_and_expiry(access_database, fixed_clock):
    await provision(access_database, fixed_clock)
    token = await login(access_database, fixed_clock)
    await provision(access_database, fixed_clock, role="viewer")
    async with access_database.session() as session, session.begin():
        service = AccessService(session, fixed_clock)
        assert (await service.authenticate(token)).role == "viewer"
        with pytest.raises(AccessDeniedError):
            await service.authenticate(token, mutation=True)
        await service.logout(token)
        with pytest.raises(AccessDeniedError):
            await service.authenticate(token)
    token = await login(access_database, fixed_clock)
    await provision(access_database, fixed_clock, enabled=False)
    async with access_database.session() as session, session.begin():
        assert await session.scalar(select(func.count()).select_from(sessions)) == 0
        with pytest.raises(AccessDeniedError):
            await AccessService(session, fixed_clock).authenticate(token)
    await provision(access_database, fixed_clock)
    token = await login(access_database, fixed_clock, lifetime=1)
    from tests.support import FixedClock

    async with access_database.session() as session, session.begin():
        with pytest.raises(AccessDeniedError):
            await AccessService(
                session, FixedClock(fixed_clock.now() + timedelta(seconds=1))
            ).authenticate(token)


async def test_administration_audit_rolls_back_with_change(access_database, fixed_clock):
    await provision(access_database, fixed_clock)
    with pytest.raises(RuntimeError):
        async with access_database.session() as session, session.begin():
            await AccessService(session, fixed_clock).administer(
                issuer="http://identity.test/realms/test",
                subject="operator-1",
                display_name="Synthetic",
                role="viewer",
                enabled=False,
                executor="test",
                reason="Injected rollback",
            )
            raise RuntimeError("rollback")
    async with access_database.session() as session, session.begin():
        user = (await session.execute(select(users))).mappings().one()
        assert user["enabled"] and user["role"] == "operator"
        assert await session.scalar(select(func.count()).select_from(audit)) == 1


async def test_revocation_waits_for_authorized_transaction(access_database, fixed_clock):
    await provision(access_database, fixed_clock)
    token = await login(access_database, fixed_clock)
    started = asyncio.Event()
    revoke_pid = None

    async def revoke():
        nonlocal revoke_pid
        async with access_database.session() as session, session.begin():
            revoke_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            await AccessService(session, fixed_clock).administer(
                issuer="http://identity.test/realms/test",
                subject="operator-1",
                display_name="Synthetic",
                role="viewer",
                enabled=False,
                executor="test",
                reason="Concurrent block",
            )

    async with access_database.session() as mutation, mutation.begin():
        await AccessService(mutation, fixed_clock).authenticate(token, mutation=True)
        task = asyncio.create_task(revoke())
        try:
            await asyncio.wait_for(started.wait(), 5)
            async with access_database.session() as observer:
                async with asyncio.timeout(5):
                    for _ in range(500):
                        blocked = await observer.scalar(
                            text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                            {"pid": revoke_pid},
                        )
                        if blocked:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        pytest.fail("Revocation did not wait on the mutation lock")
                assert not task.done()
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
    await asyncio.wait_for(task, 5)
    async with access_database.session() as session, session.begin():
        with pytest.raises(AccessDeniedError):
            await AccessService(session, fixed_clock).authenticate(token, mutation=True)
        assert await session.scalar(select(func.count()).select_from(audit)) == 2
