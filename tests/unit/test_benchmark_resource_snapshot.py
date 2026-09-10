"""The counter reader only accepts local endpoints and exact container identities."""

from __future__ import annotations

import io
import json
import sys

import pytest
from benchmarks import resource_snapshot
from benchmarks.resource_snapshot import read_stats


def test_local_unix_transport_uses_exact_readonly_one_shot_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = "a" * 64
    body = json.dumps({"id": identifier, "cpu_stats": {}}).encode()
    sent: list[bytes] = []

    class Channel:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, seconds):
            assert seconds == 10

        def connect(self, path):
            assert path == "/var/run/docker.sock"

        def sendall(self, request):
            sent.append(request)

        def makefile(self, *_args):
            return io.BytesIO(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )

    monkeypatch.setattr(resource_snapshot.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(resource_snapshot.socket, "socket", lambda *_a: Channel())
    assert read_stats("unix:///var/run/docker.sock", identifier)["id"] == identifier
    assert sent == [
        (
            f"GET /containers/{identifier}/stats?stream=false&one-shot=true HTTP/1.1\r\n"
            "Host: docker\r\nConnection: close\r\n\r\n"
        ).encode()
    ]


@pytest.mark.parametrize(
    "endpoint", ["tcp://remote:2375", "ssh://remote", "npipe:////./pipe/../other"]
)
def test_resource_reader_rejects_nonlocal_or_ambiguous_endpoint(endpoint: str) -> None:
    with pytest.raises(ValueError):
        read_stats(endpoint, "a" * 64)


@pytest.mark.parametrize("identifier", ["", "app", "../stats", "a" * 63])
def test_resource_reader_requires_exact_container_id(identifier: str) -> None:
    with pytest.raises(ValueError, match="identity"):
        read_stats("unix:///var/run/docker.sock", identifier)


@pytest.mark.parametrize("cache_key", ["inactive_file", "total_inactive_file"])
def test_snapshot_retains_cpu_counters_and_docker_memory_semantics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cache_key: str,
) -> None:
    monkeypatch.setattr(sys, "argv", ["snapshot", "unix:///var/run/docker.sock", "a" * 64])
    monkeypatch.setattr(
        resource_snapshot,
        "read_stats",
        lambda *_a: {
            "read": "2026-09-04T00:00:00Z",
            "cpu_stats": {
                "cpu_usage": {"total_usage": 100},
                "system_cpu_usage": 800,
                "online_cpus": 8,
            },
            "memory_stats": {"usage": 1000, "limit": 2000, "stats": {cache_key: 100}},
        },
    )
    assert resource_snapshot.main() == 0
    data = json.loads(capsys.readouterr().out)["a" * 64]
    assert data["memory"] == 900
    assert (data["cpu"], data["system"], data["cpus"], data["limit"]) == (100, 800, 8, 2000)


def test_snapshot_sanitizes_external_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["snapshot", "unix:///var/run/docker.sock", "a" * 64])

    def fail(*_args: object) -> None:
        raise OSError("synthetic-private-token")

    monkeypatch.setattr(resource_snapshot, "read_stats", fail)
    assert resource_snapshot.main() == 2
    output = capsys.readouterr()
    assert not output.out
    assert output.err.startswith("local Docker resource snapshot failed\nRESOURCE_FAILURE_JSON=")
    assert '"type": "OSError"' in output.err
    assert "synthetic-private-token" not in output.err
