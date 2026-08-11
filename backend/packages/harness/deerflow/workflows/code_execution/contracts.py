"""Strict contracts for isolated Workflow Python execution.

The types in this module are runtime-internal contracts, not public API DTOs.
They deliberately expose no command, argv, environment, mount, secret, image,
provider locator, or Thread identity supplied by a project member.
"""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BeforeValidator, ConfigDict, Field, Strict, field_validator, model_validator

from deerflow.workflows.canonical import (
    CanonicalJsonUtf8BudgetExceeded,
    canonical_json_value,
    canonical_json_value_with_utf8_budget,
)
from deerflow.workflows.contracts import JsonValue, WorkflowContractModel, WorkflowNodeId

CODE_RUNTIME_CONTRACT = "python3.12-v1"
CODE_NETWORK_POLICY = "deny_all"

MAX_CODE_SOURCE_BYTES = 64 * 1024
MAX_CODE_INPUT_BYTES = 1024 * 1024
MAX_CODE_RESULT_BYTES = 512 * 1024
MAX_CODE_LOG_TAIL_BYTES = 64 * 1024
MAX_CODE_TOTAL_LOG_BYTES = 256 * 1024
MAX_CODE_WALL_TIMEOUT_MS = 30_000
MAX_CODE_CPU_MILLICORES = 1_000
MAX_CODE_MEMORY_BYTES = 256 * 1024 * 1024
MAX_CODE_PIDS = 32
MAX_CODE_TMPFS_BYTES = 64 * 1024 * 1024

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _validate_sha256(value: str) -> str:
    if not _SHA256_PATTERN.fullmatch(value):
        raise ValueError("expected a lowercase SHA-256 digest")
    return value


def _validate_opaque_id(value: str) -> str:
    if not _OPAQUE_ID_PATTERN.fullmatch(value):
        raise ValueError("expected a bounded opaque identifier")
    return value


def _validate_literal_true(value: object) -> object:
    if type(value) is not bool or value is not True:
        raise ValueError("attested capability must be the JSON boolean true")
    return value


type Sha256Digest = Annotated[str, Strict(), AfterValidator(_validate_sha256)]
type OpaqueCodeId = Annotated[str, Strict(), AfterValidator(_validate_opaque_id)]
type PositiveStrictInt = Annotated[int, Strict(), Field(gt=0)]
type NonNegativeStrictInt = Annotated[int, Strict(), Field(ge=0)]
type StrictTrue = Annotated[Literal[True], BeforeValidator(_validate_literal_true)]


class FrozenCodeLimits(WorkflowContractModel):
    """Admission-frozen Code limits mirrored from ``workflow_runtime``.

    Field names intentionally match ``WorkflowCodeHardLimitsV1`` so the
    application boundary can construct this value without a second naming or
    interpretation layer.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )

    wall_timeout_ms: Annotated[PositiveStrictInt, Field(le=MAX_CODE_WALL_TIMEOUT_MS)]
    # cgroup CPU scheduling rate in millicores (1000 = one vCPU).  This is not
    # cumulative CPU time; wall_timeout_ms is the total execution deadline.
    cpu_millicores: Annotated[PositiveStrictInt, Field(le=MAX_CODE_CPU_MILLICORES)]
    memory_bytes: Annotated[PositiveStrictInt, Field(le=MAX_CODE_MEMORY_BYTES)]
    max_pids: Annotated[PositiveStrictInt, Field(le=MAX_CODE_PIDS)]
    tmpfs_bytes: Annotated[PositiveStrictInt, Field(le=MAX_CODE_TMPFS_BYTES)]
    max_source_bytes: Annotated[PositiveStrictInt, Field(le=MAX_CODE_SOURCE_BYTES)]
    max_stdout_bytes: Annotated[PositiveStrictInt, Field(le=MAX_CODE_LOG_TAIL_BYTES)]
    max_stderr_bytes: Annotated[PositiveStrictInt, Field(le=MAX_CODE_LOG_TAIL_BYTES)]
    max_result_bytes: Annotated[PositiveStrictInt, Field(le=MAX_CODE_RESULT_BYTES)]
    max_total_log_bytes: Annotated[PositiveStrictInt, Field(le=MAX_CODE_TOTAL_LOG_BYTES)]

    @model_validator(mode="after")
    def _require_sufficient_total_log_budget(self) -> Self:
        if self.max_total_log_bytes < self.max_stdout_bytes + self.max_stderr_bytes:
            raise ValueError("total Code log budget cannot be below the sum of retained tails")
        return self


DEFAULT_CODE_LIMITS = FrozenCodeLimits(
    wall_timeout_ms=MAX_CODE_WALL_TIMEOUT_MS,
    cpu_millicores=MAX_CODE_CPU_MILLICORES,
    memory_bytes=MAX_CODE_MEMORY_BYTES,
    max_pids=MAX_CODE_PIDS,
    tmpfs_bytes=MAX_CODE_TMPFS_BYTES,
    max_source_bytes=MAX_CODE_SOURCE_BYTES,
    max_stdout_bytes=MAX_CODE_LOG_TAIL_BYTES,
    max_stderr_bytes=MAX_CODE_LOG_TAIL_BYTES,
    max_result_bytes=MAX_CODE_RESULT_BYTES,
    max_total_log_bytes=MAX_CODE_TOTAL_LOG_BYTES,
)


class CodeActivationIdentity(WorkflowContractModel):
    """Server-derived activation coordinates; there is intentionally no Thread."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )

    project_id: OpaqueCodeId
    owner_user_id: OpaqueCodeId
    workflow_run_id: OpaqueCodeId
    node_id: WorkflowNodeId
    activation_id: OpaqueCodeId
    attempt: Annotated[PositiveStrictInt, Field(le=1_000_000)]

    def digest(self) -> str:
        payload: dict[str, JsonValue] = self.model_dump(mode="json")
        return hashlib.sha256(canonical_json_value(payload).encode("utf-8")).hexdigest()


class CodeProvisioningHandle(WorkflowContractModel):
    """Server-owned journal handle fixed before a provider allocates anything."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )

    lease_id: OpaqueCodeId
    reconciliation_key: Annotated[str, Strict(), Field(min_length=32, max_length=512, repr=False)]

    @property
    def reconciliation_key_hash(self) -> str:
        return hashlib.sha256(self.reconciliation_key.encode("utf-8")).hexdigest()


class IsolatedCodeExecutionRequest(WorkflowContractModel):
    """Complete, immutable input to one fresh Python activation."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )

    runtime_contract: Literal["python3.12-v1"]
    activation: CodeActivationIdentity
    profile_digest: Sha256Digest
    source: Annotated[str, Strict(), Field(min_length=1)]
    source_digest: Sha256Digest
    inputs: dict[str, JsonValue]
    limits: FrozenCodeLimits
    network_policy: Literal["deny_all"]

    @field_validator("source")
    @classmethod
    def _bound_source_utf8(cls, value: str) -> str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("source must contain only Unicode scalar values") from exc
        if len(encoded) > MAX_CODE_SOURCE_BYTES:
            raise ValueError("source exceeds the UTF-8 byte limit")
        return value

    @field_validator("inputs")
    @classmethod
    def _bound_canonical_inputs(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        try:
            canonical_json_value_with_utf8_budget(value, max_utf8_bytes=MAX_CODE_INPUT_BYTES)
        except CanonicalJsonUtf8BudgetExceeded as exc:
            raise ValueError("canonical inputs exceed the byte limit") from exc
        return value

    @model_validator(mode="after")
    def _verify_source_digest(self) -> Self:
        actual = hashlib.sha256(self.source.encode("utf-8")).hexdigest()
        if actual != self.source_digest:
            raise ValueError("source digest mismatch")
        if len(self.source.encode("utf-8")) > self.limits.max_source_bytes:
            raise ValueError("source exceeds the admission-frozen byte limit")
        return self

    def runner_envelope(self) -> dict[str, JsonValue]:
        """Return the only payload that crosses into the isolated runtime."""

        return {
            "inputs": self.inputs,
            "limits": {
                "result_bytes": self.limits.max_result_bytes,
                "stderr_tail_bytes": self.limits.max_stderr_bytes,
                "stdout_tail_bytes": self.limits.max_stdout_bytes,
            },
            "runtime_contract": self.runtime_contract,
            "source": self.source,
            "source_digest": self.source_digest,
        }


class IsolatedCodeExecutionLease(WorkflowContractModel):
    """Opaque provider lease; resource identifiers are never public DTO fields."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )

    lease_id: OpaqueCodeId
    activation_digest: Sha256Digest
    profile_digest: Sha256Digest
    resource_id: OpaqueCodeId


type CodeExecutionOutcome = Literal[
    "succeeded",
    "syntax_error",
    "runtime_error",
    "timeout",
    "resource_exhausted",
    "output_limit",
    "cancelled",
    "infrastructure_error",
]
type CodeExecutionInterruption = Literal["cancelled", "lease_lost"]


class IsolatedCodeExecutionResult(WorkflowContractModel):
    """Bounded structured candidate produced before the cleanup barrier."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )

    outcome: CodeExecutionOutcome
    exit_code: Annotated[int, Strict(), Field(ge=0, le=255)] | None
    result: dict[str, JsonValue] | None
    stdout_tail: str
    stderr_tail: str
    truncated: bool
    duration_ms: NonNegativeStrictInt
    interruption: CodeExecutionInterruption | None = None

    @field_validator("stdout_tail", "stderr_tail")
    @classmethod
    def _require_valid_log_text(cls, value: str) -> str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("log tails must be valid UTF-8") from exc
        return value

    @model_validator(mode="after")
    def _require_consistent_outcome(self) -> Self:
        if self.outcome == "succeeded":
            if self.exit_code != 0 or self.result is None or self.interruption is not None:
                raise ValueError("successful execution requires exit 0, a result, and no interruption")
            try:
                canonical_json_value_with_utf8_budget(
                    self.result,
                    max_utf8_bytes=MAX_CODE_RESULT_BYTES,
                )
            except CanonicalJsonUtf8BudgetExceeded as exc:
                raise ValueError("result exceeds the global byte limit") from exc
        elif self.result is not None:
            raise ValueError("failed execution cannot carry a candidate result")
        if self.outcome == "cancelled" and self.interruption is None:
            raise ValueError("cancelled execution requires an interruption reason")
        if self.outcome != "cancelled" and self.interruption is not None:
            raise ValueError("only cancelled execution may carry an interruption reason")
        return self


class CodeCleanupReceipt(WorkflowContractModel):
    """Authoritative cleanup result for one lease."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )

    lease_id: OpaqueCodeId
    state: Literal["destroyed_confirmed", "cleanup_pending"]
    reason: Literal[
        "completed",
        "failed",
        "timeout",
        "cancelled",
        "lease_lost",
        "worker_crash_reconcile",
        "cleanup_retry",
    ]


class CodeExecutionCompletion(WorkflowContractModel):
    """A candidate result that crossed a destroy-confirmed barrier."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )

    lease: IsolatedCodeExecutionLease
    result: IsolatedCodeExecutionResult
    cleanup: CodeCleanupReceipt

    @model_validator(mode="after")
    def _require_destroy_confirmation(self) -> Self:
        if self.cleanup.lease_id != self.lease.lease_id:
            raise ValueError("cleanup receipt does not match the execution lease")
        if self.cleanup.state != "destroyed_confirmed":
            raise ValueError("execution completion requires destroy confirmation")
        return self


class IsolatedCodeProfileAttestation(WorkflowContractModel):
    """Secret-free proof material advertised by a concrete Worker profile."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )

    profile_key: Literal["docker-python3.12-v1", "provisioner-python3.12-v1"]
    profile_digest: Sha256Digest
    runtime_contract: Literal["python3.12-v1"]
    image_digest: Sha256Digest
    runner_digest: Sha256Digest
    network_policy: Literal["deny_all"]
    fresh_activation: StrictTrue
    no_mounts: StrictTrue
    empty_environment: StrictTrue
    non_root: StrictTrue
    read_only_rootfs: StrictTrue
    no_new_privileges: StrictTrue
    capabilities_dropped: StrictTrue
    destroy_confirmation: StrictTrue
    orphan_reconciliation: StrictTrue
    orphan_fence_contract: Literal["local-posix-flock-v1", "durable-control-plane-v1"]
    maximum_limits: FrozenCodeLimits

    @model_validator(mode="after")
    def _verify_profile_digest(self) -> Self:
        payload: dict[str, JsonValue] = self.model_dump(
            mode="json",
            exclude={"profile_digest"},
        )
        actual = hashlib.sha256(canonical_json_value(payload).encode("utf-8")).hexdigest()
        if actual != self.profile_digest:
            raise ValueError("Code profile attestation digest mismatch")
        return self
