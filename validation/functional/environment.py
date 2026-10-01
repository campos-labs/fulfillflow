"""Owned Docker dependencies for the functional tool; never reuse application data."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FUNCTIONAL_ROOT = Path(__file__).resolve().parent
POSTGRES_IMAGE = (
    "postgres:18-trixie@sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280"
)
RABBIT_IMAGE = (
    "rabbitmq:4.2.4-alpine@sha256:d1b24a78c1ace826771eb8585d8f30315a64f1aad4d1fe6fcd9b4435cccc16f4"
)
PURPOSE_LABEL = "org.fulfillflow.validation.purpose"
OWNER_LABEL = "org.fulfillflow.validation.owner"
ROLE_LABEL = "org.fulfillflow.validation.role"
PURPOSE = "functional-b"
CommandRunner = Callable[[str, Sequence[str], Mapping[str, str] | None], Awaitable[str]]


class InfrastructureError(RuntimeError):
    """Controlled diagnostic: external text and command arguments are not exported."""

    def __init__(
        self,
        stage: str,
        reason: str,
        *,
        exit_code: int | None = None,
        errno: int | None = None,
        winerror: int | None = None,
    ) -> None:
        self.stage = stage
        self.reason = reason
        self.exit_code = exit_code
        self.errno = errno
        self.winerror = winerror
        super().__init__(f"{stage}: {reason}")

    def as_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "reason": self.reason,
            "type": type(self).__name__,
            "exit_code": self.exit_code,
            "errno": self.errno,
            "winerror": self.winerror,
        }


@dataclass(frozen=True)
class ConnectionInfo:
    host: str
    postgres_port: int
    amqp_port: int
    postgres_user: str
    rabbit_user: str
    postgres_password: str = field(repr=False)
    rabbit_password: str = field(repr=False)


class Infrastructure:
    """One fresh dependency pair per set; callers allocate DBs/vhosts per scenario.

    Credentials exist only in memory and the owned containers' environment. Docker
    receives variable names on argv, with their values supplied through the child
    environment. Metadata deliberately excludes Env and unfiltered inspect data.
    """

    def __init__(
        self,
        run_id: str,
        output: Path,
        *,
        runner: CommandRunner | None = None,
        command_timeout: float = 15,
        startup_timeout: float = 120,
        network_subnet: str | None = None,
    ) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", run_id):
            raise ValueError("run_id must be 1-40 lowercase letters, digits or hyphens")
        if command_timeout <= 0 or startup_timeout <= 0:
            raise ValueError("timeouts must be positive")
        self.network_subnet = self._validated_subnet(network_subnet)
        self.run_id = run_id
        self.output = output.resolve()
        if not self.output.is_relative_to(FUNCTIONAL_ROOT.resolve()):
            raise ValueError("output must be inside validation/functional")
        self.owner = f"{run_id}-{secrets.token_hex(6)}"
        self.prefix = f"ff-functional-{self.owner}"
        self.command_timeout = command_timeout
        self.startup_timeout = startup_timeout
        self._runner = runner or self._subprocess
        self._containers: dict[str, str] = {}
        self._volumes: dict[str, str] = {}
        self._network: str | None = None
        self._images: dict[str, dict[str, object]] = {}
        self._postgres_password = secrets.token_hex(24)
        self._rabbit_password = secrets.token_hex(24)
        self.connection: ConnectionInfo | None = None
        self._started = False
        self._owns_output = False
        self._deadline: float | None = None
        self._metadata: dict[str, Any] = {
            "run_id": run_id,
            "owner": self.owner,
            "purpose": PURPOSE,
            "containers": {},
            "volumes": {},
            "network": None,
            "requested_network_subnet": self.network_subnet,
            "images": {},
            "docker_server": None,
            "concurrent_containers": [],
            "resources": {
                "postgres": {"cpus": 1, "memory_bytes": 536870912},
                "rabbit": {"cpus": 1, "memory_bytes": 536870912},
            },
        }

    @property
    def metadata(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._metadata))  # type: ignore[no-any-return]

    @staticmethod
    def _validated_subnet(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[0-9.]+/28", value):
            raise ValueError("network_subnet must be an explicit private IPv4 /28")
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError:
            raise ValueError("network_subnet must be an explicit private IPv4 /28") from None
        private_ranges = (
            ipaddress.IPv4Network("10.0.0.0/8"),
            ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"),
        )
        if (
            not isinstance(network, ipaddress.IPv4Network)
            or network.prefixlen != 28
            or not any(network.subnet_of(private) for private in private_ranges)
        ):
            raise ValueError("network_subnet must be an explicit private IPv4 /28")
        return str(network)

    async def _subprocess(
        self, stage: str, args: Sequence[str], env: Mapping[str, str] | None
    ) -> str:
        docker = shutil.which("docker")
        if docker is None:
            raise InfrastructureError(stage, "docker_executable_missing")
        timeout = self.command_timeout
        if self._deadline is not None:
            timeout = min(timeout, self._deadline - time.monotonic())
        if timeout <= 0:
            raise InfrastructureError(stage, "startup_deadline_exceeded")
        child_env = os.environ.copy()
        if env:
            child_env.update(env)

        def invoke() -> bytes:
            try:
                process = subprocess.Popen(
                    [docker, *args],
                    env=child_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except OSError as exc:
                raise InfrastructureError(
                    stage,
                    "process_start_failed",
                    errno=exc.errno,
                    winerror=getattr(exc, "winerror", None),
                ) from None
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    raise InfrastructureError(stage, "command_reap_timeout") from None
                raise InfrastructureError(stage, "command_timeout") from None
            if process.returncode != 0:
                raise InfrastructureError(
                    stage, "external_command_failed", exit_code=process.returncode
                )
            return stdout + stderr if stage.startswith("logs_") else stdout

        # Psycopg's Windows SelectorEventLoop cannot create asyncio subprocesses.
        # The native command owns a finite timeout even if its awaiting task cancels.
        stdout = await asyncio.to_thread(invoke)
        try:
            return stdout.decode("utf-8").strip()
        except UnicodeError:
            raise InfrastructureError(stage, "invalid_command_encoding") from None

    async def _docker(self, stage: str, *args: str, env: Mapping[str, str] | None = None) -> str:
        return await self._runner(stage, args, env)

    @staticmethod
    def _json(stage: str, value: str) -> Any:
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            raise InfrastructureError(stage, "invalid_json_response") from None

    def _labels(self, role: str) -> list[str]:
        return [
            "--label",
            f"{PURPOSE_LABEL}={PURPOSE}",
            "--label",
            f"{OWNER_LABEL}={self.owner}",
            "--label",
            f"{ROLE_LABEL}={role}",
        ]

    def _record(self) -> None:
        if not self._owns_output:
            raise InfrastructureError("export", "output_not_owned")
        (self.output / "infrastructure.json").write_text(
            json.dumps(self._metadata, indent=2) + "\n", encoding="utf-8"
        )

    async def start(self) -> ConnectionInfo:
        if self._started:
            raise InfrastructureError("prepare", "start_already_attempted")
        self._started = True
        try:
            self.output.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise InfrastructureError("prepare", "output_already_exists") from None
        self._owns_output = True
        self._deadline = time.monotonic() + self.startup_timeout
        try:
            raw_server = await self._docker(
                "docker_server", "version", "--format", "{{json .Server}}"
            )
            self._metadata["docker_server"] = self._server_identity(raw_server)
            self._record()
            concurrent = await self._docker("inventory", "ps", "--no-trunc", "--format", "{{.ID}}")
            ids = concurrent.splitlines() if concurrent else []
            if any(not re.fullmatch(r"[a-f0-9]{64}", item) for item in ids):
                raise InfrastructureError("inventory", "invalid_container_identity")
            self._metadata["concurrent_containers"] = ids
            self._record()
            # Inspect first: no implicit pull, rebuild, or replacement is allowed.
            for role, image in (("postgres", POSTGRES_IMAGE), ("rabbit", RABBIT_IMAGE)):
                raw = await self._docker(
                    f"image_{role}",
                    "image",
                    "inspect",
                    image,
                    "--format",
                    '{"Id":{{json .Id}},"RepoDigests":{{json .RepoDigests}}}',
                )
                info = self._json(f"image_{role}", raw)
                expected_digest = image.split("@", 1)[1]
                if (
                    not isinstance(info, dict)
                    or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(info.get("Id")))
                    or not isinstance(info.get("RepoDigests"), list)
                    or not any(
                        str(item).endswith(f"@{expected_digest}") for item in info["RepoDigests"]
                    )
                ):
                    raise InfrastructureError(f"image_{role}", "image_identity_mismatch")
                self._images[role] = {"reference": image, "id": info["Id"]}
            self._metadata["images"] = self._images.copy()
            self._record()
            network_name = f"{self.prefix}-network"
            self._network = await self._docker(
                "network_create",
                "network",
                "create",
                *(("--subnet", self.network_subnet) if self.network_subnet else ()),
                *self._labels("network"),
                network_name,
            )
            if not re.fullmatch(r"[a-f0-9]{64}", self._network):
                raise InfrastructureError("network_create", "invalid_network_identity")
            self._metadata["network"] = {"id": self._network, "name": network_name}
            self._record()
            await self._inspect_network_owned(network_name)
            self._record()
            for role in ("postgres", "rabbit"):
                await self._create_container(role)
            ports: dict[str, int] = {}
            while len(ports) < 2:
                if time.monotonic() >= self._deadline:
                    raise InfrastructureError("readiness", "startup_deadline_exceeded")
                for role in ("postgres", "rabbit"):
                    if role in ports:
                        continue
                    info = await self._inspect_owned(role)
                    state = info.get("State", {})
                    if not isinstance(state, dict) or state.get("Running") is not True:
                        raise InfrastructureError(f"readiness_{role}", "container_not_running")
                    health = state.get("Health", {})
                    if isinstance(health, dict) and health.get("Status") == "healthy":
                        ports[role] = self._port(role, info)
                    elif isinstance(health, dict) and health.get("Status") == "unhealthy":
                        raise InfrastructureError(f"readiness_{role}", "container_unhealthy")
                if len(ports) < 2:
                    await asyncio.sleep(0.1)
            self.connection = ConnectionInfo(
                host="127.0.0.1",
                postgres_port=ports["postgres"],
                amqp_port=ports["rabbit"],
                postgres_user="ff_functional",
                rabbit_user="ff_functional",
                postgres_password=self._postgres_password,
                rabbit_password=self._rabbit_password,
            )
            self._metadata["ports"] = ports
            self._record()
            return self.connection
        finally:
            self._deadline = None

    @classmethod
    def _server_identity(cls, raw: str) -> dict[str, str]:
        """Keep version/platform fields only, never arbitrary engine metadata."""
        server = cls._json("docker_server", raw)
        required = ("Version", "ApiVersion", "Os", "Arch")
        optional = ("MinAPIVersion", "GitCommit", "GoVersion", "KernelVersion", "BuildTime")
        if not isinstance(server, dict) or any(
            not isinstance(server.get(key), str) or not server[key] for key in required
        ):
            raise InfrastructureError("docker_server", "invalid_server_identity")
        identity: dict[str, str] = {}
        for key in (*required, *optional):
            value = server.get(key)
            if value is None:
                continue
            if (
                not isinstance(value, str)
                or len(value) > 256
                or any(ord(character) < 32 for character in value)
            ):
                raise InfrastructureError("docker_server", "invalid_server_identity")
            identity[key] = value
        if identity["Os"] != "linux":
            raise InfrastructureError("docker_server", "linux_engine_required")
        return identity

    async def _create_container(self, role: str) -> None:
        volume = f"{self.prefix}-{role}-data"
        existing = await self._docker(
            f"volume_check_{role}",
            "volume",
            "ls",
            "--filter",
            f"name=^{volume}$",
            "--format",
            "{{.Name}}",
        )
        if existing:
            raise InfrastructureError(f"volume_check_{role}", "volume_already_exists")
        created = await self._docker(
            f"volume_{role}", "volume", "create", *self._labels(role), volume
        )
        if created != volume:
            raise InfrastructureError(f"volume_{role}", "volume_identity_mismatch")
        raw = await self._docker(
            f"volume_identity_{role}",
            "volume",
            "inspect",
            volume,
            "--format",
            '{"Name":{{json .Name}},"Labels":{{json .Labels}}}',
        )
        volume_info = self._json(f"volume_identity_{role}", raw)
        labels = volume_info.get("Labels") if isinstance(volume_info, dict) else None
        if (
            not isinstance(volume_info, dict)
            or volume_info.get("Name") != volume
            or not isinstance(labels, dict)
            or labels.get(OWNER_LABEL) != self.owner
            or labels.get(PURPOSE_LABEL) != PURPOSE
            or labels.get(ROLE_LABEL) != role
        ):
            raise InfrastructureError(f"volume_identity_{role}", "ownership_mismatch")
        self._volumes[role] = volume
        self._metadata["volumes"][role] = volume
        self._record()
        postgres = role == "postgres"
        env = (
            {
                "POSTGRES_USER": "ff_functional",
                "POSTGRES_PASSWORD": self._postgres_password,
                "POSTGRES_DB": "postgres",
            }
            if postgres
            else {
                "RABBITMQ_DEFAULT_USER": "ff_functional",
                "RABBITMQ_DEFAULT_PASS": self._rabbit_password,
                "RABBITMQ_CTL_ERL_ARGS": "+S 1:1 +A 1",
            }
        )
        args = [
            "run",
            "--detach",
            "--pull=never",
            "--name",
            f"{self.prefix}-{role}",
            *self._labels(role),
            "--network",
            str(self._network),
            "--cpus",
            "1",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--publish",
            f"127.0.0.1::{5432 if postgres else 5672}",
            "--mount",
            f"type=volume,src={volume},dst="
            f"{'/var/lib/postgresql' if postgres else '/var/lib/rabbitmq'}",
            "--health-cmd",
            "pg_isready -U ff_functional -d postgres"
            if postgres
            # Select the broker user and wait for the rabbit application, not only
            # its Erlang VM: ping can succeed while add_vhost still exits with 64.
            else "docker-entrypoint.sh rabbitmq-diagnostics -q check_running",
            "--health-interval",
            "1s",
            "--health-timeout",
            "3s",
            "--health-retries",
            "90",
        ]
        for key in env:
            args.extend(("--env", key))
        args.append(str(self._images[role]["reference"]))
        container_id = await self._docker(f"start_{role}", *args, env=env)
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise InfrastructureError(f"start_{role}", "invalid_container_identity")
        self._containers[role] = container_id
        self._metadata["containers"][role] = {"id": container_id}
        self._record()
        await self._inspect_owned(role)

    async def _inspect_network_owned(self, name: str) -> None:
        raw = await self._docker(
            "network_identity",
            "network",
            "inspect",
            str(self._network),
            "--format",
            '{"Id":{{json .Id}},"Name":{{json .Name}},"Labels":{{json .Labels}},'
            '"Driver":{{json .Driver}},"IPAMConfig":{{json .IPAM.Config}}}',
        )
        info = self._json("network_identity", raw)
        labels = info.get("Labels") if isinstance(info, dict) else None
        config = info.get("IPAMConfig") if isinstance(info, dict) else None
        if (
            not isinstance(info, dict)
            or info.get("Id") != self._network
            or info.get("Name") != name
            or info.get("Driver") != "bridge"
            or not isinstance(labels, dict)
            or labels.get(OWNER_LABEL) != self.owner
            or labels.get(PURPOSE_LABEL) != PURPOSE
            or labels.get(ROLE_LABEL) != "network"
            or not isinstance(config, list)
            or len(config) != 1
            or not isinstance(config[0], dict)
            or not isinstance(config[0].get("Subnet"), str)
        ):
            raise InfrastructureError("network_identity", "ownership_mismatch")
        try:
            subnet = ipaddress.ip_network(config[0]["Subnet"], strict=True)
        except ValueError:
            raise InfrastructureError("network_identity", "invalid_network_subnet") from None
        if not isinstance(subnet, ipaddress.IPv4Network) or (
            self.network_subnet is not None and str(subnet) != self.network_subnet
        ):
            raise InfrastructureError("network_identity", "network_subnet_mismatch")
        self._metadata["network"]["effective_subnet"] = str(subnet)

    def _matches_network(self, value: object, *, running: bool) -> bool:
        if not isinstance(value, dict) or value.get("NetworkID") != self._network:
            return False
        if self.network_subnet is None:
            return True
        if not running and value.get("IPAddress") == "" and value.get("IPPrefixLen") == 0:
            return True  # Docker releases the endpoint address after graceful stop.
        try:
            address = ipaddress.IPv4Address(value.get("IPAddress"))
        except (ValueError, TypeError):
            return False
        subnet = ipaddress.IPv4Network(self.network_subnet)
        return (
            value.get("IPPrefixLen") == subnet.prefixlen
            and address in subnet
            and address not in (subnet.network_address, subnet.broadcast_address)
        )

    async def _inspect_owned(self, role: str) -> dict[str, Any]:
        container_id = self._containers.get(role)
        if container_id is None:
            raise InfrastructureError(f"inspect_{role}", "container_not_owned")
        raw = await self._docker(
            f"inspect_{role}",
            "inspect",
            "--type",
            "container",
            container_id,
            "--format",
            '{"Id":{{json .Id}},"Image":{{json .Image}},'
            '"Labels":{{json .Config.Labels}},"State":{{json .State}},'
            '"Mounts":{{json .Mounts}},"Ports":{{json .NetworkSettings.Ports}},'
            '"Networks":{{json .NetworkSettings.Networks}},'
            '"Resources":{"NanoCpus":{{json .HostConfig.NanoCpus}},'
            '"Memory":{{json .HostConfig.Memory}},'
            '"MemorySwap":{{json .HostConfig.MemorySwap}}}}',
        )
        info = self._json(f"inspect_{role}", raw)
        labels = info.get("Labels") if isinstance(info, dict) else None
        mounts = info.get("Mounts") if isinstance(info, dict) else None
        networks = info.get("Networks") if isinstance(info, dict) else None
        resources = info.get("Resources") if isinstance(info, dict) else None
        state = info.get("State") if isinstance(info, dict) else None
        running = isinstance(state, dict) and state.get("Running") is True
        destination = "/var/lib/postgresql" if role == "postgres" else "/var/lib/rabbitmq"
        if (
            not isinstance(info, dict)
            or info.get("Id") != container_id
            or info.get("Image") != self._images[role]["id"]
            or not isinstance(labels, dict)
            or labels.get(PURPOSE_LABEL) != PURPOSE
            or labels.get(OWNER_LABEL) != self.owner
            or labels.get(ROLE_LABEL) != role
            or not isinstance(mounts, list)
            or len(mounts) != 1
            or not isinstance(mounts[0], dict)
            or mounts[0].get("Type") != "volume"
            or mounts[0].get("Name") != self._volumes[role]
            or mounts[0].get("Destination") != destination
            or mounts[0].get("RW") is not True
            or not isinstance(resources, dict)
            or resources.get("NanoCpus") != 1_000_000_000
            or resources.get("Memory") != 536_870_912
            or resources.get("MemorySwap") != 536_870_912
            or not isinstance(networks, dict)
            or len(networks) != 1
            or not all(self._matches_network(item, running=running) for item in networks.values())
        ):
            raise InfrastructureError(f"inspect_{role}", "ownership_mismatch")
        self._metadata["containers"][role]["image_id"] = info["Image"]
        self._metadata["containers"][role]["resources"] = {
            key: resources[key] for key in ("NanoCpus", "Memory", "MemorySwap")
        }
        return info

    @staticmethod
    def _port(role: str, info: Mapping[str, Any]) -> int:
        key = "5432/tcp" if role == "postgres" else "5672/tcp"
        ports = info.get("Ports")
        entries = ports.get(key) if isinstance(ports, dict) else None
        if (
            not isinstance(entries, list)
            or len(entries) != 1
            or not isinstance(entries[0], dict)
            or entries[0].get("HostIp") != "127.0.0.1"
            or not str(entries[0].get("HostPort", "")).isdigit()
        ):
            raise InfrastructureError(f"ports_{role}", "invalid_loopback_binding")
        port = int(entries[0]["HostPort"])
        if not 1 <= port <= 65535:
            raise InfrastructureError(f"ports_{role}", "invalid_loopback_binding")
        return port

    async def exec_owned(self, role: str, args: Sequence[str]) -> str:
        if role not in {"postgres", "rabbit"} or not args:
            raise ValueError("exec requires an owned role and explicit arguments")
        await self._inspect_owned(role)
        user_args = ("--user", "rabbitmq") if role == "rabbit" else ()
        return await self._docker(f"exec_{role}", "exec", *user_args, self._containers[role], *args)

    async def export_logs(self) -> dict[str, object]:
        """Export startup/lifecycle categories, never arbitrary database/broker text."""
        if not self._owns_output:
            raise InfrastructureError("export", "output_not_owned")
        result: dict[str, object] = {}
        tokens = {
            "postgres": (
                "database system is ready to accept connections",
                "database system is shut down",
            ),
            "rabbit": (
                "Server startup complete",
                "Stopping RabbitMQ",
                "BOOT FAILED",
                "Error when reading /var/lib/rabbitmq/.erlang.cookie: eacces",
            ),
        }
        for role, container_id in self._containers.items():
            try:
                await self._inspect_owned(role)
                raw = await self._docker(f"logs_{role}", "logs", "--tail", "200", container_id)
                result[role] = {"markers": {token: token in raw for token in tokens[role]}}
            except InfrastructureError as exc:
                result[role] = {"error": exc.as_dict()}
        (self.output / "dependency-lifecycle.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        return result

    async def stop(self) -> list[dict[str, object]]:
        """Stop exact owned IDs only; preserve containers, networks and named volumes."""
        errors: list[dict[str, object]] = []
        for role, container_id in reversed(tuple(self._containers.items())):
            try:
                info = await self._inspect_owned(role)
                if info.get("State", {}).get("Running") is True:
                    await self._docker(f"stop_{role}", "stop", "--time", "10", container_id)
                after = await self._inspect_owned(role)
                if after.get("State", {}).get("Running") is not False:
                    raise InfrastructureError(f"stop_{role}", "container_still_running")
                self._metadata["containers"][role]["final_state"] = {
                    key: after.get("State", {}).get(key)
                    for key in ("Status", "Running", "ExitCode", "OOMKilled", "FinishedAt")
                }
            except InfrastructureError as exc:
                errors.append(exc.as_dict())
        self._metadata["shutdown_errors"] = errors
        if self._owns_output and self.output.is_dir():
            self._record()
        return errors
