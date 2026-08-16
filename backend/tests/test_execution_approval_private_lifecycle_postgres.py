from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from langgraph.checkpoint.base import BaseCheckpointSaver
from support.private_thread_seed import seed_private_thread_database

from app.private_work import checkpointer as checkpointer_module
from app.private_work import retention_purge as retention_purge_module
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.errors import PrivateWorkConflict
from app.private_work.execution_approval import (
    HostExecutionProviderPolicySnapshot,
    WorkerHostExecutionApprovalPort,
)
from app.private_work.execution_approval_audit import (
    NoopHostExecutionApprovalAudit,
)
from app.private_work.execution_approval_lifecycle import (
    CLAIMED_EXECUTION_SETTLEMENT_GRACE_SECONDS,
    lock_execution_approval_private_rows,
    reconcile_locked_execution_approval,
)
from app.private_work.output_delivery_obligation import (
    OutputDeliveryObligationConflict,
    transition_output_delivery_obligation_for_approval_terminal,
)
from app.private_work.privacy_center import PrivacyCenterService
from app.private_work.retention_jobs import project_retention_key
from app.private_work.retention_purge import (
    RetentionExecutionApprovalActive,
    purge_private_scope,
)
from app.private_work.run_repository import (
    PrivateRunExecutionLeaseLost,
    PrivateRunRepository,
)
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.projects.models import ProjectRole
from app.worker.retention import RetentionPurgeJobHandler
from app.worker.service import LeaseLost
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.execution_approvals import (
    ExecutionApprovalOutputDeliveryObligationRow,
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import JobClaim, JobRepository, JobScope
from deerflow.persistence.private_work.model import (
    PrivateArtifactRow,
    PrivateFileRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.host_execution_approval import (
    HostExecutionOutcome,
    HostExecutionPlan,
)
from deerflow.runtime.host_execution_domain import HostExecutionDomainSnapshot


@dataclass(frozen=True)
class _RunJob:
    run: RunRow
    job: JobRow
    attempt: JobAttemptRow


class _DatetimeType(type):
    def __instancecheck__(cls, instance):
        return isinstance(instance, datetime)


class _HostClockOneDayAhead(datetime, metaclass=_DatetimeType):
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(days=1)


class _ApprovalAudit(NoopHostExecutionApprovalAudit):
    def __init__(self) -> None:
        self.terminals: list[tuple[str, str]] = []
        self.run_terminals: list[str] = []

    async def host_execution_approval_terminal(
        self,
        session,
        *,
        source_run_id,
        status,
        **kwargs,
    ) -> None:
        del session, kwargs
        self.terminals.append((source_run_id, status))

    async def run_terminal(self, session, scope, *, run_id, **kwargs) -> None:
        del session, scope, kwargs
        self.run_terminals.append(run_id)

    async def run_cancel_requested(self, *args, **kwargs) -> None:
        del args, kwargs


class _RunAudit:
    async def run_cancel_requested(self, *args, **kwargs) -> None:
        del args, kwargs

    async def run_terminal(self, *args, **kwargs) -> None:
        del args, kwargs


class _Quota:
    async def release_concurrent_run(self, *args, **kwargs) -> None:
        del args, kwargs

    async def release_file(self, *args, **kwargs) -> None:
        del args, kwargs


class _RetentionAudit:
    async def purge_completed(self, *args, **kwargs) -> None:
        del args, kwargs


class _TrackingRetentionRepository:
    def __init__(self) -> None:
        self.purged = False

    async def verify_still_eligible(self, *args, **kwargs) -> None:
        del args, kwargs

    async def physically_purge(self, *args, **kwargs) -> int:
        del args, kwargs
        self.purged = True
        return 1


class _RawCheckpointSaver(BaseCheckpointSaver):
    def __init__(self) -> None:
        super().__init__()
        self.deleted_threads: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted_threads.append(thread_id)


def _private_envelope(command: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "plan": {
            "schema_version": 1,
            "kind": "local_bash",
            "description": "owner-visible description",
            "requested_command": command,
            "effective_command": "secret-effective-command",
            "shell": "/secret/host-shell",
            "cwd": "/secret/host-cwd",
            "timeout_seconds": 60,
            "environment_keys": ["SECRET_ENV_NAME"],
            "agent_path": ["lead"],
        },
        "provider_policy": {
            "secret_provider_policy": "must-not-export",
        },
        "provider_policy_digest": "d" * 64,
    }


def _provider_policy() -> HostExecutionProviderPolicySnapshot:
    return HostExecutionProviderPolicySnapshot(
        provider_use="deerflow.sandbox.local:LocalSandboxProvider",
        host_execution_mode="local_approval_required",
        allow_host_bash=False,
        bash_command_timeout=60,
        approval_max_timeout_seconds=60,
        request_ttl_seconds=300,
        execution_domain_id="mac-primary",
    )


def _execution_domain() -> HostExecutionDomainSnapshot:
    return HostExecutionDomainSnapshot(
        configured_id="mac-primary",
        public_label="Worker host environment",
        os_name="posix",
        sys_platform="darwin",
        machine="arm64",
        device_fingerprint="d" * 64,
        environment_fingerprint="f" * 64,
        euid=501,
        egid=20,
        runtime_base_dir="/srv/actweave-runtime-a",
    )


def _valid_private_envelope(
    command: str,
) -> tuple[dict[str, object], str]:
    plan = HostExecutionPlan(
        source_tool_call_id="placeholder",
        source_run_id="placeholder",
        source_thread_id="placeholder",
        description="final spawn boundary",
        requested_command=command,
        effective_command=command,
        shell="/bin/zsh",
        cwd="/mnt/user-data/workspace",
        timeout_seconds=60,
        agent_path=("lead",),
    )
    policy = _provider_policy()
    execution_domain = _execution_domain()
    return (
        {
            "schema_version": 3,
            "plan": plan.to_private_payload(),
            "provider_policy": policy.to_payload(),
            "provider_policy_digest": policy.digest,
            "execution_domain": execution_domain.to_private_payload(),
        },
        plan.execution_digest,
    )


async def _add_thread(seed, *, owner, thread_id: str) -> None:
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=owner.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )


async def _add_worker(seed) -> uuid.UUID:
    worker_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="approval-private-lifecycle-test",
                capabilities_json=["private_run"],
                max_concurrent_jobs=4,
            )
        )
    return worker_id


async def _add_run_job(
    session,
    *,
    seed,
    owner,
    worker_id: uuid.UUID,
    thread_id: str,
    terminal: bool,
) -> _RunJob:
    now = datetime.now(UTC)
    run_id = str(uuid.uuid4())
    origin_trace_id = uuid.uuid4().hex
    lease_hash = hashlib.sha256(f"lease:{run_id}".encode()).hexdigest()
    run = RunRow(
        run_id=run_id,
        thread_id=thread_id,
        assistant_id=str(seed.project_agent_id),
        owner_user_id=str(owner.user_id),
        status="success" if terminal else "running",
        model_name="test-model",
        multitask_strategy="reject",
        metadata_json={},
        kwargs_json={},
        origin_trace_id=origin_trace_id,
        project_id=owner.project_id,
        finalization_status="complete" if terminal else "pending",
        execution_lease_token_hash=None if terminal else lease_hash,
        execution_lease_expires_at=(None if terminal else now + timedelta(minutes=5)),
        execution_heartbeat_at=None if terminal else now,
        execution_started_at=now,
    )
    session.add(run)
    await session.flush()
    job = JobRow(
        job_type="private_run",
        project_id=owner.project_id,
        owner_user_id=str(owner.user_id),
        run_id=run_id,
        origin_trace_id=origin_trace_id,
        idempotency_key=hashlib.sha256(f"job:{run_id}".encode()).hexdigest(),
        status="succeeded" if terminal else "running",
        max_attempts=3,
        attempt_count=1,
        lease_owner_id=None if terminal else worker_id,
        lease_token_hash=None if terminal else lease_hash,
        lease_expires_at=None if terminal else now + timedelta(minutes=5),
        heartbeat_at=None if terminal else now,
        retry_safety="safe",
        started_at=now,
        completed_at=now if terminal else None,
    )
    session.add(job)
    await session.flush()
    run.job_id = job.id
    attempt = JobAttemptRow(
        job_id=job.id,
        attempt_number=1,
        worker_id=worker_id,
        lease_token_hash=lease_hash,
        started_at=now,
        heartbeat_at=now,
        finished_at=now if terminal else None,
        outcome="succeeded" if terminal else None,
    )
    session.add(attempt)
    await session.flush()
    return _RunJob(run, job, attempt)


async def _add_approval(
    session,
    *,
    owner,
    thread_id: str,
    source: _RunJob,
    continuation: _RunJob | None,
    status: str,
    command: str,
    incomplete_result: bool = False,
) -> tuple[ExecutionApprovalRequestRow, ExecutionApprovalResultReceiptRow | None]:
    created_at = datetime.now(UTC) - timedelta(minutes=1)
    decision = None if status in {"staged", "pending"} else "allow_once"
    terminal = status not in {"staged", "pending", "approved", "claimed"}
    claimed = status in {"claimed", "finished", "launch_failed", "unknown"}
    execution_domain_affinity = _execution_domain().affinity
    if continuation is not None:
        continuation.job.execution_domain_affinity = execution_domain_affinity
        # The approval FK includes the continuation Job's execution-domain
        # affinity. Flush the fixture update before inserting the dependent row
        # because these test objects do not declare an ORM relationship.
        await session.flush()
    row = ExecutionApprovalRequestRow(
        project_id=owner.project_id,
        owner_user_id=str(owner.user_id),
        thread_id=thread_id,
        source_run_id=source.run.run_id,
        source_job_id=source.job.id,
        source_job_attempt_id=source.attempt.id,
        source_agent_path=["lead"],
        tool_call_id=f"call-{uuid.uuid4().hex}",
        kind="local_bash",
        command_digest="a" * 64,
        execution_domain_affinity=execution_domain_affinity,
        command_private_json=_private_envelope(command),
        status=status,
        version=2,
        decision=decision,
        decision_idempotency_key=None if decision is None else "b" * 64,
        decision_request_digest=None if decision is None else "c" * 64,
        decided_by_user_id=None if decision is None else str(owner.user_id),
        decided_at=None if decision is None else created_at + timedelta(seconds=5),
        continuation_run_id=(None if continuation is None else continuation.run.run_id),
        continuation_job_id=(None if continuation is None else continuation.job.id),
        execution_job_attempt_id=(None if not claimed or continuation is None else continuation.attempt.id),
        claimed_at=(None if not claimed else created_at + timedelta(seconds=10)),
        spawn_authorized_at=(created_at + timedelta(seconds=15) if status == "finished" else None),
        expires_at=created_at + timedelta(minutes=10),
        terminal_at=(created_at + timedelta(seconds=20) if terminal else None),
        created_at=created_at,
        updated_at=(created_at + timedelta(seconds=20) if terminal else created_at + timedelta(seconds=10)),
    )
    session.add(row)
    await session.flush()
    receipt = None
    if status in {"finished", "launch_failed"}:
        assert continuation is not None
        result_json = (
            {
                "schema_version": 1,
                "status": "finished",
                "exit_code": 0,
                "stdout": "owner-result",
            }
            if incomplete_result
            else {
                "schema_version": 1,
                "status": "finished",
                "exit_code": 0,
                "stdout": "owner-result",
                "stderr": None,
                "result_text": "owner-result",
                "reason_code": None,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "result_text_truncated": False,
            }
        )
        receipt = ExecutionApprovalResultReceiptRow(
            approval_id=row.id,
            project_id=owner.project_id,
            owner_user_id=str(owner.user_id),
            thread_id=thread_id,
            execution_job_id=continuation.job.id,
            execution_job_attempt_id=continuation.attempt.id,
            outcome="finished",
            exit_code=0,
            result_digest="e" * 64,
            result_private_json=result_json,
            created_at=created_at + timedelta(seconds=20),
        )
        session.add(receipt)
        await session.flush()
    return row, receipt


async def _add_output_delivery_obligation(
    session,
    *,
    approval: ExecutionApprovalRequestRow,
    continuation: _RunJob | None,
    status: str,
) -> ExecutionApprovalOutputDeliveryObligationRow:
    now = datetime.now(UTC)
    assigned = status != "deferred"
    has_intent = status in {"intent_recorded", "delivered"}
    payload = {
        "schema_version": 1,
        "logical_paths": ["outputs/cancel-path.txt"],
    }
    artifact_id = None
    if status == "delivered":
        assert continuation is not None
        file_row = PrivateFileRow(
            project_id=approval.project_id,
            owner_user_id=approval.owner_user_id,
            thread_id=approval.thread_id,
            kind="output",
            logical_path="outputs/cancel-path.txt",
            media_type="text/plain",
            size=1,
            sha256="9" * 64,
            status="ready",
            version=1,
            created_by_run_id=continuation.run.run_id,
            created_at=now,
            updated_at=now,
        )
        session.add(file_row)
        await session.flush()
        artifact = PrivateArtifactRow(
            project_id=approval.project_id,
            owner_user_id=approval.owner_user_id,
            thread_id=approval.thread_id,
            run_id=continuation.run.run_id,
            file_id=file_row.id,
            display_name="cancel-path.txt",
            media_type="text/plain",
            artifact_metadata={"logical_path": "outputs/cancel-path.txt"},
            created_at=now,
        )
        session.add(artifact)
        await session.flush()
        artifact_id = artifact.id
    obligation_values = {
        "approval_id": approval.id,
        "project_id": approval.project_id,
        "owner_user_id": approval.owner_user_id,
        "thread_id": approval.thread_id,
        "mode": "any_one",
        "status": status,
        "continuation_run_id": (continuation.run.run_id if assigned and continuation is not None else None),
        "continuation_job_id": (continuation.job.id if assigned and continuation is not None else None),
        "satisfied_artifact_id": artifact_id,
        "version": 1,
        "assigned_at": now if assigned else None,
        "intent_recorded_at": now if has_intent else None,
        "terminal_at": now if status == "delivered" else None,
        "created_at": now,
        "updated_at": now,
    }
    if has_intent:
        obligation_values.update(
            {
                "intent_tool_call_id": "call-present",
                "intent_digest": hashlib.sha256(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode(),
                ).hexdigest(),
                "intent_private_json": payload,
            }
        )
    obligation = ExecutionApprovalOutputDeliveryObligationRow(
        **obligation_values,
    )
    session.add(obligation)
    await session.flush()
    return obligation


def _claimed_execution_port(
    seed,
    *,
    thread_id: str,
    continuation: _RunJob,
    approval: ExecutionApprovalRequestRow,
    audit: _ApprovalAudit | None = None,
) -> WorkerHostExecutionApprovalPort:
    return WorkerHostExecutionApprovalPort(
        seed.factory,
        context=seed.owner_a,
        claim=JobClaim(
            job_id=continuation.job.id,
            attempt_id=continuation.attempt.id,
            lease_token=f"lease:{continuation.run.run_id}",
            job_type="private_run",
            scope=JobScope(
                seed.owner_a.project_id,
                str(seed.owner_a.user_id),
            ),
            run_id=continuation.run.run_id,
            occurrence_id=None,
            retry_safety="unknown",
            cancel_requested=False,
            origin_trace_id=continuation.job.origin_trace_id,
            execution_domain_affinity=(continuation.job.execution_domain_affinity),
        ),
        thread_id=thread_id,
        request_ttl_seconds=300,
        continuation_approval_id=str(approval.id),
        provider_policy=_provider_policy(),
        execution_domain=_execution_domain(),
        audit=audit or _ApprovalAudit(),
    )


async def _terminal_approval_environment(seed, owner, worker_id, *, command):
    thread_id = str(uuid.uuid4())
    await _add_thread(seed, owner=owner, thread_id=thread_id)
    async with seed.factory() as session, session.begin():
        source = await _add_run_job(
            session,
            seed=seed,
            owner=owner,
            worker_id=worker_id,
            thread_id=thread_id,
            terminal=True,
        )
        continuation = await _add_run_job(
            session,
            seed=seed,
            owner=owner,
            worker_id=worker_id,
            thread_id=thread_id,
            terminal=True,
        )
        approval, receipt = await _add_approval(
            session,
            owner=owner,
            thread_id=thread_id,
            source=source,
            continuation=continuation,
            status="finished",
            command=command,
        )
    return thread_id, source, continuation, approval, receipt


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("deleted_coordinate", ["source", "continuation"])
async def test_run_delete_removes_exact_terminal_approval_pair_and_keeps_audit(
    migrated_postgres_database_url: str,
    deleted_coordinate: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    approval_audit = _ApprovalAudit()
    try:
        worker_id = await _add_worker(seed)
        thread_id, source, continuation, approval, receipt = await _terminal_approval_environment(
            seed,
            seed.owner_a,
            worker_id,
            command="python owner-a.py",
        )
        _, _, _, other_approval, _ = await _terminal_approval_environment(
            seed,
            seed.owner_b,
            worker_id,
            command="python owner-b.py",
        )
        audit_id = uuid.uuid4()
        async with seed.factory() as session, session.begin():
            session.add(
                AuditLogRow(
                    id=audit_id,
                    actor_process="worker",
                    project_id=seed.owner_a.project_id,
                    action="host_execution.approval_terminal",
                    target_kind="run",
                    target_ref_key_id="private-lifecycle-test",
                    target_ref_hmac="f" * 64,
                    outcome="success",
                    metadata_json={"status": "finished"},
                )
            )

        target = source.run if deleted_coordinate == "source" else continuation.run
        service = PrivateRunService(
            seed.factory,
            quota=_Quota(),
            audit=_RunAudit(),
            approval_audit=approval_audit,
        )
        await service.delete(seed.owner_a, thread_id, target.run_id)

        async with seed.factory() as session:
            assert await session.get(ExecutionApprovalRequestRow, approval.id) is None
            assert receipt is not None
            assert await session.get(ExecutionApprovalResultReceiptRow, receipt.id) is None
            assert await session.get(ExecutionApprovalRequestRow, other_approval.id) is not None
            assert await session.get(AuditLogRow, audit_id) is not None
            deleted = await session.get(RunRow, target.run_id)
            assert deleted is not None and deleted.status == "deleted"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_delete_rejects_live_claimed_host_execution(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    approval_audit = _ApprovalAudit()
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="claimed",
                command="python live.py",
            )

        service = PrivateRunService(
            seed.factory,
            quota=_Quota(),
            audit=_RunAudit(),
            approval_audit=approval_audit,
        )
        monkeypatch.setattr(
            "app.private_work.run_service.datetime",
            _HostClockOneDayAhead,
        )
        with pytest.raises(PrivateWorkConflict):
            await service.delete(seed.owner_a, thread_id, source.run.run_id)

        async with seed.factory() as session:
            retained = await session.get(ExecutionApprovalRequestRow, approval.id)
            source_row = await session.get(RunRow, source.run.run_id)
            assert retained is not None and retained.status == "claimed"
            assert source_row is not None and source_row.status == "success"
        assert approval_audit.terminals == []
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_unknown_completion_uses_database_clock_before_releasing_claim_gate(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    approval_audit = _ApprovalAudit()
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="claimed",
                command="python unknown-before-deadline.py",
            )
            # This test isolates DB-clock settlement rather than frozen-plan
            # materialization; seed the durable pre-spawn prerequisite.
            approval.spawn_authorized_at = approval.claimed_at

        monkeypatch.setattr(
            "app.private_work.execution_approval._now",
            lambda: datetime.now(UTC) + timedelta(days=1),
        )
        completion_port = _claimed_execution_port(
            seed,
            thread_id=thread_id,
            continuation=continuation,
            approval=approval,
            audit=approval_audit,
        )
        await completion_port.complete_host_execution(
            str(approval.id),
            HostExecutionOutcome(
                status="unknown",
                reason_code="termination_unproven",
            ),
        )

        async with seed.factory() as session:
            retained = await session.get(ExecutionApprovalRequestRow, approval.id)
            receipt = await session.scalar(
                sa.select(ExecutionApprovalResultReceiptRow).where(
                    ExecutionApprovalResultReceiptRow.approval_id == approval.id,
                )
            )
            assert retained is not None and retained.status == "claimed"
            assert retained.terminal_at is None
            assert receipt is None
        assert approval_audit.terminals == []
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_thread_delete_removes_terminal_approval_pair_and_keeps_audit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    approval_audit = _ApprovalAudit()
    raw = _RawCheckpointSaver()
    try:
        worker_id = await _add_worker(seed)
        thread_id, _, _, approval, receipt = await _terminal_approval_environment(
            seed,
            seed.owner_a,
            worker_id,
            command="python delete-thread.py",
        )
        audit_id = uuid.uuid4()
        async with seed.factory() as session, session.begin():
            session.add(
                AuditLogRow(
                    id=audit_id,
                    actor_process="worker",
                    project_id=seed.owner_a.project_id,
                    action="host_execution.approval_terminal",
                    target_kind="run",
                    target_ref_key_id="thread-delete-private-lifecycle",
                    target_ref_hmac="8" * 64,
                    outcome="success",
                    metadata_json={"status": "finished"},
                )
            )

        saver = ProjectScopedCheckpointer(
            raw,
            seed.factory,
            quota=_Quota(),
            approval_audit=approval_audit,
        ).for_context(seed.owner_a)
        await saver.adelete_thread(thread_id, expected_version=1)

        async with seed.factory() as session:
            assert await session.get(ExecutionApprovalRequestRow, approval.id) is None
            assert receipt is not None
            assert await session.get(ExecutionApprovalResultReceiptRow, receipt.id) is None
            assert await session.get(AuditLogRow, audit_id) is not None
            thread = await session.get(ThreadMetaRow, thread_id)
            assert thread is not None and thread.deleted_at is not None
        assert raw.deleted_threads == [thread_id]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_thread_delete_rejects_recent_claim_with_expired_db_lease(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    approval_audit = _ApprovalAudit()
    raw = _RawCheckpointSaver()
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="claimed",
                command="python expired-lease-still-running.py",
            )
            expired_at = datetime.now(UTC) - timedelta(seconds=1)
            continuation.job.lease_expires_at = expired_at
            continuation.run.execution_lease_expires_at = expired_at

        saver = ProjectScopedCheckpointer(
            raw,
            seed.factory,
            quota=_Quota(),
            approval_audit=approval_audit,
        ).for_context(seed.owner_a)
        monkeypatch.setattr(
            "app.private_work.checkpointer.datetime",
            _HostClockOneDayAhead,
        )
        with pytest.raises(PrivateWorkConflict):
            await saver.adelete_thread(thread_id, expected_version=1)

        async with seed.factory() as session:
            retained = await session.get(ExecutionApprovalRequestRow, approval.id)
            thread = await session.get(ThreadMetaRow, thread_id)
            assert retained is not None and retained.status == "claimed"
            assert thread is not None and thread.deleted_at is None
        assert approval_audit.terminals == []
        assert raw.deleted_threads == []
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_private_lifecycle_final_lock_refreshes_cached_finished_row(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="claimed",
                command="python identity-map.py",
            )

        async with seed.factory() as stale_session, stale_session.begin():
            cached = await stale_session.get(
                ExecutionApprovalRequestRow,
                approval.id,
            )
            assert cached is not None and cached.status == "claimed"

            finished_at = datetime.now(UTC)
            async with seed.factory() as completion, completion.begin():
                fresh = await completion.get(
                    ExecutionApprovalRequestRow,
                    approval.id,
                    with_for_update=True,
                )
                assert fresh is not None
                fresh.spawn_authorized_at = fresh.claimed_at
                fresh.status = "finished"
                fresh.version += 1
                fresh.terminal_at = finished_at
                fresh.updated_at = finished_at
                completion.add(
                    ExecutionApprovalResultReceiptRow(
                        approval_id=fresh.id,
                        project_id=fresh.project_id,
                        owner_user_id=fresh.owner_user_id,
                        thread_id=fresh.thread_id,
                        execution_job_id=continuation.job.id,
                        execution_job_attempt_id=continuation.attempt.id,
                        outcome="finished",
                        exit_code=0,
                        result_digest="7" * 64,
                        result_private_json={
                            "schema_version": 1,
                            "status": "finished",
                            "exit_code": 0,
                            "stdout": "done",
                            "stderr": None,
                            "result_text": None,
                            "reason_code": None,
                            "stdout_truncated": False,
                            "stderr_truncated": False,
                            "result_text_truncated": False,
                        },
                        created_at=finished_at,
                    )
                )

            locked = await lock_execution_approval_private_rows(
                stale_session,
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                thread_id=thread_id,
                approval_id=approval.id,
            )
            assert len(locked.rows) == 1
            assert locked.rows[0] is cached
            assert locked.rows[0].status == "finished"
            assert locked.claimed_absolute_deadlines == {}
            await reconcile_locked_execution_approval(
                stale_session,
                locked.rows[0],
                now=finished_at + timedelta(minutes=10),
                audit=_ApprovalAudit(),
            )
            assert locked.rows[0].status == "finished"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_claim_revalidates_lease_after_waiting_for_approval_lock(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    first_lease_check_complete = asyncio.Event()
    original_assert = PrivateRunRepository.assert_execution_active
    calls = 0

    async def observed_assert(self, *args, **kwargs):
        nonlocal calls
        result = await original_assert(self, *args, **kwargs)
        calls += 1
        if calls == 1:
            first_lease_check_complete.set()
        return result

    monkeypatch.setattr(
        PrivateRunRepository,
        "assert_execution_active",
        observed_assert,
    )
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        lease_expires_at = datetime.now(UTC) + timedelta(milliseconds=400)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            continuation.job.lease_expires_at = lease_expires_at
            continuation.run.execution_lease_expires_at = lease_expires_at
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="approved",
                command="python must-not-launch.py",
            )

        lease_token = f"lease:{continuation.run.run_id}"
        port = WorkerHostExecutionApprovalPort(
            seed.factory,
            context=seed.owner_a,
            claim=JobClaim(
                job_id=continuation.job.id,
                attempt_id=continuation.attempt.id,
                lease_token=lease_token,
                job_type="private_run",
                scope=JobScope(
                    seed.owner_a.project_id,
                    str(seed.owner_a.user_id),
                ),
                run_id=continuation.run.run_id,
                occurrence_id=None,
                retry_safety="safe",
                cancel_requested=False,
                origin_trace_id=continuation.job.origin_trace_id,
                execution_domain_affinity=(continuation.job.execution_domain_affinity),
            ),
            thread_id=thread_id,
            request_ttl_seconds=300,
            continuation_approval_id=str(approval.id),
            provider_policy=_provider_policy(),
            execution_domain=_execution_domain(),
            audit=_ApprovalAudit(),
        )

        async with seed.factory() as blocker, blocker.begin():
            await blocker.execute(sa.text("SET LOCAL statement_timeout = '2s'"))
            locked = await blocker.get(
                ExecutionApprovalRequestRow,
                approval.id,
                with_for_update=True,
            )
            assert locked is not None
            claim_task = asyncio.create_task(port.claim_frozen_host_execution())
            await asyncio.wait_for(first_lease_check_complete.wait(), timeout=2)
            await asyncio.sleep(0.5)
        result = await asyncio.wait_for(claim_task, timeout=3)

        assert result.status == "denied"
        assert result.reason_code == "approval_claim_unavailable"
        assert calls == 1
        async with seed.factory() as session:
            retained = await session.get(ExecutionApprovalRequestRow, approval.id)
            job = await session.get(JobRow, continuation.job.id)
            assert retained is not None and retained.status == "approved"
            assert retained.claimed_at is None
            assert job is not None and job.retry_safety == "safe"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ["assert_execution_active", "mark_execution_side_effect_unknown"],
)
async def test_side_effect_boundary_samples_time_after_job_lock(
    migrated_postgres_database_url: str,
    method_name: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        lease_expires_at = datetime.now(UTC) + timedelta(milliseconds=300)
        async with seed.factory() as session, session.begin():
            running = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            running.job.lease_expires_at = lease_expires_at
            running.run.execution_lease_expires_at = lease_expires_at

        lease_token = f"lease:{running.run.run_id}"

        async def invoke_boundary() -> None:
            async with seed.factory() as session, session.begin():
                await session.execute(sa.text("SET LOCAL statement_timeout = '2s'"))
                method = getattr(PrivateRunRepository(session), method_name)
                await method(
                    scope=seed.owner_a.resource_scope,
                    run_id=running.run.run_id,
                    job_id=running.job.id,
                    lease_token=lease_token,
                )

        async with seed.factory() as blocker, blocker.begin():
            await blocker.execute(sa.text("SET LOCAL statement_timeout = '2s'"))
            await blocker.scalar(sa.select(JobRow.id).where(JobRow.id == running.job.id).with_for_update(of=JobRow))
            boundary_task = asyncio.create_task(invoke_boundary())
            await asyncio.sleep(0.45)
            assert not boundary_task.done()
        with pytest.raises(PrivateRunExecutionLeaseLost):
            await asyncio.wait_for(boundary_task, timeout=3)

        async with seed.factory() as session:
            job = await session.get(JobRow, running.job.id)
            assert job is not None and job.retry_safety == "safe"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retention_commit_revalidates_lease_after_scope_lock_wait(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    tracking_repository = _TrackingRetentionRepository()
    try:
        worker_id = await _add_worker(seed)
        lease_token = f"retention-lease-{uuid.uuid4().hex}"
        lease_token_hash = hashlib.sha256(lease_token.encode()).hexdigest()
        deletion_effective_at = datetime.now(UTC) - timedelta(minutes=1)
        async with seed.factory() as session, session.begin():
            project = await session.get(ProjectRow, seed.owner_a.project_id)
            assert project is not None
            project.deletion_effective_at = deletion_effective_at
            job = JobRow(
                job_type="retention_purge",
                project_id=seed.owner_a.project_id,
                owner_user_id=None,
                idempotency_key=project_retention_key(
                    seed.owner_a.project_id,
                    deletion_effective_at,
                ),
                status="running",
                max_attempts=5,
                attempt_count=1,
                lease_owner_id=worker_id,
                lease_token_hash=lease_token_hash,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                heartbeat_at=datetime.now(UTC),
                retry_safety="safe",
                started_at=datetime.now(UTC),
            )
            session.add(job)
            await session.flush()
            attempt = JobAttemptRow(
                job_id=job.id,
                attempt_number=1,
                worker_id=worker_id,
                lease_token_hash=lease_token_hash,
                started_at=datetime.now(UTC),
                heartbeat_at=datetime.now(UTC),
            )
            session.add(attempt)
            await session.flush()

        claim = JobClaim(
            job_id=job.id,
            attempt_id=attempt.id,
            lease_token=lease_token,
            job_type="retention_purge",
            scope=JobScope(seed.owner_a.project_id, None),
            run_id=None,
            occurrence_id=None,
            retry_safety="safe",
            cancel_requested=False,
        )
        handler = object.__new__(RetentionPurgeJobHandler)
        handler._sessions = seed.factory
        handler._audit = _RetentionAudit()
        handler._approval_audit = _ApprovalAudit()
        handler._quota = _Quota()
        handler._job_repository_builder = JobRepository
        handler._repository = tracking_repository
        handler._clock = lambda: datetime.now(UTC) - timedelta(days=1)
        handler._retry_initial_seconds = 2
        handler._retry_max_seconds = 300
        settlement = await handler(claim, object())

        async with seed.factory() as blocker, blocker.begin():
            await blocker.execute(sa.text("SET LOCAL statement_timeout = '2s'"))
            await blocker.scalar(sa.select(ProjectRow.id).where(ProjectRow.id == seed.owner_a.project_id).with_for_update(of=ProjectRow))
            lease_expires_at = datetime.now(UTC) + timedelta(milliseconds=400)
            async with seed.factory() as updater, updater.begin():
                await updater.execute(sa.update(JobRow).where(JobRow.id == job.id).values(lease_expires_at=lease_expires_at))
            commit_task = asyncio.create_task(settlement.commit())
            await asyncio.sleep(0.55)
            assert not commit_task.done()

        with pytest.raises(LeaseLost):
            await asyncio.wait_for(commit_task, timeout=3)
        assert not tracking_repository.purged

        async with seed.factory() as session:
            persisted_job = await session.get(JobRow, job.id)
            persisted_attempt = await session.get(JobAttemptRow, attempt.id)
            assert persisted_job is not None
            assert persisted_job.status == "running"
            assert persisted_job.lease_token_hash == lease_token_hash
            assert persisted_attempt is not None
            assert persisted_attempt.outcome is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup_kind",
    ["run_delete", "thread_delete", "retention"],
)
async def test_completion_prefix_prevents_cleanup_deadlock_and_preserves_receipt(
    migrated_postgres_database_url: str,
    cleanup_kind: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    prefix_locked = asyncio.Event()
    release_completion = asyncio.Event()

    class _PausedCompletionPort(WorkerHostExecutionApprovalPort):
        async def _lock_completion_scope_shells(self, session) -> None:
            await session.execute(sa.text("SET LOCAL statement_timeout = '2s'"))
            await super()._lock_completion_scope_shells(session)
            prefix_locked.set()
            await release_completion.wait()

    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="claimed",
                command="python concurrent-completion.py",
            )
            # The lock-order test uses a minimal command fixture. Seed the
            # already-proven spawn marker; CAS behavior has a dedicated test.
            approval.spawn_authorized_at = approval.claimed_at

        lease_token = f"lease:{continuation.run.run_id}"
        claim = JobClaim(
            job_id=continuation.job.id,
            attempt_id=continuation.attempt.id,
            lease_token=lease_token,
            job_type="private_run",
            scope=JobScope(
                seed.owner_a.project_id,
                str(seed.owner_a.user_id),
            ),
            run_id=continuation.run.run_id,
            occurrence_id=None,
            retry_safety="unknown",
            cancel_requested=False,
            origin_trace_id=continuation.job.origin_trace_id,
            execution_domain_affinity=(continuation.job.execution_domain_affinity),
        )
        completion_port = _PausedCompletionPort(
            seed.factory,
            context=seed.owner_a,
            claim=claim,
            thread_id=thread_id,
            request_ttl_seconds=300,
            continuation_approval_id=str(approval.id),
            provider_policy=_provider_policy(),
            execution_domain=_execution_domain(),
            audit=_ApprovalAudit(),
        )
        completion_task = asyncio.create_task(
            completion_port.complete_host_execution(
                str(approval.id),
                HostExecutionOutcome(
                    status="finished",
                    exit_code=0,
                    stdout="completed-before-cleanup",
                ),
            )
        )
        await asyncio.wait_for(prefix_locked.wait(), timeout=2)

        if cleanup_kind == "run_delete":
            service = PrivateRunService(
                seed.factory,
                quota=_Quota(),
                audit=_RunAudit(),
                approval_audit=_ApprovalAudit(),
            )
            cleanup_task = asyncio.create_task(
                service.delete(
                    seed.owner_a,
                    thread_id,
                    source.run.run_id,
                )
            )
            expected_error = PrivateWorkConflict
        elif cleanup_kind == "thread_delete":
            saver = ProjectScopedCheckpointer(
                _RawCheckpointSaver(),
                seed.factory,
                quota=_Quota(),
                approval_audit=_ApprovalAudit(),
            ).for_context(seed.owner_a)
            cleanup_task = asyncio.create_task(saver.adelete_thread(thread_id, expected_version=1))
            expected_error = PrivateWorkConflict
        else:

            async def purge_after_prefix() -> None:
                async with seed.factory() as session, session.begin():
                    await session.execute(sa.text("SET LOCAL statement_timeout = '2s'"))
                    await session.scalar(sa.select(ProjectRow.id).where(ProjectRow.id == seed.owner_a.project_id).with_for_update(of=ProjectRow))
                    await session.scalar(
                        sa.select(ProjectMembershipRow.project_id)
                        .where(
                            ProjectMembershipRow.project_id == seed.owner_a.project_id,
                            ProjectMembershipRow.user_id == str(seed.owner_a.user_id),
                        )
                        .with_for_update(of=ProjectMembershipRow)
                    )
                    await purge_private_scope(
                        session,
                        project_id=seed.owner_a.project_id,
                        owner_user_id=str(seed.owner_a.user_id),
                        quota=_Quota(),
                        approval_audit=_ApprovalAudit(),
                    )

            cleanup_task = asyncio.create_task(purge_after_prefix())
            expected_error = RetentionExecutionApprovalActive

        await asyncio.sleep(0.05)
        release_completion.set()
        await asyncio.wait_for(completion_task, timeout=5)
        with pytest.raises(expected_error):
            await asyncio.wait_for(cleanup_task, timeout=5)

        async with seed.factory() as session:
            persisted = await session.get(
                ExecutionApprovalRequestRow,
                approval.id,
            )
            receipt = await session.scalar(
                sa.select(ExecutionApprovalResultReceiptRow).where(
                    ExecutionApprovalResultReceiptRow.approval_id == approval.id,
                )
            )
            assert persisted is not None and persisted.status == "finished"
            assert receipt is not None
            assert receipt.result_private_json["stdout"] == ("completed-before-cleanup")
            if cleanup_kind == "thread_delete":
                thread = await session.get(ThreadMetaRow, thread_id)
                assert thread is not None and thread.deleted_at is None
    finally:
        release_completion.set()
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_completion_persists_receipt_after_membership_has_left(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="claimed",
                command="python finish-after-removal.py",
            )
            approval.spawn_authorized_at = approval.claimed_at
            membership = await session.scalar(
                sa.select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == seed.owner_a.project_id,
                    ProjectMembershipRow.user_id == str(seed.owner_a.user_id),
                )
                .with_for_update(of=ProjectMembershipRow)
            )
            assert membership is not None
            membership.status = "left"
            membership.ended_at = datetime.now(UTC)
            membership.retention_until = datetime.now(UTC) + timedelta(days=1)

        lease_token = f"lease:{continuation.run.run_id}"
        await WorkerHostExecutionApprovalPort(
            seed.factory,
            context=seed.owner_a,
            claim=JobClaim(
                job_id=continuation.job.id,
                attempt_id=continuation.attempt.id,
                lease_token=lease_token,
                job_type="private_run",
                scope=JobScope(
                    seed.owner_a.project_id,
                    str(seed.owner_a.user_id),
                ),
                run_id=continuation.run.run_id,
                occurrence_id=None,
                retry_safety="unknown",
                cancel_requested=False,
                origin_trace_id=continuation.job.origin_trace_id,
                execution_domain_affinity=(continuation.job.execution_domain_affinity),
            ),
            thread_id=thread_id,
            request_ttl_seconds=300,
            continuation_approval_id=str(approval.id),
            provider_policy=_provider_policy(),
            execution_domain=_execution_domain(),
            audit=_ApprovalAudit(),
        ).complete_host_execution(
            str(approval.id),
            HostExecutionOutcome(
                status="finished",
                exit_code=0,
                stdout="durable-after-membership-left",
            ),
        )

        async with seed.factory() as session:
            persisted = await session.get(
                ExecutionApprovalRequestRow,
                approval.id,
            )
            receipt = await session.scalar(
                sa.select(ExecutionApprovalResultReceiptRow).where(
                    ExecutionApprovalResultReceiptRow.approval_id == approval.id,
                )
            )
            assert persisted is not None and persisted.status == "finished"
            assert receipt is not None
            assert receipt.result_private_json["stdout"] == ("durable-after-membership-left")
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_owner_retention_purge_removes_approval_payload_but_keeps_audit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        worker_id = await _add_worker(seed)
        _, _, _, approval, receipt = await _terminal_approval_environment(
            seed,
            seed.owner_a,
            worker_id,
            command="python purge-owner-a.py",
        )
        _, _, _, other_approval, _ = await _terminal_approval_environment(
            seed,
            seed.owner_b,
            worker_id,
            command="python keep-owner-b.py",
        )
        audit_id = uuid.uuid4()
        async with seed.factory() as session, session.begin():
            session.add(
                AuditLogRow(
                    id=audit_id,
                    actor_process="worker",
                    project_id=seed.owner_a.project_id,
                    action="host_execution.approval_terminal",
                    target_kind="run",
                    target_ref_key_id="private-lifecycle-test",
                    target_ref_hmac="9" * 64,
                    outcome="success",
                    metadata_json={"status": "finished"},
                )
            )

        async with seed.factory() as session, session.begin():
            await purge_private_scope(
                session,
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                quota=_Quota(),
                approval_audit=_ApprovalAudit(),
            )

        async with seed.factory() as session:
            assert await session.get(ExecutionApprovalRequestRow, approval.id) is None
            assert receipt is not None
            assert await session.get(ExecutionApprovalResultReceiptRow, receipt.id) is None
            assert await session.get(ExecutionApprovalRequestRow, other_approval.id) is not None
            assert await session.get(AuditLogRow, audit_id) is not None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_owner_retention_purge_rejects_live_claimed_execution_atomically(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="claimed",
                command="python still-live.py",
            )

        monkeypatch.setattr(
            "app.private_work.retention_purge.datetime",
            _HostClockOneDayAhead,
        )
        with pytest.raises(RetentionExecutionApprovalActive):
            async with seed.factory() as session, session.begin():
                await purge_private_scope(
                    session,
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    quota=_Quota(),
                    approval_audit=_ApprovalAudit(),
                )

        async with seed.factory() as session:
            retained = await session.get(ExecutionApprovalRequestRow, approval.id)
            source_row = await session.get(RunRow, source.run.run_id)
            assert retained is not None and retained.status == "claimed"
            assert source_row is not None and source_row.first_human_message is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_privacy_export_is_owner_scoped_and_handles_sparse_result_json(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="finished",
                command="python exported-owner-command.py",
                incomplete_result=True,
            )
            membership = await session.scalar(
                sa.select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == seed.owner_a.project_id,
                    ProjectMembershipRow.user_id == str(seed.owner_a.user_id),
                )
                .with_for_update(of=ProjectMembershipRow)
            )
            assert membership is not None
            membership.status = "left"
            membership.ended_at = datetime.now(UTC)
            membership.retention_until = datetime.now(UTC) + timedelta(days=1)

        _, _, _, other_approval, _ = await _terminal_approval_environment(
            seed,
            seed.owner_b,
            worker_id,
            command="python must-not-export-owner-b.py",
        )

        async with seed.factory() as session:
            stream = await PrivacyCenterService(session).open_case_export(
                seed.owner_a.user_id,
                seed.owner_a.project_id,
                now=datetime.now(UTC),
            )
            records = [json.loads(line) async for line in stream]

        assert records[0]["schema_version"] == 3
        plans = [row["data"] for row in records if row["record_type"] == "execution_approval_plan"]
        results = [row["data"] for row in records if row["record_type"] == "execution_approval_result"]
        assert [row["approval_id"] for row in plans] == [str(approval.id)]
        assert plans[0]["requested_command"] == ("python exported-owner-command.py")
        assert results[0]["stdout"] == "owner-result"
        assert results[0]["stderr"] is None
        assert results[0]["result_text"] is None

        rendered = json.dumps(records, ensure_ascii=False)
        assert str(other_approval.id) not in rendered
        assert "must-not-export-owner-b" not in rendered
        for forbidden in (
            "secret-effective-command",
            "/secret/host-shell",
            "/secret/host-cwd",
            "SECRET_ENV_NAME",
            "must-not-export",
            "provider_policy_digest",
            "command_digest",
            "result_digest",
            "decided_by_user_id",
        ):
            assert forbidden not in rendered
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_final_spawn_authorization_rejects_capability_downgrade(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="claimed",
                command="python capability-race.py",
            )
            (
                approval.command_private_json,
                approval.command_digest,
            ) = _valid_private_envelope("python capability-race.py")
            approval.claimed_at = datetime.now(UTC)
            approval.updated_at = approval.claimed_at

        port = _claimed_execution_port(
            seed,
            thread_id=thread_id,
            continuation=continuation,
            approval=approval,
        )
        assert await port.authorize_claimed_host_execution_spawn(
            str(approval.id),
        )

        async with seed.factory() as session, session.begin():
            membership = await session.scalar(
                sa.select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == seed.owner_a.project_id,
                    ProjectMembershipRow.user_id == str(seed.owner_a.user_id),
                )
                .with_for_update(of=ProjectMembershipRow)
            )
            assert membership is not None
            membership.role = ProjectRole.EDITOR
            membership.version += 1
            await session.flush()
            authorization = asyncio.create_task(
                port.authorize_claimed_host_execution_spawn(
                    str(approval.id),
                )
            )
            await asyncio.sleep(0.05)
            assert not authorization.done()

        assert not await asyncio.wait_for(authorization, timeout=2)
        async with seed.factory() as session:
            retained = await session.get(
                ExecutionApprovalRequestRow,
                approval.id,
            )
            assert retained is not None and retained.status == "claimed"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_final_spawn_authorization_is_a_durable_one_shot_cas(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="claimed",
                command="python one-shot.py",
            )
            (
                approval.command_private_json,
                approval.command_digest,
            ) = _valid_private_envelope("python one-shot.py")
            approval.claimed_at = datetime.now(UTC)
            approval.updated_at = approval.claimed_at

        port = _claimed_execution_port(
            seed,
            thread_id=thread_id,
            continuation=continuation,
            approval=approval,
        )
        for unauthorized_outcome in (
            HostExecutionOutcome(
                status="finished",
                exit_code=0,
                result_text="must not persist",
            ),
            HostExecutionOutcome(
                status="unknown",
                reason_code="must_not_persist",
            ),
        ):
            with pytest.raises(
                RuntimeError,
                match="spawn was not durably authorized",
            ):
                await port.complete_host_execution(
                    str(approval.id),
                    unauthorized_outcome,
                )
        first, second = await asyncio.gather(
            port.authorize_claimed_host_execution_spawn(str(approval.id)),
            port.authorize_claimed_host_execution_spawn(str(approval.id)),
        )
        assert sum(value is not None for value in (first, second)) == 1
        positive = first if first is not None else second
        assert positive is not None and positive > 0

        async with seed.factory() as session:
            persisted = await session.get(
                ExecutionApprovalRequestRow,
                approval.id,
            )
            assert persisted is not None
            assert persisted.status == "claimed"
            assert persisted.spawn_authorized_at is not None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_final_spawn_authorization_rejects_expired_preparation_window(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.private_work.execution_approval._now",
        lambda: datetime(2000, 1, 1, tzinfo=UTC),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="claimed",
                command="python expired-preparation.py",
            )
            (
                approval.command_private_json,
                approval.command_digest,
            ) = _valid_private_envelope("python expired-preparation.py")
            approval.claimed_at = (
                datetime.now(UTC)
                - timedelta(
                    seconds=CLAIMED_EXECUTION_SETTLEMENT_GRACE_SECONDS,
                )
                + timedelta(milliseconds=500)
            )
            approval.updated_at = approval.claimed_at

        port = _claimed_execution_port(
            seed,
            thread_id=thread_id,
            continuation=continuation,
            approval=approval,
        )
        async with seed.factory() as blocker, blocker.begin():
            locked = await blocker.get(
                ExecutionApprovalRequestRow,
                approval.id,
                with_for_update=True,
            )
            assert locked is not None
            authorization = asyncio.create_task(
                port.authorize_claimed_host_execution_spawn(
                    str(approval.id),
                )
            )
            await asyncio.sleep(0.7)
            assert not authorization.done()
        assert not await asyncio.wait_for(authorization, timeout=2)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_final_spawn_authorization_rejects_concurrent_run_cancel(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="claimed",
                command="python cancelled-run.py",
            )
            (
                approval.command_private_json,
                approval.command_digest,
            ) = _valid_private_envelope("python cancelled-run.py")
            approval.claimed_at = datetime.now(UTC)
            approval.updated_at = approval.claimed_at

        port = _claimed_execution_port(
            seed,
            thread_id=thread_id,
            continuation=continuation,
            approval=approval,
        )
        assert await port.authorize_claimed_host_execution_spawn(
            str(approval.id),
        )

        async with seed.factory() as session, session.begin():
            run = await session.get(
                RunRow,
                continuation.run.run_id,
                with_for_update=True,
            )
            assert run is not None
            run.cancel_requested_at = datetime.now(UTC)
            await session.flush()
            authorization = asyncio.create_task(
                port.authorize_claimed_host_execution_spawn(
                    str(approval.id),
                )
            )
            await asyncio.sleep(0.05)
            assert not authorization.done()

        assert not await asyncio.wait_for(authorization, timeout=2)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_final_spawn_window_is_bounded_by_locked_lease_and_expires_while_waiting(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="claimed",
                command="python lease-window.py",
            )
            (
                approval.command_private_json,
                approval.command_digest,
            ) = _valid_private_envelope("python lease-window.py")
            approval.claimed_at = datetime.now(UTC)
            approval.updated_at = approval.claimed_at
            lease_deadline = datetime.now(UTC) + timedelta(seconds=1.2)
            continuation.job.lease_expires_at = lease_deadline
            continuation.run.execution_lease_expires_at = lease_deadline

        port = _claimed_execution_port(
            seed,
            thread_id=thread_id,
            continuation=continuation,
            approval=approval,
        )
        remaining = await port.authorize_claimed_host_execution_spawn(
            str(approval.id),
        )
        assert remaining is not None
        assert 0 < remaining <= 1.2

        async with seed.factory() as blocker, blocker.begin():
            locked = await blocker.get(
                ExecutionApprovalRequestRow,
                approval.id,
                with_for_update=True,
            )
            assert locked is not None
            authorization = asyncio.create_task(
                port.authorize_claimed_host_execution_spawn(
                    str(approval.id),
                )
            )
            await asyncio.sleep(1.4)
            assert not authorization.done()

        assert not await asyncio.wait_for(authorization, timeout=2)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_claim_rechecks_ttl_after_asset_closure_before_side_effect_mark(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    closure_entered = asyncio.Event()
    release_closure = asyncio.Event()
    closure_calls = 0
    frozen_closure = (
        (("agent", "project", "asset", "version", "a" * 64),),
        (),
        (),
        (("chat", "model", "version", 1, "b" * 64),),
        (("agent_runtime", "policy", 1, "c" * 64),),
    )

    async def paused_asset_closure(*args, **kwargs):
        nonlocal closure_calls
        del args, kwargs
        closure_calls += 1
        if closure_calls == 1:
            closure_entered.set()
            await release_closure.wait()
        return frozen_closure

    monkeypatch.setattr(
        "app.private_work.execution_approval._asset_closure",
        paused_asset_closure,
    )
    monkeypatch.setattr(
        "app.private_work.execution_approval._now",
        lambda: datetime(2000, 1, 1, tzinfo=UTC),
    )
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="approved",
                command="python closure-expired.py",
            )
            (
                approval.command_private_json,
                approval.command_digest,
            ) = _valid_private_envelope("python closure-expired.py")
            approval.expires_at = datetime.now(UTC) + timedelta(
                milliseconds=300,
            )
            approval.updated_at = datetime.now(UTC)

        port = _claimed_execution_port(
            seed,
            thread_id=thread_id,
            continuation=continuation,
            approval=approval,
        )
        claim = asyncio.create_task(port.claim_frozen_host_execution())
        await asyncio.wait_for(closure_entered.wait(), timeout=2)
        await asyncio.sleep(0.5)
        release_closure.set()

        result = await asyncio.wait_for(claim, timeout=2)

        assert result.status == "denied"
        assert result.reason_code == "approval_expired"
        assert closure_calls == 2
        async with seed.factory() as session:
            retained = await session.get(
                ExecutionApprovalRequestRow,
                approval.id,
            )
            job = await session.get(JobRow, continuation.job.id)
            assert retained is not None and retained.status == "expired"
            assert retained.claimed_at is None
            assert job is not None and job.retry_safety == "safe"
    finally:
        release_closure.set()
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entrypoint", "obligation_status"),
    [
        ("thread", "deferred"),
        ("run", "assigned"),
        ("retention", "intent_recorded"),
    ],
)
async def test_direct_cancel_entrypoints_close_active_output_obligations(
    migrated_postgres_database_url: str,
    entrypoint: str,
    obligation_status: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    audit = _ApprovalAudit()
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = None
            if obligation_status != "deferred":
                continuation = await _add_run_job(
                    session,
                    seed=seed,
                    owner=seed.owner_a,
                    worker_id=worker_id,
                    thread_id=thread_id,
                    terminal=False,
                )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status=("pending" if obligation_status == "deferred" else "approved"),
                command=f"python {entrypoint}.py",
            )
            await _add_output_delivery_obligation(
                session,
                approval=approval,
                continuation=continuation,
                status=obligation_status,
            )
            approval_id = approval.id

        saver = object.__new__(checkpointer_module._ScopedCheckpointSaver)
        saver._approval_audit = audit
        saver._context = seed.owner_a
        run_service = object.__new__(PrivateRunService)
        run_service._approval_audit = audit
        async with seed.factory() as session, session.begin():
            now = datetime.now(UTC)
            row = await session.get(
                ExecutionApprovalRequestRow,
                approval_id,
                with_for_update=True,
            )
            assert row is not None
            if entrypoint == "thread":
                await saver._cancel_thread_execution_approval(
                    session,
                    row,
                    now=now,
                )
            elif entrypoint == "run":
                await run_service._cancel_execution_approval(
                    session,
                    seed.owner_a,
                    row,
                    now=now,
                )
            else:
                await retention_purge_module._cancel_retention_approval(
                    session,
                    row,
                    now=now,
                    request_id=seed.owner_a.request_id,
                    audit=audit,
                )

        async with seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                approval_id,
            )
            assert approval is not None and approval.status == "cancelled"
            assert obligation is not None
            assert obligation.status == "cancelled"
            assert obligation.terminal_at is not None
            assert obligation.version == 2
        assert audit.terminals == [(source.run.run_id, "cancelled")]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_direct_cancel_without_output_obligation_remains_valid(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    audit = _ApprovalAudit()
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=None,
                status="pending",
                command="python no-obligation.py",
            )
            approval_id = approval.id

        run_service = object.__new__(PrivateRunService)
        run_service._approval_audit = audit
        async with seed.factory() as session, session.begin():
            row = await session.get(
                ExecutionApprovalRequestRow,
                approval_id,
                with_for_update=True,
            )
            assert row is not None
            await run_service._cancel_execution_approval(
                session,
                seed.owner_a,
                row,
                now=datetime.now(UTC),
            )

        async with seed.factory() as session:
            row = await session.get(ExecutionApprovalRequestRow, approval_id)
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                approval_id,
            )
            assert row is not None and row.status == "cancelled"
            assert obligation is None
        assert audit.terminals == [(source.run.run_id, "cancelled")]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_output_obligation_terminal_transition_rejects_scope_mismatch(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="approved",
                command="python wrong-scope.py",
            )
            await _add_output_delivery_obligation(
                session,
                approval=approval,
                continuation=continuation,
                status="assigned",
            )
            approval_id = approval.id

        with pytest.raises(OutputDeliveryObligationConflict):
            async with seed.factory() as session, session.begin():
                row = await session.get(
                    ExecutionApprovalRequestRow,
                    approval_id,
                    with_for_update=True,
                )
                assert row is not None
                await transition_output_delivery_obligation_for_approval_terminal(
                    session,
                    approval=SimpleNamespace(
                        id=row.id,
                        project_id=row.project_id,
                        owner_user_id=str(seed.owner_b.user_id),
                        thread_id=row.thread_id,
                    ),
                    approval_status="cancelled",
                    now=datetime.now(UTC),
                )

        async with seed.factory() as session:
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                approval_id,
            )
            assert obligation is not None and obligation.status == "assigned"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retention_catch_does_not_cancel_already_delivered_obligation(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    audit = _ApprovalAudit()
    try:
        worker_id = await _add_worker(seed)
        thread_id = str(uuid.uuid4())
        await _add_thread(seed, owner=seed.owner_a, thread_id=thread_id)
        async with seed.factory() as session, session.begin():
            source = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=True,
            )
            continuation = await _add_run_job(
                session,
                seed=seed,
                owner=seed.owner_a,
                worker_id=worker_id,
                thread_id=thread_id,
                terminal=False,
            )
            approval, _ = await _add_approval(
                session,
                owner=seed.owner_a,
                thread_id=thread_id,
                source=source,
                continuation=continuation,
                status="approved",
                command="python already-delivered.py",
            )
            await _add_output_delivery_obligation(
                session,
                approval=approval,
                continuation=continuation,
                status="delivered",
            )
            approval_id = approval.id

        async with seed.factory() as session, session.begin():
            row = await session.get(
                ExecutionApprovalRequestRow,
                approval_id,
                with_for_update=True,
            )
            assert row is not None
            with pytest.raises(RetentionExecutionApprovalActive):
                await retention_purge_module._cancel_retention_approval(
                    session,
                    row,
                    now=datetime.now(UTC),
                    request_id=seed.owner_a.request_id,
                    audit=audit,
                )

        async with seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                approval_id,
            )
            assert approval is not None and approval.status == "approved"
            assert obligation is not None and obligation.status == "delivered"
            assert obligation.satisfied_artifact_id is not None
        assert audit.terminals == []
    finally:
        await seed.engine.dispose()
