"""Session-bound durable job repository."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Literal, Protocol, TypeGuard

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.jobs.model import DeadJobRow, JobAttemptRow, JobRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.user.model import UserRow
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


def _is_durable_terminal_successor(row: JobRow) -> bool:
    predecessor_job_id = row.predecessor_dead_job_id
    return (
        row.job_type in {"private_run", "automation_run"}
        and predecessor_job_id is not None
        and row.idempotency_key
        == _durable_terminal_successor_idempotency_key(
            predecessor_job_id,
        )
    )


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


_ISSUED_REQUEUE_EVENTS: dict[
    int,
    tuple[
        weakref.ReferenceType[DeadJobRequeuedEvent],
        tuple[
            uuid.UUID,
            uuid.UUID,
            uuid.UUID,
            str,
            JobType,
            int,
            RetrySafety,
        ],
    ],
] = {}
_ISSUED_REQUEUE_EVENTS_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class _DeadTerminalReconciliationCursor:
    dead_at: datetime
    job_id: uuid.UUID


_DEAD_TERMINAL_RECONCILIATION_PAGE_SIZE = 100
# This cursor is deliberately process-local: every Worker process makes its own
# bounded progress, while PostgreSQL row locks and the unique successor lineage
# remain the cross-process correctness fence.  Compare-and-set advancement keeps
# concurrent claim loops in one process from moving a shared cursor backwards;
# weak Engine keys discard state when a session factory is retired.
_DEAD_TERMINAL_RECONCILIATION_CURSORS: weakref.WeakKeyDictionary[
    object,
    dict[
        tuple[tuple[str, ...], str | None],
        _DeadTerminalReconciliationCursor,
    ],
] = weakref.WeakKeyDictionary()
_DEAD_TERMINAL_RECONCILIATION_CURSORS_LOCK = Lock()


def _dead_terminal_reconciliation_cursor(
    bind: object,
    scope: tuple[tuple[str, ...], str | None],
) -> _DeadTerminalReconciliationCursor | None:
    """Read one process-local liveness hint without granting authority."""

    with _DEAD_TERMINAL_RECONCILIATION_CURSORS_LOCK:
        return _DEAD_TERMINAL_RECONCILIATION_CURSORS.get(bind, {}).get(scope)


def _advance_dead_terminal_reconciliation_cursor(
    bind: object,
    scope: tuple[tuple[str, ...], str | None],
    *,
    expected: _DeadTerminalReconciliationCursor | None,
    updated: _DeadTerminalReconciliationCursor | None,
) -> None:
    """Advance a scan page only when a concurrent claimant did not move it."""

    with _DEAD_TERMINAL_RECONCILIATION_CURSORS_LOCK:
        scoped = _DEAD_TERMINAL_RECONCILIATION_CURSORS.get(bind)
        current = None if scoped is None else scoped.get(scope)
        if current != expected:
            return
        if updated is None:
            if scoped is None:
                return
            scoped.pop(scope, None)
            if not scoped:
                _DEAD_TERMINAL_RECONCILIATION_CURSORS.pop(bind, None)
            return
        if scoped is None:
            scoped = {}
            _DEAD_TERMINAL_RECONCILIATION_CURSORS[bind] = scoped
        scoped[scope] = updated


def consume_issued_dead_job_requeued_event(
    value: object,
) -> TypeGuard[DeadJobRequeuedEvent]:
    if type(value) is not DeadJobRequeuedEvent:
        return False
    with _ISSUED_REQUEUE_EVENTS_LOCK:
        issued = _ISSUED_REQUEUE_EVENTS.pop(id(value), None)
    try:
        return (
            issued is not None
            and issued[0]() is value
            and issued[1]
            == (
                value.project_id,
                value.predecessor_job_id,
                value.successor_job_id,
                value.request_id,
                value.job_type,
                value.attempt_count,
                value.retry_safety,
            )
        )
    except AttributeError:
        return False


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


def _lease_token_hash(lease_token: str) -> str:
    return hashlib.sha256(lease_token.encode("utf-8")).hexdigest()


class JobRepository:
    """Session-bound job state machine; callers own commit and rollback."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        owner_ref_hasher: Callable[[str], JobOwnerRef] | None = None,
        terminal_port: JobTerminalPort | None = None,
    ) -> None:
        self.session = session
        self._owner_ref_hasher = owner_ref_hasher
        self._terminal_port = terminal_port

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        result = value or datetime.now(UTC)
        if result.tzinfo is None:
            raise ValueError("job transition time must be timezone-aware")
        return result

    @staticmethod
    def _scope_predicates(scope: JobScope) -> tuple[sa.ColumnElement[bool], ...]:
        if type(scope) is not JobScope:
            raise TypeError("JobScope is required")
        owner = JobRow.owner_user_id.is_(None) if scope.owner_user_id is None else JobRow.owner_user_id == scope.owner_user_id
        return (JobRow.project_id == scope.project_id, owner)

    @staticmethod
    def _same_authority(row: JobRow, request: EnqueueJob) -> bool:
        retention = request.owner_private_generation if type(request.owner_private_generation) is RetentionPurgeJobAuthority else None
        return (
            row.project_id == request.scope.project_id
            and row.owner_user_id == request.scope.owner_user_id
            and row.owner_private_generation == request.owner_private_generation.generation
            and row.retention_resource_kind == (None if retention is None else retention.resource_kind)
            and row.retention_effective_at == (None if retention is None else retention.effective_at)
            and row.retention_membership_id == (None if retention is None else retention.membership_id)
            and row.run_id == request.run_id
            and row.automation_occurrence_id == request.occurrence_id
            and row.namespace == request.namespace
            and row.predecessor_dead_job_id == request.predecessor_dead_job_id
            and row.origin_trace_id == request.origin_trace_id
            and row.execution_domain_affinity == request.execution_domain_affinity
            and row.max_attempts == request.max_attempts
            and row.retry_safety == request.retry_safety
            and row.priority == request.priority
        )

    async def _enqueue(self, request: EnqueueJob) -> tuple[uuid.UUID, bool]:
        if type(request) is not EnqueueJob:
            raise TypeError("EnqueueJob is required")
        now = datetime.now(UTC)
        job_id = uuid.uuid4()
        retention = request.owner_private_generation if type(request.owner_private_generation) is RetentionPurgeJobAuthority else None
        inserted_id = await self.session.scalar(
            pg_insert(JobRow)
            .values(
                id=job_id,
                job_type=request.job_type,
                project_id=request.scope.project_id,
                owner_user_id=request.scope.owner_user_id,
                owner_private_generation=request.owner_private_generation.generation,
                retention_resource_kind=(None if retention is None else retention.resource_kind),
                retention_effective_at=(None if retention is None else retention.effective_at),
                retention_membership_id=(None if retention is None else retention.membership_id),
                namespace=request.namespace,
                run_id=request.run_id,
                automation_occurrence_id=request.occurrence_id,
                predecessor_dead_job_id=request.predecessor_dead_job_id,
                origin_trace_id=request.origin_trace_id,
                execution_domain_affinity=request.execution_domain_affinity,
                idempotency_key=request.idempotency_key,
                status="queued",
                priority=request.priority,
                available_at=(request.available_at if request.available_at is not None else sa.func.clock_timestamp()),
                attempt_count=0,
                max_attempts=request.max_attempts,
                retry_safety=request.retry_safety,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[JobRow.job_type, JobRow.idempotency_key],
            )
            .returning(JobRow.id)
        )
        if inserted_id is not None:
            return inserted_id, True
        existing = (
            await self.session.execute(
                sa.select(JobRow).where(
                    JobRow.job_type == request.job_type,
                    JobRow.idempotency_key == request.idempotency_key,
                )
            )
        ).scalar_one()
        if not self._same_authority(existing, request):
            raise JobIdempotencyConflict("job idempotency authority conflict")
        return existing.id, False

    async def _lock_authority(
        self,
        project_id: uuid.UUID,
        owner_user_id: str | None,
    ) -> bool:
        """Lock Project -> Membership before any terminal Job/Run mutation."""

        project = await self.session.scalar(sa.select(ProjectRow.id).where(ProjectRow.id == project_id).with_for_update(of=ProjectRow))
        if project is None:
            return False
        if owner_user_id is None:
            return True
        membership = await self.session.scalar(
            sa.select(ProjectMembershipRow.id)
            .where(
                ProjectMembershipRow.project_id == project_id,
                ProjectMembershipRow.user_id == owner_user_id,
            )
            .with_for_update(of=ProjectMembershipRow)
        )
        return membership is not None

    async def _lock_job_authority(self, job_id: uuid.UUID) -> bool:
        coordinates = (
            await self.session.execute(
                sa.select(
                    JobRow.project_id,
                    JobRow.owner_user_id,
                ).where(JobRow.id == job_id)
            )
        ).one_or_none()
        if coordinates is None:
            return False
        return await self._lock_authority(
            coordinates.project_id,
            coordinates.owner_user_id,
        )

    async def _lock_claim_authority(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str | None,
        job_type: JobType,
        owner_private_generation: int | None,
        retention_resource_kind: str | None,
        retention_effective_at: datetime | None,
        retention_membership_id: uuid.UUID | None,
    ) -> bool:
        """Lock Project -> Membership -> User before a claim mutation."""

        project = (
            await self.session.execute(
                sa.select(
                    ProjectRow.status,
                    ProjectRow.is_suspended,
                    ProjectRow.membership_version,
                    ProjectRow.deletion_effective_at,
                )
                .where(ProjectRow.id == project_id)
                .with_for_update(read=True, of=ProjectRow)
            )
        ).one_or_none()
        if project is None:
            return False
        retention = job_type == "retention_purge"
        if not retention and (project.status != "active" or project.is_suspended is not False):
            return False
        if owner_user_id is None:
            return (
                retention
                and retention_resource_kind == "project"
                and type(owner_private_generation) is int
                and owner_private_generation >= 1
                and isinstance(retention_effective_at, datetime)
                and retention_effective_at.tzinfo is not None
                and retention_membership_id is None
                and project.status == "pending_deletion"
                and project.membership_version == owner_private_generation
                and project.deletion_effective_at == retention_effective_at
            )
        membership = (
            await self.session.execute(
                sa.select(
                    ProjectMembershipRow.id,
                    ProjectMembershipRow.status,
                    ProjectMembershipRow.activation_generation,
                    ProjectMembershipRow.retention_until,
                )
                .where(
                    ProjectMembershipRow.project_id == project_id,
                    ProjectMembershipRow.user_id == owner_user_id,
                )
                .with_for_update(read=True, of=ProjectMembershipRow)
            )
        ).one_or_none()
        if membership is None:
            return False
        if not retention and membership.status != "active":
            return False
        if type(owner_private_generation) is not int or owner_private_generation < 1:
            return False
        lifecycle = (
            await self.session.execute(
                sa.select(
                    UserRow.private_retention_state,
                    UserRow.private_retention_generation,
                    UserRow.private_retention_effective_at,
                )
                .where(UserRow.id == owner_user_id)
                .with_for_update(read=True, of=UserRow)
            )
        ).one_or_none()
        if lifecycle is None:
            return False
        if retention:
            if retention_resource_kind == "former_owner":
                return (
                    retention_membership_id == membership.id
                    and membership.status in {"left", "removed"}
                    and membership.activation_generation == owner_private_generation
                    and membership.retention_until is not None
                    and isinstance(retention_effective_at, datetime)
                    and retention_effective_at.tzinfo is not None
                )
            return (
                retention_resource_kind == "account"
                and retention_membership_id is None
                and isinstance(retention_effective_at, datetime)
                and retention_effective_at.tzinfo is not None
                and lifecycle.private_retention_state == "pending_deletion"
                and lifecycle.private_retention_generation == owner_private_generation
                and lifecycle.private_retention_effective_at == retention_effective_at
            )
        if retention_resource_kind is not None or retention_effective_at is not None or retention_membership_id is not None:
            return False
        return lifecycle.private_retention_state == "active" and lifecycle.private_retention_generation == owner_private_generation

    async def _lock_memory_prepare_before_job(
        self,
        *,
        job_id: uuid.UUID,
        project_id: uuid.UUID,
        owner_user_id: str | None,
    ) -> None:
        """Complete the Memory preparation lock prefix before claiming its Job.

        ``claim_next`` may itself terminalize an expired/cancel-requested Job.
        For a durable Dream preparation that transition also updates its run
        row, so the claimant must own Thread -> preparation before Job.  Local
        imports keep the generic jobs module free of an import cycle.
        """

        if owner_user_id is None:
            return
        from deerflow.persistence.private_work.memory_document_model import (
            MemoryDreamPrepareRunRow,
        )
        from deerflow.persistence.thread_meta.model import ThreadMetaRow

        coordinates = (
            await self.session.execute(
                sa.select(
                    MemoryDreamPrepareRunRow.thread_id,
                    MemoryDreamPrepareRunRow.namespace,
                ).where(
                    MemoryDreamPrepareRunRow.job_id == job_id,
                    MemoryDreamPrepareRunRow.project_id == project_id,
                    MemoryDreamPrepareRunRow.owner_user_id == owner_user_id,
                )
            )
        ).one_or_none()
        if coordinates is None:
            # Account reset may have removed the private row while leaving a
            # leased Job for cooperative Worker cancellation.
            return
        await self.session.execute(
            sa.select(ThreadMetaRow.thread_id)
            .where(
                ThreadMetaRow.project_id == project_id,
                ThreadMetaRow.owner_user_id == owner_user_id,
                ThreadMetaRow.thread_id == coordinates.thread_id,
            )
            .with_for_update(of=ThreadMetaRow)
        )
        await self.session.execute(
            sa.select(MemoryDreamPrepareRunRow.job_id)
            .where(
                MemoryDreamPrepareRunRow.job_id == job_id,
                MemoryDreamPrepareRunRow.project_id == project_id,
                MemoryDreamPrepareRunRow.owner_user_id == owner_user_id,
                MemoryDreamPrepareRunRow.namespace == coordinates.namespace,
            )
            .with_for_update(of=MemoryDreamPrepareRunRow)
        )

    async def enqueue(self, request: EnqueueJob) -> uuid.UUID:
        job_id, _created = await self._enqueue(request)
        return job_id

    async def _finish_current_attempt(
        self,
        row: JobRow,
        *,
        outcome: str,
        now: datetime,
        public_error_code: str | None = None,
    ) -> None:
        result = await self.session.execute(
            sa.update(JobAttemptRow)
            .where(
                JobAttemptRow.job_id == row.id,
                JobAttemptRow.attempt_number == row.attempt_count,
                JobAttemptRow.lease_token_hash == row.lease_token_hash,
                JobAttemptRow.outcome.is_(None),
            )
            .values(
                heartbeat_at=now,
                finished_at=now,
                outcome=outcome,
                public_error_code=public_error_code,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError("active job attempt authority is missing")

    def _owner_ref(self, owner_user_id: str | None) -> JobOwnerRef | None:
        if owner_user_id is None:
            return None
        if self._owner_ref_hasher is None:
            raise JobOwnerRefRequired("owner HMAC is required for private dead jobs")
        owner_ref = self._owner_ref_hasher(owner_user_id)
        if type(owner_ref) is not JobOwnerRef:
            raise TypeError("owner_ref_hasher must return JobOwnerRef")
        return owner_ref

    async def _publish_terminal(
        self,
        row: JobRow,
        *,
        status: Literal["cancelled", "dead"],
        public_error_code: str | None,
        now: datetime,
    ) -> JobTerminalResult:
        if self._terminal_port is None:
            return JobTerminalResult(run_terminal_published=False)
        result = await self._terminal_port.job_terminalized(
            self.session,
            JobTerminalEvent(
                job_id=row.id,
                project_id=row.project_id,
                owner_user_id=row.owner_user_id,
                run_id=row.run_id,
                occurrence_id=row.automation_occurrence_id,
                job_type=row.job_type,
                status=status,
                retry_safety=row.retry_safety,
                public_error_code=public_error_code,
                cancel_reason=row.cancel_reason,
                occurred_at=now,
                attempt_count=row.attempt_count,
                origin_trace_id=row.origin_trace_id,
            ),
        )
        if type(result) is not JobTerminalResult:
            raise TypeError("job terminal port returned an invalid result")
        return result

    async def _mark_dead(
        self,
        row: JobRow,
        *,
        owner_ref: JobOwnerRef | None,
        public_error_code: str,
        now: datetime,
    ) -> JobTerminalResult:
        row.status = "dead"
        row.public_error_code = public_error_code
        row.lease_owner_id = None
        row.lease_token_hash = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        row.completed_at = now
        row.updated_at = now
        self.session.add(
            DeadJobRow(
                job_id=row.id,
                project_id=row.project_id,
                owner_ref_key_id=None if owner_ref is None else owner_ref.key_id,
                owner_ref_hmac=None if owner_ref is None else owner_ref.hmac_hex,
                job_type=row.job_type,
                attempt_count=row.attempt_count,
                retry_safety=row.retry_safety,
                public_error_code=public_error_code,
                dead_at=now,
            )
        )
        return await self._publish_terminal(
            row,
            status="dead",
            public_error_code=public_error_code,
            now=now,
        )

    async def _settle_unowned_cancel(self, row: JobRow, *, now: datetime) -> None:
        if row.status in {"leased", "running"}:
            await self._finish_current_attempt(row, outcome="cancelled", now=now)
        row.status = "cancelled"
        row.lease_owner_id = None
        row.lease_token_hash = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        row.public_error_code = None
        row.completed_at = now
        row.updated_at = now
        await self._publish_terminal(
            row,
            status="cancelled",
            public_error_code=None,
            now=now,
        )

    async def _prepare_dead_terminal_successor(
        self,
        *,
        job_types: list[str],
        execution_domain_claimable: sa.ColumnElement[bool],
        execution_domain_affinity: str | None,
        now: datetime,
    ) -> uuid.UUID | None:
        """Create and bind one terminal-only successor for a legacy dead Run.

        The dead Job and its append-only dead projection remain untouched. The
        successor is a new lineage node, and the Run (plus an Automation
        occurrence when present) is rebound in this same transaction only
        after the private terminal port proves durable terminal authority.
        """

        prove = getattr(
            self._terminal_port,
            "durable_dead_terminal_reconciliation_allowed",
            None,
        )
        rebind = getattr(
            self._terminal_port,
            "rebind_durable_terminal_successor",
            None,
        )
        run_job_types = sorted(set(job_types) & {"private_run", "automation_run"})
        if not run_job_types or not callable(prove) or not callable(rebind):
            return None

        cursor_scope = (
            tuple(run_job_types),
            execution_domain_affinity,
        )
        session_bind = self.session.get_bind()
        cursor_bind = getattr(session_bind, "engine", session_bind)
        starting_cursor = _dead_terminal_reconciliation_cursor(
            cursor_bind,
            cursor_scope,
        )

        def candidate_statement(
            after: _DeadTerminalReconciliationCursor | None,
        ) -> sa.Select:
            statement = (
                sa.select(
                    JobRow.id,
                    JobRow.project_id,
                    JobRow.owner_user_id,
                    JobRow.job_type,
                    JobRow.owner_private_generation,
                    JobRow.retention_resource_kind,
                    JobRow.retention_effective_at,
                    JobRow.retention_membership_id,
                    DeadJobRow.dead_at.label("reconciliation_dead_at"),
                )
                .join(
                    DeadJobRow,
                    sa.and_(
                        DeadJobRow.job_id == JobRow.id,
                        DeadJobRow.project_id == JobRow.project_id,
                        DeadJobRow.job_type == JobRow.job_type,
                        DeadJobRow.retry_safety == JobRow.retry_safety,
                        DeadJobRow.attempt_count == JobRow.attempt_count,
                        DeadJobRow.public_error_code == JobRow.public_error_code,
                    ),
                )
                .where(
                    JobRow.job_type.in_(run_job_types),
                    JobRow.status == "dead",
                    execution_domain_claimable,
                    sa.or_(
                        sa.and_(
                            JobRow.public_error_code == "SIDE_EFFECT_STATE_UNKNOWN",
                            JobRow.retry_safety != "safe",
                        ),
                        sa.and_(
                            JobRow.public_error_code == "ATTEMPTS_EXHAUSTED",
                            JobRow.attempt_count >= JobRow.max_attempts,
                        ),
                    ),
                    ~sa.exists(
                        sa.select(1).where(
                            JobRow.__table__.alias("durable_terminal_successor").c.predecessor_dead_job_id == JobRow.id,
                        )
                    ),
                )
                .order_by(DeadJobRow.dead_at, JobRow.id)
                .limit(_DEAD_TERMINAL_RECONCILIATION_PAGE_SIZE)
            )
            if after is not None:
                statement = statement.where(
                    sa.or_(
                        DeadJobRow.dead_at > after.dead_at,
                        sa.and_(
                            DeadJobRow.dead_at == after.dead_at,
                            JobRow.id > after.job_id,
                        ),
                    )
                )
            return statement

        candidates = tuple((await self.session.execute(candidate_statement(starting_cursor))).all())
        if not candidates and starting_cursor is not None:
            # The cursor reached the end (or its old page disappeared). Wrap in
            # one extra bounded query; never offset-scan the dead set.
            candidates = tuple((await self.session.execute(candidate_statement(None))).all())
        if not candidates:
            _advance_dead_terminal_reconciliation_cursor(
                cursor_bind,
                cursor_scope,
                expected=starting_cursor,
                updated=None,
            )
            return None

        for candidate in candidates:
            candidate_cursor = _DeadTerminalReconciliationCursor(
                dead_at=candidate.reconciliation_dead_at,
                job_id=candidate.id,
            )
            savepoint = await self.session.begin_nested()
            try:
                if not await self._lock_claim_authority(
                    project_id=candidate.project_id,
                    owner_user_id=candidate.owner_user_id,
                    job_type=candidate.job_type,
                    owner_private_generation=(candidate.owner_private_generation),
                    retention_resource_kind=(candidate.retention_resource_kind),
                    retention_effective_at=(candidate.retention_effective_at),
                    retention_membership_id=(candidate.retention_membership_id),
                ):
                    await savepoint.rollback()
                    continue
                predecessor = await self.session.scalar(
                    sa.select(JobRow)
                    .where(
                        JobRow.id == candidate.id,
                        JobRow.project_id == candidate.project_id,
                        JobRow.owner_user_id == candidate.owner_user_id,
                        JobRow.job_type == candidate.job_type,
                        JobRow.status == "dead",
                        execution_domain_claimable,
                    )
                    .with_for_update(of=JobRow, skip_locked=True)
                )
                if predecessor is None:
                    await savepoint.rollback()
                    continue
                dead = await self.session.scalar(
                    sa.select(DeadJobRow).where(
                        DeadJobRow.job_id == predecessor.id,
                        DeadJobRow.project_id == predecessor.project_id,
                        DeadJobRow.job_type == predecessor.job_type,
                        DeadJobRow.retry_safety == predecessor.retry_safety,
                        DeadJobRow.attempt_count == predecessor.attempt_count,
                        DeadJobRow.public_error_code == predecessor.public_error_code,
                    )
                )
                successor_exists = await self.session.scalar(
                    sa.select(
                        sa.exists().where(
                            JobRow.predecessor_dead_job_id == predecessor.id,
                        )
                    )
                )
                eligible_error = (predecessor.public_error_code == "SIDE_EFFECT_STATE_UNKNOWN" and predecessor.retry_safety != "safe") or (
                    predecessor.public_error_code == "ATTEMPTS_EXHAUSTED" and predecessor.attempt_count >= predecessor.max_attempts
                )
                if (
                    dead is None
                    or successor_exists is not False
                    or not eligible_error
                    or predecessor.owner_user_id is None
                    or predecessor.run_id is None
                    or predecessor.origin_trace_id is None
                    or type(predecessor.owner_private_generation) is not int
                    or predecessor.owner_private_generation < 1
                ):
                    await savepoint.rollback()
                    continue
                public_error_code = predecessor.public_error_code
                if public_error_code not in {
                    "SIDE_EFFECT_STATE_UNKNOWN",
                    "ATTEMPTS_EXHAUSTED",
                }:
                    await savepoint.rollback()
                    continue
                proof = await prove(
                    self.session,
                    DurableDeadTerminalReconciliationRequest(
                        predecessor_job_id=predecessor.id,
                        project_id=predecessor.project_id,
                        owner_user_id=predecessor.owner_user_id,
                        run_id=predecessor.run_id,
                        occurrence_id=predecessor.automation_occurrence_id,
                        job_type=predecessor.job_type,
                        retry_safety=predecessor.retry_safety,
                        attempt_count=predecessor.attempt_count,
                        max_attempts=predecessor.max_attempts,
                        public_error_code=public_error_code,
                        origin_trace_id=predecessor.origin_trace_id,
                        occurred_at=now,
                    ),
                )
                if type(proof) is not bool:
                    raise TypeError("durable dead terminal proof returned an invalid result")
                if not proof:
                    await savepoint.rollback()
                    continue
                successor_id, created = await self._enqueue(
                    EnqueueJob(
                        job_type=predecessor.job_type,
                        scope=JobScope(
                            predecessor.project_id,
                            predecessor.owner_user_id,
                        ),
                        idempotency_key=(
                            _durable_terminal_successor_idempotency_key(
                                predecessor.id,
                            )
                        ),
                        run_id=predecessor.run_id,
                        occurrence_id=(predecessor.automation_occurrence_id),
                        max_attempts=predecessor.max_attempts,
                        owner_private_generation=AccountPrivateGeneration(
                            owner_user_id=predecessor.owner_user_id,
                            generation=(predecessor.owner_private_generation),
                        ),
                        retry_safety="safe",
                        priority=predecessor.priority,
                        available_at=now,
                        predecessor_dead_job_id=predecessor.id,
                        origin_trace_id=predecessor.origin_trace_id,
                        execution_domain_affinity=(predecessor.execution_domain_affinity),
                    )
                )
                if not created:
                    raise RuntimeError("durable terminal successor identity was already used")
                rebound = await rebind(
                    self.session,
                    DurableTerminalSuccessorRebindRequest(
                        predecessor_job_id=predecessor.id,
                        successor_job_id=successor_id,
                        project_id=predecessor.project_id,
                        owner_user_id=predecessor.owner_user_id,
                        run_id=predecessor.run_id,
                        occurrence_id=(predecessor.automation_occurrence_id),
                        job_type=predecessor.job_type,
                        origin_trace_id=predecessor.origin_trace_id,
                        occurred_at=now,
                    ),
                )
                if type(rebound) is not bool:
                    raise TypeError("durable terminal successor rebind returned an invalid result")
                if not rebound:
                    raise RuntimeError("durable terminal successor rebind lost authority")
                await self.session.flush()
                await savepoint.commit()
                _advance_dead_terminal_reconciliation_cursor(
                    cursor_bind,
                    cursor_scope,
                    expected=starting_cursor,
                    updated=candidate_cursor,
                )
                return successor_id
            except BaseException:
                if savepoint.is_active:
                    await savepoint.rollback()
                raise
        last_candidate = candidates[-1]
        _advance_dead_terminal_reconciliation_cursor(
            cursor_bind,
            cursor_scope,
            expected=starting_cursor,
            updated=_DeadTerminalReconciliationCursor(
                dead_at=last_candidate.reconciliation_dead_at,
                job_id=last_candidate.id,
            ),
        )
        return None

    async def claim_next(
        self,
        *,
        worker_id: uuid.UUID,
        capabilities: frozenset[str],
        lease_seconds: int,
        execution_domain_affinity: str | None = None,
        now: datetime | None = None,
    ) -> JobClaim | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if execution_domain_affinity is not None and _SHA256_HEX.fullmatch(execution_domain_affinity) is None:
            raise ValueError(
                "execution domain affinity must be a lowercase SHA-256 digest",
            )
        job_types = sorted(
            capabilities
            & {
                "private_run",
                "automation_run",
                "retention_purge",
                "mcp_discovery",
                "memory_dream",
                "memory_dream_prepare",
                "memory_seal",
            }
        )
        if not job_types:
            return None
        execution_domain_claimable = JobRow.execution_domain_affinity.is_(None)
        if execution_domain_affinity is not None:
            execution_domain_claimable = sa.or_(
                execution_domain_claimable,
                JobRow.execution_domain_affinity == execution_domain_affinity,
            )
        explicit_claimed_at = self._now(now) if now is not None else None
        preferred_candidate_id: uuid.UUID | None = None
        dead_reconciliation_attempted = False

        def claimable_at(value):
            return sa.or_(
                sa.and_(
                    JobRow.status.in_(("queued", "retry_wait")),
                    JobRow.available_at <= value,
                ),
                sa.and_(
                    JobRow.status.in_(("leased", "running")),
                    JobRow.lease_expires_at <= value,
                ),
            )

        def row_is_claimable(row: JobRow, value: datetime) -> bool:
            if row.status in {"queued", "retry_wait"}:
                return row.available_at <= value
            return row.status in {"leased", "running"} and row.lease_expires_at is not None and row.lease_expires_at <= value

        skipped_ids: set[uuid.UUID] = set()
        for _ in range(100):
            # Evaluate candidate eligibility against PostgreSQL time. Worker
            # clock skew must not make a live lease or future Job appear due.
            candidate_at = (
                explicit_claimed_at
                if explicit_claimed_at is not None
                else await self.session.scalar(
                    sa.select(sa.func.clock_timestamp()),
                )
            )
            if not isinstance(candidate_at, datetime) or candidate_at.tzinfo is None:
                raise RuntimeError("database claim clock is unavailable")
            candidate_claimable = claimable_at(candidate_at)
            candidate_statement = (
                sa.select(
                    JobRow.id,
                    JobRow.project_id,
                    JobRow.owner_user_id,
                    JobRow.job_type,
                    JobRow.owner_private_generation,
                    JobRow.retention_resource_kind,
                    JobRow.retention_effective_at,
                    JobRow.retention_membership_id,
                )
                .where(
                    JobRow.job_type.in_(job_types),
                    candidate_claimable,
                    execution_domain_claimable,
                )
                .order_by(
                    JobRow.priority.desc(),
                    JobRow.available_at,
                    JobRow.created_at,
                    JobRow.id,
                )
                .limit(1)
            )
            if preferred_candidate_id is not None:
                candidate_statement = candidate_statement.where(
                    JobRow.id == preferred_candidate_id,
                )
            if skipped_ids:
                candidate_statement = candidate_statement.where(JobRow.id.not_in(skipped_ids))
            candidate = (await self.session.execute(candidate_statement)).one_or_none()
            if candidate is None:
                if preferred_candidate_id is not None:
                    preferred_candidate_id = None
                    continue
                if not dead_reconciliation_attempted:
                    dead_reconciliation_attempted = True
                    reconciliation_at = (
                        explicit_claimed_at
                        if explicit_claimed_at is not None
                        else await self.session.scalar(
                            sa.select(sa.func.clock_timestamp()),
                        )
                    )
                    if not isinstance(reconciliation_at, datetime) or reconciliation_at.tzinfo is None:
                        raise RuntimeError("database claim clock is unavailable")
                    preferred_candidate_id = await self._prepare_dead_terminal_successor(
                        job_types=job_types,
                        execution_domain_claimable=(execution_domain_claimable),
                        execution_domain_affinity=execution_domain_affinity,
                        now=reconciliation_at,
                    )
                    if preferred_candidate_id is not None:
                        continue
                return None

            savepoint = await self.session.begin_nested()
            try:
                if not await self._lock_claim_authority(
                    project_id=candidate.project_id,
                    owner_user_id=candidate.owner_user_id,
                    job_type=candidate.job_type,
                    owner_private_generation=(candidate.owner_private_generation),
                    retention_resource_kind=candidate.retention_resource_kind,
                    retention_effective_at=candidate.retention_effective_at,
                    retention_membership_id=candidate.retention_membership_id,
                ):
                    await savepoint.rollback()
                    skipped_ids.add(candidate.id)
                    continue
                if candidate.job_type == "memory_dream_prepare":
                    await self._lock_memory_prepare_before_job(
                        job_id=candidate.id,
                        project_id=candidate.project_id,
                        owner_user_id=candidate.owner_user_id,
                    )
                row = (
                    await self.session.execute(
                        sa.select(JobRow)
                        .where(
                            JobRow.id == candidate.id,
                            JobRow.project_id == candidate.project_id,
                            JobRow.owner_user_id == candidate.owner_user_id,
                            JobRow.job_type == candidate.job_type,
                            JobRow.owner_private_generation == candidate.owner_private_generation,
                            JobRow.retention_resource_kind == candidate.retention_resource_kind,
                            JobRow.retention_effective_at == candidate.retention_effective_at,
                            JobRow.retention_membership_id == candidate.retention_membership_id,
                            JobRow.job_type.in_(job_types),
                            execution_domain_claimable,
                        )
                        .with_for_update(of=JobRow, skip_locked=True)
                    )
                ).scalar_one_or_none()
                if row is None:
                    await savepoint.rollback()
                    skipped_ids.add(candidate.id)
                    continue
                if row.status in {"leased", "running"}:
                    active_attempt_id = await self.session.scalar(
                        sa.select(JobAttemptRow.id)
                        .where(
                            JobAttemptRow.job_id == row.id,
                            JobAttemptRow.attempt_number == row.attempt_count,
                            JobAttemptRow.lease_token_hash == row.lease_token_hash,
                            JobAttemptRow.outcome.is_(None),
                        )
                        .with_for_update(of=JobAttemptRow),
                    )
                    if active_attempt_id is None:
                        raise RuntimeError(
                            "active job attempt authority is missing",
                        )
                claimed_at = (
                    explicit_claimed_at
                    if explicit_claimed_at is not None
                    else await self.session.scalar(
                        sa.select(sa.func.clock_timestamp()),
                    )
                )
                if not isinstance(claimed_at, datetime) or claimed_at.tzinfo is None:
                    raise RuntimeError("database claim clock is unavailable")
                if not row_is_claimable(row, claimed_at):
                    await savepoint.rollback()
                    skipped_ids.add(candidate.id)
                    continue
                await savepoint.commit()
            except BaseException:
                if savepoint.is_active:
                    await savepoint.rollback()
                raise

            if row.status in {"queued", "retry_wait"} and row.cancel_requested_at is not None:
                await self._settle_unowned_cancel(row, now=claimed_at)
                await self.session.flush()
                return None

            if row.job_type in {"private_run", "automation_run"} and normalize_trace_id(row.origin_trace_id) is None:
                raise RuntimeError("Run job trace authority is invalid")

            if row.status in {"leased", "running"}:
                terminal_takeover = False
                takeover = getattr(
                    self._terminal_port,
                    "durable_terminal_takeover_allowed",
                    None,
                )
                if callable(takeover) and row.job_type in {"private_run", "automation_run"} and row.owner_user_id is not None and row.run_id is not None and row.origin_trace_id is not None:
                    terminal_takeover = await takeover(
                        self.session,
                        DurableTerminalTakeoverRequest(
                            job_id=row.id,
                            attempt_id=active_attempt_id,
                            project_id=row.project_id,
                            owner_user_id=row.owner_user_id,
                            run_id=row.run_id,
                            job_type=row.job_type,
                            retry_safety=row.retry_safety,
                            attempt_count=row.attempt_count,
                            max_attempts=row.max_attempts,
                            origin_trace_id=row.origin_trace_id,
                            occurred_at=claimed_at,
                        ),
                    )
                    if type(terminal_takeover) is not bool:
                        raise TypeError(
                            "durable terminal takeover port returned an invalid result",
                        )
                if not terminal_takeover and row.retry_safety != "safe":
                    public_error_code = "SIDE_EFFECT_STATE_UNKNOWN"
                    owner_ref = self._owner_ref(row.owner_user_id)
                    await self._finish_current_attempt(
                        row,
                        outcome="dead",
                        now=claimed_at,
                        public_error_code=public_error_code,
                    )
                    await self._mark_dead(
                        row,
                        owner_ref=owner_ref,
                        public_error_code=public_error_code,
                        now=claimed_at,
                    )
                    await self.session.flush()
                    return None
                if not terminal_takeover and row.cancel_requested_at is not None:
                    await self._settle_unowned_cancel(row, now=claimed_at)
                    await self.session.flush()
                    return None
                if not terminal_takeover and row.attempt_count >= row.max_attempts:
                    owner_ref = self._owner_ref(row.owner_user_id)
                    await self._finish_current_attempt(
                        row,
                        outcome="dead",
                        now=claimed_at,
                        public_error_code="ATTEMPTS_EXHAUSTED",
                    )
                    await self._mark_dead(
                        row,
                        owner_ref=owner_ref,
                        public_error_code="ATTEMPTS_EXHAUSTED",
                        now=claimed_at,
                    )
                    await self.session.flush()
                    return None
                await self._finish_current_attempt(
                    row,
                    outcome="lease_lost",
                    now=claimed_at,
                    public_error_code="LEASE_EXPIRED",
                )

            lease_token = secrets.token_urlsafe(32)
            token_hash = _lease_token_hash(lease_token)
            attempt_id = uuid.uuid4()
            attempt_number = row.attempt_count + 1
            row.status = "leased"
            row.attempt_count = attempt_number
            row.lease_owner_id = worker_id
            row.lease_token_hash = token_hash
            row.lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
            row.heartbeat_at = claimed_at
            row.started_at = row.started_at or claimed_at
            row.updated_at = claimed_at
            self.session.add(
                JobAttemptRow(
                    id=attempt_id,
                    job_id=row.id,
                    attempt_number=attempt_number,
                    worker_id=worker_id,
                    lease_token_hash=token_hash,
                    started_at=claimed_at,
                    heartbeat_at=claimed_at,
                )
            )
            await self.session.flush()
            return JobClaim(
                job_id=row.id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                job_type=row.job_type,
                scope=JobScope(row.project_id, row.owner_user_id),
                run_id=row.run_id,
                occurrence_id=row.automation_occurrence_id,
                retry_safety=row.retry_safety,
                cancel_requested=False,
                namespace=row.namespace,
                origin_trace_id=row.origin_trace_id,
                execution_domain_affinity=row.execution_domain_affinity,
                predecessor_dead_job_id=row.predecessor_dead_job_id,
                settlement_only=_is_durable_terminal_successor(row),
            )
        return None

    async def mark_running(
        self,
        job_id: uuid.UUID,
        *,
        lease_token: str,
        now: datetime | None = None,
    ) -> bool:
        changed_at = self._now(now)
        result = await self.session.execute(
            sa.update(JobRow)
            .where(
                JobRow.id == job_id,
                JobRow.status == "leased",
                JobRow.lease_token_hash == _lease_token_hash(lease_token),
                JobRow.lease_expires_at > changed_at,
            )
            .values(status="running", updated_at=changed_at)
        )
        return result.rowcount == 1

    async def release_unstarted_claim(
        self,
        job_id: uuid.UUID,
        *,
        lease_token: str,
        attempt_id: uuid.UUID,
        expected_worker_id: uuid.UUID,
        now: datetime | None = None,
    ) -> JobUnstartedClaimRelease | Literal[False]:
        """Release one exact committed claim before its handler fence opens."""

        if not isinstance(job_id, uuid.UUID):
            raise TypeError("job_id must be a UUID")
        if not isinstance(attempt_id, uuid.UUID):
            raise TypeError("attempt_id must be a UUID")
        if not isinstance(expected_worker_id, uuid.UUID):
            raise TypeError("expected_worker_id must be a UUID")
        if type(lease_token) is not str or not lease_token:
            raise ValueError("lease_token must be non-empty")
        coordinates = (
            await self.session.execute(
                sa.select(
                    JobRow.project_id,
                    JobRow.owner_user_id,
                ).where(JobRow.id == job_id)
            )
        ).one_or_none()
        if coordinates is None or not await self._lock_authority(
            coordinates.project_id,
            coordinates.owner_user_id,
        ):
            return False
        token_hash = _lease_token_hash(lease_token)
        row = (
            await self.session.execute(
                sa.select(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.project_id == coordinates.project_id,
                    JobRow.owner_user_id == coordinates.owner_user_id,
                    JobRow.status.in_(("leased", "running")),
                    JobRow.lease_owner_id == expected_worker_id,
                    JobRow.lease_token_hash == token_hash,
                )
                .with_for_update(of=JobRow)
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        if row.run_id is not None:
            # Importing the Run model at module load would execute the
            # ``deerflow.persistence.run`` compatibility facade, which also
            # imports the agent-runtime-backed repository.  Keep the generic
            # Job persistence facade safe for lightweight Memory imports.
            from deerflow.persistence.run.model import RunRow

            run_id = await self.session.scalar(
                sa.select(RunRow.run_id)
                .where(
                    RunRow.project_id == row.project_id,
                    RunRow.owner_user_id == row.owner_user_id,
                    RunRow.run_id == row.run_id,
                    RunRow.job_id == row.id,
                )
                .with_for_update(of=RunRow)
            )
            if run_id is None:
                return False
        attempt = (
            await self.session.execute(
                sa.select(JobAttemptRow)
                .where(
                    JobAttemptRow.id == attempt_id,
                    JobAttemptRow.job_id == row.id,
                    JobAttemptRow.attempt_number == row.attempt_count,
                    JobAttemptRow.worker_id == expected_worker_id,
                    JobAttemptRow.lease_token_hash == token_hash,
                    JobAttemptRow.outcome.is_(None),
                )
                .with_for_update(of=JobAttemptRow)
            )
        ).scalar_one_or_none()
        if attempt is None or attempt.execution_started_at is not None:
            return False
        released_at = self._now(now) if now is not None else await self.session.scalar(sa.select(sa.func.clock_timestamp()))
        if not isinstance(released_at, datetime) or released_at.tzinfo is None:
            raise RuntimeError("database release clock is unavailable")
        if row.cancel_requested_at is not None:
            await self._settle_unowned_cancel(row, now=released_at)
            await self.session.flush()
            return JobUnstartedClaimRelease(disposition="cancelled")
        if row.attempt_count >= row.max_attempts:
            return False
        attempt.heartbeat_at = released_at
        attempt.finished_at = released_at
        attempt.outcome = "retry"
        row.status = "queued"
        row.available_at = released_at
        row.lease_owner_id = None
        row.lease_token_hash = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        row.public_error_code = None
        row.updated_at = released_at
        await self.session.flush()
        return JobUnstartedClaimRelease(disposition="requeued")

    async def heartbeat(
        self,
        job_id: uuid.UUID,
        *,
        lease_token: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> JobHeartbeat | Literal[False]:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        token_hash = _lease_token_hash(lease_token)
        row = (
            await self.session.execute(
                sa.select(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.status.in_(("leased", "running")),
                    JobRow.lease_token_hash == token_hash,
                )
                .with_for_update(of=JobRow)
            )
        ).scalar_one_or_none()
        if row is None:
            return False

        # The active attempt is part of the lease authority. Lock it before
        # sampling production time so a wait on either row cannot carry a
        # pre-lock timestamp past the old deadline and revive an expired lease.
        attempt = (
            await self.session.execute(
                sa.select(JobAttemptRow)
                .where(
                    JobAttemptRow.job_id == job_id,
                    JobAttemptRow.attempt_number == row.attempt_count,
                    JobAttemptRow.lease_token_hash == token_hash,
                    JobAttemptRow.outcome.is_(None),
                )
                .with_for_update(of=JobAttemptRow)
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise RuntimeError("active job attempt authority is missing")

        heartbeat_at = self._now(now) if now is not None else await self.session.scalar(sa.select(sa.func.clock_timestamp()))
        if not isinstance(heartbeat_at, datetime) or heartbeat_at.tzinfo is None:
            raise RuntimeError("database heartbeat clock is unavailable")
        if row.lease_expires_at is None or row.lease_expires_at <= heartbeat_at:
            return False

        row.heartbeat_at = heartbeat_at
        row.lease_expires_at = heartbeat_at + timedelta(seconds=lease_seconds)
        row.updated_at = heartbeat_at
        attempt.heartbeat_at = heartbeat_at
        await self.session.flush()
        return JobHeartbeat(cancel_requested=row.cancel_requested_at is not None)

    async def request_cancel(
        self,
        scope: JobScope,
        job_id: uuid.UUID,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        requested_at = self._now(now)
        if not reason or len(reason) > 64:
            raise ValueError("cancel reason must be between 1 and 64 characters")
        result = await self.session.execute(
            sa.update(JobRow)
            .where(
                JobRow.id == job_id,
                JobRow.status.in_(("queued", "leased", "running", "retry_wait")),
                *self._scope_predicates(scope),
            )
            .values(
                cancel_requested_at=sa.func.coalesce(JobRow.cancel_requested_at, requested_at),
                cancel_reason=sa.func.coalesce(JobRow.cancel_reason, reason),
                updated_at=requested_at,
            )
        )
        return result.rowcount == 1

    async def settle_requested_cancel(
        self,
        scope: JobScope,
        job_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Settle a requested cancellation only while no Worker owns it."""

        settled_at = self._now(now)
        if not await self._lock_job_authority(job_id):
            return False
        row = (
            await self.session.execute(
                sa.select(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.status.in_(("queued", "retry_wait")),
                    JobRow.cancel_requested_at.is_not(None),
                    *self._scope_predicates(scope),
                )
                .with_for_update(of=JobRow)
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        await self._settle_unowned_cancel(row, now=settled_at)
        await self.session.flush()
        return True

    async def _settle_owned(
        self,
        job_id: uuid.UUID,
        *,
        lease_token: str,
        status: Literal["succeeded", "cancelled"],
        attempt_outcome: Literal["succeeded", "cancelled"],
        now: datetime | None,
    ) -> bool:
        settled_at = self._now(now)
        if not await self._lock_job_authority(job_id):
            return False
        token_hash = _lease_token_hash(lease_token)
        result = await self.session.execute(
            sa.update(JobRow)
            .where(
                JobRow.id == job_id,
                JobRow.status.in_(("leased", "running")),
                JobRow.lease_token_hash == token_hash,
                JobRow.lease_expires_at > settled_at,
            )
            .values(
                status=status,
                public_error_code=None,
                lease_owner_id=None,
                lease_token_hash=None,
                lease_expires_at=None,
                heartbeat_at=None,
                completed_at=settled_at,
                updated_at=settled_at,
            )
        )
        if result.rowcount != 1:
            return False
        attempt_result = await self.session.execute(
            sa.update(JobAttemptRow)
            .where(
                JobAttemptRow.job_id == job_id,
                JobAttemptRow.lease_token_hash == token_hash,
                JobAttemptRow.outcome.is_(None),
            )
            .values(
                heartbeat_at=settled_at,
                finished_at=settled_at,
                outcome=attempt_outcome,
            )
        )
        if attempt_result.rowcount != 1:
            raise RuntimeError("active job attempt authority is missing")
        return True

    async def settle_success(
        self,
        job_id: uuid.UUID,
        *,
        lease_token: str,
        now: datetime | None = None,
    ) -> bool:
        return await self._settle_owned(
            job_id,
            lease_token=lease_token,
            status="succeeded",
            attempt_outcome="succeeded",
            now=now,
        )

    async def settle_cancelled(
        self,
        job_id: uuid.UUID,
        *,
        lease_token: str,
        now: datetime | None = None,
    ) -> bool:
        return await self._settle_owned(
            job_id,
            lease_token=lease_token,
            status="cancelled",
            attempt_outcome="cancelled",
            now=now,
        )

    async def retry_or_dead(
        self,
        job_id: uuid.UUID,
        *,
        lease_token: str,
        public_error_code: str,
        retryable: bool = True,
        retry_initial_seconds: int,
        retry_max_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        result = await self.retry_or_dead_result(
            job_id,
            lease_token=lease_token,
            public_error_code=public_error_code,
            retryable=retryable,
            retry_initial_seconds=retry_initial_seconds,
            retry_max_seconds=retry_max_seconds,
            now=now,
        )
        return result.changed

    async def retry_or_dead_result(
        self,
        job_id: uuid.UUID,
        *,
        lease_token: str,
        public_error_code: str,
        retryable: bool = True,
        retry_initial_seconds: int,
        retry_max_seconds: int,
        now: datetime | None = None,
    ) -> JobRetryResult:
        failed_at = self._now(now)
        if not public_error_code or len(public_error_code) > 64:
            raise ValueError("public_error_code must be between 1 and 64 characters")
        if type(retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not await self._lock_job_authority(job_id):
            return JobRetryResult(
                changed=False,
                run_terminal_published=False,
            )
        token_hash = _lease_token_hash(lease_token)
        row = (
            await self.session.execute(
                sa.select(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.status.in_(("leased", "running")),
                    JobRow.lease_token_hash == token_hash,
                    JobRow.lease_expires_at > failed_at,
                )
                .with_for_update(of=JobRow)
            )
        ).scalar_one_or_none()
        if row is None:
            return JobRetryResult(
                changed=False,
                run_terminal_published=False,
            )
        if retryable and row.retry_safety == "safe" and row.attempt_count < row.max_attempts:
            delay = retry_backoff_seconds(
                attempt_count=row.attempt_count,
                initial_seconds=retry_initial_seconds,
                max_seconds=retry_max_seconds,
            )
            await self._finish_current_attempt(
                row,
                outcome="retry",
                now=failed_at,
                public_error_code=public_error_code,
            )
            row.status = "retry_wait"
            row.available_at = failed_at + timedelta(seconds=delay)
            row.public_error_code = public_error_code
            row.lease_owner_id = None
            row.lease_token_hash = None
            row.lease_expires_at = None
            row.heartbeat_at = None
            row.updated_at = failed_at
            return JobRetryResult(
                changed=True,
                run_terminal_published=False,
            )

        dead_error_code = _dead_error_code_for_failure(
            retry_safety=row.retry_safety,
            public_error_code=public_error_code,
            retryable=retryable,
        )
        owner_ref = self._owner_ref(row.owner_user_id)
        await self._finish_current_attempt(
            row,
            outcome="dead",
            now=failed_at,
            public_error_code=dead_error_code,
        )
        terminal = await self._mark_dead(
            row,
            owner_ref=owner_ref,
            public_error_code=dead_error_code,
            now=failed_at,
        )
        return JobRetryResult(
            changed=True,
            run_terminal_published=terminal.run_terminal_published,
        )

    async def list_dead(
        self,
        scope: JobScope,
        *,
        limit: int,
    ) -> tuple[DeadJobRecord, ...]:
        if type(scope) is not JobScope:
            raise TypeError("JobScope is required")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        predicates: list[sa.ColumnElement[bool]] = [DeadJobRow.project_id == scope.project_id]
        predicates.append(JobRow.owner_user_id.is_(None) if scope.owner_user_id is None else JobRow.owner_user_id == scope.owner_user_id)
        rows = (await self.session.execute(sa.select(DeadJobRow).join(JobRow, JobRow.id == DeadJobRow.job_id).where(*predicates).order_by(DeadJobRow.dead_at.desc(), DeadJobRow.job_id.desc()).limit(limit))).scalars()
        return tuple(
            DeadJobRecord(
                job_id=row.job_id,
                project_id=row.project_id,
                job_type=row.job_type,
                attempt_count=row.attempt_count,
                retry_safety=row.retry_safety,
                public_error_code=row.public_error_code,
                dead_at=row.dead_at,
            )
            for row in rows
        )

    async def requeue_safe(
        self,
        scope: JobScope,
        dead_job_id: uuid.UUID,
        *,
        idempotency_key: str,
        max_attempts: int,
        request_id: str,
        audit_port: JobAuditPort,
    ) -> uuid.UUID:
        if type(scope) is not JobScope:
            raise TypeError("JobScope is required")
        dead_job_id = self._validate_requeue_request(dead_job_id, request_id)
        predicates: list[sa.ColumnElement[bool]] = [
            DeadJobRow.job_id == dead_job_id,
            DeadJobRow.project_id == scope.project_id,
        ]
        predicates.append(JobRow.owner_user_id.is_(None) if scope.owner_user_id is None else JobRow.owner_user_id == scope.owner_user_id)
        return await self._requeue_safe_exact(
            predicates,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            request_id=request_id,
            audit_port=audit_port,
        )

    async def requeue_safe_system(
        self,
        project_id: uuid.UUID,
        dead_job_id: uuid.UUID,
        *,
        idempotency_key: str,
        max_attempts: int,
        request_id: str,
        audit_port: JobAuditPort,
    ) -> uuid.UUID:
        try:
            project_id = uuid.UUID(str(project_id))
        except (TypeError, ValueError):
            raise TypeError("system requeue requires a project UUID")
        dead_job_id = self._validate_requeue_request(dead_job_id, request_id)
        return await self._requeue_safe_exact(
            [
                DeadJobRow.job_id == dead_job_id,
                DeadJobRow.project_id == project_id,
            ],
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            request_id=request_id,
            audit_port=audit_port,
            platform_requeue=True,
        )

    @staticmethod
    def _validate_requeue_request(
        dead_job_id: uuid.UUID,
        request_id: str,
    ) -> uuid.UUID:
        try:
            dead_job_id = uuid.UUID(str(dead_job_id))
        except (TypeError, ValueError):
            raise TypeError("dead job ID must be a UUID")
        if type(request_id) is not str or not 1 <= len(request_id) <= 512:
            raise ValueError("request_id must be between 1 and 512 characters")
        return dead_job_id

    async def _requeue_safe_exact(
        self,
        predecessor_predicates: list[sa.ColumnElement[bool]],
        *,
        idempotency_key: str,
        max_attempts: int,
        request_id: str,
        audit_port: JobAuditPort,
        platform_requeue: bool = False,
    ) -> uuid.UUID:
        pair = (
            await self.session.execute(
                sa.select(DeadJobRow, JobRow)
                .join(
                    JobRow,
                    sa.and_(
                        JobRow.id == DeadJobRow.job_id,
                        JobRow.project_id == DeadJobRow.project_id,
                    ),
                )
                .where(*predecessor_predicates)
                .with_for_update(of=(DeadJobRow, JobRow))
            )
        ).one_or_none()
        if pair is None:
            raise JobRequeueForbidden("dead job is unavailable for safe requeue")
        dead, predecessor = pair
        if dead.retry_safety != "safe" or predecessor.status != "dead" or predecessor.retry_safety != "safe":
            raise JobRequeueForbidden("dead job is unavailable for safe requeue")
        if platform_requeue and (predecessor.job_type != "retention_purge" or predecessor.owner_user_id is not None or predecessor.run_id is not None or predecessor.automation_occurrence_id is not None):
            raise JobRequeueForbidden("dead job is unavailable for safe requeue")

        existing_successor = (await self.session.execute(sa.select(JobRow).where(JobRow.predecessor_dead_job_id == predecessor.id).with_for_update(of=JobRow))).scalar_one_or_none()
        if existing_successor is not None:
            same_owner = existing_successor.owner_user_id is None if predecessor.owner_user_id is None else existing_successor.owner_user_id == predecessor.owner_user_id
            if (
                existing_successor.project_id == predecessor.project_id
                and same_owner
                and existing_successor.job_type == predecessor.job_type
                and existing_successor.run_id == predecessor.run_id
                and existing_successor.automation_occurrence_id == predecessor.automation_occurrence_id
                and existing_successor.namespace == predecessor.namespace
                and existing_successor.origin_trace_id == predecessor.origin_trace_id
                and existing_successor.execution_domain_affinity == predecessor.execution_domain_affinity
                and existing_successor.owner_private_generation == predecessor.owner_private_generation
                and existing_successor.retention_resource_kind == predecessor.retention_resource_kind
                and existing_successor.retention_effective_at == predecessor.retention_effective_at
                and existing_successor.retention_membership_id == predecessor.retention_membership_id
                and existing_successor.status == "queued"
                and existing_successor.attempt_count == 0
                and existing_successor.retry_safety == "safe"
            ):
                return existing_successor.id
            raise JobRequeueForbidden("safe requeue successor authority is invalid")

        if type(predecessor.owner_private_generation) is not int or predecessor.owner_private_generation < 1 or (predecessor.job_type != "retention_purge" and predecessor.owner_user_id is None):
            raise JobRequeueForbidden(
                "dead job account-private generation is invalid",
            )

        request = EnqueueJob(
            job_type=predecessor.job_type,
            scope=JobScope(predecessor.project_id, predecessor.owner_user_id),
            idempotency_key=idempotency_key,
            run_id=predecessor.run_id,
            occurrence_id=predecessor.automation_occurrence_id,
            max_attempts=max_attempts,
            owner_private_generation=(
                RetentionPurgeJobAuthority(
                    resource_kind=predecessor.retention_resource_kind,
                    project_id=predecessor.project_id,
                    owner_user_id=predecessor.owner_user_id,
                    generation=predecessor.owner_private_generation,
                    effective_at=predecessor.retention_effective_at,
                    membership_id=predecessor.retention_membership_id,
                )
                if predecessor.job_type == "retention_purge"
                else AccountPrivateGeneration(
                    owner_user_id=predecessor.owner_user_id,
                    generation=predecessor.owner_private_generation,
                )
            ),
            namespace=predecessor.namespace,
            retry_safety="safe",
            priority=predecessor.priority,
            predecessor_dead_job_id=predecessor.id,
            origin_trace_id=predecessor.origin_trace_id,
            execution_domain_affinity=predecessor.execution_domain_affinity,
        )
        successor_id, created = await self._enqueue(request)
        if created:
            successor_owner = JobRow.owner_user_id.is_(None) if predecessor.owner_user_id is None else JobRow.owner_user_id == predecessor.owner_user_id
            successor = (
                await self.session.execute(
                    sa.select(JobRow)
                    .where(
                        JobRow.id == successor_id,
                        JobRow.project_id == predecessor.project_id,
                        successor_owner,
                        JobRow.job_type == predecessor.job_type,
                        JobRow.run_id == predecessor.run_id,
                        JobRow.automation_occurrence_id == predecessor.automation_occurrence_id,
                        JobRow.predecessor_dead_job_id == predecessor.id,
                        JobRow.owner_private_generation == predecessor.owner_private_generation,
                        JobRow.retention_resource_kind == predecessor.retention_resource_kind,
                        JobRow.retention_effective_at == predecessor.retention_effective_at,
                        JobRow.retention_membership_id == predecessor.retention_membership_id,
                        JobRow.execution_domain_affinity == predecessor.execution_domain_affinity,
                        JobRow.status == "queued",
                        JobRow.attempt_count == 0,
                        JobRow.retry_safety == "safe",
                    )
                    .with_for_update(of=JobRow)
                )
            ).scalar_one_or_none()
            if successor is None:
                raise JobRequeueForbidden(
                    "safe requeue successor authority is invalid",
                )
            event = object.__new__(DeadJobRequeuedEvent)
            event_project_id = uuid.UUID(str(predecessor.project_id))
            predecessor_id = uuid.UUID(str(predecessor.id))
            event_successor_id = uuid.UUID(str(successor.id))
            object.__setattr__(event, "project_id", event_project_id)
            object.__setattr__(event, "predecessor_job_id", predecessor_id)
            object.__setattr__(event, "successor_job_id", event_successor_id)
            object.__setattr__(event, "request_id", request_id)
            object.__setattr__(event, "job_type", successor.job_type)
            object.__setattr__(event, "attempt_count", successor.attempt_count)
            object.__setattr__(event, "retry_safety", successor.retry_safety)
            snapshot = (
                event_project_id,
                predecessor_id,
                event_successor_id,
                request_id,
                successor.job_type,
                successor.attempt_count,
                successor.retry_safety,
            )
            identity = id(event)

            def discard(
                reference: weakref.ReferenceType[DeadJobRequeuedEvent],
            ) -> None:
                with _ISSUED_REQUEUE_EVENTS_LOCK:
                    current = _ISSUED_REQUEUE_EVENTS.get(identity)
                    if current is not None and current[0] is reference:
                        del _ISSUED_REQUEUE_EVENTS[identity]

            reference = weakref.ref(event, discard)
            with _ISSUED_REQUEUE_EVENTS_LOCK:
                _ISSUED_REQUEUE_EVENTS[identity] = (reference, snapshot)
            try:
                await audit_port.dead_job_requeued(
                    self.session,
                    event,
                )
            finally:
                consume_issued_dead_job_requeued_event(event)
        return successor_id


__all__ = [
    "DurableDeadTerminalReconciliationRequest",
    "DurableTerminalSuccessorRebindRequest",
    "DurableTerminalTakeoverRequest",
    "EnqueueJob",
    "DeadJobRecord",
    "DeadJobRequeuedEvent",
    "JobAuditPort",
    "JobClaim",
    "JobHeartbeat",
    "JobIdempotencyConflict",
    "JobOwnerRef",
    "JobOwnerRefRequired",
    "JobRepository",
    "JobRetryResult",
    "JobRequeueForbidden",
    "JobScope",
    "JobTerminalResult",
    "JobType",
    "JobUnstartedClaimRelease",
    "RetrySafety",
    "RetentionPurgeJobAuthority",
    "consume_issued_dead_job_requeued_event",
    "retry_backoff_seconds",
]
