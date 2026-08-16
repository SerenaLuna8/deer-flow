"""Shared transactional convergence for host-execution approvals."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import PrivateWorkContext
from app.private_work.execution_approval_audit import (
    HostExecutionApprovalAuditPort,
    NoopHostExecutionApprovalAudit,
)
from app.private_work.output_delivery_obligation import (
    OutputDeliveryObligationConflict,
    settle_continuation_output_delivery,
    transition_output_delivery_obligation_for_approval_terminal,
)
from app.private_work.run_metadata import (
    RunHostExecutionSuspensionInvalid,
    run_host_execution_suspension,
)
from deerflow.persistence.execution_approvals import (
    EXECUTION_APPROVAL_ACTIVE_STATUSES,
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow
from deerflow.persistence.run.model import RunRow
from deerflow.runtime.private_scope import PrivateResourceScope

_ACTIVE_STATUSES = tuple(sorted(EXECUTION_APPROVAL_ACTIVE_STATUSES))
CLAIMED_EXECUTION_SETTLEMENT_GRACE_SECONDS = 30


class ExecutionApprovalPrivateLifecycleConflict(RuntimeError):
    """Approval dependency coordinates changed or are internally incomplete."""


def staged_approval_has_exact_suspension_marker(
    approval: ExecutionApprovalRequestRow,
    source_run: RunRow,
) -> bool:
    """Return whether a staged approval owns this Run's durable success proof.

    Presence with any other coordinate is corruption, not absence.  Callers
    must fail their whole transaction rather than using a mismatched marker to
    cancel, expire, or otherwise terminalize a different approval.
    """

    try:
        marker = run_host_execution_suspension(source_run.metadata_json)
    except RunHostExecutionSuspensionInvalid:
        raise ExecutionApprovalPrivateLifecycleConflict() from None
    if marker is None:
        return False
    try:
        exact = (
            approval.status == "staged"
            and marker.approval_id == uuid.UUID(str(approval.id))
            and marker.source_job_id == uuid.UUID(str(approval.source_job_id))
            and marker.producing_attempt_id == uuid.UUID(str(approval.source_job_attempt_id))
            and uuid.UUID(str(source_run.project_id)) == uuid.UUID(str(approval.project_id))
            and source_run.owner_user_id == approval.owner_user_id
            and source_run.thread_id == approval.thread_id
            and source_run.run_id == approval.source_run_id
            and uuid.UUID(str(source_run.job_id)) == uuid.UUID(str(approval.source_job_id))
        )
    except (AttributeError, TypeError, ValueError):
        raise ExecutionApprovalPrivateLifecycleConflict() from None
    if not exact:
        raise ExecutionApprovalPrivateLifecycleConflict()
    return True


async def reject_sealed_staged_approval_terminalization(
    session: AsyncSession,
    approval: ExecutionApprovalRequestRow,
) -> None:
    """Fail a cleanup transaction that would erase a sealed source success."""

    if approval.status != "staged":
        return
    source_run = await session.scalar(
        sa.select(RunRow)
        .where(
            RunRow.project_id == approval.project_id,
            RunRow.owner_user_id == approval.owner_user_id,
            RunRow.thread_id == approval.thread_id,
            RunRow.run_id == approval.source_run_id,
            RunRow.job_id == approval.source_job_id,
        )
        .with_for_update(of=RunRow)
        .execution_options(populate_existing=True)
    )
    if source_run is None:
        raise ExecutionApprovalPrivateLifecycleConflict()
    if staged_approval_has_exact_suspension_marker(approval, source_run):
        raise ExecutionApprovalPrivateLifecycleConflict()


class ExecutionApprovalContinuationQuotaPort(Protocol):
    async def release_concurrent_run(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        request_id: str,
    ) -> None: ...


class ExecutionApprovalContinuationRunAuditPort(Protocol):
    async def run_cancel_requested(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        run_id: str,
        job_id: uuid.UUID,
    ) -> None: ...

    async def run_terminal(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        job_type: str,
        status: str,
        public_error_code: str | None,
        request_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ApprovalRunDependency:
    owner_user_id: str
    thread_id: str
    run_id: str
    job_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ApprovalJobDependency:
    """An exact Job coordinate not necessarily mirrored by ``Run.job_id``."""

    owner_user_id: str
    run_id: str
    job_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class LockedExecutionApprovalRows:
    rows: tuple[ExecutionApprovalRequestRow, ...]
    claimed_absolute_deadlines: dict[uuid.UUID, datetime]
    jobs: Mapping[uuid.UUID, JobRow]
    runs: Mapping[str, RunRow]
    active_attempts: Mapping[uuid.UUID, tuple[JobAttemptRow, ...]]


def cancel_locked_execution_approval_continuation(
    row: ExecutionApprovalRequestRow,
    locked: LockedExecutionApprovalRows,
    *,
    now: datetime,
    reason: str,
) -> Literal["requested", "cancelled", "terminal"]:
    """Cancel a continuation using the canonical prelocked Job/Run rows."""

    if not reason or len(reason) > 64:
        raise ValueError("cancel reason must be between 1 and 64 characters")
    if row.continuation_job_id is None or row.continuation_run_id is None:
        return "terminal"
    job = locked.jobs.get(row.continuation_job_id)
    run = locked.runs.get(row.continuation_run_id)
    if (
        job is None
        or run is None
        or job.project_id != row.project_id
        or job.owner_user_id != row.owner_user_id
        or job.run_id != row.continuation_run_id
        or job.job_type not in {"private_run", "automation_run"}
        or run.project_id != row.project_id
        or run.owner_user_id != row.owner_user_id
        or run.thread_id != row.thread_id
        or run.job_id != row.continuation_job_id
    ):
        raise ExecutionApprovalPrivateLifecycleConflict()
    if job.status in {"succeeded", "failed", "cancelled", "dead"} and run.status in {
        "success",
        "error",
        "timeout",
        "interrupted",
        "deleted",
    }:
        return "terminal"
    if job.status not in {"queued", "leased", "running", "retry_wait"} or run.status not in {
        "pending",
        "running",
    }:
        raise ExecutionApprovalPrivateLifecycleConflict()

    job.cancel_requested_at = job.cancel_requested_at or now
    job.cancel_reason = job.cancel_reason or reason
    job.updated_at = now
    run.cancel_requested_at = run.cancel_requested_at or now
    run.cancel_reason = run.cancel_reason or reason
    run.updated_at = now
    if job.status in {"leased", "running"}:
        return "requested"

    # A queued/retry-wait Job has no live Worker lease and can be settled
    # synchronously without acquiring any lock after the approval row.
    job.status = "cancelled"
    job.lease_owner_id = None
    job.lease_token_hash = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    job.completed_at = now
    run.status = "interrupted"
    run.error = run.cancel_reason
    run.execution_lease_token_hash = None
    run.execution_lease_expires_at = None
    run.execution_heartbeat_at = None
    return "cancelled"


def claimed_execution_absolute_deadline(
    row: ExecutionApprovalRequestRow,
) -> datetime:
    """Return the frozen, non-renewable command completion deadline."""

    claimed_at = row.claimed_at
    envelope = row.command_private_json
    plan = envelope.get("plan") if isinstance(envelope, dict) else None
    timeout_seconds = plan.get("timeout_seconds") if isinstance(plan, dict) else None
    if claimed_at is None or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        # A claimed row was validated before launch. If legacy corruption
        # removes that proof, fail closed with a bounded conservative window.
        return max(row.expires_at, row.updated_at).astimezone(UTC) + timedelta(
            hours=1,
        )
    return claimed_at.astimezone(UTC) + timedelta(
        seconds=(timeout_seconds + CLAIMED_EXECUTION_SETTLEMENT_GRACE_SECONDS),
    )


async def lock_execution_approval_private_rows(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    owner_user_id: str | None,
    thread_id: str | None = None,
    related_run_id: str | None = None,
    approval_id: uuid.UUID | None = None,
    active_only: bool = False,
    extra_run_dependencies: tuple[ApprovalRunDependency, ...] = (),
    extra_job_dependencies: tuple[ApprovalJobDependency, ...] = (),
    lock_active_attempts: bool = False,
) -> LockedExecutionApprovalRows:
    """Lock Job -> Run -> optional active attempt -> approval for one boundary.

    The initial approval query is authority-free discovery of server-owned FK
    coordinates. Command stage/claim/completion first lock their Job and Run,
    so those dependency locks prevent a matching approval from advancing ahead
    of this transaction before the final approval lock is acquired.
    """

    scope_predicates = [
        ExecutionApprovalRequestRow.project_id == project_id,
    ]
    if owner_user_id is not None:
        scope_predicates.append(
            ExecutionApprovalRequestRow.owner_user_id == owner_user_id,
        )
    if thread_id is not None:
        scope_predicates.append(
            ExecutionApprovalRequestRow.thread_id == thread_id,
        )
    if related_run_id is not None:
        scope_predicates.append(
            sa.or_(
                ExecutionApprovalRequestRow.source_run_id == related_run_id,
                ExecutionApprovalRequestRow.continuation_run_id == related_run_id,
            )
        )
    if approval_id is not None:
        scope_predicates.append(
            ExecutionApprovalRequestRow.id == approval_id,
        )
    discovery_predicates = list(scope_predicates)
    if active_only:
        discovery_predicates.append(
            ExecutionApprovalRequestRow.status.in_(_ACTIVE_STATUSES),
        )

    discovered = tuple((await session.execute(sa.select(ExecutionApprovalRequestRow).where(*discovery_predicates).order_by(ExecutionApprovalRequestRow.id))).scalars())
    discovered_coordinates = {
        row.id: (
            row.project_id,
            row.owner_user_id,
            row.thread_id,
            row.source_run_id,
            row.source_job_id,
            row.source_job_attempt_id,
            row.continuation_run_id,
            row.continuation_job_id,
            row.execution_job_attempt_id,
        )
        for row in discovered
    }

    job_coordinates: dict[uuid.UUID, tuple[str, str]] = {}
    run_coordinates: dict[
        str,
        tuple[str, str, uuid.UUID | None],
    ] = {}
    for row in discovered:
        job_coordinates[row.source_job_id] = (
            row.owner_user_id,
            row.source_run_id,
        )
        run_coordinates[row.source_run_id] = (
            row.owner_user_id,
            row.thread_id,
            row.source_job_id,
        )
        if row.continuation_job_id is not None:
            assert row.continuation_run_id is not None
            job_coordinates[row.continuation_job_id] = (
                row.owner_user_id,
                row.continuation_run_id,
            )
            run_coordinates[row.continuation_run_id] = (
                row.owner_user_id,
                row.thread_id,
                row.continuation_job_id,
            )
    for dependency in extra_run_dependencies:
        run_coordinates[dependency.run_id] = (
            dependency.owner_user_id,
            dependency.thread_id,
            dependency.job_id,
        )
        if dependency.job_id is not None:
            job_coordinates[dependency.job_id] = (
                dependency.owner_user_id,
                dependency.run_id,
            )
    for dependency in extra_job_dependencies:
        job_coordinates[dependency.job_id] = (
            dependency.owner_user_id,
            dependency.run_id,
        )

    actual_jobs: dict[uuid.UUID, tuple[str, str]] = {}
    locked_job_rows: dict[uuid.UUID, JobRow] = {}
    if job_coordinates:
        locked_jobs = tuple(
            (
                await session.execute(
                    sa.select(JobRow)
                    .where(
                        JobRow.project_id == project_id,
                        JobRow.id.in_(tuple(job_coordinates)),
                    )
                    .order_by(JobRow.id)
                    .with_for_update(of=JobRow)
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        locked_job_rows = {row.id: row for row in locked_jobs}
        actual_jobs = {row.id: (row.owner_user_id, row.run_id) for row in locked_jobs}
        if actual_jobs != job_coordinates:
            raise ExecutionApprovalPrivateLifecycleConflict()

    actual_runs: dict[str, tuple[str, str, uuid.UUID | None]] = {}
    locked_run_rows: dict[str, RunRow] = {}
    if run_coordinates:
        locked_runs = tuple(
            (
                await session.execute(
                    sa.select(RunRow)
                    .where(
                        RunRow.project_id == project_id,
                        RunRow.run_id.in_(tuple(run_coordinates)),
                    )
                    .order_by(RunRow.run_id)
                    .with_for_update(of=RunRow)
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        locked_run_rows = {row.run_id: row for row in locked_runs}
        actual_runs = {
            row.run_id: (
                row.owner_user_id,
                row.thread_id,
                row.job_id,
            )
            for row in locked_runs
        }
        if actual_runs != run_coordinates:
            raise ExecutionApprovalPrivateLifecycleConflict()

    # For force-revocation callers, an unresolved attempt is live execution
    # authority even if a stale or
    # partially settled Job row is already terminal. Lock every exact Job's
    # outcome=NULL attempt so thread deletion cannot leave that authority
    # behind when its Run still needs revocation/finalization.
    attempt_job_rows = tuple(locked_job_rows.values()) if lock_active_attempts else ()
    active_attempts: dict[uuid.UUID, tuple[JobAttemptRow, ...]] = {}
    if attempt_job_rows:
        locked_attempt_rows = tuple(
            (
                await session.execute(
                    sa.select(JobAttemptRow)
                    .where(
                        JobAttemptRow.job_id.in_(
                            tuple(row.id for row in attempt_job_rows),
                        ),
                        JobAttemptRow.outcome.is_(None),
                    )
                    .order_by(
                        JobAttemptRow.job_id,
                        JobAttemptRow.attempt_number,
                    )
                    .with_for_update(of=JobAttemptRow)
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        active_attempts = {job.id: tuple(attempt for attempt in locked_attempt_rows if attempt.job_id == job.id) for job in attempt_job_rows}

    if not discovered:
        return LockedExecutionApprovalRows(
            (),
            {},
            locked_job_rows,
            locked_run_rows,
            active_attempts,
        )
    approval_ids = tuple(row.id for row in discovered)
    locked_approvals = tuple(
        (
            await session.execute(
                sa.select(ExecutionApprovalRequestRow)
                .where(
                    *scope_predicates,
                    ExecutionApprovalRequestRow.id.in_(approval_ids),
                )
                .order_by(ExecutionApprovalRequestRow.id)
                .with_for_update(of=ExecutionApprovalRequestRow)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    if tuple(row.id for row in locked_approvals) != approval_ids:
        raise ExecutionApprovalPrivateLifecycleConflict()
    locked_coordinates = {
        row.id: (
            row.project_id,
            row.owner_user_id,
            row.thread_id,
            row.source_run_id,
            row.source_job_id,
            row.source_job_attempt_id,
            row.continuation_run_id,
            row.continuation_job_id,
            row.execution_job_attempt_id,
        )
        for row in locked_approvals
    }
    if locked_coordinates != discovered_coordinates:
        # A continuation was linked between discovery and dependency locking.
        # Retry so its newly authoritative Job/Run can be locked first.
        raise ExecutionApprovalPrivateLifecycleConflict()
    return LockedExecutionApprovalRows(
        locked_approvals,
        {row.id: claimed_execution_absolute_deadline(row) for row in locked_approvals if row.status == "claimed"},
        locked_job_rows,
        locked_run_rows,
        active_attempts,
    )


def _terminalize(
    row: ExecutionApprovalRequestRow,
    *,
    status: str,
    now: datetime,
) -> None:
    row.status = status
    row.version += 1
    row.terminal_at = now
    row.updated_at = now


def _has_live_execution_lease(
    job: JobRow | None,
    run: RunRow | None,
    attempt: JobAttemptRow | None,
    *,
    now: datetime,
) -> bool:
    return (
        job is not None
        and run is not None
        and attempt is not None
        and job.status in {"leased", "running"}
        and run.status == "running"
        and job.lease_expires_at is not None
        and job.lease_expires_at > now
        and run.execution_lease_expires_at is not None
        and run.execution_lease_expires_at > now
        and job.lease_token_hash is not None
        and job.lease_token_hash == run.execution_lease_token_hash
        and job.lease_token_hash == attempt.lease_token_hash
        and attempt.finished_at is None
    )


async def reconcile_locked_execution_approval(
    session: AsyncSession,
    row: ExecutionApprovalRequestRow,
    *,
    now: datetime,
    audit: HostExecutionApprovalAuditPort | None = None,
) -> None:
    """Converge one already-locked active row using durable lease authority."""

    approval_audit = audit or NoopHostExecutionApprovalAudit()
    if row.status in {"pending", "approved"} and row.expires_at <= now:
        _terminalize(row, status="expired", now=now)
        try:
            await transition_output_delivery_obligation_for_approval_terminal(
                session,
                approval=row,
                approval_status="expired",
                now=now,
            )
        except OutputDeliveryObligationConflict:
            raise ExecutionApprovalPrivateLifecycleConflict() from None
        await approval_audit.host_execution_approval_terminal(
            session,
            project_id=row.project_id,
            source_run_id=row.source_run_id,
            status="expired",
            request_id=None,
            occurred_at=now,
        )
        return
    if row.status == "staged":
        source_job = await session.scalar(
            sa.select(JobRow).where(
                JobRow.id == row.source_job_id,
                JobRow.project_id == row.project_id,
                JobRow.owner_user_id == row.owner_user_id,
                JobRow.run_id == row.source_run_id,
            )
        )
        source_run = await session.scalar(
            sa.select(RunRow).where(
                RunRow.run_id == row.source_run_id,
                RunRow.project_id == row.project_id,
                RunRow.owner_user_id == row.owner_user_id,
                RunRow.thread_id == row.thread_id,
                RunRow.job_id == row.source_job_id,
            )
        )
        source_attempt = await session.scalar(
            sa.select(JobAttemptRow).where(
                JobAttemptRow.id == row.source_job_attempt_id,
                JobAttemptRow.job_id == row.source_job_id,
            )
        )
        if source_run is not None and staged_approval_has_exact_suspension_marker(row, source_run):
            # Checkpoint-safe success already won this race.  A lease timeout
            # may trigger takeover, but cannot turn the sealed source result
            # into cancellation before that attempt repairs the terminal.
            return
        if not _has_live_execution_lease(
            source_job,
            source_run,
            source_attempt,
            now=now,
        ):
            _terminalize(row, status="cancelled", now=now)
            try:
                await transition_output_delivery_obligation_for_approval_terminal(
                    session,
                    approval=row,
                    approval_status="cancelled",
                    now=now,
                )
            except OutputDeliveryObligationConflict:
                raise ExecutionApprovalPrivateLifecycleConflict() from None
            await approval_audit.host_execution_approval_terminal(
                session,
                project_id=row.project_id,
                source_run_id=row.source_run_id,
                status="cancelled",
                request_id=(source_job.origin_trace_id if source_job is not None else None),
                occurred_at=now,
            )
        return
    if row.status == "approved":
        if row.continuation_job_id is None or row.continuation_run_id is None:
            return
        continuation_job = await session.scalar(
            sa.select(JobRow).where(
                JobRow.id == row.continuation_job_id,
                JobRow.project_id == row.project_id,
                JobRow.owner_user_id == row.owner_user_id,
                JobRow.run_id == row.continuation_run_id,
            )
        )
        continuation_run = await session.scalar(
            sa.select(RunRow).where(
                RunRow.run_id == row.continuation_run_id,
                RunRow.project_id == row.project_id,
                RunRow.owner_user_id == row.owner_user_id,
                RunRow.thread_id == row.thread_id,
                RunRow.job_id == row.continuation_job_id,
            )
        )
        waiting = continuation_job is not None and continuation_run is not None and continuation_job.status in {"queued", "retry_wait"} and continuation_job.retry_safety == "safe" and continuation_run.status in {"pending", "running"}
        continuation_attempt = await session.scalar(sa.select(JobAttemptRow).where(JobAttemptRow.job_id == row.continuation_job_id).order_by(JobAttemptRow.attempt_number.desc()).limit(1))
        leased_or_running_not_started = (
            continuation_job is not None
            and continuation_run is not None
            and continuation_attempt is not None
            and continuation_job.status in {"leased", "running"}
            and continuation_run.status == "pending"
            and continuation_job.retry_safety == "safe"
            and continuation_job.lease_expires_at is not None
            and continuation_job.lease_expires_at > now
            and continuation_job.lease_token_hash is not None
            and continuation_job.lease_token_hash == continuation_attempt.lease_token_hash
            and continuation_attempt.attempt_number == continuation_job.attempt_count
            and continuation_attempt.finished_at is None
        )
        if waiting or leased_or_running_not_started:
            return
        if not _has_live_execution_lease(
            continuation_job,
            continuation_run,
            continuation_attempt,
            now=now,
        ):
            _terminalize(row, status="cancelled", now=now)
            try:
                await transition_output_delivery_obligation_for_approval_terminal(
                    session,
                    approval=row,
                    approval_status="cancelled",
                    now=now,
                )
            except OutputDeliveryObligationConflict:
                raise ExecutionApprovalPrivateLifecycleConflict() from None
            await approval_audit.host_execution_approval_terminal(
                session,
                project_id=row.project_id,
                source_run_id=row.source_run_id,
                status="cancelled",
                request_id=(continuation_job.origin_trace_id if continuation_job is not None else None),
                occurred_at=now,
            )
        return
    if row.status != "claimed":
        return

    # A lost DB lease only proves that the Worker stopped heartbeating. The
    # already-launched Local process may continue until its frozen subprocess
    # timeout. Keep the exclusive active row and emit no terminal audit until
    # that non-renewable deadline has passed.
    if now < claimed_execution_absolute_deadline(row):
        return

    receipt = await session.scalar(
        sa.select(ExecutionApprovalResultReceiptRow.id).where(
            ExecutionApprovalResultReceiptRow.approval_id == row.id,
            ExecutionApprovalResultReceiptRow.project_id == row.project_id,
            ExecutionApprovalResultReceiptRow.owner_user_id == row.owner_user_id,
            ExecutionApprovalResultReceiptRow.thread_id == row.thread_id,
        )
    )
    if receipt is not None or row.continuation_job_id is None or row.continuation_run_id is None or row.execution_job_attempt_id is None:
        # Completion commits the receipt and terminal approval state together.
        # A partial pair or an incomplete claim is therefore ambiguous.
        _terminalize(row, status="unknown", now=now)
        try:
            await transition_output_delivery_obligation_for_approval_terminal(
                session,
                approval=row,
                approval_status="unknown",
                now=now,
            )
        except OutputDeliveryObligationConflict:
            raise ExecutionApprovalPrivateLifecycleConflict() from None
        await approval_audit.host_execution_approval_terminal(
            session,
            project_id=row.project_id,
            source_run_id=row.source_run_id,
            status="unknown",
            request_id=None,
            occurred_at=now,
        )
        return

    job = await session.scalar(
        sa.select(JobRow).where(
            JobRow.id == row.continuation_job_id,
            JobRow.project_id == row.project_id,
            JobRow.owner_user_id == row.owner_user_id,
            JobRow.run_id == row.continuation_run_id,
        )
    )
    run = await session.scalar(
        sa.select(RunRow).where(
            RunRow.run_id == row.continuation_run_id,
            RunRow.project_id == row.project_id,
            RunRow.owner_user_id == row.owner_user_id,
            RunRow.thread_id == row.thread_id,
            RunRow.job_id == row.continuation_job_id,
        )
    )
    attempt = await session.scalar(
        sa.select(JobAttemptRow).where(
            JobAttemptRow.id == row.execution_job_attempt_id,
            JobAttemptRow.job_id == row.continuation_job_id,
        )
    )
    if not _has_live_execution_lease(job, run, attempt, now=now):
        _terminalize(row, status="unknown", now=now)
        try:
            await transition_output_delivery_obligation_for_approval_terminal(
                session,
                approval=row,
                approval_status="unknown",
                now=now,
            )
        except OutputDeliveryObligationConflict:
            raise ExecutionApprovalPrivateLifecycleConflict() from None
        await approval_audit.host_execution_approval_terminal(
            session,
            project_id=row.project_id,
            source_run_id=row.source_run_id,
            status="unknown",
            request_id=job.origin_trace_id if job is not None else None,
            occurred_at=now,
        )


async def reconcile_locked_execution_approval_and_continuation(
    session: AsyncSession,
    row: ExecutionApprovalRequestRow,
    *,
    locked: LockedExecutionApprovalRows,
    context: PrivateWorkContext,
    now: datetime,
    quota: ExecutionApprovalContinuationQuotaPort,
    run_audit: ExecutionApprovalContinuationRunAuditPort,
    approval_audit: HostExecutionApprovalAuditPort | None = None,
) -> None:
    """Expire an approval and synchronously close its unleased continuation.

    All callers must use ``lock_execution_approval_private_rows`` first so the
    canonical Job -> Run -> active attempt -> approval lock order is preserved. This wrapper is
    shared by ordinary Run admission and approval APIs; otherwise either path
    could expire the row while leaving an affinity-orphaned queued Job alive.
    """

    if row not in locked.rows:
        raise ExecutionApprovalPrivateLifecycleConflict()
    status_before = row.status
    await reconcile_locked_execution_approval(
        session,
        row,
        now=now,
        audit=approval_audit,
    )
    if status_before != "approved" or row.status != "expired" or row.continuation_job_id is None or row.continuation_run_id is None:
        return
    job = locked.jobs.get(row.continuation_job_id)
    if job is None:
        raise ExecutionApprovalPrivateLifecycleConflict()
    cancel_result = cancel_locked_execution_approval_continuation(
        row,
        locked,
        now=now,
        reason="approval_expired",
    )
    if cancel_result == "requested":
        await run_audit.run_cancel_requested(
            session,
            context,
            run_id=row.continuation_run_id,
            job_id=row.continuation_job_id,
        )
        return
    await quota.release_concurrent_run(
        session,
        context.resource_scope,
        run_id=row.continuation_run_id,
        request_id=context.request_id,
    )
    if cancel_result == "cancelled":
        await run_audit.run_terminal(
            session,
            context.resource_scope,
            run_id=row.continuation_run_id,
            job_id=row.continuation_job_id,
            job_type=job.job_type,
            status="interrupted",
            public_error_code=None,
            request_id=context.request_id,
        )


async def lock_and_reconcile_active_execution_approval(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    owner_user_id: str,
    thread_id: str,
    now: datetime,
    audit: HostExecutionApprovalAuditPort | None = None,
) -> ExecutionApprovalRequestRow | None:
    """Lock the thread's active row and hide it after lazy convergence."""

    locked = await lock_execution_approval_private_rows(
        session,
        project_id=project_id,
        owner_user_id=owner_user_id,
        thread_id=thread_id,
        active_only=True,
    )
    row = locked.rows[0] if locked.rows else None
    if row is None:
        return None
    await reconcile_locked_execution_approval(
        session,
        row,
        now=now,
        audit=audit,
    )
    return row if row.status in EXECUTION_APPROVAL_ACTIVE_STATUSES else None


async def converge_execution_approvals_for_terminal_job(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    owner_user_id: str,
    run_id: str,
    job_id: uuid.UUID,
    terminal_job_status: Literal["cancelled", "dead"],
    now: datetime,
    request_id: str | None = None,
    audit: HostExecutionApprovalAuditPort | None = None,
) -> None:
    """Close active approvals whose exact source or continuation Job died."""

    rows = (
        await session.scalars(
            sa.select(ExecutionApprovalRequestRow)
            .where(
                ExecutionApprovalRequestRow.project_id == project_id,
                ExecutionApprovalRequestRow.owner_user_id == owner_user_id,
                sa.or_(
                    sa.and_(
                        ExecutionApprovalRequestRow.source_run_id == run_id,
                        ExecutionApprovalRequestRow.source_job_id == job_id,
                        ExecutionApprovalRequestRow.status == "staged",
                    ),
                    sa.and_(
                        ExecutionApprovalRequestRow.continuation_run_id == run_id,
                        ExecutionApprovalRequestRow.continuation_job_id == job_id,
                        ExecutionApprovalRequestRow.status.in_(
                            (
                                "approved",
                                "claimed",
                                "finished",
                                "launch_failed",
                            )
                        ),
                    ),
                ),
            )
            .with_for_update(of=ExecutionApprovalRequestRow)
            .execution_options(populate_existing=True)
        )
    ).all()
    approval_audit = audit or NoopHostExecutionApprovalAudit()
    for row in rows:
        if row.status == "staged":
            source_run = await session.scalar(
                sa.select(RunRow)
                .where(
                    RunRow.project_id == row.project_id,
                    RunRow.owner_user_id == row.owner_user_id,
                    RunRow.thread_id == row.thread_id,
                    RunRow.run_id == row.source_run_id,
                    RunRow.job_id == row.source_job_id,
                )
                .with_for_update(of=RunRow)
                .execution_options(populate_existing=True)
            )
            if source_run is None:
                raise ExecutionApprovalPrivateLifecycleConflict()
            if staged_approval_has_exact_suspension_marker(row, source_run):
                # A terminal Job callback cannot erase a success proof that
                # committed first.  Roll back terminalization so a takeover
                # can settle the source and repair its stream atomically.
                raise ExecutionApprovalPrivateLifecycleConflict()
        if row.status in {"finished", "launch_failed"}:
            try:
                await settle_continuation_output_delivery(
                    session,
                    approval_id_value=str(row.id),
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    thread_id=row.thread_id,
                    continuation_run_id=run_id,
                    continuation_job_id=job_id,
                    settled_status=("interrupted" if terminal_job_status == "cancelled" else "error"),
                    now=now,
                )
            except OutputDeliveryObligationConflict:
                raise ExecutionApprovalPrivateLifecycleConflict() from None
            continue
        # Before a claim, no process launch is possible.  After a claim, a
        # dead/cancelled Job cannot prove whether the host process launched.
        if row.status == "claimed" and now < claimed_execution_absolute_deadline(row):
            continue
        status = "unknown" if row.status == "claimed" else "cancelled"
        _terminalize(row, status=status, now=now)
        try:
            await transition_output_delivery_obligation_for_approval_terminal(
                session,
                approval=row,
                approval_status=status,
                now=now,
            )
        except OutputDeliveryObligationConflict:
            raise ExecutionApprovalPrivateLifecycleConflict() from None
        await approval_audit.host_execution_approval_terminal(
            session,
            project_id=row.project_id,
            source_run_id=row.source_run_id,
            status=status,
            request_id=request_id,
            occurred_at=now,
        )


__all__ = [
    "ApprovalRunDependency",
    "ExecutionApprovalPrivateLifecycleConflict",
    "ExecutionApprovalContinuationQuotaPort",
    "ExecutionApprovalContinuationRunAuditPort",
    "LockedExecutionApprovalRows",
    "cancel_locked_execution_approval_continuation",
    "claimed_execution_absolute_deadline",
    "converge_execution_approvals_for_terminal_job",
    "lock_execution_approval_private_rows",
    "lock_and_reconcile_active_execution_approval",
    "reconcile_locked_execution_approval",
    "reconcile_locked_execution_approval_and_continuation",
    "reject_sealed_staged_approval_terminalization",
    "staged_approval_has_exact_suspension_marker",
]
