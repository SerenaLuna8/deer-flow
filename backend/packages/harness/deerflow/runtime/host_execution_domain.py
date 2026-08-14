"""Stable, Worker-owned affinity for Local host process execution.

The random Worker registration id is a lease coordinate, not a machine/user
execution boundary.  This snapshot combines an operator-provisioned stable id
with the physical properties that materially select where a Local command will
run.  Only its digest is used by the generic Job queue; the private snapshot is
kept inside the owner-private approval envelope.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

_SCHEMA_VERSION = 1
_AFFINITY_RE = re.compile(r"[0-9a-f]{64}")
_DOMAIN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_BIDI_CONTROL_RE = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_MACHINE_ID_RE = re.compile(r"[0-9a-fA-F]{32}")


def _canonical_digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()


def host_execution_environment_fingerprint(
    environment: Mapping[str, str],
) -> str:
    """Hash one sanitized Local subprocess environment without persisting it."""

    if not isinstance(environment, Mapping) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items()):
        raise ValueError("host execution environment must contain strings")
    return hashlib.sha256(
        json.dumps(
            dict(environment),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class HostExecutionDomainSnapshot:
    """Private execution-domain identity captured once at Worker startup."""

    configured_id: str = field(repr=False)
    public_label: str
    os_name: str
    sys_platform: str
    machine: str
    device_fingerprint: str
    environment_fingerprint: str
    euid: int
    egid: int
    runtime_base_dir: str = field(repr=False)
    schema_version: int = field(default=_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        if _DOMAIN_ID_RE.fullmatch(self.configured_id) is None:
            raise ValueError("configured execution domain id is invalid")
        if (
            not isinstance(self.public_label, str)
            or not self.public_label
            or len(self.public_label) > 64
            or self.public_label != self.public_label.strip()
            or _CONTROL_RE.search(self.public_label) is not None
            or _BIDI_CONTROL_RE.search(self.public_label) is not None
        ):
            raise ValueError("public execution domain label is invalid")
        for name in ("os_name", "sys_platform", "machine"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 128 or value != value.strip() or _CONTROL_RE.search(value) is not None:
                raise ValueError(f"{name} is invalid")
        if _AFFINITY_RE.fullmatch(self.device_fingerprint) is None:
            raise ValueError("device_fingerprint must be a lowercase SHA-256 digest")
        if _AFFINITY_RE.fullmatch(self.environment_fingerprint) is None:
            raise ValueError(
                "environment_fingerprint must be a lowercase SHA-256 digest",
            )
        for name in ("euid", "egid"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        base_dir = Path(self.runtime_base_dir)
        if not isinstance(self.runtime_base_dir, str) or not self.runtime_base_dir or not base_dir.is_absolute() or str(base_dir.resolve()) != self.runtime_base_dir:
            raise ValueError("runtime_base_dir must be a canonical absolute path")

    def _affinity_payload(self) -> dict[str, object]:
        # The public label is deliberately presentation-only: renaming it does
        # not strand an already approved continuation on the queue.
        return {
            "schema_version": self.schema_version,
            "configured_id": self.configured_id,
            "os_name": self.os_name,
            "sys_platform": self.sys_platform,
            "machine": self.machine,
            "device_fingerprint": self.device_fingerprint,
            "environment_fingerprint": self.environment_fingerprint,
            "euid": self.euid,
            "egid": self.egid,
            "runtime_base_dir": self.runtime_base_dir,
        }

    @property
    def affinity(self) -> str:
        return _canonical_digest(self._affinity_payload())

    @property
    def configured_id_hash(self) -> str:
        return hashlib.sha256(
            f"host-execution-domain-id:{self.configured_id}".encode(),
        ).hexdigest()

    def to_private_payload(self) -> dict[str, object]:
        return {
            **self._affinity_payload(),
            "public_label": self.public_label,
            "affinity": self.affinity,
        }

    @classmethod
    def from_private_payload(
        cls,
        payload: object,
    ) -> HostExecutionDomainSnapshot:
        expected = {
            "schema_version",
            "configured_id",
            "public_label",
            "os_name",
            "sys_platform",
            "machine",
            "device_fingerprint",
            "environment_fingerprint",
            "euid",
            "egid",
            "runtime_base_dir",
            "affinity",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("invalid host execution domain snapshot")
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("unsupported host execution domain snapshot")
        affinity = payload.get("affinity")
        if not isinstance(affinity, str) or _AFFINITY_RE.fullmatch(affinity) is None:
            raise ValueError("invalid host execution domain affinity")
        snapshot = cls(
            configured_id=payload.get("configured_id"),
            public_label=payload.get("public_label"),
            os_name=payload.get("os_name"),
            sys_platform=payload.get("sys_platform"),
            machine=payload.get("machine"),
            device_fingerprint=payload.get("device_fingerprint"),
            environment_fingerprint=payload.get("environment_fingerprint"),
            euid=payload.get("euid"),
            egid=payload.get("egid"),
            runtime_base_dir=payload.get("runtime_base_dir"),
        )
        if snapshot.affinity != affinity:
            raise ValueError("host execution domain affinity mismatch")
        return snapshot

    @classmethod
    def capture(cls, app_config: AppConfig) -> HostExecutionDomainSnapshot:
        """Capture the exact Local execution surface of the current Worker."""

        from deerflow.config.paths import get_paths
        from deerflow.sandbox.env_policy import build_sandbox_env
        from deerflow.sandbox.security import resolve_host_bash_execution_mode

        if resolve_host_bash_execution_mode(app_config).value != "local_approval_required":
            raise ValueError("host execution domain is only available in Local approval mode")
        approval = app_config.sandbox.host_execution_approval
        configured_id = approval.execution_domain_id
        if configured_id is None:
            raise ValueError("host execution domain id is unavailable")
        get_euid = getattr(os, "geteuid", None)
        get_egid = getattr(os, "getegid", None)
        if not callable(get_euid) or not callable(get_egid):
            raise ValueError("Local approval requires a POSIX execution identity")
        return cls(
            configured_id=configured_id,
            public_label=approval.execution_domain_label,
            os_name=os.name,
            sys_platform=sys.platform,
            machine=platform.machine() or "unknown",
            device_fingerprint=_device_fingerprint(),
            environment_fingerprint=host_execution_environment_fingerprint(
                build_sandbox_env(None),
            ),
            euid=get_euid(),
            egid=get_egid(),
            runtime_base_dir=str(get_paths().base_dir.resolve()),
        )


def _device_fingerprint() -> str:
    """Hash a restart-stable device identity without persisting its raw value.

    The fingerprint deliberately includes the OS/container namespace, not just
    CPU architecture or a hostname. Linux boot or namespace replacement may
    invalidate an outstanding approval; that fail-closed behavior is safer
    than moving an approved host command to another execution surface.
    """

    if sys.platform == "darwin":
        try:
            platform_result = subprocess.run(
                ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError(
                "stable macOS execution device identity is unavailable",
            ) from None
        match = re.search(
            r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{36})"',
            platform_result.stdout,
        )
        if match is None:
            raise ValueError(
                "stable macOS execution device identity is unavailable",
            )
        try:
            platform_uuid = str(uuid.UUID(match.group(1)))
        except ValueError:
            raise ValueError(
                "stable macOS execution device identity is unavailable",
            ) from None
        return hashlib.sha256(
            f"darwin-platform-uuid:{platform_uuid}".encode(),
        ).hexdigest()

    if sys.platform.startswith("linux"):
        machine_id: str | None = None
        for path in (
            Path("/etc/machine-id"),
            Path("/var/lib/dbus/machine-id"),
        ):
            try:
                candidate = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if _MACHINE_ID_RE.fullmatch(candidate) is not None:
                machine_id = candidate.lower()
                break
        try:
            raw_boot_id = (
                Path("/proc/sys/kernel/random/boot_id")
                .read_text(
                    encoding="utf-8",
                )
                .strip()
            )
            if re.fullmatch(r"[0-9A-Fa-f-]{36}", raw_boot_id) is None:
                raise ValueError
            boot_id = str(uuid.UUID(raw_boot_id))
            namespace_parts = []
            for namespace in (
                "user",
                "mnt",
                "pid",
                "net",
                "uts",
                "ipc",
                "cgroup",
            ):
                stat_result = os.stat(f"/proc/self/ns/{namespace}")
                if stat_result.st_dev < 0 or stat_result.st_ino <= 0:
                    raise ValueError
                namespace_parts.append(
                    f"{namespace}:{stat_result.st_dev}:{stat_result.st_ino}",
                )
        except (OSError, ValueError):
            raise ValueError(
                "stable Linux execution namespace identity is unavailable",
            ) from None
        if not machine_id or not boot_id:
            raise ValueError(
                "stable Linux execution namespace identity is unavailable",
            )
        raw = "|".join((machine_id, boot_id, *namespace_parts))
        return hashlib.sha256(
            f"linux-execution-namespace:{raw}".encode(),
        ).hexdigest()

    raise ValueError("stable host execution device identity is unavailable")


def validate_execution_domain_affinity(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _AFFINITY_RE.fullmatch(value) is None:
        raise ValueError("execution domain affinity must be a lowercase SHA-256 digest")
    return value


__all__ = [
    "HostExecutionDomainSnapshot",
    "host_execution_environment_fingerprint",
    "validate_execution_domain_affinity",
]
