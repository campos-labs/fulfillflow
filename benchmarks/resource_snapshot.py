"""Read local Docker one-shot counters; the caller bounds this process lifetime."""

from __future__ import annotations

import http.client
import json
import re
import socket
import sys
import time
from typing import Any, BinaryIO

from benchmarks.collection_diagnostics import PREFIX, failure


class _Pipe:
    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream

    def makefile(self, *_args: object) -> BinaryIO:
        return self.stream


def read_stats(endpoint: str, identifier: str) -> dict[str, Any]:
    """Accept only a local socket/pipe and an exact container ID, never TCP/SSH."""
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("invalid container identity")
    path = f"/containers/{identifier}/stats?stream=false&one-shot=true"
    if endpoint.startswith("npipe:////./pipe/"):
        name = endpoint.removeprefix("npipe:////./pipe/")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("invalid local pipe")
        with open("\\\\.\\pipe\\" + name, "r+b", buffering=0) as stream:
            stream.write(
                f"GET {path} HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n".encode()
            )
            response = http.client.HTTPResponse(_Pipe(stream))  # type: ignore[arg-type]
            response.begin()
            if response.status != 200:
                raise ValueError("Docker stats unavailable")
            payload = json.loads(response.read())
    elif endpoint.startswith("unix:///"):
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise ValueError("local Unix sockets are unavailable")
        with socket.socket(family, socket.SOCK_STREAM) as channel:
            channel.settimeout(10)
            channel.connect(endpoint.removeprefix("unix://"))
            channel.sendall(
                f"GET {path} HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n".encode()
            )
            response = http.client.HTTPResponse(channel)
            response.begin()
            if response.status != 200:
                raise ValueError("Docker stats unavailable")
            payload = json.loads(response.read())
    else:
        raise ValueError("only local Docker endpoints are supported")
    # Docker 27's one-shot response omits ID; the GET path is bound to the full
    # verified container ID. Reject an explicit conflicting identity if supplied.
    if not isinstance(payload, dict) or payload.get("id") not in (None, "", identifier):
        raise ValueError("Docker stats identity mismatch")
    return payload


def main() -> int:
    started = time.monotonic()
    stage = "arguments"
    try:
        endpoint, *identifiers = sys.argv[1:]
        result = {}
        for identifier in identifiers:
            stage = "transport"
            raw = read_stats(endpoint, identifier)
            stage = "normalize"
            cpu, memory = raw["cpu_stats"], raw["memory_stats"]
            cache = memory.get("stats", {}).get(
                "total_inactive_file", memory.get("stats", {}).get("inactive_file", 0)
            )
            usage = memory["usage"]
            result[identifier] = {
                "read": raw["read"],
                "cpu": cpu["cpu_usage"]["total_usage"],
                "system": cpu["system_cpu_usage"],
                "cpus": cpu["online_cpus"],
                "memory": usage - cache if cache < usage else usage,
                "limit": memory["limit"],
            }
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print("local Docker resource snapshot failed", file=sys.stderr)
        print(PREFIX + json.dumps(failure(exc, stage, started)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
