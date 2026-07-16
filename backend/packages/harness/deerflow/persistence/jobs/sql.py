"""Session-bound durable job repository."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.jobs.model import DeadJobRow, JobAttemptRow, JobRow

JobType = Literal["private_run", "automation_run", "retention_purge"]
RetrySafety = Literal["safe", "unknown", "unsafe"]

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


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
class EnqueueJob:
    job_type: JobType
    scope: JobScope
    idempotency_key: str
    run_id: str | None
    occurrence_id: str | None
    max_attempts: int
    retry_safety: RetrySafety = "safe"
    priority: int = 0
    available_at: datetime | None = None
    predecessor_dead_job_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if type(self.scope) is not JobScope:
            raise TypeError("JobScope is required")
        if self.job_type not in {"private_run", "automation_run", "retention_purge"}:
            raise ValueError("unsupported job type")
        if _SHA256_HEX.fullmatch(self.idempotency_key) is None:
            raise ValueError("idempotency_key must be a lowercase SHA-256 digest")
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        if self.retry_safety not in {"safe", "unknown", "unsafe"}:
            raise ValueError("unsupported retry safety")
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
        elif self.scope.owner_user_id is not None or self.run_id is not None or self.occurrence_id is not None:
            raise ValueError("retention_purge requires project-only authority")


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


@dataclass(frozen=True, slots=True)
class JobHeartbeat:
    cancel_requested: bool


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


@dataclass(frozen=True, slots=True)
class DeadJobRequeuedEvent:
    project_id: uuid.UUID
    predecessor_job_id: uuid.UUID
    successor_job_id: uuid.UUID
    request_id: str


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


class JobAuditPort(Protocol):
    async def dead_job_requeued(
        self,
        session: AsyncSession,
        event: DeadJobRequeuedEvent,
    ) -> None: ...


class JobTerminalPort(Protocol):
    async def job_terminalized(
        self,
        session: AsyncSession,
        event: JobTerminalEvent,
    ) -> None: ...


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
        return (
            row.project_id == request.scope.project_id
            and row.owner_user_id == request.scope.owner_user_id
            and row.run_id == request.run_id
            and row.automation_occurrence_id == request.occurrence_id
            and row.predecessor_dead_job_id == request.predecessor_dead_job_id
            and row.max_attempts == request.max_attempts
            and row.retry_safety == request.retry_safety
            and row.priority == request.priority
        )

    async def _enqueue(self, request: EnqueueJob) -> tuple[uuid.UUID, bool]:
        if type(request) is not EnqueueJob:
            raise TypeError("EnqueueJob is required")
        now = datetime.now(UTC)
        job_id = uuid.uuid4()
        inserted_id = await self.session.scalar(
            pg_insert(JobRow)
            .values(
                id=job_id,
                job_type=request.job_type,
                project_id=request.scope.project_id,
                owner_user_id=request.scope.owner_user_id,
                run_id=request.run_id,
                automation_occurrence_id=request.occurrence_id,
                predecessor_dead_job_id=request.predecessor_dead_job_id,
                idempotency_key=request.idempotency_key,
                status="queued",
                priority=request.priority,
                available_at=request.available_at or now,
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
    ) -> None:
        if self._terminal_port is None:
            return
        await self._terminal_port.job_terminalized(
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
            ),
        )

    async def _mark_dead(
        self,
        row: JobRow,
        *,
        owner_ref: JobOwnerRef | None,
        public_error_code: str,
        now: datetime,
    ) -> None:
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
        await self._publish_terminal(
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
        row.completed_at = now
        row.updated_at = now
        await self._publish_terminal(
            row,
            status="cancelled",
            public_error_code=None,
            now=now,
        )

    async def claim_next(
        self,
        *,
        worker_id: uuid.UUID,
        capabilities: frozenset[str],
        lease_seconds: int,
        now: datetime | None = None,
    ) -> JobClaim | None:
        claimed_at = self._now(now)
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        job_types = sorted(capabilities & {"private_run", "automation_run", "retention_purge"})
        if not job_types:
            return None
        claimable = sa.or_(
            sa.and_(
                JobRow.status.in_(("queued", "retry_wait")),
                JobRow.available_at <= claimed_at,
            ),
            sa.and_(
                JobRow.status.in_(("leased", "running")),
                JobRow.lease_expires_at <= claimed_at,
            ),
        )
        for _ in range(100):
            row = (
                await self.session.execute(
                    sa.select(JobRow)
                    .where(
                        JobRow.job_type.in_(job_types),
                        claimable,
                    )
                    .order_by(
                        JobRow.priority.desc(),
                        JobRow.available_at,
                        JobRow.created_at,
                        JobRow.id,
                    )
                    .with_for_update(of=JobRow, skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None

            if row.status in {"queued", "retry_wait"} and row.cancel_requested_at is not None:
                await self._settle_unowned_cancel(row, now=claimed_at)
                await self.session.flush()
                continue

            if row.status in {"leased", "running"}:
                if row.retry_safety != "safe":
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
                    continue
                if row.cancel_requested_at is not None:
                    await self._settle_unowned_cancel(row, now=claimed_at)
                    await self.session.flush()
                    continue
                if row.attempt_count >= row.max_attempts:
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
                    continue
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

    async def heartbeat(
        self,
        job_id: uuid.UUID,
        *,
        lease_token: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> JobHeartbeat | Literal[False]:
        heartbeat_at = self._now(now)
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        token_hash = _lease_token_hash(lease_token)
        updated = (
            await self.session.execute(
                sa.update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.status.in_(("leased", "running")),
                    JobRow.lease_token_hash == token_hash,
                    JobRow.lease_expires_at > heartbeat_at,
                )
                .values(
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=heartbeat_at + timedelta(seconds=lease_seconds),
                    updated_at=heartbeat_at,
                )
                .returning(JobRow.id, JobRow.cancel_requested_at)
            )
        ).one_or_none()
        if updated is None:
            return False
        attempt_result = await self.session.execute(
            sa.update(JobAttemptRow)
            .where(
                JobAttemptRow.job_id == job_id,
                JobAttemptRow.lease_token_hash == token_hash,
                JobAttemptRow.outcome.is_(None),
            )
            .values(heartbeat_at=heartbeat_at)
        )
        if attempt_result.rowcount != 1:
            raise RuntimeError("active job attempt authority is missing")
        return JobHeartbeat(cancel_requested=updated.cancel_requested_at is not None)

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
        retry_initial_seconds: int,
        retry_max_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        failed_at = self._now(now)
        if not public_error_code or len(public_error_code) > 64:
            raise ValueError("public_error_code must be between 1 and 64 characters")
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
            return False
        if row.retry_safety == "safe" and row.attempt_count < row.max_attempts:
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
            return True

        dead_error_code = "SIDE_EFFECT_STATE_UNKNOWN" if row.retry_safety != "safe" else public_error_code
        owner_ref = self._owner_ref(row.owner_user_id)
        await self._finish_current_attempt(
            row,
            outcome="dead",
            now=failed_at,
            public_error_code=dead_error_code,
        )
        await self._mark_dead(
            row,
            owner_ref=owner_ref,
            public_error_code=dead_error_code,
            now=failed_at,
        )
        return True

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
        if not request_id or len(request_id) > 128:
            raise ValueError("request_id must be between 1 and 128 characters")
        predicates: list[sa.ColumnElement[bool]] = [
            DeadJobRow.job_id == dead_job_id,
            DeadJobRow.project_id == scope.project_id,
        ]
        predicates.append(JobRow.owner_user_id.is_(None) if scope.owner_user_id is None else JobRow.owner_user_id == scope.owner_user_id)
        result = await self.session.execute(sa.select(DeadJobRow, JobRow).join(JobRow, JobRow.id == DeadJobRow.job_id).where(*predicates).with_for_update(of=JobRow))
        pair = result.one_or_none()
        if pair is None or pair[0].retry_safety != "safe":
            raise JobRequeueForbidden("dead job is unavailable for safe requeue")
        _dead, predecessor = pair
        request = EnqueueJob(
            job_type=predecessor.job_type,
            scope=JobScope(predecessor.project_id, predecessor.owner_user_id),
            idempotency_key=idempotency_key,
            run_id=predecessor.run_id,
            occurrence_id=predecessor.automation_occurrence_id,
            max_attempts=max_attempts,
            retry_safety="safe",
            priority=predecessor.priority,
            predecessor_dead_job_id=predecessor.id,
        )
        successor_id, created = await self._enqueue(request)
        if created:
            await audit_port.dead_job_requeued(
                self.session,
                DeadJobRequeuedEvent(
                    project_id=predecessor.project_id,
                    predecessor_job_id=predecessor.id,
                    successor_job_id=successor_id,
                    request_id=request_id,
                ),
            )
        return successor_id


__all__ = [
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
    "JobRequeueForbidden",
    "JobScope",
    "JobType",
    "RetrySafety",
    "retry_backoff_seconds",
]
