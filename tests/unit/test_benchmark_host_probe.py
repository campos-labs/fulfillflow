"""Host inspection is always simulated, including on CI and Windows developers' hosts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from benchmarks.campaign import CampaignManifest, HostContract
from benchmarks.collectors import EnvironmentMismatchError, ExternalCommandError
from benchmarks.host_probe import HostProbe

IDENTITY = {
    "os": "Windows",
    "os_version": "10.0.12345",
    "os_build": "12345.100",
    "cpu_model": "Synthetic CPU",
    "physical_cores": 4,
    "logical_processors": 8,
    "physical_memory_bytes": 16 * 1024**3,
    "docker_engine": "27.2.0",
    "docker_compose": "2.29.2-desktop.2",
    "wsl_version": "2.3.24.0",
    "wsl_kernel": "5.15.153.1-microsoft-standard-WSL2",
    "docker_cpus": 8,
    "docker_memory_bytes": 8 * 1024**3,
}
CONDITIONS = {
    "ac_power": True,
    "power_plan_guid": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "concurrent_containers": 0,
}
METRICS = {
    "available_memory_bytes": 4 * 1024**3,
    "committed_bytes": 10 * 1024**3,
    "commit_limit_bytes": 20 * 1024**3,
    "pagefile_allocated_bytes": 4 * 1024**3,
    "pagefile_used_bytes": 0,
}
CONTAINERS = {"app": "a" * 64, "postgres": "b" * 64, "loadgen": "c" * 64}


def _contract() -> HostContract:
    return HostContract.model_validate({"identity": IDENTITY, "conditions": CONDITIONS})


class Commands:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.identity = dict(IDENTITY)
        self.state = {**CONDITIONS, **METRICS}
        self.extra_container = False
        self.swap = "SwapTotal: 2097152 kB\nSwapFree: 1048576 kB"

    def __call__(self, command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        assert timeout == 2
        self.calls.append(command)
        if command[0] == "powershell":
            assert command[1:3] == ["-NoProfile", "-NonInteractive"]
            payload = self.identity if "Win32_Processor" in command[-1] else self.state
            output = json.dumps({**payload, "hostname": "PRIVATE", "user": "PRIVATE"})
        elif command[:2] == ["docker", "version"]:
            output = str(self.identity.get("docker_engine", ""))
        elif command[:3] == ["docker", "compose", "version"]:
            output = str(self.identity.get("docker_compose", ""))
        elif command[:2] == ["docker", "info"]:
            output = json.dumps(
                {
                    "cpus": self.identity.get("docker_cpus"),
                    "memory": self.identity.get("docker_memory_bytes"),
                    "kernel": self.identity.get("wsl_kernel"),
                }
            )
        elif command == ["wsl", "--version"]:
            output = "WSL version: " + str(self.identity.get("wsl_version", ""))
            output = "\x00".join(output)  # WSL can emit NUL-interleaved console text.
        elif command[-2:] == ["uname", "-r"]:
            output = str(self.identity.get("wsl_kernel", ""))
        elif command[-2:] == ["cat", "/proc/meminfo"]:
            output = self.swap
        elif command[:2] == ["docker", "ps"]:
            output = "\n".join(CONTAINERS.values())
            if self.extra_container:
                output += "\n" + "d" * 64
        else:
            raise AssertionError("unexpected command")
        return subprocess.CompletedProcess(command, 0, output, "PRIVATE stderr")


def test_identity_is_observed_once_and_dynamic_state_is_refreshed_without_personal_data() -> None:
    commands = Commands()
    probe = HostProbe(
        _contract(), official=True, timeout_seconds=2, command_runner=commands, platform="win32"
    )
    first = probe.identity()
    count = len(commands.calls)
    assert probe.identity() == first
    assert len(commands.calls) == count
    assert all(item["matches"] is True for item in first.values())
    state = probe.dynamic(CONTAINERS)
    for key in CONDITIONS:
        assert state[key] == {
            "expected": CONDITIONS[key],
            "observed": CONDITIONS[key],
            "matches": True,
            "status": "confirmed",
        }
    assert state["wsl_swap_free_bytes"]["observed"] == 1024**3
    commands.state["available_memory_bytes"] = 1  # Observed, not an unapproved threshold.
    assert probe.dynamic(CONTAINERS)["available_memory_bytes"]["observed"] == 1
    assert "PRIVATE" not in json.dumps([first, state])
    assert "d" * 64 not in json.dumps(state)


@pytest.mark.parametrize("field", list(IDENTITY))
@pytest.mark.parametrize("missing", [False, True])
def test_every_essential_identity_field_fails_closed_on_missing_or_drift(
    field: str, missing: bool
) -> None:
    commands = Commands()
    if missing:
        commands.identity.pop(field)
    else:
        value = commands.identity[field]
        commands.identity[field] = value + 1 if isinstance(value, int) else str(value) + "-changed"
        if field == "wsl_version":
            commands.identity[field] = "9.9.9.9"
    probe = HostProbe(
        _contract(), official=True, timeout_seconds=2, command_runner=commands, platform="win32"
    )
    with pytest.raises(EnvironmentMismatchError) as captured:
        probe.identity()
    assert captured.value.report[field]["matches"] is False


@pytest.mark.parametrize("field", list(CONDITIONS))
def test_dynamic_drift_fails_before_admitting_a_repetition(field: str) -> None:
    commands = Commands()
    if field == "concurrent_containers":
        commands.extra_container = True
    else:
        commands.state[field] = False if field == "ac_power" else "0" * 8 + CONDITIONS[field][8:]
    probe = HostProbe(
        _contract(), official=True, timeout_seconds=2, command_runner=commands, platform="win32"
    )
    with pytest.raises(EnvironmentMismatchError) as captured:
        probe.dynamic(CONTAINERS)
    assert captured.value.report[field]["matches"] is False
    assert "d" * 64 not in json.dumps(captured.value.report)


def test_optional_unavailable_metrics_do_not_block_official_campaign() -> None:
    commands = Commands()
    commands.state = dict(CONDITIONS)
    commands.swap = "unavailable"
    probe = HostProbe(
        _contract(), official=True, timeout_seconds=2, command_runner=commands, platform="win32"
    )
    state = probe.dynamic(CONTAINERS)
    for key in (*METRICS, "wsl_swap_total_bytes", "wsl_swap_free_bytes"):
        assert state[key] == {
            "expected": None,
            "observed": None,
            "matches": None,
            "status": "not_confirmed",
        }


@pytest.mark.parametrize("field", ["ac_power", "power_plan_guid", "concurrent_containers"])
def test_missing_dynamic_essential_is_never_reported_as_matching(field: str) -> None:
    commands = Commands()
    commands.state.pop(field)
    ids = dict(CONTAINERS)
    if field == "concurrent_containers":
        ids["app"] = "e" * 64  # Campaign container absent from the observed running set.
    probe = HostProbe(
        _contract(), official=True, timeout_seconds=2, command_runner=commands, platform="win32"
    )
    with pytest.raises(EnvironmentMismatchError) as captured:
        probe.dynamic(ids)
    assert captured.value.report[field]["observed"] is None
    assert captured.value.report[field]["matches"] is False


def test_explicit_nonofficial_expectations_are_also_enforced() -> None:
    commands = Commands()
    commands.state["ac_power"] = False
    probe = HostProbe(
        _contract(), official=False, timeout_seconds=2, command_runner=commands, platform="win32"
    )
    with pytest.raises(EnvironmentMismatchError):
        probe.dynamic(CONTAINERS)


@pytest.mark.parametrize("output", ["not JSON PRIVATE", "[]", "{}"])
def test_unavailable_or_malformed_probes_never_become_confirmed_observations(output: str) -> None:
    def commands(command: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, output, "PRIVATE")

    probe = HostProbe(
        _contract(), official=True, timeout_seconds=2, command_runner=commands, platform="win32"
    )
    with pytest.raises(EnvironmentMismatchError) as captured:
        probe.identity()
    assert "PRIVATE" not in json.dumps(captured.value.report)


def test_command_failure_and_unsupported_host_are_explicitly_unconfirmed() -> None:
    calls: list[bool] = []

    def failed(_command: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(True)
        raise ExternalCommandError("PRIVATE")

    empty = HostContract.model_validate({"identity": {}, "conditions": {}})
    smoke = HostProbe(
        empty, official=False, timeout_seconds=2, command_runner=failed, platform="win32"
    )
    assert all(item["status"] == "not_confirmed" for item in smoke.identity().values())
    assert calls
    calls.clear()
    official = HostProbe(
        _contract(), official=True, timeout_seconds=2, command_runner=failed, platform="linux"
    )
    with pytest.raises(EnvironmentMismatchError):
        official.identity()
    assert calls == []


def test_complete_synthetic_official_declarations_validate_and_missing_essential_does_not() -> None:
    payload = json.loads(
        Path("benchmarks/fixtures/smoke-campaign.json").read_text(encoding="utf-8")
    )
    payload.update(
        official=True, repetitions=5, stabilization_seconds=1, host=_contract().model_dump()
    )
    assert CampaignManifest.model_validate(payload).official
    for section in ("identity", "conditions"):
        for field in payload["host"][section]:
            value = payload["host"][section][field]
            payload["host"][section][field] = None
            with pytest.raises(ValueError, match="essential host expectation"):
                CampaignManifest.model_validate(payload)
            payload["host"][section][field] = value
