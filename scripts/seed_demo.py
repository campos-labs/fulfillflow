"""Safely install the deterministic FulfillFlow demo dataset."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from benchmarks.seed_loader import SeedSafetyError, load_dataset  # noqa: E402

from fulfillflow.asyncio_support import run_async  # noqa: E402
from fulfillflow.config import DatabaseSettings  # noqa: E402


def main() -> int:
    """Load demo data only after all explicit safety gates pass."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-database-name",
        default=os.environ.get("SEED_CONFIRM_DATABASE_NAME"),
        help="literal PostgreSQL database name; may also use SEED_CONFIRM_DATABASE_NAME",
    )
    args = parser.parse_args()
    if not args.confirm_database_name:
        parser.error("--confirm-database-name is required")
    app_env = os.environ.get("APP_ENV")
    if app_env is None:
        parser.error("APP_ENV must be set explicitly")
    settings = DatabaseSettings(_env_file=None)
    try:
        result = run_async(
            load_dataset(
                "demo",
                database_url=settings.database_dsn,
                app_env=app_env,
                confirmed_database_name=args.confirm_database_name,
            )
        )
    except SeedSafetyError as exc:
        print(f"seed refused: {exc}", file=sys.stderr)
        return 2
    action = "inserted" if result.inserted else "already exact; no-op"
    print(
        f"demo dataset {action} in database {result.database_name}; "
        f"counts={result.counts}; logical_sha256={result.logical_hash}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
