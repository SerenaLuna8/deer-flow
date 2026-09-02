"""Gateway-owned Execution Approval reads and approval-first decisions.

The Gateway never launches a process.  It projects owner-scoped approval state
and records the browser decision; ``decide()`` keeps its decision transaction,
transaction-external Run admission, and verification transaction in order.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    ExecutionApprovalConflict,
    ExecutionApprovalNotFound,
    PrivateWorkError,
    PrivateWorkUnavailable,
)
from app.private_work.execution_approval_audit import (
    HostExecutionApprovalAuditPort,
    NoopHostExecutionApprovalAudit,
)
from app.private_work.execution_approval_codec import (
    _frozen_plan_from_row,
    _outcome_from_receipt,
)
from app.private_work.execution_approval_lifecycle import (
    ExecutionApprovalContinuationQuotaPort,
    ExecutionApprovalContinuationRunAuditPort,
    ExecutionApprovalPrivateLifecycleConflict,
    LockedExecutionApprovalRows,
    _database_now,
    lock_execution_approval_private_rows,
    reconcile_locked_execution_approval_and_continuation,
)
from app.private_work.execution_approval_policy import (
    HostExecutionProviderPolicySnapshot,
    _canonical_digest,
)
from app.private_work.execution_profile import RequestedRunExecutionProfile
from app.private_work.output_delivery_obligation import (
    OutputDeliveryObligationConflict,
    transition_output_delivery_obligation_for_approval_terminal,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_admission import (
    PrivateRunAdmissionServerContext,
    PrivateRunAdmissionService,
)
from app.private_work.run_repository import (
    PrivateRunCreate,
    PrivateRunRepository,
)
from app.projects.capabilities import Capability
from deerflow.persistence.execution_approvals import (
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)

_CLAIM_TTL_SECONDS = 60
_CONTINUATION_NAME = "host-execution-continuation:v1"


def _decision_digest(
    *,
    approval_id: uuid.UUID,
    source_run_id: str,
    decision: str,
    expected_version: int,
) -> str:
    return _canonical_digest(
        {
            "schema_version": 1,
            "approval_id": str(approval_id),
            "source_run_id": source_run_id,
            "decision": decision,
            "expected_version": expected_version,
        },
    )


def _idempotency_digest(value: uuid.UUID) -> str:
    return hashlib.sha256(
        f"execution-approval-decision:{value}".encode(),
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionApprovalProjection:
    schema_version: int
    server_time: datetime
    approval: dict[str, object] | None


class ExecutionApprovalService:
    """Owner-scoped Gateway reads and approval-first decisions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        admission: PrivateRunAdmissionService,
        provider_policy: HostExecutionProviderPolicySnapshot,
        quota: ExecutionApprovalContinuationQuotaPort,
        run_audit: ExecutionApprovalContinuationRunAuditPort,
        audit: HostExecutionApprovalAuditPort | None = None,
    ) -> None:
        if type(provider_policy) is not HostExecutionProviderPolicySnapshot:
            raise TypeError("provider_policy snapshot is required")
        self._factory = session_factory
        self._admission = admission
        self._provider_policy = provider_policy
        self._quota = quota
        self._run_audit = run_audit
        self._audit = audit or NoopHostExecutionApprovalAudit()

    @staticmethod
    async def _lock_thread(
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> None:
        from app.private_work.thread_repository import PrivateThreadRepository

        thread = await PrivateThreadRepository(session).get(
            scope=context.resource_scope,
            thread_id=thread_id,
            lock=True,
        )
        if thread is None:
            raise ExecutionApprovalNotFound(context.request_id)

    @staticmethod
    async def _locked_row(
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        approval_id: uuid.UUID,
    ) -> tuple[ExecutionApprovalRequestRow | None, LockedExecutionApprovalRows]:
        try:
            locked = await lock_execution_approval_private_rows(
                session,
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                approval_id=approval_id,
            )
        except ExecutionApprovalPrivateLifecycleConflict:
            raise PrivateWorkUnavailable(context.request_id) from None
        return (locked.rows[0] if locked.rows else None), locked

    async def _reconcile_if_needed(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        row: ExecutionApprovalRequestRow,
        *,
        locked: LockedExecutionApprovalRows,
        now: datetime,
    ) -> None:
        try:
            await reconcile_locked_execution_approval_and_continuation(
                session,
                row,
                locked=locked,
                context=context,
                now=now,
                quota=self._quota,
                run_audit=self._run_audit,
                approval_audit=self._audit,
            )
        except ExecutionApprovalPrivateLifecycleConflict:
            raise PrivateWorkUnavailable(context.request_id) from None

    @staticmethod
    async def _project(
        session: AsyncSession,
        context: PrivateWorkContext,
        row: ExecutionApprovalRequestRow | None,
        *,
        now: datetime,
    ) -> ExecutionApprovalProjection:
        if row is None or row.status == "staged":
            return ExecutionApprovalProjection(1, now, None)
        try:
            plan, _policy, execution_domain = _frozen_plan_from_row(row)
        except (TypeError, ValueError):
            raise PrivateWorkUnavailable(context.request_id) from None
        agent_path = list(plan.agent_path)
        is_subagent = len(agent_path) > 1 or agent_path[0] != "lead"
        continuation: dict[str, object] | None = None
        if row.continuation_run_id is not None:
            run = await PrivateRunRepository(session).get(
                scope=context.resource_scope,
                run_id=row.continuation_run_id,
            )
            if run is not None:
                continuation = {"run_id": run.run_id, "status": run.status}
        approval: dict[str, object] = {
            "approval_id": str(row.id),
            "source_run_id": row.source_run_id,
            "source_tool_call_id": row.tool_call_id,
            "status": row.status,
            "version": str(row.version),
            "execution_domain": {
                "label": execution_domain.public_label,
                "effective_user_label": "Worker process identity",
            },
            "command_preview": plan.requested_command,
            "cwd_preview": "/mnt/user-data/workspace",
            "timeout_seconds": plan.timeout_seconds,
            "source_agent": {
                "kind": "subagent" if is_subagent else "lead",
                "label": (agent_path[-1].removeprefix("subagent:") if is_subagent else "Project Assistant"),
                "path": agent_path,
            },
            "risk_level": "host_execution",
            "warning_code": "LOCAL_PROCESS_RUNS_ON_HOST",
            "can_decide": (row.status == "pending" and Capability.PRIVATE_WORK_APPROVE_HOST_EXECUTION in context.capabilities),
            "continuation_run": continuation,
        }
        if row.status == "pending":
            approval.update(
                {
                    "decision_expires_at": row.expires_at.isoformat(),
                    "remaining_ttl_seconds": max(
                        0,
                        int((row.expires_at - now).total_seconds()),
                    ),
                },
            )
        elif row.status == "approved":
            approval.update(
                {
                    "decision_at": (row.decided_at or row.updated_at).isoformat(),
                    "claim_expires_at": row.expires_at.isoformat(),
                },
            )
        elif row.status == "claimed":
            approval["claimed_at"] = (row.claimed_at or row.updated_at).isoformat()
        elif row.status in {"finished", "launch_failed"}:
            receipt = await session.scalar(
                sa.select(ExecutionApprovalResultReceiptRow).where(
                    ExecutionApprovalResultReceiptRow.approval_id == row.id,
                ),
            )
            try:
                outcome = _outcome_from_receipt(row, receipt)
            except (TypeError, ValueError):
                raise PrivateWorkUnavailable(context.request_id) from None
            approval["finished_at"] = (row.terminal_at or row.updated_at).isoformat()
            if outcome.status == "finished":
                approval["exit_code"] = outcome.exit_code
                approval["result_summary_code"] = "PROCESS_EXITED"
            else:
                approval["reason_code"] = outcome.reason_code
        elif row.status == "unknown":
            approval.update(
                {
                    "finished_at": (row.terminal_at or row.updated_at).isoformat(),
                    "warning_code": "HOST_EXECUTION_STATE_UNKNOWN",
                },
            )
        elif row.status == "denied":
            approval.update(
                {
                    "decision_at": (row.decided_at or row.updated_at).isoformat(),
                    "denial_delivery_status": "not_required",
                },
            )
        elif row.status in {"expired", "cancelled"}:
            approval.update(
                {
                    "finished_at": (row.terminal_at or row.updated_at).isoformat(),
                    "reason_code": (("DECISION_TTL_EXPIRED" if row.decision is None else "CLAIM_TTL_EXPIRED") if row.status == "expired" else ("SOURCE_RUN_CANCELLED" if row.decision is None else "CLAIM_OR_POLICY_INVALIDATED")),
                },
            )
        return ExecutionApprovalProjection(1, now, approval)

    async def active(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> ExecutionApprovalProjection:
        try:
            async with self._factory() as session, session.begin():
                await PrivateWorkRevalidator().require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                    lock=True,
                )
                await self._lock_thread(session, context, thread_id)
                try:
                    locked = await lock_execution_approval_private_rows(
                        session,
                        project_id=context.project_id,
                        owner_user_id=str(context.user_id),
                        thread_id=thread_id,
                        active_only=True,
                    )
                except ExecutionApprovalPrivateLifecycleConflict:
                    raise PrivateWorkUnavailable(context.request_id) from None
                now = await _database_now(session)
                row = locked.rows[0] if locked.rows else None
                if row is not None:
                    await self._reconcile_if_needed(
                        session,
                        context,
                        row,
                        locked=locked,
                        now=now,
                    )
                    if row.status not in {"pending", "approved", "claimed"}:
                        row = None
                return await self._project(session, context, row, now=now)
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def get(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        approval_id: uuid.UUID,
    ) -> ExecutionApprovalProjection:
        try:
            async with self._factory() as session, session.begin():
                await PrivateWorkRevalidator().require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                    lock=True,
                )
                await self._lock_thread(session, context, thread_id)
                row, locked = await self._locked_row(
                    session,
                    context,
                    thread_id=thread_id,
                    approval_id=approval_id,
                )
                if row is None:
                    raise ExecutionApprovalNotFound(context.request_id)
                now = await _database_now(session)
                await self._reconcile_if_needed(
                    session,
                    context,
                    row,
                    locked=locked,
                    now=now,
                )
                return await self._project(session, context, row, now=now)
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def decide(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        source_run_id: str,
        approval_id: uuid.UUID,
        decision: Literal["allow_once", "deny"],
        expected_version: int,
        idempotency_key: uuid.UUID,
    ) -> ExecutionApprovalProjection:
        if expected_version < 1:
            raise ExecutionApprovalConflict(context.request_id)
        idempotency_digest = _idempotency_digest(idempotency_key)
        request_digest = _decision_digest(
            approval_id=approval_id,
            source_run_id=source_run_id,
            decision=decision,
            expected_version=expected_version,
        )
        source_model_name: str | None = None
        should_admit = False
        admission_digest: str | None = None
        execution_domain_affinity: str | None = None
        channel_user_id: str | None = None
        try:
            async with self._factory() as session, session.begin():
                await PrivateWorkRevalidator().require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.PRIVATE_WORK_APPROVE_HOST_EXECUTION,
                    lock=True,
                )
                await self._lock_thread(session, context, thread_id)
                row, locked = await self._locked_row(
                    session,
                    context,
                    thread_id=thread_id,
                    approval_id=approval_id,
                )
                if row is None or row.source_run_id != source_run_id:
                    raise ExecutionApprovalNotFound(context.request_id)
                decided_at = await _database_now(session)
                status_before_reconciliation = row.status
                await self._reconcile_if_needed(
                    session,
                    context,
                    row,
                    locked=locked,
                    now=decided_at,
                )
                if row.status != status_before_reconciliation and row.status in {"expired", "cancelled", "unknown"}:
                    # Commit lifecycle convergence and its audit append before
                    # returning the now-terminal server projection.
                    return await self._project(
                        session,
                        context,
                        row,
                        now=decided_at,
                    )
                if row.decision_idempotency_key == idempotency_digest:
                    if row.decision_request_digest != request_digest:
                        raise ExecutionApprovalConflict(context.request_id)
                    if row.decision != decision:
                        raise ExecutionApprovalConflict(context.request_id)
                    if decision == "allow_once" and row.status == "approved":
                        try:
                            (
                                _plan,
                                persisted_policy,
                                persisted_domain,
                            ) = _frozen_plan_from_row(row)
                        except (TypeError, ValueError):
                            raise ExecutionApprovalConflict(
                                context.request_id,
                            ) from None
                        if not self._provider_policy.approval_enabled or persisted_policy != self._provider_policy:
                            raise ExecutionApprovalConflict(context.request_id)
                        if persisted_domain.affinity != row.execution_domain_affinity:
                            raise ExecutionApprovalConflict(context.request_id)
                    if decision != "allow_once" or row.status != "approved" or row.continuation_run_id is not None:
                        return await self._project(
                            session,
                            context,
                            row,
                            now=decided_at,
                        )
                    should_admit = True
                    admission_digest = row.decision_request_digest
                elif decision == "allow_once" and row.status == "approved" and row.decision == "allow_once" and row.continuation_run_id is None and row.version == expected_version:
                    # The decision is already durable, but the first Gateway
                    # may have failed before deterministic continuation
                    # admission. A refreshed owner may resume that admission
                    # with a new browser idempotency key without replacing the
                    # original decision evidence.
                    try:
                        (
                            _plan,
                            persisted_policy,
                            persisted_domain,
                        ) = _frozen_plan_from_row(row)
                    except (TypeError, ValueError):
                        raise ExecutionApprovalConflict(
                            context.request_id,
                        ) from None
                    if row.expires_at <= decided_at or not self._provider_policy.approval_enabled or persisted_policy != self._provider_policy or not row.decision_request_digest:
                        raise ExecutionApprovalConflict(context.request_id)
                    if persisted_domain.affinity != row.execution_domain_affinity:
                        raise ExecutionApprovalConflict(context.request_id)
                    should_admit = True
                    admission_digest = row.decision_request_digest
                else:
                    if row.status == "expired":
                        # Reconciliation and its audit append share this
                        # transaction. Returning the terminal projection lets
                        # the context manager commit both before the caller
                        # observes expiry; raising here would roll them back.
                        return await self._project(
                            session,
                            context,
                            row,
                            now=decided_at,
                        )
                    if row.status != "pending" or row.version != expected_version:
                        raise ExecutionApprovalConflict(context.request_id)
                    try:
                        (
                            _plan,
                            persisted_policy,
                            persisted_domain,
                        ) = _frozen_plan_from_row(row)
                    except (TypeError, ValueError):
                        raise ExecutionApprovalConflict(context.request_id) from None
                    if not self._provider_policy.approval_enabled or persisted_policy != self._provider_policy:
                        raise ExecutionApprovalConflict(context.request_id)
                    if persisted_domain.affinity != row.execution_domain_affinity:
                        raise ExecutionApprovalConflict(context.request_id)
                    if decision == "deny":
                        row.status = "denied"
                        row.decision = "deny"
                        row.decision_idempotency_key = idempotency_digest
                        row.decision_request_digest = request_digest
                        row.decided_by_user_id = str(context.user_id)
                        row.decided_at = decided_at
                        row.terminal_at = decided_at
                        row.version += 1
                        row.updated_at = decided_at
                        try:
                            await transition_output_delivery_obligation_for_approval_terminal(
                                session,
                                approval=row,
                                approval_status="denied",
                                now=decided_at,
                            )
                        except OutputDeliveryObligationConflict:
                            raise ExecutionApprovalConflict(
                                context.request_id,
                            ) from None
                        await self._audit.host_execution_approval_decided(
                            session,
                            context,
                            source_run_id=row.source_run_id,
                            decision="deny",
                            occurred_at=decided_at,
                        )
                        await session.flush()
                        return await self._project(
                            session,
                            context,
                            row,
                            now=decided_at,
                        )
                    row.status = "approved"
                    row.decision = "allow_once"
                    row.decision_idempotency_key = idempotency_digest
                    row.decision_request_digest = request_digest
                    row.decided_by_user_id = str(context.user_id)
                    row.decided_at = decided_at
                    row.expires_at = decided_at + timedelta(
                        seconds=_CLAIM_TTL_SECONDS,
                    )
                    row.version += 1
                    row.updated_at = decided_at
                    await self._audit.host_execution_approval_decided(
                        session,
                        context,
                        source_run_id=row.source_run_id,
                        decision="allow_once",
                        occurred_at=decided_at,
                    )
                    await session.flush()
                    should_admit = True
                    admission_digest = request_digest
                if should_admit:
                    execution_domain_affinity = row.execution_domain_affinity
                    try:
                        continuation_plan, _policy, _domain = _frozen_plan_from_row(row)
                    except (TypeError, ValueError):
                        raise ExecutionApprovalConflict(
                            context.request_id,
                        ) from None
                    if continuation_plan.channel_identity_mode == "set":
                        channel_user_id = continuation_plan.channel_user_id
                source_run = await PrivateRunRepository(session).get(
                    scope=context.resource_scope,
                    run_id=source_run_id,
                    lock=True,
                )
                if source_run is None or source_run.thread_id != thread_id or source_run.status != "success":
                    raise ExecutionApprovalConflict(context.request_id)
                source_model_name = source_run.model_name
        except PrivateWorkError:
            raise
        except IntegrityError:
            raise ExecutionApprovalConflict(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

        if not should_admit:  # pragma: no cover - all branches return or admit
            raise ExecutionApprovalConflict(context.request_id)
        if admission_digest is None:  # pragma: no cover - guarded above
            raise ExecutionApprovalConflict(context.request_id)
        if execution_domain_affinity is None:  # pragma: no cover - guarded above
            raise ExecutionApprovalConflict(context.request_id)
        continuation_run_id = str(
            uuid.uuid5(approval_id, _CONTINUATION_NAME),
        )
        continuation = await self._admission.admit(
            context,
            thread_id,
            PrivateRunCreate(
                run_id=continuation_run_id,
                metadata={"execution_approval_continuation": True},
                kwargs={
                    "input": {"messages": []},
                    "config": {},
                    "command": None,
                    "stream_mode": ["values", "messages-tuple", "custom"],
                    "stream_subgraphs": True,
                },
                execution_profile=RequestedRunExecutionProfile(
                    model_name=source_model_name,
                ),
            ),
            server_context=PrivateRunAdmissionServerContext(
                host_execution_approval_id=approval_id,
                host_execution_decision_digest=admission_digest,
                host_execution_domain_affinity=execution_domain_affinity,
                channel_user_id=channel_user_id,
            ),
        )
        try:
            async with self._factory() as session, session.begin():
                await PrivateWorkRevalidator().require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.PRIVATE_WORK_APPROVE_HOST_EXECUTION,
                    lock=True,
                )
                await self._lock_thread(session, context, thread_id)
                row, locked = await self._locked_row(
                    session,
                    context,
                    thread_id=thread_id,
                    approval_id=approval_id,
                )
                linked_at = await _database_now(session)
                if row is not None:
                    status_before_reconciliation = row.status
                    await self._reconcile_if_needed(
                        session,
                        context,
                        row,
                        locked=locked,
                        now=linked_at,
                    )
                    if row.status != status_before_reconciliation and row.status in {"expired", "cancelled", "unknown"}:
                        return await self._project(
                            session,
                            context,
                            row,
                            now=linked_at,
                        )
                if (
                    row is None
                    or row.status != "approved"
                    or row.source_run_id != source_run_id
                    or row.decision != "allow_once"
                    or row.decision_request_digest != admission_digest
                    or row.continuation_run_id != continuation.run.run_id
                    or row.continuation_job_id != continuation.job.job_id
                ):
                    raise ExecutionApprovalConflict(context.request_id)
                return await self._project(
                    session,
                    context,
                    row,
                    now=linked_at,
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None


__all__ = [
    "ExecutionApprovalProjection",
    "ExecutionApprovalService",
]
