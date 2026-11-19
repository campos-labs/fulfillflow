"""Restricted administration of Core access; no credentials in command arguments."""

import argparse

from fulfillflow.access.service import AccessService
from fulfillflow.asyncio_support import run_async
from fulfillflow.config import DatabaseSettings
from fulfillflow.db import Database
from fulfillflow.shared import SystemClock


async def administer(args: argparse.Namespace) -> None:
    database = Database.from_settings(DatabaseSettings())
    try:
        async with database.session() as session, session.begin():
            await AccessService(session, SystemClock()).administer(
                issuer=args.issuer,
                subject=args.subject,
                display_name=args.name,
                role=args.role,
                enabled=not args.block,
                executor=args.executor,
                reason=args.reason,
                revoke_sessions=args.revoke_sessions,
            )
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", choices=("viewer", "operator"), required=True)
    parser.add_argument("--executor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--block", action="store_true")
    parser.add_argument("--revoke-sessions", action="store_true")
    args = parser.parse_args()
    try:
        run_async(administer(args))
    except Exception:
        raise SystemExit(
            "Access update failed; verify input and Core database availability"
        ) from None
    print("Access and audit committed")


if __name__ == "__main__":
    main()
