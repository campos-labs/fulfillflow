"""Local worker heartbeat; no HTTP listener and no inference from broker depth."""

import json
import os
import time
from pathlib import Path
from typing import Any

STAGES = {
    "core": ("publish", "publish_notifications", "receive", "process"),
    "tracking": ("publish", "receive", "process"),
    "notifications": ("receive", "process"),
}


def heartbeat_path(service: str) -> Path:
    return Path(os.environ.get("WORKER_HEARTBEAT_PATH", f"/tmp/fulfillflow-{service}-worker.json"))


class Heartbeat:
    def __init__(self, path: Path, service: str) -> None:
        self.path = path
        self.service = service
        self.stages: dict[str, dict[str, Any]] = {
            stage: {"state": "starting", "at": time.monotonic()} for stage in STAGES[service]
        }
        self.stopping = False

    def record(self, stage: str, state: str) -> None:
        self.stages[stage] = {"state": state, "at": time.monotonic()}

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                dict(
                    service=self.service,
                    pid=os.getpid(),
                    at=time.monotonic(),
                    stopping=self.stopping,
                    stages=self.stages,
                )
            ),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self.path)


def healthy(path: Path, service: str, *, now: float | None = None) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        current = time.monotonic() if now is None else now
        return bool(
            data["service"] == service
            and not data["stopping"]
            and 0 <= current - data["at"] < 5
            and all(
                data["stages"][stage]["state"] == "ready"
                and 0 <= current - data["stages"][stage]["at"] < 45
                for stage in STAGES[service]
            )
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Local worker heartbeat healthcheck")
    parser.add_argument("--service", required=True, choices=tuple(STAGES))
    arguments = parser.parse_args()
    raise SystemExit(0 if healthy(heartbeat_path(arguments.service), arguments.service) else 1)
