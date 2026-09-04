"""Read-only Windows/WSL2 observations with an explicit, non-personal allowlist."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping

from pydantic import ValidationError

from benchmarks.campaign import HostConditions, HostContract, HostIdentity
from benchmarks.collectors import CommandRunner, EnvironmentMismatchError, run_capture

# Never serialize CIM objects, command output, environment, container names or errors.
_WINDOWS_IDENTITY = r"""
$osInfo = Get-CimInstance Win32_OperatingSystem
$cpuInfo = @(Get-CimInstance Win32_Processor)
$ramInfo = Get-CimInstance Win32_ComputerSystem
$buildInfo = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
@{
    os = 'Windows'; os_version = $osInfo.Version
    os_build = "$($osInfo.BuildNumber).$($buildInfo.UBR)"
    cpu_model = (($cpuInfo.Name | Sort-Object -Unique) -join ' / ')
    physical_cores = ($cpuInfo | Measure-Object NumberOfCores -Sum).Sum
    logical_processors = ($cpuInfo | Measure-Object NumberOfLogicalProcessors -Sum).Sum
    physical_memory_bytes = $ramInfo.TotalPhysicalMemory
} | ConvertTo-Json -Compress
"""
_WINDOWS_STATE = r"""
$state = @{}
try {
    $batteries = @(Get-CimInstance -Namespace root\wmi -ClassName BatteryStatus)
    $unknown = @($batteries | Where-Object { $null -eq $_.PowerOnline })
    if ($batteries.Count -gt 0 -and $unknown.Count -eq 0) {
        $state.ac_power = @($batteries | Where-Object { -not $_.PowerOnline }).Count -eq 0
    }
} catch {}
try {
    $plan = powercfg /GETACTIVESCHEME
    $guidPattern = '[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
    if ($LASTEXITCODE -eq 0 -and $plan -match $guidPattern) {
        $state.power_plan_guid = $Matches[0].ToLowerInvariant()
    }
} catch {}
try {
    $memory = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    $state.available_memory_bytes = $memory.AvailableBytes
    $state.committed_bytes = $memory.CommittedBytes
    $state.commit_limit_bytes = $memory.CommitLimit
} catch {}
try {
    $pages = @(Get-CimInstance Win32_PageFileUsage)
    $allocated = ($pages | Measure-Object AllocatedBaseSize -Sum).Sum
    $state.pagefile_allocated_bytes = [long]$allocated * 1MB
    $state.pagefile_used_bytes = [long](($pages | Measure-Object CurrentUsage -Sum).Sum) * 1MB
} catch {}
$state | ConvertTo-Json -Compress
"""
_DYNAMIC_METRICS = (
    "available_memory_bytes",
    "committed_bytes",
    "commit_limit_bytes",
    "pagefile_allocated_bytes",
    "pagefile_used_bytes",
    "wsl_swap_total_bytes",
    "wsl_swap_free_bytes",
)
_POWERSHELL_PREFIX = (
    "$ErrorActionPreference = 'Stop'; "
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
)


class HostProbe:
    """Explicit execution only; constructors/imports never inspect the real host."""

    def __init__(
        self,
        contract: HostContract,
        *,
        official: bool,
        timeout_seconds: float,
        command_runner: CommandRunner | None = None,
        platform: str | None = None,
    ) -> None:
        self.contract = contract
        self.official = official
        self.timeout_seconds = timeout_seconds
        self.runner = command_runner or run_capture
        self.platform = sys.platform if platform is None else platform
        self._identity: dict[str, object] | None = None

    def identity(self) -> dict[str, dict[str, object]]:
        """Cache the stable observation, but never substitute expectations for it."""
        if self._identity is None:
            values: dict[str, object] = {}
            if self.platform == "win32":
                values.update(self._powershell(_WINDOWS_IDENTITY))
                values["docker_engine"] = self._text(
                    ["docker", "version", "--format", "{{.Server.Version}}"]
                )
                values["docker_compose"] = self._text(
                    ["docker", "compose", "version", "--short"]
                ).removeprefix("v")
                info = self._json(
                    [
                        "docker",
                        "info",
                        "--format",
                        '{"cpus":{{.NCPU}},"memory":{{.MemTotal}},'
                        '"kernel":{{json .KernelVersion}}}',
                    ]
                )
                values["docker_cpus"] = info.get("cpus")
                values["docker_memory_bytes"] = info.get("memory")
                kernel = self._text(
                    ["wsl", "--distribution", "docker-desktop", "--exec", "uname", "-r"]
                )
                # The active Docker daemon must use the observed WSL2 kernel.
                values["wsl_kernel"] = kernel if kernel == info.get("kernel") else None
                version = self._text(["wsl", "--version"]).splitlines()
                match = re.search(r"\b\d+(?:\.\d+)+\b", version[0]) if version else None
                values["wsl_version"] = match.group() if match else None
            self._identity = _validated_fields(HostIdentity, values)
        return self._compare(self.contract.identity.model_dump(), self._identity)

    def dynamic(self, container_ids: Mapping[str, str]) -> dict[str, dict[str, object]]:
        """Capture each repetition's state; memory/swap have no automatic threshold."""
        values: dict[str, object] = {}
        if self.platform == "win32":
            values.update(self._powershell(_WINDOWS_STATE))
            values["concurrent_containers"] = None
            memory = self._text(
                ["wsl", "--distribution", "docker-desktop", "--exec", "cat", "/proc/meminfo"]
            )
            for key, target in (
                ("SwapTotal", "wsl_swap_total_bytes"),
                ("SwapFree", "wsl_swap_free_bytes"),
            ):
                match = re.search(rf"^{key}:\s+(\d+)\s+kB$", memory, re.MULTILINE)
                values[target] = int(match[1]) * 1024 if match else None
            # Only a count leaves the probe; no foreign container name or identifier.
            output = self._text(["docker", "ps", "--no-trunc", "--format", "{{.ID}}"])
            identifiers = output.splitlines()
            if identifiers and all(re.fullmatch(r"[0-9a-f]{64}", item) for item in identifiers):
                active = set(identifiers)
                if set(container_ids.values()) <= active:
                    values["concurrent_containers"] = len(active - set(container_ids.values()))
        observed = _validated_fields(HostConditions, values)
        for field in _DYNAMIC_METRICS:
            value = values.get(field)
            observed[field] = value if type(value) is int and value >= 0 else None
        expected = self.contract.conditions.model_dump()
        expected.update(dict.fromkeys(_DYNAMIC_METRICS))
        return self._compare(expected, observed, optional=_DYNAMIC_METRICS)

    def _compare(
        self,
        expected: dict[str, object],
        observed: dict[str, object],
        *,
        optional: tuple[str, ...] = (),
    ) -> dict[str, dict[str, object]]:
        report: dict[str, dict[str, object]] = {}
        for key, value in expected.items():
            actual = observed.get(key)
            required = value is not None or (self.official and key not in optional)
            report[key] = {
                "expected": value,
                "observed": actual,
                "matches": actual is not None and actual == value if required else None,
                "status": "confirmed" if actual is not None else "not_confirmed",
            }
        if any(item["matches"] is False for item in report.values()):
            raise EnvironmentMismatchError(report)
        return report

    def _powershell(self, script: str) -> dict[str, object]:
        return self._json(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _POWERSHELL_PREFIX + script]
        )

    def _json(self, command: list[str]) -> dict[str, object]:
        try:
            result = json.loads(self._text(command))
        except ValueError:
            return {}
        return result if isinstance(result, dict) else {}

    def _text(self, command: list[str]) -> str:
        try:
            completed = self.runner(command, self.timeout_seconds)
            return completed.stdout.replace("\x00", "").strip() if completed.returncode == 0 else ""
        except (RuntimeError, OSError, ValueError):
            # No raw exception or command output is persisted, even on probe failure.
            return ""


def _validated_fields(
    model: type[HostIdentity] | type[HostConditions], values: dict[str, object]
) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for field in model.model_fields:
        try:
            sanitized[field] = model.model_validate({field: values.get(field)}).model_dump()[field]
        except ValidationError:
            sanitized[field] = None
    return sanitized
