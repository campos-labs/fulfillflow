from __future__ import annotations

import ipaddress
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from validation.functional import environment as target


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], Mapping[str, str] | None]] = []
        self.infra: target.Infrastructure
        self.running = {"postgres": True, "rabbit": True}
        self.fail_stage: str | None = None
        self.foreign_role: str | None = None
        self.existing_volume = False
        self.wrong_image = False
        self.host_ip = "127.0.0.1"
        self.server = {
            "Version": "29.0.1",
            "ApiVersion": "1.52",
            "Os": "linux",
            "Arch": "amd64",
            "KernelVersion": "6.6.87.2-microsoft-standard-WSL2",
            "Components": [{"Details": {"private": "not-exported"}}],
        }

    async def __call__(self, stage: str, args: Sequence[str], env: Mapping[str, str] | None) -> str:
        self.calls.append((stage, tuple(args), env))
        if stage == self.fail_stage:
            raise target.InfrastructureError(stage, "external_command_failed", exit_code=17)
        if stage == "docker_server":
            return json.dumps(self.server)
        if stage == "inventory":
            return "e" * 64
        if stage.startswith("image_"):
            role = stage.removeprefix("image_")
            digest = "bad" if self.wrong_image else str(args[2]).split("@", 1)[1]
            return json.dumps(
                {
                    "Id": "sha256:" + ("a" if role == "postgres" else "b") * 64,
                    "RepoDigests": [f"example@{digest}"],
                }
            )
        if stage == "network_create":
            return "c" * 64
        if stage == "network_identity":
            return json.dumps(
                {
                    "Id": "c" * 64,
                    "Name": f"{self.infra.prefix}-network",
                    "Driver": "bridge",
                    "Labels": self.labels("network"),
                    "IPAMConfig": [{"Subnet": self.infra.network_subnet or "172.30.0.0/16"}],
                }
            )
        if stage.startswith("volume_check_"):
            return "already-there" if self.existing_volume else ""
        if stage.startswith("volume_identity_"):
            role = stage.removeprefix("volume_identity_")
            return json.dumps({"Name": args[2], "Labels": self.labels(role)})
        if stage.startswith("volume_"):
            return args[-1]
        if stage.startswith("start_"):
            return ("1" if stage.endswith("postgres") else "2") * 64
        if stage.startswith("inspect_"):
            role = stage.removeprefix("inspect_")
            return json.dumps(self.inspect(role))
        if stage.startswith("stop_"):
            self.running[stage.removeprefix("stop_")] = False
            return args[-1]
        if stage.startswith("exec_"):
            return "[]"
        if stage.startswith("logs_"):
            return "database system is ready to accept connections\nSECRET BUSINESS PAYLOAD"
        raise AssertionError(f"unexpected stage {stage}")

    def labels(self, role: str) -> dict[str, str]:
        return {
            target.PURPOSE_LABEL: target.PURPOSE,
            target.OWNER_LABEL: "foreign" if role == self.foreign_role else self.infra.owner,
            target.ROLE_LABEL: role,
        }

    def inspect(self, role: str) -> dict[str, Any]:
        postgres = role == "postgres"
        return {
            "Id": ("1" if postgres else "2") * 64,
            "Image": "sha256:" + ("a" if postgres else "b") * 64,
            "Labels": self.labels(role),
            "State": {
                "Running": self.running[role],
                "Health": {"Status": "healthy"},
                "ExitCode": 0,
                "Status": "running" if self.running[role] else "exited",
            },
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": f"{self.infra.prefix}-{role}-data",
                    "Destination": "/var/lib/postgresql" if postgres else "/var/lib/rabbitmq",
                    "RW": True,
                }
            ],
            "Networks": {
                "own": {
                    "NetworkID": "c" * 64,
                    "IPAddress": str(
                        ipaddress.IPv4Network(
                            self.infra.network_subnet or "172.30.0.0/16"
                        ).network_address
                        + 2
                    ),
                    "IPPrefixLen": 28 if self.infra.network_subnet else 16,
                }
            },
            "Resources": {
                "NanoCpus": 1_000_000_000,
                "Memory": 536_870_912,
                "MemorySwap": 536_870_912,
            },
            "Ports": {
                "5432/tcp" if postgres else "5672/tcp": [
                    {"HostIp": self.host_ip, "HostPort": "35432" if postgres else "35672"}
                ]
            },
        }


@pytest.fixture
def setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[target.Infrastructure, FakeDocker]:
    monkeypatch.setattr(target, "FUNCTIONAL_ROOT", tmp_path)
    fake = FakeDocker()
    infra = target.Infrastructure("test-set", tmp_path / "infra", runner=fake)
    fake.infra = infra
    return infra, fake


async def test_fresh_resources_loopback_no_pull_and_secret_free_metadata(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, fake = setup
    connection = await infra.start()
    assert connection.host == "127.0.0.1"
    assert connection.postgres_port == 35432
    assert connection.amqp_port == 35672
    assert infra.metadata["concurrent_containers"] == ["e" * 64]
    assert infra.metadata["docker_server"]["Version"] == "29.0.1"
    assert infra.metadata["docker_server"]["KernelVersion"].endswith("WSL2")
    assert "Components" not in infra.metadata["docker_server"]
    assert infra.metadata["containers"]["postgres"]["image_id"] == "sha256:" + "a" * 64
    assert infra.metadata["containers"]["rabbit"]["resources"] == {
        "NanoCpus": 1_000_000_000,
        "Memory": 536_870_912,
        "MemorySwap": 536_870_912,
    }
    assert connection.postgres_password not in repr(connection)
    runs = [call for call in fake.calls if call[0].startswith("start_")]
    assert len(runs) == 2
    for _stage, args, env in runs:
        assert "--pull=never" in args
        assert env
        assert all(
            value not in args
            for value in (connection.postgres_password, connection.rabbit_password)
        )
    text = (infra.output / "infrastructure.json").read_text(encoding="utf-8")
    assert connection.postgres_password not in text
    assert connection.rabbit_password not in text
    assert "Env" not in text
    assert not any("pull" in args for _, args, _ in fake.calls)
    assert await infra.stop() == []
    stopped_ids = [args[-1] for stage, args, _ in fake.calls if stage.startswith("stop_")]
    assert stopped_ids == ["2" * 64, "1" * 64]
    assert not any("rm" in args or "prune" in args for _, args, _ in fake.calls)


async def test_existing_destination_refuses_before_any_docker_call(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, fake = setup
    infra.output.mkdir()
    sentinel = infra.output / "sentinel.txt"
    sentinel.write_text("original", encoding="utf-8")
    metadata = infra.output / "infrastructure.json"
    metadata.write_text("original metadata", encoding="utf-8")
    with pytest.raises(target.InfrastructureError, match="output_already_exists"):
        await infra.start()
    assert fake.calls == []
    assert sentinel.read_text() == "original"
    assert await infra.stop() == []
    with pytest.raises(target.InfrastructureError, match="output_not_owned"):
        await infra.export_logs()
    assert metadata.read_text() == "original metadata"


async def test_missing_frozen_image_propagates_specific_exit_and_creates_no_resources(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, fake = setup
    fake.fail_stage = "image_rabbit"
    with pytest.raises(target.InfrastructureError) as caught:
        await infra.start()
    assert caught.value.as_dict()["exit_code"] == 17
    assert caught.value.stage == "image_rabbit"
    assert all(
        not stage.startswith(("network_", "volume_", "start_")) for stage, _, _ in fake.calls
    )


async def test_image_digest_mismatch_blocks_start(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, fake = setup
    fake.wrong_image = True
    with pytest.raises(target.InfrastructureError, match="image_identity_mismatch"):
        await infra.start()


@pytest.mark.parametrize(
    "server, reason",
    [
        (None, "invalid_server_identity"),
        ({}, "invalid_server_identity"),
        (
            {"Version": "29", "ApiVersion": "1.52", "Os": "windows", "Arch": "amd64"},
            "linux_engine_required",
        ),
        (
            {"Version": "29\nsecret", "ApiVersion": "1.52", "Os": "linux", "Arch": "amd64"},
            "invalid_server_identity",
        ),
    ],
)
async def test_invalid_engine_identity_refuses_before_resource_creation(
    setup: tuple[target.Infrastructure, FakeDocker], server: object, reason: str
) -> None:
    infra, fake = setup

    async def command(stage: str, args: Sequence[str], env: Mapping[str, str] | None) -> str:
        if stage == "docker_server":
            return json.dumps(server)
        return await fake(stage, args, env)

    infra._runner = command
    with pytest.raises(target.InfrastructureError, match=reason):
        await infra.start()
    assert fake.calls == []
    assert infra.metadata["containers"] == {}


async def test_existing_volume_never_reused(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, fake = setup
    fake.existing_volume = True
    with pytest.raises(target.InfrastructureError, match="volume_already_exists"):
        await infra.start()
    assert all(not stage.startswith("start_") for stage, _, _ in fake.calls)


async def test_volume_ownership_mismatch_prevents_mounting_any_container(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, fake = setup
    fake.foreign_role = "postgres"
    with pytest.raises(target.InfrastructureError, match="ownership_mismatch") as caught:
        await infra.start()
    assert caught.value.stage == "volume_identity_postgres"
    assert all(not stage.startswith("start_") for stage, _, _ in fake.calls)
    assert infra.metadata["volumes"] == {}


async def test_inspect_diagnostics_and_environment_are_not_exported(
    setup: tuple[target.Infrastructure, FakeDocker], monkeypatch: pytest.MonkeyPatch
) -> None:
    infra, fake = setup
    original = fake.inspect

    def inspected(role: str) -> dict[str, Any]:
        info = original(role)
        info["Env"] = ["PRIVATE_TOKEN=not-exported"]
        info["State"]["Error"] = "private-command"
        info["State"]["Health"]["Log"] = [{"Output": "private-diagnostic"}]
        return info

    monkeypatch.setattr(fake, "inspect", inspected)
    await infra.start()
    assert await infra.stop() == []
    exported = (infra.output / "infrastructure.json").read_text()
    assert "PRIVATE_TOKEN" not in exported
    assert "private-" not in exported
    assert "not-exported" not in exported


async def test_partial_start_retains_original_failure_and_stops_only_created_container(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, fake = setup
    fake.fail_stage = "start_rabbit"
    with pytest.raises(target.InfrastructureError) as caught:
        await infra.start()
    assert caught.value.stage == "start_rabbit"
    assert caught.value.exit_code == 17
    assert set(infra.metadata["containers"]) == {"postgres"}
    assert infra.metadata["containers"]["postgres"]["id"] == "1" * 64
    assert await infra.stop() == []
    stops = [stage for stage, _, _ in fake.calls if stage.startswith("stop_")]
    assert stops == ["stop_postgres"]


@pytest.mark.parametrize("field", ["Image", "Mounts", "Networks"])
async def test_foreign_storage_network_or_image_refuses_exec(
    setup: tuple[target.Infrastructure, FakeDocker],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    infra, fake = setup
    await infra.start()
    original = fake.inspect

    def changed(role: str) -> dict[str, Any]:
        info = original(role)
        info[field] = {
            "Image": "sha256:" + "f" * 64,
            "Mounts": [
                {"Type": "volume", "Name": "historical-data", "Destination": "/var/lib/postgresql"}
            ],
            "Networks": {"foreign": {"NetworkID": "e" * 64}},
        }[field]
        return info

    monkeypatch.setattr(fake, "inspect", changed)
    with pytest.raises(target.InfrastructureError, match="ownership_mismatch"):
        await infra.exec_owned("postgres", ["psql", "-c", "SELECT 1"])
    assert not any(stage.startswith("exec_") for stage, _, _ in fake.calls)


async def test_stop_refuses_foreign_resource_but_stops_other_owned_resource(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, fake = setup
    await infra.start()
    fake.foreign_role = "rabbit"
    errors = await infra.stop()
    assert errors[0]["reason"] == "ownership_mismatch"
    stopped = [stage for stage, _, _ in fake.calls if stage.startswith("stop_")]
    assert stopped == ["stop_postgres"]
    assert fake.running["rabbit"] is True


async def test_exec_rechecks_ownership_before_command(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, fake = setup
    await infra.start()
    assert (
        await infra.exec_owned("rabbit", ["rabbitmqctl", "list_vhosts", "--formatter=json"]) == "[]"
    )
    fake.foreign_role = "rabbit"
    with pytest.raises(target.InfrastructureError, match="ownership_mismatch"):
        await infra.exec_owned("rabbit", ["rabbitmqctl", "add_vhost", "isolated"])
    calls = [stage for stage, _, _ in fake.calls if stage.startswith("exec_")]
    assert calls == ["exec_rabbit"]


async def test_non_loopback_port_refused(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, fake = setup
    fake.host_ip = "0.0.0.0"
    with pytest.raises(target.InfrastructureError, match="invalid_loopback_binding"):
        await infra.start()


async def test_log_export_keeps_only_controlled_markers(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, _fake = setup
    await infra.start()
    result = await infra.export_logs()
    assert "postgres" in result
    text = (infra.output / "dependency-lifecycle.json").read_text()
    assert "SECRET" not in text
    assert "database system is ready to accept connections" in text


async def test_repeated_start_is_refused(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, _fake = setup
    await infra.start()
    with pytest.raises(target.InfrastructureError, match="start_already_attempted"):
        await infra.start()


@pytest.mark.parametrize("run_id", ["../x", "UPPER", "", "x" * 41])
def test_run_id_requires_safe_namespace(run_id: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run_id"):
        target.Infrastructure(run_id, tmp_path)


def test_output_outside_new_folder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(target, "FUNCTIONAL_ROOT", tmp_path / "allowed")
    with pytest.raises(ValueError, match="inside validation/functional"):
        target.Infrastructure("safe", tmp_path)


async def test_subprocess_error_does_not_expose_args_or_stderr(
    setup: tuple[target.Infrastructure, FakeDocker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infra, _fake = setup
    monkeypatch.setattr(target.shutil, "which", lambda _name: "docker.exe")

    class Process:
        returncode = 19

        def communicate(self, timeout: float) -> tuple[bytes, bytes]:
            assert timeout > 0
            return b"", b"private token and business data"

    monkeypatch.setattr(target.subprocess, "Popen", lambda *args, **kwargs: Process())
    with pytest.raises(target.InfrastructureError) as caught:
        await infra._subprocess("probe", ["exec", "private-arg"], None)
    diagnostic = json.dumps(caught.value.as_dict())
    assert caught.value.exit_code == 19
    assert "private" not in diagnostic


async def test_subprocess_timeout_kills_and_reaps_only_its_child(
    setup: tuple[target.Infrastructure, FakeDocker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infra, _fake = setup
    monkeypatch.setattr(target.shutil, "which", lambda _name: "docker.exe")

    class Process:
        returncode: int | None = None
        killed = False

        def communicate(self, timeout: float) -> tuple[bytes, bytes]:
            if not self.killed:
                raise subprocess.TimeoutExpired("do-not-export", timeout)
            self.returncode = -1
            return b"", b""

        def kill(self) -> None:
            self.killed = True

    process = Process()
    monkeypatch.setattr(target.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(target.InfrastructureError, match="command_timeout"):
        await infra._subprocess("probe", ["version"], None)
    assert process.killed
    assert process.returncode == -1


async def test_rabbit_cli_and_health_probe_use_broker_identity(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, fake = setup
    await infra.start()
    start = next(call for call in fake.calls if call[0] == "start_rabbit")
    args = start[1]
    assert args[args.index("--health-cmd") + 1] == (
        "docker-entrypoint.sh rabbitmq-diagnostics -q check_running"
    )
    assert start[2]["RABBITMQ_CTL_ERL_ARGS"] == "+S 1:1 +A 1"
    await infra.exec_owned("rabbit", ["rabbitmqctl", "list_vhosts"])
    command = next(call[1] for call in reversed(fake.calls) if call[0] == "exec_rabbit")
    assert command[:4] == ("exec", "--user", "rabbitmq", "2" * 64)


@pytest.mark.parametrize("field", ["NanoCpus", "Memory", "MemorySwap"])
async def test_changed_resource_configuration_refuses_exec(
    setup: tuple[target.Infrastructure, FakeDocker],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    infra, fake = setup
    await infra.start()
    original = fake.inspect

    def changed(role: str) -> dict[str, Any]:
        info = original(role)
        info["Resources"][field] = 0
        return info

    monkeypatch.setattr(fake, "inspect", changed)
    with pytest.raises(target.InfrastructureError, match="ownership_mismatch"):
        await infra.exec_owned("postgres", ["psql", "-c", "SELECT 1"])
    assert not any(stage.startswith("exec_") for stage, _, _ in fake.calls)


async def test_successful_stop_command_without_stopped_container_reports_failure(
    setup: tuple[target.Infrastructure, FakeDocker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infra, fake = setup
    await infra.start()
    original = fake.inspect

    def changed(role: str) -> dict[str, Any]:
        info = original(role)
        if role == "rabbit":
            info["State"]["Running"] = True
        return info

    monkeypatch.setattr(fake, "inspect", changed)
    errors = await infra.stop()
    assert errors == [
        target.InfrastructureError("stop_rabbit", "container_still_running").as_dict()
    ]
    assert fake.running["postgres"] is False


@pytest.mark.parametrize("ports", [None, [], {"5672/tcp": [None]}, {"5672/tcp": []}])
def test_malformed_port_response_remains_a_controlled_error(ports: object) -> None:
    with pytest.raises(target.InfrastructureError, match="invalid_loopback_binding"):
        target.Infrastructure._port("rabbit", {"Ports": ports})


async def test_native_start_failure_preserves_errno_without_external_text(
    setup: tuple[target.Infrastructure, FakeDocker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infra, _fake = setup
    monkeypatch.setattr(target.shutil, "which", lambda _name: "docker.exe")

    def fail(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "secret-path-and-command")

    monkeypatch.setattr(target.subprocess, "Popen", fail)
    with pytest.raises(target.InfrastructureError) as caught:
        await infra._subprocess("inspect_rabbit", ["inspect", "not-exported"], None)
    assert caught.value.errno == 13
    assert caught.value.stage == "inspect_rabbit"
    assert "secret" not in json.dumps(caught.value.as_dict())


async def test_cookie_startup_failure_export_is_specific_and_sanitized(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    infra, fake = setup
    await infra.start()

    async def diagnostic(stage: str, args: Sequence[str], env: Mapping[str, str] | None) -> str:
        if stage == "logs_rabbit":
            return (
                "BOOT FAILED secret-key\n"
                "Error when reading /var/lib/rabbitmq/.erlang.cookie: eacces\n"
            )
        return await fake(stage, args, env)

    infra._runner = diagnostic
    result = await infra.export_logs()
    assert result["rabbit"]["markers"]["BOOT FAILED"]
    assert result["rabbit"]["markers"][
        "Error when reading /var/lib/rabbitmq/.erlang.cookie: eacces"
    ]
    assert "secret-key" not in (infra.output / "dependency-lifecycle.json").read_text()


@pytest.mark.parametrize(
    "subnet",
    [
        "10.254.240.1/28",
        "10.254.240.0/24",
        "8.8.8.0/28",
        "127.0.0.0/28",
        "169.254.0.0/28",
        "fd00::/28",
        "10.254.240.0/255.255.255.240",
        "not-a-subnet",
    ],
)
def test_invalid_explicit_subnet_is_refused_before_any_command(
    setup: tuple[target.Infrastructure, FakeDocker], subnet: str
) -> None:
    previous, fake = setup
    with pytest.raises(ValueError, match="private IPv4 /28"):
        target.Infrastructure("subnet-test", previous.output, runner=fake, network_subnet=subnet)
    assert fake.calls == []


async def test_explicit_subnet_is_applied_recorded_and_checked_after_stop(
    setup: tuple[target.Infrastructure, FakeDocker], monkeypatch: pytest.MonkeyPatch
) -> None:
    previous, fake = setup
    infra = target.Infrastructure(
        "subnet-test", previous.output, runner=fake, network_subnet="10.254.240.0/28"
    )
    fake.infra = infra
    original = fake.inspect

    def inspected(role: str) -> dict[str, Any]:
        result = original(role)
        if not fake.running[role]:
            result["Networks"]["own"].update(IPAddress="", IPPrefixLen=0)
        return result

    monkeypatch.setattr(fake, "inspect", inspected)
    await infra.start()
    arguments = next(args for stage, args, _ in fake.calls if stage == "network_create")
    assert arguments[arguments.index("--subnet") + 1] == "10.254.240.0/28"
    assert infra.metadata["requested_network_subnet"] == "10.254.240.0/28"
    assert infra.metadata["network"]["effective_subnet"] == "10.254.240.0/28"
    assert await infra.stop() == []


async def test_network_subnet_mismatch_blocks_before_volumes_or_containers(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    previous, fake = setup
    infra = target.Infrastructure(
        "subnet-test", previous.output, runner=fake, network_subnet="10.254.240.0/28"
    )
    fake.infra = infra

    async def command(stage: str, args: Sequence[str], env: Mapping[str, str] | None) -> str:
        raw = await fake(stage, args, env)
        if stage == "network_identity":
            info = json.loads(raw)
            info["IPAMConfig"][0]["Subnet"] = "10.254.241.0/28"
            return json.dumps(info)
        return raw

    infra._runner = command
    with pytest.raises(target.InfrastructureError, match="network_subnet_mismatch"):
        await infra.start()
    assert all(not stage.startswith(("volume_", "start_")) for stage, _, _ in fake.calls)


@pytest.mark.parametrize("field,value", [("IPAddress", "10.254.241.2"), ("IPPrefixLen", 24)])
async def test_running_container_must_use_the_requested_subnet(
    setup: tuple[target.Infrastructure, FakeDocker],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    previous, fake = setup
    infra = target.Infrastructure(
        "subnet-test", previous.output, runner=fake, network_subnet="10.254.240.0/28"
    )
    fake.infra = infra
    await infra.start()
    original = fake.inspect

    def inspected(role: str) -> dict[str, Any]:
        info = original(role)
        info["Networks"]["own"][field] = value
        return info

    monkeypatch.setattr(fake, "inspect", inspected)
    with pytest.raises(target.InfrastructureError, match="ownership_mismatch"):
        await infra.exec_owned("rabbit", ["rabbitmq-diagnostics", "check_running"])
    assert all(stage != "exec_rabbit" for stage, _, _ in fake.calls)


async def test_network_create_failure_never_retries_or_selects_another_subnet(
    setup: tuple[target.Infrastructure, FakeDocker],
) -> None:
    previous, fake = setup
    infra = target.Infrastructure(
        "subnet-test", previous.output, runner=fake, network_subnet="10.254.240.0/28"
    )
    fake.infra = infra
    fake.fail_stage = "network_create"
    with pytest.raises(target.InfrastructureError) as caught:
        await infra.start()
    assert caught.value.stage == "network_create"
    assert caught.value.exit_code == 17
    assert [stage for stage, _, _ in fake.calls].count("network_create") == 1
    assert all(not stage.startswith(("volume_", "start_")) for stage, _, _ in fake.calls)
