"""Opaque harness contract for one-time Local host-execution approval.

The harness can describe and stage an exact process launch, but it cannot grant
authority to execute one.  An app-owned port installed in trusted runtime
context owns durable approval state and completion receipts.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

HOST_EXECUTION_APPROVAL_CONTEXT_KEY = "__host_execution_approval_port"
HOST_EXECUTION_AGENT_PATH_CONTEXT_KEY = "__host_execution_agent_path"
HOST_EXECUTION_MAX_REQUESTED_COMMAND_BYTES = 65_536
HOST_EXECUTION_MAX_TOOL_CALL_ID_BYTES = 128
HOST_EXECUTION_MAX_CHANNEL_USER_ID_LENGTH = 256
_HOST_EXECUTION_BIDI_CONTROLS = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        *[chr(value) for value in range(0x202A, 0x202F)],
        *[chr(value) for value in range(0x2066, 0x2070)],
    },
)

HostExecutionApprovalStatus = Literal["pending", "denied"]
HostExecutionFrozenClaimStatus = Literal[
    "not_applicable",
    "claimed",
    "replay",
    "denied",
]
HostExecutionOutcomeStatus = Literal["finished", "launch_failed", "unknown"]
HostExecutionChannelIdentityMode = Literal["absent", "unset", "set"]


@dataclass(frozen=True, slots=True, repr=False)
class HostExecutionSkillSecretSource:
    """Secret-free identity of one Skill activation source.

    The path and complete logical-name/target-environment mapping select an
    exact admitted Skill in the private Run. ``explicit`` preserves
    slash-activation precedence when multiple active Skills declare the same
    target environment name. Secret values and Secret Generation identifiers
    never enter this frozen plan.
    """

    skill_path: str
    secret_names: tuple[str, ...]
    explicit: bool
    target_envs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.skill_path, str) or not self.skill_path or not posixpath.isabs(self.skill_path) or posixpath.normpath(self.skill_path) != self.skill_path:
            raise ValueError("skill secret source path must be canonical and absolute")
        if not self.secret_names or any(not isinstance(name, str) or not name for name in self.secret_names) or tuple(sorted(set(self.secret_names))) != self.secret_names:
            raise ValueError(
                "skill secret source names must be non-empty, unique, and sorted",
            )
        if not self.target_envs:
            object.__setattr__(self, "target_envs", self.secret_names)
        if len(self.target_envs) != len(self.secret_names) or any(not isinstance(target_env, str) or not target_env for target_env in self.target_envs) or len(set(self.target_envs)) != len(self.target_envs):
            raise ValueError(
                "skill secret source targets must align with names and be unique",
            )
        if type(self.explicit) is not bool:
            raise ValueError("skill secret source explicit flag must be a boolean")

    @property
    def secret_bindings(self) -> tuple[tuple[str, str], ...]:
        return tuple(zip(self.secret_names, self.target_envs, strict=True))

    def to_payload(self, *, schema_version: int = 4) -> dict[str, object]:
        payload: dict[str, object] = {
            "skill_path": self.skill_path,
            "secret_names": list(self.secret_names),
            "explicit": self.explicit,
        }
        if schema_version >= 4:
            payload["target_envs"] = list(self.target_envs)
        return payload

    @classmethod
    def from_payload(
        cls,
        payload: object,
        *,
        schema_version: int,
    ) -> HostExecutionSkillSecretSource:
        expected = {
            "skill_path",
            "secret_names",
            "explicit",
        }
        if schema_version >= 4:
            expected.add("target_envs")
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("invalid frozen skill secret source")
        names = payload.get("secret_names")
        target_envs = payload.get("target_envs", names)
        if not isinstance(names, list) or not isinstance(target_envs, list):
            raise ValueError("invalid frozen skill secret source")
        return cls(
            skill_path=payload.get("skill_path"),
            secret_names=tuple(names),
            explicit=payload.get("explicit"),
            target_envs=tuple(target_envs),
        )

    @property
    def sort_key(self) -> tuple[str, bool, tuple[tuple[str, str], ...]]:
        return (self.skill_path, self.explicit, self.secret_bindings)


@dataclass(frozen=True, slots=True, repr=False)
class HostExecutionPlan:
    """One normalized Local shell launch plus its non-authority source anchors."""

    source_tool_call_id: str
    source_run_id: str
    source_thread_id: str
    description: str
    requested_command: str
    effective_command: str
    shell: str
    cwd: str | None
    timeout_seconds: int
    environment_keys: tuple[str, ...] = ()
    skill_secret_sources: tuple[HostExecutionSkillSecretSource, ...] = ()
    legacy_environment_keys: tuple[str, ...] = ()
    agent_path: tuple[str, ...] = ("lead",)
    channel_identity_mode: HostExecutionChannelIdentityMode = "absent"
    channel_user_id: str | None = None
    schema_version: int = field(default=4, init=False)
    kind: Literal["local_shell"] = field(default="local_shell", init=False)

    def __post_init__(self) -> None:
        required = {
            "source_tool_call_id": self.source_tool_call_id,
            "source_run_id": self.source_run_id,
            "source_thread_id": self.source_thread_id,
            "requested_command": self.requested_command,
            "effective_command": self.effective_command,
            "shell": self.shell,
        }
        for name, value in required.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required")
        if len(self.source_tool_call_id.encode("utf-8", errors="surrogatepass")) > HOST_EXECUTION_MAX_TOOL_CALL_ID_BYTES:
            raise ValueError("source_tool_call_id is too long")
        if len(self.requested_command.encode("utf-8", errors="surrogatepass")) > HOST_EXECUTION_MAX_REQUESTED_COMMAND_BYTES:
            raise ValueError("requested_command is too long")
        if any(character in _HOST_EXECUTION_BIDI_CONTROLS for command in (self.requested_command, self.effective_command) for character in command):
            raise ValueError("host execution command contains bidirectional control characters")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(self.description, str):
            raise ValueError("description must be a string")
        if self.cwd is not None and (not isinstance(self.cwd, str) or not self.cwd):
            raise ValueError("cwd must be a non-empty string or None")
        if any(not isinstance(name, str) or not name for name in self.environment_keys):
            raise ValueError("environment_keys must contain non-empty strings")
        if tuple(sorted(set(self.environment_keys))) != self.environment_keys:
            raise ValueError("environment_keys must be unique and sorted")
        if any(type(source) is not HostExecutionSkillSecretSource for source in self.skill_secret_sources):
            raise ValueError("skill_secret_sources must contain typed sources")
        if tuple(sorted(set(self.skill_secret_sources), key=lambda source: source.sort_key)) != self.skill_secret_sources:
            raise ValueError("skill_secret_sources must be unique and sorted")
        if any(not isinstance(name, str) or not name for name in self.legacy_environment_keys):
            raise ValueError(
                "legacy_environment_keys must contain non-empty strings",
            )
        if tuple(sorted(set(self.legacy_environment_keys))) != self.legacy_environment_keys:
            raise ValueError(
                "legacy_environment_keys must be unique and sorted",
            )
        if not set(self.legacy_environment_keys).issubset(self.environment_keys):
            raise ValueError(
                "legacy_environment_keys must be included in environment_keys",
            )
        if not self.agent_path or any(not isinstance(part, str) or not part for part in self.agent_path):
            raise ValueError("agent_path must contain non-empty strings")
        if self.channel_identity_mode not in {"absent", "unset", "set"}:
            raise ValueError("channel_identity_mode is invalid")
        if self.channel_identity_mode == "set":
            if not isinstance(self.channel_user_id, str) or not self.channel_user_id or len(self.channel_user_id) > HOST_EXECUTION_MAX_CHANNEL_USER_ID_LENGTH:
                raise ValueError(
                    "set channel identity requires a bounded channel_user_id",
                )
        elif self.channel_user_id is not None:
            raise ValueError(
                "only set channel identity may carry channel_user_id",
            )

    def execution_payload(self) -> dict[str, object]:
        """Return the canonical logical launch authorized by the user.

        ``effective_command`` and ``cwd`` contain provider/run-local host paths.
        Private Skill roots are intentionally ephemeral, so a continuation Run
        must remap the frozen logical command against its own exact asset mount
        instead of reusing those stale host paths.  The app authority separately
        verifies that the source and continuation asset closures are identical.
        """

        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "requested_command": self.requested_command,
            "shell": self.shell,
            "timeout_seconds": self.timeout_seconds,
            "environment_keys": list(self.environment_keys),
            "agent_path": list(self.agent_path),
            "channel_identity_mode": self.channel_identity_mode,
            "channel_user_id": self.channel_user_id,
        }
        if self.schema_version >= 3:
            payload["skill_secret_sources"] = [source.to_payload(schema_version=self.schema_version) for source in self.skill_secret_sources]
            payload["legacy_environment_keys"] = list(
                self.legacy_environment_keys,
            )
        return payload

    def to_private_payload(self) -> dict[str, object]:
        """Return the complete secret-free plan for owner-private storage."""

        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "kind": "local_bash",
            "description": self.description,
            "requested_command": self.requested_command,
            "effective_command": self.effective_command,
            "shell": self.shell,
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "environment_keys": list(self.environment_keys),
            "agent_path": list(self.agent_path),
            "channel_identity_mode": self.channel_identity_mode,
            "channel_user_id": self.channel_user_id,
        }
        if self.schema_version >= 3:
            payload["skill_secret_sources"] = [source.to_payload(schema_version=self.schema_version) for source in self.skill_secret_sources]
            payload["legacy_environment_keys"] = list(
                self.legacy_environment_keys,
            )
        return payload

    @classmethod
    def from_private_payload(
        cls,
        payload: object,
        *,
        source_tool_call_id: str,
        source_run_id: str,
        source_thread_id: str,
    ) -> HostExecutionPlan:
        """Strictly reconstruct one app-persisted frozen plan."""

        v2_keys = {
            "schema_version",
            "kind",
            "description",
            "requested_command",
            "effective_command",
            "shell",
            "cwd",
            "timeout_seconds",
            "environment_keys",
            "agent_path",
            "channel_identity_mode",
            "channel_user_id",
        }
        v3_keys = v2_keys | {
            "skill_secret_sources",
            "legacy_environment_keys",
        }
        if not isinstance(payload, dict):
            raise ValueError("invalid frozen host execution payload")
        schema_version = payload.get("schema_version")
        if (schema_version == 2 and set(payload) != v2_keys) or (schema_version in {3, 4} and set(payload) != v3_keys) or schema_version not in {2, 3, 4}:
            raise ValueError("invalid frozen host execution payload")
        if payload.get("kind") != "local_bash":
            raise ValueError("unsupported frozen host execution payload")
        environment_keys = payload.get("environment_keys")
        agent_path = payload.get("agent_path")
        if not isinstance(environment_keys, list) or not isinstance(agent_path, list):
            raise ValueError("invalid frozen host execution payload")
        skill_secret_sources: tuple[HostExecutionSkillSecretSource, ...] = ()
        legacy_environment_keys: tuple[str, ...] = ()
        if schema_version in {3, 4}:
            raw_sources = payload.get("skill_secret_sources")
            raw_legacy_keys = payload.get("legacy_environment_keys")
            if not isinstance(raw_sources, list) or not isinstance(
                raw_legacy_keys,
                list,
            ):
                raise ValueError("invalid frozen host execution payload")
            skill_secret_sources = tuple(
                HostExecutionSkillSecretSource.from_payload(
                    source,
                    schema_version=schema_version,
                )
                for source in raw_sources
            )
            legacy_environment_keys = tuple(raw_legacy_keys)
        plan = cls(
            source_tool_call_id=source_tool_call_id,
            source_run_id=source_run_id,
            source_thread_id=source_thread_id,
            description=payload.get("description"),
            requested_command=payload.get("requested_command"),
            effective_command=payload.get("effective_command"),
            shell=payload.get("shell"),
            cwd=payload.get("cwd"),
            timeout_seconds=payload.get("timeout_seconds"),
            environment_keys=tuple(environment_keys),
            skill_secret_sources=skill_secret_sources,
            legacy_environment_keys=legacy_environment_keys,
            agent_path=tuple(agent_path),
            channel_identity_mode=payload.get("channel_identity_mode"),
            channel_user_id=payload.get("channel_user_id"),
        )
        if schema_version != 4:
            object.__setattr__(plan, "schema_version", schema_version)
        return plan

    @property
    def execution_digest(self) -> str:
        return self.execution_digest_for_schema(self.schema_version)

    def execution_digest_for_schema(self, schema_version: int) -> str:
        """Hash this logical launch using one supported persisted schema.

        Rebased continuations are constructed by the current process, but an
        already-approved v2 launch must still be compared with its original v2
        shape. Secret-bearing v2 plans remain rejected by the runner because
        they lack an exact Skill source closure.
        """

        if isinstance(schema_version, bool) or schema_version not in {2, 3, 4}:
            raise ValueError("unsupported host execution schema version")
        payload = self.execution_payload()
        payload["schema_version"] = schema_version
        if schema_version == 2:
            payload.pop("skill_secret_sources", None)
            payload.pop("legacy_environment_keys", None)
        elif schema_version == 3:
            payload["skill_secret_sources"] = [source.to_payload(schema_version=3) for source in self.skill_secret_sources]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="surrogatepass")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class HostExecutionApprovalArtifact:
    """Minimal public checkpoint anchor; command details stay in app projection."""

    approval_id: str
    source_run_id: str
    source_tool_call_id: str
    schema_version: int = field(default=1, init=False)
    kind: Literal["local_shell"] = field(default="local_shell", init=False)

    def __post_init__(self) -> None:
        if not self.approval_id:
            raise ValueError("approval_id is required")
        if not self.source_run_id:
            raise ValueError("source_run_id is required")
        if not self.source_tool_call_id:
            raise ValueError("source_tool_call_id is required")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "approval_id": self.approval_id,
            "source_run_id": self.source_run_id,
            "source_tool_call_id": self.source_tool_call_id,
        }


@dataclass(frozen=True, slots=True)
class HostExecutionApprovalResult:
    """Decision returned by the trusted approval port for one exact plan."""

    status: HostExecutionApprovalStatus
    approval_id: str | None = None
    artifact: HostExecutionApprovalArtifact | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"pending", "denied"}:
            raise ValueError("unsupported host execution approval result")
        if self.status == "pending":
            if self.artifact is None:
                raise ValueError("pending result requires an approval artifact")
            if self.approval_id not in {None, self.artifact.approval_id}:
                raise ValueError("pending approval_id must match its artifact")
            if self.reason_code is not None:
                raise ValueError("pending result cannot carry reason_code")
            object.__setattr__(self, "approval_id", self.artifact.approval_id)
            return
        if self.artifact is not None or self.approval_id is not None:
            raise ValueError("only pending result may carry approval data")
        if not self.reason_code:
            raise ValueError("denied result requires reason_code")

    @classmethod
    def pending(
        cls,
        artifact: HostExecutionApprovalArtifact,
    ) -> HostExecutionApprovalResult:
        return cls(status="pending", artifact=artifact)

    @classmethod
    def denied(cls, reason_code: str) -> HostExecutionApprovalResult:
        return cls(status="denied", reason_code=reason_code)


@dataclass(frozen=True, slots=True, repr=False)
class HostExecutionOutcome:
    """Private completion supplied after an approved launch is consumed."""

    status: HostExecutionOutcomeStatus
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    result_text: str | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status == "finished" and (isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)):
            raise ValueError("finished outcome requires exit_code")
        if self.status != "finished" and not self.reason_code:
            raise ValueError("non-finished outcome requires reason_code")


@dataclass(frozen=True, slots=True, repr=False)
class HostExecutionFrozenClaim:
    """Result of atomically consuming a durable continuation authority.

    Only the app adapter may construct a ``claimed`` result from its private
    persisted plan.  The Worker never accepts a model/tool-supplied replacement
    command for an approved continuation.
    """

    status: HostExecutionFrozenClaimStatus
    approval_id: str | None = None
    plan: HostExecutionPlan | None = None
    outcome: HostExecutionOutcome | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status == "claimed":
            if not self.approval_id or self.plan is None:
                raise ValueError(
                    "claimed result requires approval_id and frozen plan",
                )
            if self.reason_code is not None:
                raise ValueError("claimed result cannot carry reason_code")
            if self.outcome is not None:
                raise ValueError("claimed result cannot carry outcome")
            return
        if self.status == "replay":
            if not self.approval_id or self.plan is None or self.outcome is None:
                raise ValueError(
                    "replay result requires approval_id, frozen plan, and outcome",
                )
            if self.outcome.status not in {"finished", "launch_failed"}:
                raise ValueError("replay outcome must have a durable receipt")
            if self.reason_code is not None:
                raise ValueError("replay result cannot carry reason_code")
            return
        if self.approval_id is not None or self.plan is not None or self.outcome is not None:
            raise ValueError("only claimed or replay result may carry approval data")
        if self.status == "denied" and not self.reason_code:
            raise ValueError("denied result requires reason_code")
        if self.status == "not_applicable" and self.reason_code is not None:
            raise ValueError("not_applicable result cannot carry reason_code")

    @classmethod
    def not_applicable(cls) -> HostExecutionFrozenClaim:
        return cls(status="not_applicable")

    @classmethod
    def claimed(
        cls,
        approval_id: str,
        plan: HostExecutionPlan,
    ) -> HostExecutionFrozenClaim:
        return cls(
            status="claimed",
            approval_id=approval_id,
            plan=plan,
        )

    @classmethod
    def denied(cls, reason_code: str) -> HostExecutionFrozenClaim:
        return cls(status="denied", reason_code=reason_code)

    @classmethod
    def replay(
        cls,
        approval_id: str,
        plan: HostExecutionPlan,
        outcome: HostExecutionOutcome,
    ) -> HostExecutionFrozenClaim:
        return cls(
            status="replay",
            approval_id=approval_id,
            plan=plan,
            outcome=outcome,
        )


@runtime_checkable
class HostExecutionApprovalPort(Protocol):
    """App-owned staging boundary used by Lead and delegated Agents.

    A staged request can only become executable through the separately claimed
    frozen continuation. This port therefore never grants inline execution.
    """

    async def request_host_execution(
        self,
        plan: HostExecutionPlan,
    ) -> HostExecutionApprovalResult: ...


@runtime_checkable
class HostExecutionContinuationPort(Protocol):
    """App-owned one-shot claim used before any continuation model call."""

    async def claim_frozen_host_execution(
        self,
    ) -> HostExecutionFrozenClaim: ...

    async def complete_host_execution(
        self,
        approval_id: str,
        outcome: HostExecutionOutcome,
    ) -> None: ...

    def prepare_host_execution_environment(self) -> dict[str, str] | None:
        """Return one verified sanitized startup-equivalent environment.

        ``None`` means the Worker environment drifted after startup. Values
        remain Worker-local and must never enter approval persistence or UI.
        """

        ...


@runtime_checkable
class HostExecutionRetrySafetyFencePort(Protocol):
    """App port that atomically commits a receipt with one retry fence."""

    async def complete_host_execution_with_retry_safety_fence(
        self,
        approval_id: str,
        outcome: HostExecutionOutcome,
        retry_safety_fence: object,
    ) -> None: ...


@runtime_checkable
class HostExecutionOutputDeliveryPort(Protocol):
    """Optional server-owned continuation output-delivery projection."""

    async def output_delivery_requirement_paths(self) -> tuple[str, ...]: ...


__all__ = [
    "HOST_EXECUTION_AGENT_PATH_CONTEXT_KEY",
    "HOST_EXECUTION_APPROVAL_CONTEXT_KEY",
    "HOST_EXECUTION_MAX_REQUESTED_COMMAND_BYTES",
    "HOST_EXECUTION_MAX_TOOL_CALL_ID_BYTES",
    "HostExecutionApprovalArtifact",
    "HostExecutionApprovalPort",
    "HostExecutionApprovalResult",
    "HostExecutionContinuationPort",
    "HostExecutionFrozenClaim",
    "HostExecutionOutcome",
    "HostExecutionOutputDeliveryPort",
    "HostExecutionPlan",
    "HostExecutionSkillSecretSource",
    "HostExecutionRetrySafetyFencePort",
]
