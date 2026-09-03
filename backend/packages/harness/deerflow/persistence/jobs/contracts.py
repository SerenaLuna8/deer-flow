"""Durable Job contracts: scopes, requests, claims, terminal events, ports, and errors."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.user.private_lifecycle import AccountPrivateGeneration
from deerflow.public_error_codes import LLM_PUBLIC_ERROR_CODES
from deerflow.trace_context import normalize_trace_id

JobType = Literal[
    "private_run",
    "automation_run",
    "retention_purge",
    "mcp_discovery",
    "memory_dream",
    "memory_dream_prepare",
    "memory_seal",
]
RetrySafety = Literal["safe", "unknown", "unsafe"]

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_DETERMINISTIC_NONRETRYABLE_ERROR_CODES = (
    frozenset(
        {
            "MODEL_OUTPUT_LIMIT",
            "GRAPH_RECURSION_LIMIT",
            "PROVIDER_REQUEST_USAGE_UNSUPPORTED",
            "PROVIDER_REQUEST_PROFILE_DRIFT",
            "CONTEXT_CAPACITY_EXCEEDED",
            "CONTEXT_PROVIDER_CALL_AMBIGUOUS",
            "LOOP_SAFETY_LIMIT",
            "OUTPUT_DELIVERY_INCOMPLETE",
            "CURRENT_UPLOAD_UNAVAILABLE",
        }
    )
    | LLM_PUBLIC_ERROR_CODES
)


def _durable_terminal_successor_idempotency_key(
    predecessor_job_id: uuid.UUID,
) -> str:
    """Persist the settlement-only mode in a domain-separated Job identity."""

    return hashlib.sha256((f"durable-terminal-settlement-successor:v1:{predecessor_job_id}").encode()).hexdigest()


def _dead_error_code_for_failure(
    *,
    retry_safety: RetrySafety,
    public_error_code: str,
    retryable: bool,
) -> str:
    """Preserve only reviewed deterministic codes after an earlier side effect."""

    if retry_safety != "safe" and not (not retryable and public_error_code in _DETERMINISTIC_NONRETRYABLE_ERROR_CODES):
        return "SIDE_EFFECT_STATE_UNKNOWN"
    return public_error_code


@dataclass(frozen=True, slots=True)
class JobScope:
    project_id: uuid.UUID
    owner_user_id: str | None

    def __post_init__(self) -> None:
        try:
            project_id = uuid.UUID(str(self.project_id))
            owner_user_id = None if self.owner_user_id is None else str(uuid.UUID(self.owner_user_id))
        except (TypeError, ValueError):
            raise ValueError("job scope requires valid project and owner UUIDs") from None
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "owner_user_id", owner_user_id)


@dataclass(frozen=True, slots=True)
class RetentionPurgeJobAuthority:
    """Restart-safe authority for one exact destructive retention case."""

    resource_kind: Literal["project", "former_owner", "account"]
    project_id: uuid.UUID
    owner_user_id: str | None
    generation: int
    effective_at: datetime
    membership_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        scope = JobScope(self.project_id, self.owner_user_id)
        if (
            self.resource_kind not in {"project", "former_owner", "account"}
            or type(self.generation) is not int
            or self.generation < 1
            or not isinstance(self.effective_at, datetime)
            or self.effective_at.tzinfo is None
            or self.effective_at.utcoffset() is None
        ):
            raise ValueError("invalid retention purge authority")
        membership_id = self.membership_id
        if membership_id is not None:
            try:
                membership_id = uuid.UUID(str(membership_id))
            except (TypeError, ValueError):
                raise ValueError("invalid retention membership authority") from None
        if self.resource_kind == "project":
            if scope.owner_user_id is not None or membership_id is not None:
                raise ValueError("project retention authority has invalid owner coordinates")
        elif self.resource_kind == "former_owner":
            if scope.owner_user_id is None or membership_id is None:
                raise ValueError("former_owner retention authority requires membership")
        elif scope.owner_user_id is None or membership_id is not None:
            raise ValueError("account retention authority has invalid owner coordinates")
        object.__setattr__(self, "project_id", scope.project_id)
        object.__setattr__(self, "owner_user_id", scope.owner_user_id)
        object.__setattr__(self, "membership_id", membership_id)
        object.__setattr__(self, "effective_at", self.effective_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class EnqueueJob:
    job_type: JobType
    scope: JobScope
    idempotency_key: str
    run_id: str | None
    occurrence_id: str | None
    max_attempts: int
    owner_private_generation: AccountPrivateGeneration | RetentionPurgeJobAuthority
    namespace: str | None = None
    origin_trace_id: str | None = None
    retry_safety: RetrySafety = "safe"
    priority: int = 0
    available_at: datetime | None = None
    predecessor_dead_job_id: uuid.UUID | None = None
    execution_domain_affinity: str | None = None

    def __post_init__(self) -> None:
        if type(self.scope) is not JobScope:
            raise TypeError("JobScope is required")
        if self.job_type not in {
            "private_run",
            "automation_run",
            "retention_purge",
            "mcp_discovery",
            "memory_dream",
            "memory_dream_prepare",
            "memory_seal",
        }:
            raise ValueError("unsupported job type")
        if _SHA256_HEX.fullmatch(self.idempotency_key) is None:
            raise ValueError("idempotency_key must be a lowercase SHA-256 digest")
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        if self.retry_safety not in {"safe", "unknown", "unsafe"}:
            raise ValueError("unsupported retry safety")
        if self.job_type == "retention_purge":
            if type(self.owner_private_generation) is not RetentionPurgeJobAuthority:
                raise TypeError(
                    "retention_purge requires RetentionPurgeJobAuthority",
                )
            if self.owner_private_generation.project_id != self.scope.project_id or self.owner_private_generation.owner_user_id != self.scope.owner_user_id:
                raise ValueError("retention purge authority scope mismatch")
        else:
            if type(self.owner_private_generation) is not AccountPrivateGeneration:
                raise TypeError(
                    "owner Jobs require AccountPrivateGeneration",
                )
            if self.owner_private_generation.owner_user_id != self.scope.owner_user_id:
                raise ValueError(
                    "account-private generation owner does not match Job scope owner",
                )
        if self.execution_domain_affinity is not None:
            if _SHA256_HEX.fullmatch(self.execution_domain_affinity) is None:
                raise ValueError(
                    "execution domain affinity must be a lowercase SHA-256 digest",
                )
            if self.job_type != "private_run":
                raise ValueError(
                    "execution domain affinity is supported only for private_run jobs",
                )
        if not -32768 <= self.priority <= 32767:
            raise ValueError("priority is outside the smallint range")
        if self.available_at is not None and self.available_at.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")
        if self.job_type == "private_run":
            if self.scope.owner_user_id is None or not self.run_id or self.occurrence_id is not None:
                raise ValueError("private_run requires owner and run authority only")
        elif self.job_type == "automation_run":
            if self.scope.owner_user_id is None or not self.run_id or not self.occurrence_id:
                raise ValueError("automation_run requires owner, run, and occurrence authority")
        elif self.job_type == "retention_purge" and (self.run_id is not None or self.occurrence_id is not None):
            raise ValueError(
                "retention_purge requires project or exact former-owner authority",
            )
        elif self.job_type == "mcp_discovery" and (self.scope.owner_user_id is None or self.run_id is not None or self.occurrence_id is not None):
            raise ValueError(
                "mcp_discovery requires project owner authority without Run or occurrence",
            )
        elif self.job_type in {"memory_dream", "memory_dream_prepare"}:
            if self.scope.owner_user_id is None or self.run_id is not None or self.occurrence_id is not None:
                raise ValueError(f"{self.job_type} requires owner authority without Run or occurrence")
            if not self.namespace or len(self.namespace) > 255:
                raise ValueError(f"{self.job_type} requires a bounded namespace")
        elif self.job_type == "memory_seal":
            # For seal jobs the namespace column carries the Thread id — the
            # coordinate the Worker drains — instead of a Memory namespace.
            if self.scope.owner_user_id is None or self.run_id is not None or self.occurrence_id is not None:
                raise ValueError("memory_seal requires owner authority without Run or occurrence")
            if not self.namespace or len(self.namespace) > 255:
                raise ValueError("memory_seal requires a bounded thread coordinate")
        if self.job_type not in {"memory_dream", "memory_dream_prepare", "memory_seal"} and self.namespace is not None:
            raise ValueError(f"{self.job_type} does not accept a memory namespace")
        normalized_trace_id = normalize_trace_id(self.origin_trace_id)
        if self.job_type in {"private_run", "automation_run"}:
            if normalized_trace_id is None:
                raise ValueError("Run jobs require a valid origin trace")
            object.__setattr__(self, "origin_trace_id", normalized_trace_id)
        elif self.origin_trace_id is not None:
            raise ValueError(f"{self.job_type} does not accept an origin trace")


@dataclass(frozen=True, slots=True)
class JobClaim:
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    lease_token: str = field(repr=False)
    job_type: JobType
    scope: JobScope
    run_id: str | None
    occurrence_id: str | None
    retry_safety: RetrySafety
    cancel_requested: bool
    namespace: str | None = None
    origin_trace_id: str | None = None
    execution_domain_affinity: str | None = None
    predecessor_dead_job_id: uuid.UUID | None = None
    settlement_only: bool = False


@dataclass(frozen=True, slots=True)
class JobHeartbeat:
    cancel_requested: bool


@dataclass(frozen=True, slots=True)
class JobUnstartedClaimRelease:
    disposition: Literal["requeued", "cancelled"]


@dataclass(frozen=True, slots=True)
class JobOwnerRef:
    key_id: str
    hmac_hex: str

    def __post_init__(self) -> None:
        if not self.key_id or len(self.key_id) > 64:
            raise ValueError("owner reference key id is invalid")
        if _SHA256_HEX.fullmatch(self.hmac_hex) is None:
            raise ValueError("owner reference must be a lowercase HMAC-SHA256 digest")


@dataclass(frozen=True, slots=True)
class DeadJobRecord:
    job_id: uuid.UUID
    project_id: uuid.UUID
    job_type: JobType
    attempt_count: int
    retry_safety: RetrySafety
    public_error_code: str
    dead_at: datetime


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class DeadJobRequeuedEvent:
    project_id: uuid.UUID
    predecessor_job_id: uuid.UUID
    successor_job_id: uuid.UUID
    request_id: str = field(repr=False)
    job_type: JobType
    attempt_count: int
    retry_safety: RetrySafety


@dataclass(frozen=True, slots=True)
class JobTerminalEvent:
    job_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str | None = field(repr=False)
    run_id: str | None = field(repr=False)
    occurrence_id: str | None
    job_type: JobType
    status: Literal["cancelled", "dead"]
    retry_safety: RetrySafety
    public_error_code: str | None
    cancel_reason: str | None
    occurred_at: datetime
    attempt_count: int = 0
    origin_trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class JobTerminalResult:
    run_terminal_published: bool


@dataclass(frozen=True, slots=True)
class DurableTerminalTakeoverRequest:
    """Coordinates for one expired Job whose durable terminal may settle it."""

    job_id: uuid.UUID
    attempt_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str = field(repr=False)
    run_id: str = field(repr=False)
    job_type: Literal["private_run", "automation_run"]
    retry_safety: RetrySafety
    attempt_count: int
    max_attempts: int
    origin_trace_id: str = field(repr=False)
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class DurableDeadTerminalReconciliationRequest:
    """Exact dead Run coordinates eligible for terminal-only reconciliation."""

    predecessor_job_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str = field(repr=False)
    run_id: str = field(repr=False)
    occurrence_id: str | None
    job_type: Literal["private_run", "automation_run"]
    retry_safety: RetrySafety
    attempt_count: int
    max_attempts: int
    public_error_code: Literal[
        "SIDE_EFFECT_STATE_UNKNOWN",
        "ATTEMPTS_EXHAUSTED",
    ]
    origin_trace_id: str = field(repr=False)
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class DurableTerminalSuccessorRebindRequest:
    """Atomically move one running Run to its terminal-only successor Job."""

    predecessor_job_id: uuid.UUID
    successor_job_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str = field(repr=False)
    run_id: str = field(repr=False)
    occurrence_id: str | None
    job_type: Literal["private_run", "automation_run"]
    origin_trace_id: str = field(repr=False)
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class JobRetryResult:
    changed: bool
    run_terminal_published: bool


class JobAuditPort(Protocol):
    async def dead_job_requeued(
        self,
        session: AsyncSession,
        event: DeadJobRequeuedEvent,
    ) -> None: ...


class JobTerminalPort(Protocol):
    async def durable_terminal_takeover_allowed(
        self,
        session: AsyncSession,
        event: DurableTerminalTakeoverRequest,
    ) -> bool: ...

    async def durable_dead_terminal_reconciliation_allowed(
        self,
        session: AsyncSession,
        event: DurableDeadTerminalReconciliationRequest,
    ) -> bool: ...

    async def rebind_durable_terminal_successor(
        self,
        session: AsyncSession,
        event: DurableTerminalSuccessorRebindRequest,
    ) -> bool: ...

    async def job_terminalized(
        self,
        session: AsyncSession,
        event: JobTerminalEvent,
    ) -> JobTerminalResult: ...


class JobIdempotencyConflict(RuntimeError):
    """The idempotency key already identifies different job authority."""


class JobOwnerRefRequired(RuntimeError):
    """A private dead projection cannot be written without an owner HMAC."""


class JobRequeueForbidden(RuntimeError):
    """The dead job is absent from scope or is not safe to requeue."""


def retry_backoff_seconds(
    *,
    attempt_count: int,
    initial_seconds: int,
    max_seconds: int,
) -> int:
    if attempt_count < 1 or initial_seconds < 1 or max_seconds < initial_seconds:
        raise ValueError("invalid retry backoff inputs")
    delay = initial_seconds
    for _ in range(attempt_count - 1):
        delay = min(max_seconds, delay * 2)
        if delay == max_seconds:
            break
    return delay
