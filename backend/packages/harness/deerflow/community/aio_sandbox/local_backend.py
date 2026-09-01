"""Local container backend for sandbox provisioning.

Manages sandbox containers using Docker or Apple Container on the local machine.
Handles container lifecycle, port allocation, and cross-process container discovery.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import shlex
import subprocess
import time
import uuid
from datetime import datetime
from typing import Literal

from deerflow.community.remote_file_authority import PRIVATE_ROOT_BOOTSTRAP_SCRIPT
from deerflow.utils.network import get_free_port, release_port

from .backend import SandboxBackend, wait_for_sandbox_ready
from .sandbox_info import SandboxInfo

logger = logging.getLogger(__name__)

_APPLE_MANAGED_LABEL = "io.actweave.sandbox.managed"
_APPLE_SCHEMA_LABEL = "io.actweave.sandbox.schema"
_APPLE_SCHEMA_VERSION = "1"
_PRIVATE_OWNER_LABEL = "io.actweave.run-mount-owner"


def _validated_private_owner_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = uuid.UUID(hex=value)
    except (AttributeError, ValueError):
        raise RuntimeError("Invalid private container owner") from None
    if parsed.hex != value:
        raise RuntimeError("Invalid private container owner")
    return value


def _parse_docker_timestamp(raw: str) -> float:
    """Parse Docker's ISO 8601 timestamp into a Unix epoch float.

    Docker returns timestamps with nanosecond precision and a trailing ``Z``
    (e.g. ``2026-04-08T01:22:50.123456789Z``).  Python's ``fromisoformat``
    accepts at most microseconds and (pre-3.11) does not accept ``Z``, so the
    string is normalized before parsing.  Returns ``0.0`` on empty input or
    parse failure so callers can use ``0.0`` as a sentinel for "unknown age".
    """
    if not raw:
        return 0.0
    try:
        s = raw.strip()
        if "." in s:
            dot_pos = s.index(".")
            tz_start = dot_pos + 1
            while tz_start < len(s) and s[tz_start].isdigit():
                tz_start += 1
            frac = s[dot_pos + 1 : tz_start][:6]  # truncate to microseconds
            tz_suffix = s[tz_start:]
            s = s[: dot_pos + 1] + frac + tz_suffix
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError) as e:
        logger.debug(f"Could not parse docker timestamp {raw!r}: {e}")
        return 0.0


def _extract_host_port(inspect_entry: dict, container_port: int) -> int | None:
    """Extract the host port mapped to ``container_port/tcp`` from a docker inspect entry.

    Returns None if the container has no port mapping for that port.
    """
    try:
        ports = (inspect_entry.get("NetworkSettings") or {}).get("Ports") or {}
        bindings = ports.get(f"{container_port}/tcp") or []
        if bindings:
            host_port = bindings[0].get("HostPort")
            if host_port:
                return int(host_port)
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _parse_apple_container_payload(raw: str, *, operation: str) -> list[dict]:
    """Parse Apple Container's version-1 structured container output.

    Both ``container list --format json`` and ``container inspect`` return a
    top-level JSON array of managed-container objects. Keep this parser strict
    so a CLI schema change cannot be mistaken for a dead container.
    """
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse Apple Container {operation} output") from exc
    if not isinstance(payload, list) or any(not isinstance(entry, dict) for entry in payload):
        raise RuntimeError(f"Failed to parse Apple Container {operation} output")
    return payload


def _is_managed_apple_sandbox(entry: dict) -> bool:
    """Return whether an Apple container is owned by this backend schema."""
    container_id = entry.get("id")
    configuration = entry.get("configuration")
    if not isinstance(container_id, str) or not isinstance(configuration, dict):
        return False
    if configuration.get("id") != container_id:
        return False
    labels = configuration.get("labels")
    return isinstance(labels, dict) and labels.get(_APPLE_MANAGED_LABEL) == "true" and labels.get(_APPLE_SCHEMA_LABEL) == _APPLE_SCHEMA_VERSION


def _extract_apple_sandbox_ipv4(entry: dict) -> str | None:
    """Return the usable IPv4 address on Apple Container's default VM network."""
    status = entry.get("status")
    if not isinstance(status, dict) or str(status.get("state", "")).lower() != "running":
        return None
    networks = status.get("networks")
    if not isinstance(networks, list):
        return None
    for network in networks:
        if not isinstance(network, dict) or network.get("network") != "default":
            continue
        raw_address = network.get("ipv4Address")
        if not isinstance(raw_address, str) or not raw_address:
            continue
        try:
            interface = ipaddress.ip_interface(raw_address)
        except ValueError:
            continue
        address = interface.ip
        if not isinstance(address, ipaddress.IPv4Address):
            continue
        if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local:
            continue
        return str(address)
    return None


def _apple_sandbox_url(entry: dict) -> str | None:
    address = _extract_apple_sandbox_ipv4(entry)
    return f"http://{address}:8080" if address else None


def _format_container_mount(runtime: str, host_path: str, container_path: str, read_only: bool) -> list[str]:
    """Format a bind-mount argument for the selected runtime.

    Docker's ``-v host:container`` syntax is ambiguous for Windows drive-letter
    paths like ``D:/...`` because ``:`` is both the drive separator and the
    volume separator. Use ``--mount type=bind,...`` for Docker to avoid that
    parsing ambiguity. Apple Container keeps using ``-v``.
    """
    if runtime == "docker":
        mount_spec = f"type=bind,src={host_path},dst={container_path}"
        if read_only:
            mount_spec += ",readonly"
        return ["--mount", mount_spec]

    mount_spec = f"{host_path}:{container_path}"
    if read_only:
        mount_spec += ":ro"
    return ["-v", mount_spec]


def _redact_container_command_for_log(cmd: list[str]) -> list[str]:
    """Return a Docker/Container command with secrets and host paths redacted."""
    redacted: list[str] = []
    redact_next_env = False
    redact_next_mount = False
    redact_next_volume = False

    def redact_mount_spec(value: str) -> str:
        return ",".join(f"{part.split('=', 1)[0]}=<redacted>" if part.startswith(("src=", "source=")) else part for part in value.split(","))

    def redact_volume_spec(value: str) -> str:
        target_offset = value.find(":/")
        return f"<redacted>{value[target_offset:]}" if target_offset >= 0 else "<redacted>"

    for arg in cmd:
        if redact_next_env:
            if "=" in arg:
                key = arg.split("=", 1)[0]
                redacted.append(f"{key}=<redacted>" if key else "<redacted>")
            else:
                redacted.append(arg)
            redact_next_env = False
            continue

        if redact_next_mount:
            redacted.append(redact_mount_spec(arg))
            redact_next_mount = False
            continue

        if redact_next_volume:
            redacted.append(redact_volume_spec(arg))
            redact_next_volume = False
            continue

        if arg in {"-e", "--env"}:
            redacted.append(arg)
            redact_next_env = True
            continue

        if arg.startswith("--env="):
            value = arg.removeprefix("--env=")
            if "=" in value:
                key = value.split("=", 1)[0]
                redacted.append(f"--env={key}=<redacted>" if key else "--env=<redacted>")
            else:
                redacted.append(arg)
            continue

        if arg == "--mount":
            redacted.append(arg)
            redact_next_mount = True
            continue

        if arg in {"-v", "--volume"}:
            redacted.append(arg)
            redact_next_volume = True
            continue

        if arg.startswith("--mount="):
            redacted.append(f"--mount={redact_mount_spec(arg.removeprefix('--mount='))}")
            continue

        if arg.startswith("--volume="):
            redacted.append(f"--volume={redact_volume_spec(arg.removeprefix('--volume='))}")
            continue

        redacted.append(arg)

    return redacted


def _format_container_command_for_log(cmd: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return shlex.join(cmd)


def _normalize_sandbox_host(host: str) -> str:
    return host.strip().lower()


def _is_ipv6_loopback_sandbox_host(host: str) -> bool:
    return _normalize_sandbox_host(host) in {"::1", "[::1]"}


def _is_loopback_sandbox_host(host: str) -> bool:
    return _normalize_sandbox_host(host) in {"", "localhost", "127.0.0.1", "::1", "[::1]"}


def _resolve_docker_bind_host(sandbox_host: str | None = None, bind_host: str | None = None) -> str:
    """Choose the host interface for legacy Docker ``-p`` sandbox publishing.

    Host-process runs talk to sandboxes through localhost and should not expose
    the sandbox HTTP API on every host interface. Non-loopback sandbox hosts
    keep the legacy broad bind unless operators opt into a narrower bind with
    ``ACT_WEAVE_SANDBOX_BIND_HOST``. When operators choose
    an IPv6 loopback sandbox host, bind Docker to IPv6 loopback as well so the
    advertised sandbox URL and published socket use the same address family.
    """
    explicit_bind = bind_host if bind_host is not None else os.environ.get("ACT_WEAVE_SANDBOX_BIND_HOST")
    if explicit_bind is not None:
        explicit_bind = explicit_bind.strip()
        if explicit_bind:
            logger.debug("Docker sandbox bind: %s (explicit bind host override)", explicit_bind)
            return explicit_bind

    host = sandbox_host if sandbox_host is not None else os.environ.get("ACT_WEAVE_SANDBOX_HOST", "localhost")
    if _is_ipv6_loopback_sandbox_host(host):
        logger.debug("Docker sandbox bind: [::1] (IPv6 loopback sandbox host)")
        return "[::1]"
    if _is_loopback_sandbox_host(host):
        logger.debug("Docker sandbox bind: 127.0.0.1 (loopback default)")
        return "127.0.0.1"

    logger.debug("Docker sandbox bind: 0.0.0.0 (non-loopback sandbox host compatibility)")
    return "0.0.0.0"


def _is_no_such_container_error(stderr: str, container_name: str) -> bool:
    """Return True only when stderr definitively says the container does not exist.

    Docker reports "No such object" / "No such container". Apple Container
    reports a generic "not found", so that phrase is only trusted when the
    message also names the inspected container (or refers to a
    container/object); transient failures whose text happens to contain
    "not found" (e.g. "command not found", "context not found") must stay on
    the raise path instead of being misread as a dead container.
    """
    message = stderr.lower()
    if "no such object" in message or "no such container" in message:
        return True
    if "not found" not in message:
        return False
    return container_name.lower() in message or "container" in message or "object" in message


def _is_container_name_conflict(error: str) -> bool:
    message = error.lower()
    return "is already in use by container" in message or "conflict. the container name" in message or ("container with id" in message and "already exists" in message)


class LocalContainerBackend(SandboxBackend):
    """Backend that manages sandbox containers locally using Docker or Apple Container.

    On macOS, automatically prefers Apple Container if available, otherwise falls back to Docker.
    On other platforms, uses Docker.

    Features:
    - Deterministic container naming for cross-process discovery
    - Port allocation with thread-safe utilities
    - Container lifecycle management (start/stop with --rm)
    - Support for volume mounts and environment variables
    """

    def __init__(
        self,
        *,
        image: str,
        base_port: int,
        container_prefix: str,
        config_mounts: list,
        environment: dict[str, str],
    ):
        """Initialize the local container backend.

        Args:
            image: Container image to use.
            base_port: Base port number to start searching for free ports.
            container_prefix: Prefix for container names (e.g., "deer-flow-sandbox").
            config_mounts: Volume mount configurations from config (list of VolumeMountConfig).
            environment: Environment variables to inject into containers.
        """
        self._image = image
        self._base_port = base_port
        self._container_prefix = container_prefix
        self._config_mounts = config_mounts
        self._environment = environment
        self._runtime = self._detect_runtime()

    @property
    def runtime(self) -> str:
        """The detected container runtime ("docker" or "container")."""
        return self._runtime

    def _detect_runtime(self) -> str:
        """Detect which container runtime to use.

        On macOS, prefer Apple Container if available, otherwise fall back to Docker.
        On other platforms, use Docker.

        Returns:
            "container" for Apple Container, "docker" for Docker.
        """
        import platform

        if platform.system() == "Darwin":
            try:
                result = subprocess.run(
                    ["container", "--version"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                logger.info(f"Detected Apple Container: {result.stdout.strip()}")
                return "container"
            except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                logger.info("Apple Container not available, falling back to Docker")

        return "docker"

    # ── SandboxBackend interface ──────────────────────────────────────────

    def create(
        self,
        thread_id: str | None,
        sandbox_id: str,
        extra_mounts: list[tuple[str, str, bool]] | None = None,
        *,
        user_id: str | None = None,
    ) -> SandboxInfo:
        """Start a new container and return its connection info.

        Args:
            thread_id: Thread ID for which the sandbox is being created. Useful for backends that want to organize sandboxes by thread.
            sandbox_id: Deterministic sandbox identifier (used in container name).
            extra_mounts: Additional volume mounts as (host_path, container_path, read_only) tuples.
            user_id: User bucket already reflected in extra_mounts. Accepted for
                interface compatibility with remote backends.

        Returns:
            SandboxInfo with container details.

        Raises:
            RuntimeError: If the container fails to start.
        """
        return self._create(
            thread_id,
            sandbox_id,
            extra_mounts,
            user_id=user_id,
            private=False,
            private_owner_id=None,
        )

    def create_private(
        self,
        thread_id: str | None,
        sandbox_id: str,
        extra_mounts: list[tuple[str, str, bool]] | None = None,
        *,
        user_id: str | None = None,
        private_owner_id: str | None = None,
    ) -> SandboxInfo:
        """Start one fresh private container without global config mounts."""

        private_owner_id = _validated_private_owner_id(private_owner_id)
        if not sandbox_id.startswith("private-") or any(not read_only for _host, _container, read_only in (extra_mounts or ())):
            raise RuntimeError("Invalid private container request")
        return self._create(
            thread_id,
            sandbox_id,
            extra_mounts,
            user_id=user_id,
            private=True,
            private_owner_id=private_owner_id,
        )

    def _create(
        self,
        thread_id: str | None,
        sandbox_id: str,
        extra_mounts: list[tuple[str, str, bool]] | None,
        *,
        user_id: str | None,
        private: bool,
        private_owner_id: str | None,
    ) -> SandboxInfo:
        """Shared local create path with private discovery/mount isolation."""

        del thread_id, user_id
        container_name = f"{self._container_prefix}-{sandbox_id}"
        start_options = (
            {
                "include_config_mounts": False,
                "private_owner_id": private_owner_id,
            }
            if private
            else {}
        )

        if self._runtime == "container":
            try:
                container_id = self._start_container(
                    container_name,
                    None,
                    extra_mounts,
                    **start_options,
                )
            except RuntimeError as exc:
                if not private and _is_container_name_conflict(str(exc)):
                    logger.warning(
                        "Apple container name %s already exists, attempting discovery",
                        container_name,
                    )
                    existing = self.discover(sandbox_id)
                    if existing is not None:
                        return existing
                raise

            try:
                entry = self._wait_for_apple_container_network(container_name, timeout=10)
                sandbox_url = _apple_sandbox_url(entry) if entry is not None else None
                if not sandbox_url:
                    raise RuntimeError("Apple sandbox container did not receive a usable network address")
                configuration = entry.get("configuration")
                created_raw = configuration.get("creationDate", "") if isinstance(configuration, dict) else ""
                return SandboxInfo(
                    sandbox_id=sandbox_id,
                    sandbox_url=sandbox_url,
                    container_name=container_name,
                    container_id=container_id,
                    created_at=_parse_docker_timestamp(str(created_raw)) or time.time(),
                )
            except BaseException:
                self._stop_private_container(container_id or container_name)
                raise

        # Retry loop: if Docker rejects the port (e.g. a stale container still
        # holds the binding after a process restart), skip that port and try the
        # next one.  The socket-bind check in get_free_port mirrors Docker's
        # 0.0.0.0 bind, but Docker's port-release can be slightly asynchronous,
        # so a reactive fallback here ensures we always make progress.
        _next_start = self._base_port
        container_id: str | None = None
        port: int = 0
        for _attempt in range(10):
            port = get_free_port(start_port=_next_start)
            try:
                container_id = self._start_container(
                    container_name,
                    port,
                    extra_mounts,
                    **start_options,
                )
                break
            except RuntimeError as exc:
                release_port(port)
                err = str(exc)
                err_lower = err.lower()
                # Port already bound: skip this port and retry with the next one.
                if "port is already allocated" in err or "address already in use" in err_lower:
                    logger.warning(f"Port {port} rejected by Docker (already allocated), retrying with next port")
                    _next_start = port + 1
                    continue
                # Container-name conflict: another process may have already started
                # the deterministic sandbox container for this sandbox_id. Try to
                # discover and adopt the existing container instead of failing.
                if not private and _is_container_name_conflict(err_lower):
                    logger.warning(f"Container name {container_name} already in use, attempting to discover existing sandbox instance")
                    existing = self.discover(sandbox_id)
                    if existing is not None:
                        return existing
                raise
        else:
            raise RuntimeError("Could not start sandbox container: all candidate ports are already allocated by Docker")

        # A non-loopback Sandbox host can expose containers through
        # host.docker.internal rather than localhost.
        sandbox_host = os.environ.get("ACT_WEAVE_SANDBOX_HOST", "localhost")
        return SandboxInfo(
            sandbox_id=sandbox_id,
            sandbox_url=f"http://{sandbox_host}:{port}",
            container_name=container_name,
            container_id=container_id,
        )

    def destroy(self, info: SandboxInfo) -> None:
        """Stop the container and release its port."""
        # Prefer container_id, fall back to container_name (both accepted by docker stop).
        # This ensures containers discovered via list_running() (which only has the name)
        # can also be stopped.
        stop_target = info.container_id or info.container_name
        if stop_target:
            self._stop_container(stop_target)
        if self._runtime == "docker":
            self._release_sandbox_port(info)

    def destroy_private(self, info: SandboxInfo) -> None:
        """Strictly destroy a private container or keep its lease retryable."""

        stop_target = info.container_id or info.container_name
        if not stop_target:
            raise RuntimeError("Private container identity is unavailable")
        self._stop_private_container(stop_target)
        if self._runtime == "docker":
            self._release_sandbox_port(info)

    def readback_private_owner_state(
        self,
        info: SandboxInfo,
        owner_id: str,
    ) -> Literal["active", "absent"]:
        """Read back exact owner-label presence without guessing on failures."""

        owner_id = _validated_private_owner_id(owner_id) or ""
        container_name = info.container_name
        if not container_name or not info.sandbox_id.startswith("private-"):
            raise RuntimeError("Private container identity is unavailable")
        if self._runtime == "container":
            entry = self._inspect_apple_container(container_name)
            if entry is None:
                return "absent"
            configuration = entry.get("configuration")
            labels = configuration.get("labels") if isinstance(configuration, dict) else None
        else:
            entry = self._inspect_docker_container(container_name)
            if entry is None:
                return "absent"
            configuration = entry.get("Config")
            labels = configuration.get("Labels") if isinstance(configuration, dict) else None
        if not isinstance(labels, dict) or labels.get(_APPLE_MANAGED_LABEL) != "true" or labels.get(_APPLE_SCHEMA_LABEL) != _APPLE_SCHEMA_VERSION or labels.get(_PRIVATE_OWNER_LABEL) != owner_id:
            raise RuntimeError("Private container owner label mismatch")
        return "active"

    def readback_private_run_mount_state(
        self,
        info: SandboxInfo,
        owner_id: str,
        *,
        daemon_source: str,
        container_path: str,
    ) -> Literal["active", "absent"]:
        """Read back the exact owner and Docker-daemon mount coordinates."""

        if type(daemon_source) is not str or not daemon_source or type(container_path) is not str or not container_path.startswith("/"):
            raise RuntimeError("Private run read-only mount mismatch")
        state = self.readback_private_owner_state(info, owner_id)
        if state == "absent" or self._runtime != "docker":
            return state
        entry = self._inspect_docker_container(info.container_name)
        if entry is None:
            return "absent"
        mounts = entry.get("Mounts")
        if not isinstance(mounts, list):
            raise RuntimeError("Private run read-only mount mismatch")
        matches = [mount for mount in mounts if isinstance(mount, dict) and mount.get("Destination") == container_path]
        if len(matches) != 1 or matches[0].get("Type") != "bind" or matches[0].get("Source") != daemon_source or matches[0].get("RW") is not False:
            raise RuntimeError("Private run read-only mount mismatch")
        return "active"

    def run_readonly_mounts_ready(self) -> bool:
        """Probe the selected local runtime without exposing daemon output."""

        command = ["docker", "info", "--format", "{{json .ServerVersion}}"] if self._runtime == "docker" else ["container", "list", "--format", "json"]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return False
            payload = json.loads(result.stdout or "")
        except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
            return False
        if self._runtime == "docker":
            return type(payload) is str and bool(payload.strip())
        return isinstance(payload, list)

    def ensure_private_owner_absent(
        self,
        owner_id: str,
        *,
        expected_sandbox_id: str | None,
    ) -> None:
        """Destroy and read back every exact owner-labeled private container."""

        owner_id = _validated_private_owner_id(owner_id) or ""
        if expected_sandbox_id is not None:
            if type(expected_sandbox_id) is not str or not expected_sandbox_id.startswith("private-") or len(expected_sandbox_id) > 255:
                raise RuntimeError("Private container identity is unavailable")
            expected = SandboxInfo(
                sandbox_id=expected_sandbox_id,
                sandbox_url="",
                container_name=(f"{self._container_prefix}-{expected_sandbox_id}"),
                container_id=(f"{self._container_prefix}-{expected_sandbox_id}"),
            )
            state = self.readback_private_owner_state(expected, owner_id)
            if state == "active":
                self.destroy_private(expected)
            if self.readback_private_owner_state(expected, owner_id) != "absent":
                raise RuntimeError("Private container absence was not confirmed")

        observed = self._list_private_owner_strict(owner_id)
        for info in observed:
            if expected_sandbox_id is not None and info.sandbox_id != expected_sandbox_id:
                raise RuntimeError("Private container owner is ambiguous")
            self.destroy_private(info)
        if self._list_private_owner_strict(owner_id):
            raise RuntimeError("Private container absence was not confirmed")

    def _list_private_owner_strict(
        self,
        owner_id: str,
    ) -> list[SandboxInfo]:
        """Enumerate one exact owner label; runtime failures are never absence."""

        owner_id = _validated_private_owner_id(owner_id) or ""
        if self._runtime == "container":
            try:
                result = subprocess.run(
                    ["container", "list", "--format", "json"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(
                    "Failed to enumerate private containers",
                ) from exc
            if result.returncode != 0:
                raise RuntimeError("Failed to enumerate private containers")
            entries = _parse_apple_container_payload(
                result.stdout,
                operation="private owner list",
            )
            infos: list[SandboxInfo] = []
            prefix = f"{self._container_prefix}-"
            for entry in entries:
                container_name = entry.get("id")
                configuration = entry.get("configuration")
                labels = configuration.get("labels") if isinstance(configuration, dict) else None
                if (
                    not isinstance(container_name, str)
                    or not container_name.startswith(prefix)
                    or not isinstance(labels, dict)
                    or labels.get(_APPLE_MANAGED_LABEL) != "true"
                    or labels.get(_APPLE_SCHEMA_LABEL) != _APPLE_SCHEMA_VERSION
                    or labels.get(_PRIVATE_OWNER_LABEL) != owner_id
                ):
                    continue
                sandbox_id = container_name.removeprefix(prefix)
                if not sandbox_id.startswith("private-"):
                    raise RuntimeError("Private container identity is unavailable")
                infos.append(
                    SandboxInfo(
                        sandbox_id=sandbox_id,
                        sandbox_url=_apple_sandbox_url(entry) or "",
                        container_name=container_name,
                        container_id=container_name,
                    )
                )
            return infos

        try:
            result = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    f"label={_APPLE_MANAGED_LABEL}=true",
                    "--filter",
                    f"label={_APPLE_SCHEMA_LABEL}={_APPLE_SCHEMA_VERSION}",
                    "--filter",
                    f"label={_PRIVATE_OWNER_LABEL}={owner_id}",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Failed to enumerate private containers") from exc
        if result.returncode != 0:
            raise RuntimeError("Failed to enumerate private containers")
        names = tuple(name.strip() for name in result.stdout.splitlines() if name.strip())
        prefix = f"{self._container_prefix}-"
        infos = []
        for container_name in names:
            if not container_name.startswith(prefix):
                raise RuntimeError("Private container identity is unavailable")
            entry = self._inspect_docker_container(container_name)
            if entry is None:
                continue
            configuration = entry.get("Config")
            labels = configuration.get("Labels") if isinstance(configuration, dict) else None
            if not isinstance(labels, dict) or labels.get(_APPLE_MANAGED_LABEL) != "true" or labels.get(_APPLE_SCHEMA_LABEL) != _APPLE_SCHEMA_VERSION or labels.get(_PRIVATE_OWNER_LABEL) != owner_id:
                raise RuntimeError("Private container owner label mismatch")
            sandbox_id = container_name.removeprefix(prefix)
            if not sandbox_id.startswith("private-"):
                raise RuntimeError("Private container identity is unavailable")
            infos.append(
                SandboxInfo(
                    sandbox_id=sandbox_id,
                    sandbox_url="",
                    container_name=container_name,
                    container_id=(entry.get("Id") or container_name),
                )
            )
        return infos

    def initialize_private_roots(self, info: SandboxInfo) -> None:
        """Create fixed private roots for the image's unprivileged user.

        The AIO HTTP process runs as ``gem`` and cannot create entries below the
        image-owned ``/mnt`` directory.  Bootstrap through the local container
        runtime as root with one fixed Python program, then let the descriptor-
        based guest authority verify the roots through the ordinary HTTP path.
        """

        expected_name = f"{self._container_prefix}-{info.sandbox_id}"
        if self._runtime not in {"container", "docker"} or not info.sandbox_id.startswith("private-") or info.container_name != expected_name:
            raise RuntimeError("Private container identity is unavailable")

        command = [
            self._runtime,
            "exec",
            "--user",
            "0:0",
            expected_name,
            "/usr/bin/python3",
            "-I",
            "-S",
            "-c",
            PRIVATE_ROOT_BOOTSTRAP_SCRIPT,
        ]
        try:
            subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError("Failed to initialize private sandbox roots") from exc

    @staticmethod
    def _release_sandbox_port(info: SandboxInfo) -> None:
        # Extract port from sandbox_url for release
        try:
            from urllib.parse import urlparse

            port = urlparse(info.sandbox_url).port
            if port:
                release_port(port)
        except Exception:
            pass

    def is_alive(self, info: SandboxInfo) -> bool:
        """Check if the container is still running (lightweight, no HTTP)."""
        if self._runtime == "container" and info.container_name:
            entry = self._inspect_apple_container(info.container_name)
            return entry is not None and _is_managed_apple_sandbox(entry) and _apple_sandbox_url(entry) == info.sandbox_url
        if info.container_name:
            return self._is_container_running(info.container_name)
        return False

    def discover(self, sandbox_id: str) -> SandboxInfo | None:
        """Discover an existing container by its deterministic name.

        Checks if a container with the expected name is running, retrieves its
        port, and verifies it responds to health checks.

        Args:
            sandbox_id: The deterministic sandbox ID (determines container name).

        Returns:
            SandboxInfo if container found and healthy, None otherwise. A
            failed runtime check (e.g. transient daemon error) also returns
            None — discovery must not adopt a container it cannot verify, and
            falling through to create keeps acquire recoverable instead of
            hard-failing on a hiccup.
        """
        container_name = f"{self._container_prefix}-{sandbox_id}"

        if self._runtime == "container":
            try:
                entry = self._inspect_apple_container(container_name)
            except RuntimeError as exc:
                logger.warning(
                    "Could not verify Apple container %s during discovery; not adopting it: %s",
                    container_name,
                    exc,
                )
                return None
            if entry is None or not _is_managed_apple_sandbox(entry):
                return None
            sandbox_url = _apple_sandbox_url(entry)
            if not sandbox_url or not wait_for_sandbox_ready(sandbox_url, timeout=5):
                return None
            configuration = entry.get("configuration")
            created_raw = configuration.get("creationDate", "") if isinstance(configuration, dict) else ""
            return SandboxInfo(
                sandbox_id=sandbox_id,
                sandbox_url=sandbox_url,
                container_name=container_name,
                container_id=container_name,
                created_at=_parse_docker_timestamp(str(created_raw)),
            )

        try:
            running = self._is_container_running(container_name)
        except RuntimeError as e:
            logger.warning(f"Could not verify container {container_name} during discovery; not adopting it: {e}")
            return None

        if not running:
            return None

        port = self._get_container_port(container_name)
        if port is None:
            return None

        sandbox_host = os.environ.get("ACT_WEAVE_SANDBOX_HOST", "localhost")
        sandbox_url = f"http://{sandbox_host}:{port}"
        if not wait_for_sandbox_ready(sandbox_url, timeout=5):
            return None

        return SandboxInfo(
            sandbox_id=sandbox_id,
            sandbox_url=sandbox_url,
            container_name=container_name,
        )

    def list_running(self) -> list[SandboxInfo]:
        """Enumerate all running containers matching the configured prefix.

        Uses a single ``docker ps`` call to list container names, then a
        single batched ``docker inspect`` call to retrieve creation timestamp
        and port mapping for all containers at once.  Total subprocess calls:
        2 (down from 2N+1 in the naive per-container approach).

        Note: Docker's ``--filter name=`` performs *substring* matching,
        so a secondary ``startswith`` check is applied to ensure only
        containers with the exact prefix are included.

        Containers without port mappings are still included (with empty
        sandbox_url) so that startup reconciliation can adopt orphans
        regardless of their port state.
        """
        if self._runtime == "container":
            return self._list_running_apple()

        # Step 1: enumerate container names via docker ps
        try:
            result = subprocess.run(
                [
                    self._runtime,
                    "ps",
                    "--filter",
                    f"name={self._container_prefix}-",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                logger.warning(
                    "Failed to list running containers with %s ps (returncode=%s, stderr=%s)",
                    self._runtime,
                    result.returncode,
                    stderr or "<empty>",
                )
                return []
            if not result.stdout.strip():
                return []
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning(f"Failed to list running containers: {e}")
            return []

        # Filter to names matching our exact prefix (docker filter is substring-based)
        container_names = [name.strip() for name in result.stdout.strip().splitlines() if name.strip().startswith(self._container_prefix + "-")]
        if not container_names:
            return []

        # Step 2: batched docker inspect — single subprocess call for all containers
        inspections = self._batch_inspect(container_names)

        infos: list[SandboxInfo] = []
        sandbox_host = os.environ.get("ACT_WEAVE_SANDBOX_HOST", "localhost")
        for container_name in container_names:
            data = inspections.get(container_name)
            if data is None:
                # Container disappeared between ps and inspect, or inspect failed
                continue
            created_at, host_port = data
            sandbox_id = container_name[len(self._container_prefix) + 1 :]
            sandbox_url = f"http://{sandbox_host}:{host_port}" if host_port else ""

            infos.append(
                SandboxInfo(
                    sandbox_id=sandbox_id,
                    sandbox_url=sandbox_url,
                    container_name=container_name,
                    created_at=created_at,
                )
            )

        logger.info(f"Found {len(infos)} running sandbox container(s)")
        return infos

    def _list_running_apple(self) -> list[SandboxInfo]:
        """Enumerate managed Apple Container 1.x sandboxes."""
        try:
            result = subprocess.run(
                ["container", "list", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning("Failed to list running Apple containers: %s", exc)
            return []

        if result.returncode != 0:
            logger.warning(
                "Failed to list running Apple containers (returncode=%s, stderr=%s)",
                result.returncode,
                (result.stderr or "").strip() or "<empty>",
            )
            return []

        try:
            entries = _parse_apple_container_payload(result.stdout, operation="list")
        except RuntimeError as exc:
            logger.warning("%s", exc)
            return []

        prefix = self._container_prefix + "-"
        infos: list[SandboxInfo] = []
        for entry in entries:
            container_name = entry.get("id")
            status = entry.get("status")
            if not isinstance(container_name, str) or not container_name.startswith(prefix):
                continue
            if not isinstance(status, dict) or str(status.get("state", "")).lower() != "running":
                continue
            if not _is_managed_apple_sandbox(entry):
                continue

            configuration = entry.get("configuration")
            created_raw = configuration.get("creationDate", "") if isinstance(configuration, dict) else ""
            sandbox_url = _apple_sandbox_url(entry)
            if not sandbox_url:
                logger.warning("Ignoring managed Apple sandbox %s without a usable default-network IPv4 address", container_name)
                continue
            infos.append(
                SandboxInfo(
                    sandbox_id=container_name[len(prefix) :],
                    sandbox_url=sandbox_url,
                    container_name=container_name,
                    container_id=container_name,
                    created_at=_parse_docker_timestamp(str(created_raw)),
                )
            )

        logger.info("Found %d running Apple sandbox container(s)", len(infos))
        return infos

    def _batch_inspect(self, container_names: list[str]) -> dict[str, tuple[float, int | None]]:
        """Batch-inspect containers in a single subprocess call.

        Returns a mapping of ``container_name -> (created_at, host_port)``.
        Missing containers or parse failures are silently dropped from the result.
        """
        if not container_names:
            return {}
        try:
            result = subprocess.run(
                [self._runtime, "inspect", *container_names],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning(f"Failed to batch-inspect containers: {e}")
            return {}

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            logger.warning(
                "Failed to batch-inspect containers with %s inspect (returncode=%s, stderr=%s)",
                self._runtime,
                result.returncode,
                stderr or "<empty>",
            )
            return {}

        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse docker inspect output as JSON: {e}")
            return {}

        out: dict[str, tuple[float, int | None]] = {}
        for entry in payload:
            # ``Name`` is prefixed with ``/`` in the docker inspect response
            name = (entry.get("Name") or "").lstrip("/")
            if not name:
                continue
            created_at = _parse_docker_timestamp(entry.get("Created", ""))
            host_port = _extract_host_port(entry, 8080)
            out[name] = (created_at, host_port)
        return out

    # ── Container operations ─────────────────────────────────────────────

    def _start_container(
        self,
        container_name: str,
        port: int | None,
        extra_mounts: list[tuple[str, str, bool]] | None = None,
        *,
        include_config_mounts: bool = True,
        private_owner_id: str | None = None,
    ) -> str:
        """Start a new container.

        Args:
            container_name: Name for the container.
            port: Host port to map to container port 8080 for Docker. Apple
                Container uses its private VM address and must pass None.
            extra_mounts: Additional volume mounts.

        Returns:
            The container ID.

        Raises:
            RuntimeError: If container fails to start.
        """
        cmd = [self._runtime, "run"]

        # Docker-specific security options
        if self._runtime == "docker":
            cmd.extend(["--security-opt", "seccomp=unconfined"])

        cmd.extend(["--rm", "-d"])
        if self._runtime == "docker":
            if port is None:
                raise ValueError("Docker sandbox requires a host port")
            port_mapping = f"{_resolve_docker_bind_host()}:{port}:8080"
            cmd.extend(["-p", port_mapping])
        if self._runtime == "container" or private_owner_id is not None:
            cmd.extend(
                [
                    "--label",
                    f"{_APPLE_MANAGED_LABEL}=true",
                    "--label",
                    f"{_APPLE_SCHEMA_LABEL}={_APPLE_SCHEMA_VERSION}",
                ]
            )
        if private_owner_id is not None:
            cmd.extend(
                [
                    "--label",
                    f"{_PRIVATE_OWNER_LABEL}={private_owner_id}",
                ],
            )
        cmd.extend(["--name", container_name])

        # Environment variables
        for key, value in self._environment.items():
            cmd.extend(["-e", f"{key}={value}"])

        # Config-level volume mounts
        if include_config_mounts:
            for mount in self._config_mounts:
                cmd.extend(
                    _format_container_mount(
                        self._runtime,
                        mount.host_path,
                        mount.container_path,
                        mount.read_only,
                    )
                )

        # Extra mounts (thread-specific, skills, etc.)
        if extra_mounts:
            for host_path, container_path, read_only in extra_mounts:
                cmd.extend(
                    _format_container_mount(
                        self._runtime,
                        host_path,
                        container_path,
                        read_only,
                    )
                )

        cmd.append(self._image)

        log_cmd = _format_container_command_for_log(_redact_container_command_for_log(cmd))
        logger.info(f"Starting container using {self._runtime}: {log_cmd}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            container_id = result.stdout.strip()
            logger.info(f"Started container {container_name} (ID: {container_id}) using {self._runtime}")
            return container_id
        except subprocess.CalledProcessError as e:
            stderr = e.stderr or ""
            returncode = e.returncode
            if "port is already allocated" in stderr or "address already in use" in stderr.lower():
                safe_detail = "port is already allocated"
            elif _is_container_name_conflict(stderr):
                safe_detail = "Conflict. The container name is already in use"
            else:
                safe_detail = "runtime rejected the request"

        logger.error(
            "Failed to start container using %s (returncode=%s)",
            self._runtime,
            returncode,
        )
        raise RuntimeError(f"Failed to start sandbox container: {safe_detail}")

    def _stop_container(self, container_id: str) -> None:
        """Stop a container (--rm ensures automatic removal)."""
        try:
            subprocess.run(
                [self._runtime, "stop", container_id],
                capture_output=True,
                text=True,
                check=True,
            )
            logger.info(f"Stopped container {container_id} using {self._runtime}")
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to stop container {container_id}: {e.stderr}")

    def _stop_private_container(self, container_id: str) -> None:
        """Stop one private container, reporting unconfirmed destruction."""

        try:
            subprocess.run(
                [self._runtime, "stop", container_id],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            logger.info(f"Stopped private container {container_id} using {self._runtime}")
        except subprocess.CalledProcessError as exc:
            if _is_no_such_container_error(exc.stderr or "", container_id):
                logger.info(f"Private container {container_id} was already absent")
                return
            raise RuntimeError("Failed to destroy private container") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Failed to destroy private container") from exc

    def _is_container_running(self, container_name: str) -> bool:
        """Check if a named container is currently running.

        This enables cross-process container discovery — any process can detect
        containers started by another process via the deterministic container name.

        Raises:
            RuntimeError: If the container runtime cannot answer the inspect
                query. A failed check is intentionally distinct from a
                definitive "container does not exist" result so callers do not
                destroy healthy containers during transient Docker/Container
                daemon failures.
        """
        if self._runtime == "container":
            entry = self._inspect_apple_container(container_name)
            if entry is None:
                return False
            status = entry.get("status")
            if not isinstance(status, dict) or not isinstance(status.get("state"), str):
                raise RuntimeError("Failed to parse Apple Container inspect output")
            return status["state"].lower() == "running"

        try:
            result = subprocess.run(
                [self._runtime, "inspect", "-f", "{{.State.Running}}", container_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Timed out checking container {container_name}") from exc

        if result.returncode == 0:
            return result.stdout.strip().lower() == "true"
        if _is_no_such_container_error(result.stderr, container_name):
            return False
        raise RuntimeError(f"Failed to inspect container {container_name}: {result.stderr.strip()}")

    def _inspect_apple_container(self, container_name: str) -> dict | None:
        """Return one Apple managed-container entry, or None when absent."""
        try:
            result = subprocess.run(
                ["container", "inspect", container_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Timed out checking container {container_name}") from exc
        except (FileNotFoundError, OSError) as exc:
            raise RuntimeError(f"Failed to inspect container {container_name}") from exc

        if result.returncode != 0:
            if _is_no_such_container_error(result.stderr or "", container_name):
                return None
            raise RuntimeError(f"Failed to inspect container {container_name}: {(result.stderr or '').strip()}")

        entries = _parse_apple_container_payload(result.stdout, operation="inspect")
        matches = [entry for entry in entries if entry.get("id") == container_name]
        if len(matches) != 1:
            raise RuntimeError("Failed to parse Apple Container inspect output")
        return matches[0]

    def _inspect_docker_container(self, container_name: str) -> dict | None:
        """Return one Docker inspect entry, or None only for confirmed absence."""

        try:
            result = subprocess.run(
                ["docker", "inspect", container_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Failed to inspect container {container_name}") from exc
        if result.returncode != 0:
            if _is_no_such_container_error(result.stderr or "", container_name):
                return None
            raise RuntimeError(f"Failed to inspect container {container_name}: {(result.stderr or '').strip()}")
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("Failed to parse Docker inspect output") from exc
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise RuntimeError("Failed to parse Docker inspect output")
        return payload[0]

    def _wait_for_apple_container_network(self, container_name: str, *, timeout: float) -> dict | None:
        """Wait briefly for Apple Container to assign its default-network IP."""
        deadline = time.monotonic() + timeout
        while True:
            entry = self._inspect_apple_container(container_name)
            if entry is not None:
                status = entry.get("status")
                state = str(status.get("state", "")).lower() if isinstance(status, dict) else ""
                if state == "running" and _is_managed_apple_sandbox(entry) and _apple_sandbox_url(entry):
                    return entry
                if state in {"stopped", "stopping"}:
                    return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.2)

    def _get_container_port(self, container_name: str) -> int | None:
        """Get the host port of a running container.

        Args:
            container_name: The container name to inspect.

        Returns:
            The host port mapped to container port 8080, or None if not found.
        """
        if self._runtime == "container":
            return None

        try:
            result = subprocess.run(
                [self._runtime, "port", container_name, "8080"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                # Output format: "0.0.0.0:PORT" or ":::PORT"
                port_str = result.stdout.strip().split(":")[-1]
                return int(port_str)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            pass
        return None
