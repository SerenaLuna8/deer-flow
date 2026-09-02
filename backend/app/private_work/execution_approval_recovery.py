"""Settlement-time and replay recovery for staged Execution Approvals.

These functions run inside a caller-owned Worker settlement transaction: they
accept the open session, take their own row locks in the established order,
and never begin or commit a transaction themselves.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.execution_approval_audit import (
    HostExecutionApprovalAuditPort,
    NoopHostExecutionApprovalAudit,
)
from app.private_work.execution_approval_lifecycle import (
    ExecutionApprovalPrivateLifecycleConflict,
    _database_now,
    staged_approval_has_exact_suspension_marker,
)
from app.private_work.output_delivery_obligation import (
    OutputDeliveryObligationConflict,
    seal_source_output_delivery_obligation,
    transition_output_delivery_obligation_for_approval_terminal,
)
from app.private_work.run_metadata import (
    RunHostExecutionSuspensionInvalid,
    run_host_execution_suspension,
)
from deerflow.persistence.execution_approvals import ExecutionApprovalRequestRow
from deerflow.persistence.jobs.model import DeadJobRow, JobRow
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.persistence.run.model import RunRow


async def _staged_approval_source_job_id(
    session: AsyncSession,
    *,
    claim: JobClaim,
) -> uuid.UUID:
    """Resolve the immutable producing Job behind a settlement successor."""

    if not claim.settlement_only:
        return claim.job_id
    predecessor_job_id = claim.predecessor_dead_job_id
    if predecessor_job_id is None:
        raise ExecutionApprovalPrivateLifecycleConflict()
    successor = await session.get(JobRow, claim.job_id)
    predecessor = await session.get(JobRow, predecessor_job_id)
    dead = await session.get(DeadJobRow, predecessor_job_id)
    if (
        successor is None
        or predecessor is None
        or dead is None
        or successor.predecessor_dead_job_id != predecessor_job_id
        or successor.project_id != claim.scope.project_id
        or successor.owner_user_id != claim.scope.owner_user_id
        or successor.run_id != claim.run_id
        or successor.job_type != claim.job_type
        or successor.origin_trace_id != claim.origin_trace_id
        or successor.retry_safety != "safe"
        or predecessor.status != "dead"
        or predecessor.project_id != successor.project_id
        or predecessor.owner_user_id != successor.owner_user_id
        or predecessor.run_id != successor.run_id
        or predecessor.job_type != successor.job_type
        or predecessor.origin_trace_id != successor.origin_trace_id
        or dead.project_id != predecessor.project_id
        or dead.job_type != predecessor.job_type
        or dead.retry_safety != predecessor.retry_safety
        or dead.attempt_count != predecessor.attempt_count
        or dead.public_error_code != predecessor.public_error_code
        or not ((predecessor.public_error_code == "SIDE_EFFECT_STATE_UNKNOWN" and predecessor.retry_safety != "safe") or (predecessor.public_error_code == "ATTEMPTS_EXHAUSTED" and predecessor.attempt_count >= predecessor.max_attempts))
    ):
        raise ExecutionApprovalPrivateLifecycleConflict()
    return predecessor_job_id


async def settle_staged_execution_approvals(
    session: AsyncSession,
    *,
    claim: JobClaim,
    succeeded: bool,
    suspended_approval_id: str | None,
    request_ttl_seconds: int,
    durable_terminal_replay: bool = False,
    audit: HostExecutionApprovalAuditPort | None = None,
) -> None:
    """Seal and activate the exact approval named by a suspended success."""

    if type(durable_terminal_replay) is not bool:
        raise ExecutionApprovalPrivateLifecycleConflict()
    if claim.run_id is None or claim.scope.owner_user_id is None:
        if suspended_approval_id is not None:
            raise ExecutionApprovalPrivateLifecycleConflict()
        return
    source_job_id = await _staged_approval_source_job_id(
        session,
        claim=claim,
    )
    suspended_id: uuid.UUID | None = None
    if suspended_approval_id is not None:
        try:
            suspended_id = uuid.UUID(suspended_approval_id)
        except (TypeError, ValueError):
            raise ExecutionApprovalPrivateLifecycleConflict() from None
    if durable_terminal_replay and suspended_id is not None:
        recovered_id = await recover_staged_execution_approval_id(
            session,
            claim=claim,
        )
        if recovered_id != str(suspended_id):
            raise ExecutionApprovalPrivateLifecycleConflict()
    rows = tuple(
        (
            await session.execute(
                sa.select(ExecutionApprovalRequestRow)
                .where(
                    ExecutionApprovalRequestRow.project_id == claim.scope.project_id,
                    ExecutionApprovalRequestRow.owner_user_id == claim.scope.owner_user_id,
                    ExecutionApprovalRequestRow.source_run_id == claim.run_id,
                    ExecutionApprovalRequestRow.source_job_id == source_job_id,
                    ExecutionApprovalRequestRow.status == "staged",
                )
                .with_for_update(),
                execution_options={"populate_existing": True},
            )
        ).scalars()
    )
    source_run = await session.scalar(
        sa.select(RunRow)
        .where(
            RunRow.project_id == claim.scope.project_id,
            RunRow.owner_user_id == claim.scope.owner_user_id,
            RunRow.run_id == claim.run_id,
            RunRow.job_id == claim.job_id,
        )
        .with_for_update(of=RunRow)
        .execution_options(populate_existing=True)
    )
    if source_run is None:
        raise ExecutionApprovalPrivateLifecycleConflict()
    sealed_rows = tuple(
        row
        for row in rows
        if staged_approval_has_exact_suspension_marker(
            row,
            source_run,
            bound_job_id=claim.job_id,
        )
    )
    if sealed_rows and (not succeeded or suspended_id is None or len(sealed_rows) != 1 or sealed_rows[0].id != suspended_id):
        # The marker is a checkpoint-safe durable success proof.  A later
        # stream ACK/error/cancel cannot reinterpret that exact source result
        # as failure; roll back so the recovery path can repair its terminal.
        raise ExecutionApprovalPrivateLifecycleConflict()
    settled_at = await _database_now(session)
    approval_audit = audit or NoopHostExecutionApprovalAudit()
    if succeeded:
        # A live result must name this exact attempt's staged request. Durable
        # terminal replay may name its earlier producing attempt only after the
        # server-only Run marker above re-proves that exact coordinate.
        if suspended_id is not None and (len(rows) != 1 or rows[0].id != suspended_id or (rows[0].source_job_attempt_id != claim.attempt_id and not durable_terminal_replay)):
            raise ExecutionApprovalPrivateLifecycleConflict()
    elif suspended_id is not None:
        raise ExecutionApprovalPrivateLifecycleConflict()
    for row in rows:
        available = succeeded and suspended_id == row.id and (row.source_job_attempt_id == claim.attempt_id or durable_terminal_replay)
        if available:
            try:
                await seal_source_output_delivery_obligation(
                    session,
                    approval=row,
                    now=settled_at,
                )
            except OutputDeliveryObligationConflict:
                raise ExecutionApprovalPrivateLifecycleConflict() from None
        row.status = "pending" if available else "cancelled"
        row.version += 1
        row.expires_at = settled_at + timedelta(seconds=request_ttl_seconds)
        row.terminal_at = None if available else settled_at
        row.updated_at = settled_at
        if available:
            await approval_audit.host_execution_approval_available(
                session,
                project_id=row.project_id,
                source_run_id=row.source_run_id,
                request_id=claim.origin_trace_id,
                occurred_at=settled_at,
            )
        else:
            try:
                await transition_output_delivery_obligation_for_approval_terminal(
                    session,
                    approval=row,
                    approval_status="cancelled",
                    now=settled_at,
                )
            except OutputDeliveryObligationConflict:
                raise ExecutionApprovalPrivateLifecycleConflict() from None
            await approval_audit.host_execution_approval_terminal(
                session,
                project_id=row.project_id,
                source_run_id=row.source_run_id,
                status="cancelled",
                request_id=claim.origin_trace_id,
                occurred_at=settled_at,
            )


async def recover_staged_execution_approval_id(
    session: AsyncSession,
    *,
    claim: JobClaim,
) -> str | None:
    """Recover typed suspension authority after durable-terminal replay.

    The public stream terminal intentionally contains no approval coordinate.
    Recovery therefore verifies the server-only Run marker written immediately
    before that terminal against the exact staged approval and its producing
    JobAttempt. Browser and stream metadata never select an approval.
    """

    if claim.run_id is None or claim.scope.owner_user_id is None:
        raise ExecutionApprovalPrivateLifecycleConflict()
    source_job_id = await _staged_approval_source_job_id(
        session,
        claim=claim,
    )
    run = await session.scalar(
        sa.select(RunRow)
        .where(
            RunRow.project_id == claim.scope.project_id,
            RunRow.owner_user_id == claim.scope.owner_user_id,
            RunRow.run_id == claim.run_id,
            RunRow.job_id == claim.job_id,
        )
        .with_for_update(of=RunRow)
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise ExecutionApprovalPrivateLifecycleConflict()
    try:
        marker = run_host_execution_suspension(run.metadata_json)
    except RunHostExecutionSuspensionInvalid:
        raise ExecutionApprovalPrivateLifecycleConflict() from None
    if marker is None:
        return None
    if marker.source_job_id != source_job_id:
        raise ExecutionApprovalPrivateLifecycleConflict()
    approval = await session.scalar(
        sa.select(ExecutionApprovalRequestRow)
        .where(
            ExecutionApprovalRequestRow.id == marker.approval_id,
            ExecutionApprovalRequestRow.project_id == claim.scope.project_id,
            ExecutionApprovalRequestRow.owner_user_id == claim.scope.owner_user_id,
            ExecutionApprovalRequestRow.thread_id == run.thread_id,
            ExecutionApprovalRequestRow.source_run_id == claim.run_id,
            ExecutionApprovalRequestRow.source_job_id == source_job_id,
            ExecutionApprovalRequestRow.source_job_attempt_id == marker.producing_attempt_id,
            ExecutionApprovalRequestRow.status == "staged",
        )
        .with_for_update(of=ExecutionApprovalRequestRow)
        .execution_options(populate_existing=True)
    )
    if approval is None:
        raise ExecutionApprovalPrivateLifecycleConflict()
    return str(approval.id)


__all__ = [
    "recover_staged_execution_approval_id",
    "settle_staged_execution_approvals",
]
