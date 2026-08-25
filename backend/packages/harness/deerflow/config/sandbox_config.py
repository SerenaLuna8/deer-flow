import re
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_LOCAL_RESERVED_MOUNT_PREFIXES = (
    "/mnt/acp-workspace",
    "/mnt/user-data",
)
_BIDI_CONTROL_RE = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def _uses_local_sandbox_provider(provider_use: str) -> bool:
    # Import lazily: sandbox_provider imports deerflow.config, so importing the
    # classifier while this config module is loading would create a cycle.
    from deerflow.sandbox.security import uses_local_sandbox_provider_use

    return uses_local_sandbox_provider_use(provider_use)


class VolumeMountConfig(BaseModel):
    """Configuration for a volume mount."""

    host_path: str = Field(
        ...,
        description=(
            "Source path for the mount. Resolution depends on the active provider: "
            "``LocalSandboxProvider`` checks this path from the Worker process — in "
            "``make dev`` that is the host machine, but in Docker deployments "
            "(``make up`` / docker-compose) it is the path *inside* the "
            "``deer-flow-worker`` container, so the host directory must also be "
            "bind-mounted into the Worker service for the mount to take effect. "
            "``AioSandboxProvider`` (DooD) passes this value straight to ``docker -v`` "
            "for the sandbox container, where it is resolved by the host Docker daemon "
            "from the host machine's perspective."
        ),
    )
    container_path: str = Field(..., description="Path inside the container")
    read_only: bool = Field(default=False, description="Whether the mount is read-only")


class HostExecutionApprovalConfig(BaseModel):
    """Restart-required policy for Local Provider host-command approval."""

    mode: Literal["disabled", "approval_required"] = Field(
        default="disabled",
        description=("Host command approval mode for LocalSandboxProvider. approval_required stages every exact command for one-time user approval."),
    )
    request_ttl_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Maximum time an unapproved host-execution request remains actionable.",
    )
    max_timeout_seconds: int = Field(
        default=600,
        ge=1,
        le=3600,
        description="Hard upper bound for one approved host process launch.",
    )
    execution_domain_id: str | None = Field(
        default=None,
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        description=("Stable operator-provisioned identity for the Local host execution domain. It is private affinity authority and is never returned to the browser."),
    )
    execution_domain_label: str = Field(
        default="Worker host environment",
        min_length=1,
        max_length=64,
        description=("Operator-reviewed public label shown on host execution approval cards. Do not put hostnames, usernames, paths, or secrets here."),
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_execution_domain_label(self) -> Self:
        if self.execution_domain_label != self.execution_domain_label.strip():
            raise ValueError("execution_domain_label must not have surrounding whitespace")
        if re.search(r"[\x00-\x1f\x7f]", self.execution_domain_label) or (_BIDI_CONTROL_RE.search(self.execution_domain_label) is not None):
            raise ValueError("execution_domain_label must not contain control characters")
        return self


class SandboxConfig(BaseModel):
    """Config section for a sandbox.

    Common options:
        use: Class path of the sandbox provider (required)
        allow_host_bash: Enable host-side bash execution for LocalSandboxProvider.
            Dangerous and intended only for fully trusted local workflows.

    AioSandboxProvider and BoxliteProvider shared options:
        image: Sandbox image to use (Docker/AIO image or BoxLite OCI image)
        replicas: Maximum active + warm sandboxes/VMs per gateway process (default: 3). When the limit is reached, warm/least-recently-used sandboxes are evicted to make room; active sandboxes are not forcibly stopped.
        idle_timeout: Idle timeout in seconds before released warm sandboxes/VMs are stopped (default: 600 = 10 minutes). Set to 0 to disable.
        environment: Environment variables to inject into the sandbox (values starting with $ are resolved from host env)

    BoxliteProvider specific options:
        health_check_skip_seconds: Optional reclaim-time skip window in seconds for recently released warm VMs. Default behavior is 0.0 = always validate before reuse.

    AioSandboxProvider specific options:
        port: Docker local-backend host-port search base (default: 8080).
            Apple Container uses its private VM address and ignores this field.
        container_prefix: Prefix for container names (default: deer-flow-sandbox)
        mounts: List of volume mounts to share directories with the container
    """

    use: str = Field(
        ...,
        description="Class path of the sandbox provider (e.g. deerflow.sandbox.local:LocalSandboxProvider)",
    )
    allow_host_bash: bool = Field(
        default=False,
        description="Allow the bash tool to execute directly on the host when using LocalSandboxProvider. Dangerous; intended only for fully trusted local environments.",
    )
    compose_dood_p03_v1_verified: bool = Field(
        default=False,
        strict=True,
        description=("Operator attestation that the versioned P-03 Compose DooD read-only mount probe passed for this deployment. Keep false unless the real dual-view guest and cross-Worker probe succeeds."),
    )
    boxlite_p04_v1_verified: bool = Field(
        default=False,
        strict=True,
        description=("Operator attestation that the versioned P-04 BoxLite Run Skill mount probe passed on this exact virtualization target. Keep false unless the real unprivileged VM and exact owner-reconciliation probe succeeds."),
    )
    e2b_p05_v1_verified: bool = Field(
        default=False,
        strict=True,
        description=("Operator attestation that the versioned P-05 E2B Run Skill upload probe passed for this exact account/template/domain. Keep false unless the real unprivileged VM and exact owner-reconciliation probe succeeds."),
    )
    host_execution_approval: HostExecutionApprovalConfig = Field(
        default_factory=HostExecutionApprovalConfig,
        description=("One-time approval policy for commands launched on the host by LocalSandboxProvider. Isolated sandbox providers execute directly."),
    )
    image: str | None = Field(
        default=None,
        description="Sandbox image to use (Docker/AIO image or BoxLite OCI image)",
    )
    port: int | None = Field(
        default=None,
        description="Docker local-backend host-port search base. Apple Container uses its private VM address and ignores this field.",
    )
    replicas: int | None = Field(
        default=None,
        description="Maximum active + warm sandboxes/VMs per gateway process (default: 3). Warm/least-recently-used entries are evicted to make room; active sandboxes are not forcibly stopped.",
    )
    container_prefix: str | None = Field(
        default=None,
        description="Prefix for container names",
    )
    idle_timeout: int | None = Field(
        default=None,
        description="Idle timeout in seconds before released warm sandboxes/VMs are stopped (default: 600 = 10 minutes). Set to 0 to disable.",
    )
    health_check_skip_seconds: float | None = Field(
        default=None,
        ge=0,
        description="BoxLite-only reclaim skip window in seconds for boxes recently released by this provider instance. Set to 0 to always validate before warm reuse.",
    )
    mounts: list[VolumeMountConfig] = Field(
        default_factory=list,
        description="List of volume mounts to share directories between host and container",
    )
    environment: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables to inject into the sandbox container. Values starting with $ will be resolved from host environment variables.",
    )

    bash_output_max_chars: int = Field(
        default=20000,
        ge=0,
        description="Maximum characters to keep from bash tool output. Output exceeding this limit is middle-truncated (head + tail), preserving the first and last half. Set to 0 to disable truncation.",
    )
    read_file_output_max_chars: int = Field(
        default=50000,
        ge=0,
        description="Maximum characters to keep from read_file tool output. Output exceeding this limit is head-truncated. Set to 0 to disable truncation.",
    )
    ls_output_max_chars: int = Field(
        default=20000,
        ge=0,
        description="Maximum characters to keep from ls tool output. Output exceeding this limit is head-truncated. Set to 0 to disable truncation.",
    )
    bash_command_timeout: int = Field(
        default=600,
        gt=0,
        description=(
            "Maximum wall-clock seconds a host bash command may run before it is terminated, process group and all (LocalSandboxProvider). "
            "Keeps a blocking foreground command (e.g. an un-backgrounded server) from hanging the turn; background `&` processes return immediately."
        ),
    )
    provisioner_api_key: str | None = Field(
        default=None,
        description=("API key sent as X-API-Key to the Provisioner service. It must match PROVISIONER_API_KEY in the Provisioner process; the Provisioner rejects every /api/* request when its key is unset or mismatched."),
    )

    @model_validator(mode="after")
    def validate_host_execution_modes(self) -> Self:
        if self.allow_host_bash and self.host_execution_approval.mode == "approval_required":
            raise ValueError(
                "sandbox.allow_host_bash and sandbox.host_execution_approval.mode=approval_required are mutually exclusive",
            )
        if self.host_execution_approval.mode == "approval_required" and _uses_local_sandbox_provider(self.use):
            if self.host_execution_approval.execution_domain_id is None:
                raise ValueError(
                    "Local approval mode requires sandbox.host_execution_approval.execution_domain_id",
                )
            for mount in self.mounts:
                host_path = Path(mount.host_path).expanduser()
                container_path = mount.container_path.rstrip("/") or "/"
                if not host_path.is_absolute():
                    raise ValueError(
                        "Local approval mode requires every sandbox.mounts host_path to be absolute",
                    )
                if not host_path.exists():
                    raise ValueError(
                        "Local approval mode requires every sandbox.mounts host_path to exist at startup",
                    )
                if not container_path.startswith("/"):
                    raise ValueError(
                        "Local approval mode requires every sandbox.mounts container_path to be absolute",
                    )
                if any(container_path == prefix or container_path.startswith(prefix + "/") for prefix in _LOCAL_RESERVED_MOUNT_PREFIXES):
                    raise ValueError(
                        "Local approval mode rejects sandbox.mounts entries under reserved container paths",
                    )
        return self

    model_config = ConfigDict(extra="allow")
