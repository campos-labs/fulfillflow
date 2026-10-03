"""Test-only ASGI completion observer; no application state or SQL changes."""

from __future__ import annotations

import hmac
import json
import os
from typing import Any
from uuid import UUID


class CompletionObserver:
    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token
        self.active = 0
        self.started = 0
        self.finished = 0
        self.last_status: int | None = None
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == "/__c_observer":
            headers = dict(scope.get("headers", []))
            valid = hmac.compare_digest(headers.get(b"x-c-observer", b""), self.token.encode())
            body = (
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "active": self.active,
                        "started": self.started,
                        "finished": self.finished,
                        "last_status": self.last_status,
                        "requests": self.requests,
                    }
                ).encode()
                if valid
                else b"{}"
            )
            await send(
                {"type": "http.response.start", "status": 200 if valid else 403, "headers": []}
            )
            await send({"type": "http.response.body", "body": body})
            return
        record = None
        raw_id = dict(scope.get("headers", [])).get(b"x-request-id", b"")
        try:
            request_id = str(UUID(raw_id.decode("ascii")))
        except (ValueError, UnicodeDecodeError):
            request_id = None
        if request_id is not None:
            record = {
                "request_id": request_id,
                "sequence": self.started + 1,
                "kind": "public_webhook"
                if scope["path"].startswith("/api/v1/carriers/")
                and scope["path"].endswith("/events")
                else "internal_or_other",
                "finished": False,
                "status": None,
            }
            self.requests.append(record)
        self.active += 1
        self.started += 1
        status = None

        async def observe(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observe)
        finally:
            self.active -= 1
            self.finished += 1
            self.last_status = status
            if record is not None:
                record.update(finished=True, status=status)


def request_finished(states: dict[str, Any], request_id: str) -> bool:
    public = [
        r
        for r in states.get("core", {}).get("requests", [])
        if r["request_id"] == request_id and r["kind"] == "public_webhook"
    ]
    related = [
        r
        for state in states.values()
        for r in state.get("requests", [])
        if r["request_id"] == request_id
    ]
    return (
        len(public) == 1
        and bool(related)
        and all(r["finished"] for r in related)
        and all(state["active"] == 0 for state in states.values())
    )
