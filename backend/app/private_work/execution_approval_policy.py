"""Frozen Local host-execution provider policy for Execution Approval.

The snapshot is app-owned, non-secret scalar configuration.  It is digested
into every private approval envelope so policy drift is rejected instead of
silently re-interpreted when a frozen plan is claimed or replayed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

_PROVIDER_POLICY_SCHEMA_VERSION = 2
_HOST_EXECUTION_MODES = frozenset(
    {
        "isolated_direct",
        "local_disabled",
        "local_approval_required",
        "local_legacy_allow",
    },
)


def _canonical_digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="surrogatepass"),
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class HostExecutionProviderPolicySnapshot:
    """Strict, app-owned Local host-execution policy snapshot.

    The snapshot deliberately contains only non-secret scalar configuration.
    A disabled or isolated snapshot is representable so drift can be compared
    and rejected instead of failing while constructing the trusted adapter.
    """

    provider_use: str
    host_execution_mode: str
    allow_host_bash: bool
    bash_command_timeout: int
    approval_max_timeout_seconds: int
    request_ttl_seconds: int
    execution_domain_id: str | None
    local_mounts: tuple[tuple[str, str, bool], ...] = ()
    skills_container_path: str = "/mnt/skills"
    schema_version: int = field(
        default=_PROVIDER_POLICY_SCHEMA_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.provider_use, str) or not self.provider_use or self.provider_use.strip() != self.provider_use:
            raise ValueError("provider_use must be a non-empty exact string")
        if self.host_execution_mode not in _HOST_EXECUTION_MODES:
            raise ValueError("host_execution_mode is invalid")
        if type(self.allow_host_bash) is not bool:
            raise TypeError("allow_host_bash must be a boolean")
        for name in (
            "bash_command_timeout",
            "approval_max_timeout_seconds",
            "request_ttl_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.skills_container_path, str) or not self.skills_container_path.startswith("/") or (self.skills_container_path != "/" and self.skills_container_path.endswith("/")):
            raise ValueError("skills_container_path must be a normalized absolute path")
        if self.execution_domain_id is not None and (not isinstance(self.execution_domain_id, str) or not self.execution_domain_id or self.execution_domain_id.strip() != self.execution_domain_id):
            raise ValueError(
                "execution_domain_id must be an exact non-empty string",
            )
        if self.approval_enabled and self.execution_domain_id is None:
            raise ValueError(
                "Local approval policy requires an execution_domain_id",
            )
        if not isinstance(self.local_mounts, tuple):
            raise TypeError("local_mounts must be a tuple")
        for mount in self.local_mounts:
            if (
                not isinstance(mount, tuple)
                or len(mount) != 3
                or not isinstance(mount[0], str)
                or not mount[0]
                or not isinstance(mount[1], str)
                or not mount[1].startswith("/")
                or (mount[1] != "/" and mount[1].endswith("/"))
                or type(mount[2]) is not bool
            ):
                raise ValueError("local_mounts contains an invalid mount")

    @property
    def approval_enabled(self) -> bool:
        return self.host_execution_mode == "local_approval_required" and self.allow_host_bash is False

    @property
    def execution_timeout_seconds(self) -> int:
        return min(
            self.bash_command_timeout,
            self.approval_max_timeout_seconds,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_use": self.provider_use,
            "host_execution_mode": self.host_execution_mode,
            "allow_host_bash": self.allow_host_bash,
            "bash_command_timeout": self.bash_command_timeout,
            "approval_max_timeout_seconds": (self.approval_max_timeout_seconds),
            "request_ttl_seconds": self.request_ttl_seconds,
            "execution_domain_id": self.execution_domain_id,
            "local_mounts": [
                {
                    "host_path": host_path,
                    "container_path": container_path,
                    "read_only": read_only,
                }
                for host_path, container_path, read_only in self.local_mounts
            ],
            "skills_container_path": self.skills_container_path,
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_payload())

    @classmethod
    def from_payload(cls, payload: object) -> HostExecutionProviderPolicySnapshot:
        expected = {
            "schema_version",
            "provider_use",
            "host_execution_mode",
            "allow_host_bash",
            "bash_command_timeout",
            "approval_max_timeout_seconds",
            "request_ttl_seconds",
            "execution_domain_id",
            "local_mounts",
            "skills_container_path",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("invalid provider policy snapshot")
        if payload.get("schema_version") != _PROVIDER_POLICY_SCHEMA_VERSION:
            raise ValueError("unsupported provider policy snapshot")
        mounts_payload = payload.get("local_mounts")
        if not isinstance(mounts_payload, list):
            raise ValueError("invalid provider policy mounts")
        local_mounts: list[tuple[str, str, bool]] = []
        for mount in mounts_payload:
            if not isinstance(mount, dict) or set(mount) != {
                "host_path",
                "container_path",
                "read_only",
            }:
                raise ValueError("invalid provider policy mount")
            local_mounts.append(
                (
                    mount.get("host_path"),
                    mount.get("container_path"),
                    mount.get("read_only"),
                ),
            )
        return cls(
            provider_use=payload.get("provider_use"),
            host_execution_mode=payload.get("host_execution_mode"),
            allow_host_bash=payload.get("allow_host_bash"),
            bash_command_timeout=payload.get("bash_command_timeout"),
            approval_max_timeout_seconds=payload.get(
                "approval_max_timeout_seconds",
            ),
            request_ttl_seconds=payload.get("request_ttl_seconds"),
            execution_domain_id=payload.get("execution_domain_id"),
            local_mounts=tuple(local_mounts),
            skills_container_path=payload.get("skills_container_path"),
        )

    @classmethod
    def from_app_config(
        cls,
        app_config: AppConfig,
    ) -> HostExecutionProviderPolicySnapshot:
        """Build the app-owned snapshot from typed runtime configuration."""

        from deerflow.sandbox.security import resolve_host_bash_execution_mode

        sandbox = getattr(app_config, "sandbox", None)
        approval = getattr(sandbox, "host_execution_approval", None)
        if sandbox is None or approval is None:
            raise ValueError("sandbox host execution policy is unavailable")
        mode = resolve_host_bash_execution_mode(app_config)
        local_mounts = tuple(
            (
                (str(Path(mount.host_path).expanduser().resolve(strict=False)) if Path(mount.host_path).is_absolute() else mount.host_path),
                mount.container_path.rstrip("/") or "/",
                mount.read_only,
            )
            for mount in sandbox.mounts
        )
        skills_container_path = app_config.skills.container_path.rstrip("/") or "/"
        return cls(
            provider_use=getattr(sandbox, "use", None),
            host_execution_mode=mode.value,
            allow_host_bash=getattr(sandbox, "allow_host_bash", None),
            bash_command_timeout=getattr(sandbox, "bash_command_timeout", None),
            approval_max_timeout_seconds=getattr(
                approval,
                "max_timeout_seconds",
                None,
            ),
            request_ttl_seconds=getattr(
                approval,
                "request_ttl_seconds",
                None,
            ),
            execution_domain_id=getattr(
                approval,
                "execution_domain_id",
                None,
            ),
            local_mounts=local_mounts,
            skills_container_path=skills_container_path,
        )


__all__ = [
    "HostExecutionProviderPolicySnapshot",
]
