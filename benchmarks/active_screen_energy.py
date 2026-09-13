"""Fail-closed host heartbeat checks, exclusive to the active-screen diagnostic."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path


class EnergyConditionError(RuntimeError):
    pass


def require_energy() -> None:
    path = os.environ.get("FULFILLFLOW_ACTIVE_GUARD")
    if not path:
        raise EnergyConditionError("active-screen observer is required")
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        age = (datetime.now(UTC) - datetime.fromisoformat(state["heartbeat_utc"])).total_seconds()
        if (
            state["ready"] is not True
            or state["failure"] != ""
            or state["display"] != 1
            or not 0 <= age <= 3
        ):
            raise EnergyConditionError("screen/session/AC condition or observer freshness failed")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EnergyConditionError("active-screen evidence is unavailable") from exc
