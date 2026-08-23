from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.private_thread_seed import seed_private_thread_database

from app.audit.models import AuditAction
from app.audit.service import (
    AuditService,
    _bind_gateway_audit_process,
    _bind_worker_audit_process,
)
from app.audit.sinks import OperationalAuditSink
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    ExecutionApprovalConflict,
    PrivateWorkConflict,
    PrivateWorkUnavailable,
)
from app.private_work.execution_approval import (
    ExecutionApprovalService,
    HostExecutionProviderPolicySnapshot,
    WorkerHostExecutionApprovalPort,
    recover_staged_execution_approval_id,
    settle_staged_execution_approvals,
)
from app.private_work.execution_approval_audit import (
    NoopHostExecutionApprovalAudit,
)
from app.private_work.execution_approval_lifecycle import (
    ExecutionApprovalPrivateLifecycleConflict,
    claimed_execution_absolute_deadline,
    lock_and_reconcile_active_execution_approval,
    reconcile_locked_execution_approval,
)
from app.private_work.file_finalizer import (
    PrivateFileFinalizer,
    _AfterFile,
    _StagedFile,
)
from app.private_work.output_delivery_obligation import (
    OutputDeliveryObligationConflict,
    assign_output_delivery_obligation,
    settle_continuation_output_delivery,
)
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_metadata import RUN_HOST_EXECUTION_SUSPENSION_KEY
from app.private_work.run_repository import (
    PrivateRunCreate,
    PrivateRunExecutionLeaseLost,
    PrivateRunRepository,
)
from app.private_work.run_service import PrivateRunService
from app.private_work.sandbox_files import PrivateFileRunScope
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.jobs import PrivateRunJobRepository
from app.reliability.owner_refs import AuditHmacKeyring
from app.reliability.run_execution.boundary import PrivateRunExecutionBoundary
from app.reliability.run_execution.contracts import AgentExecutionResult
from app.reliability.run_execution.handler import PrivateRunJobHandler
from app.reliability.run_execution.settlement import PrivateRunJobTerminalPort
from app.worker.service import LeaseLost
from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.file_authority import AuthorityManifest, AuthorityManifestEntry
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.execution_approvals import (
    ExecutionApprovalOutputDeliveryCandidateRow,
    ExecutionApprovalOutputDeliveryObligationRow,
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import (
    JobClaim,
    JobOwnerRef,
    JobRepository,
    JobScope,
    JobTerminalEvent,
)
from deerflow.persistence.private_work import (
    PrivateArtifactRow,
    PrivateFileChunkRow,
    PrivateFileRow,
    RunAssetVersionRow,
)
from deerflow.persistence.private_work.file_repository import PrivateFileRepository
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow
from deerflow.persistence.system_runtime_settings import (
    RunRuntimePolicySnapshotRow,
    SystemRuntimePolicyRow,
    SystemRuntimePolicyVersionRow,
)
from deerflow.persistence.system_settings import (
    RunModelConfigSnapshotRow,
    SystemModelConfigRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow
from deerflow.runtime.events.models import StreamFrame, StreamLeaseProof
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.host_execution_approval import (
    HostExecutionOutcome,
    HostExecutionPlan,
)
from deerflow.runtime.host_execution_domain import HostExecutionDomainSnapshot
from deerflow.runtime.host_execution_runner import (
    execute_frozen_host_execution_continuation,
)


async def _running_job(
    session,
    *,
    project_id: uuid.UUID,
    owner_user_id: str,
    thread_id: str,
    agent_id: uuid.UUID,
    worker_id: uuid.UUID,
    run_id: str,
    lease_token: str,
    model_ref: str,
    execution_domain_affinity: str | None = None,
) -> tuple[JobRow, JobAttemptRow]:
    now = datetime.now(UTC)
    origin_trace_id = uuid.uuid4().hex
    token_hash = hashlib.sha256(lease_token.encode()).hexdigest()
    run = RunRow(
        run_id=run_id,
        thread_id=thread_id,
        assistant_id=str(agent_id),
        owner_user_id=owner_user_id,
        status="running",
        model_name=model_ref,
        multitask_strategy="reject",
        metadata_json={},
        kwargs_json={},
        origin_trace_id=origin_trace_id,
        project_id=project_id,
        finalization_status="pending",
        execution_lease_token_hash=token_hash,
        execution_lease_expires_at=now + timedelta(minutes=5),
        execution_heartbeat_at=now,
        execution_started_at=now,
    )
    session.add(run)
    await session.flush()
    job = JobRow(
        job_type="private_run",
        project_id=project_id,
        owner_user_id=owner_user_id,
        run_id=run_id,
        origin_trace_id=origin_trace_id,
        execution_domain_affinity=execution_domain_affinity,
        idempotency_key=hashlib.sha256(f"job:{run_id}".encode()).hexdigest(),
        status="running",
        max_attempts=3,
        attempt_count=1,
        lease_owner_id=worker_id,
        lease_token_hash=token_hash,
        lease_expires_at=now + timedelta(minutes=5),
        heartbeat_at=now,
        retry_safety="safe",
        started_at=now,
    )
    session.add(job)
    await session.flush()
    run.job_id = job.id
    attempt = JobAttemptRow(
        job_id=job.id,
        attempt_number=1,
        worker_id=worker_id,
        lease_token_hash=token_hash,
        started_at=now,
        heartbeat_at=now,
    )
    session.add(attempt)
    await session.flush()
    return job, attempt


def _provider_policy(
    *,
    execution_domain_id: str = "mac-primary",
) -> HostExecutionProviderPolicySnapshot:
    return HostExecutionProviderPolicySnapshot(
        provider_use="deerflow.sandbox.local:LocalSandboxProvider",
        host_execution_mode="local_approval_required",
        allow_host_bash=False,
        bash_command_timeout=60,
        approval_max_timeout_seconds=60,
        request_ttl_seconds=300,
        execution_domain_id=execution_domain_id,
    )


def _execution_domain(
    *,
    configured_id: str = "mac-primary",
    runtime_base_dir: str = "/srv/actweave-runtime-a",
    device_fingerprint: str = "d" * 64,
) -> HostExecutionDomainSnapshot:
    return HostExecutionDomainSnapshot(
        configured_id=configured_id,
        public_label="Worker host environment",
        os_name="posix",
        sys_platform="darwin",
        machine="arm64",
        device_fingerprint=device_fingerprint,
        environment_fingerprint="f" * 64,
        euid=501,
        egid=20,
        runtime_base_dir=runtime_base_dir,
    )


class _FailingHostExecutionAudit(NoopHostExecutionApprovalAudit):
    def __init__(self, phase: str) -> None:
        self._phase = phase

    def _raise_if_selected(self, phase: str) -> None:
        if self._phase == phase:
            raise RuntimeError(f"{phase} audit unavailable")

    async def host_execution_approval_requested(self, *args, **kwargs) -> None:
        del args, kwargs
        self._raise_if_selected("requested")

    async def host_execution_approval_decided(self, *args, **kwargs) -> None:
        del args, kwargs
        self._raise_if_selected("decided")

    async def host_execution_approval_claimed(self, *args, **kwargs) -> None:
        del args, kwargs
        self._raise_if_selected("claimed")

    async def host_execution_approval_terminal(self, *args, **kwargs) -> None:
        del args, kwargs
        self._raise_if_selected("terminal")


class _NeverContinuationAdmission:
    def __init__(self) -> None:
        self.calls = 0

    async def admit(self, *args, **kwargs):
        del args, kwargs
        self.calls += 1
        raise AssertionError("expired approval must not admit a continuation")


class _DatetimeType(type):
    def __instancecheck__(cls, instance):
        return isinstance(instance, datetime)


class _HostClockOneDayAhead(datetime, metaclass=_DatetimeType):
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(days=1)


class _FailingContinuationAdmission:
    async def admit(self, *args, **kwargs):
        del args, kwargs
        raise PrivateWorkUnavailable("clock-test-admission-failed")


async def _prepare_clock_scenario(
    database_url: str,
    *,
    stage: bool = True,
):
    seed = await seed_private_thread_database(database_url)
    thread_id = str(uuid.uuid4())
    source_run_id = str(uuid.uuid4())
    worker_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="approval-clock-test",
                capabilities_json=["private_run"],
                max_concurrent_jobs=1,
            )
        )
    source = await PrivateRunAdmissionService(seed.factory).admit(
        seed.owner_a,
        thread_id,
        PrivateRunCreate(
            run_id=source_run_id,
            kwargs={"input": {"messages": []}},
        ),
    )
    async with seed.factory() as session, session.begin():
        source_job = await session.get(JobRow, source.job.job_id)
        assert source_job is not None
        # The PostgreSQL core gate intentionally reuses one database across
        # files. Make this scenario's newly admitted Job outrank unrelated
        # queued fixtures so it exercises the exact approval clock under test.
        source_job.priority = 32_767
        jobs = JobRepository(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"private_run"}),
            lease_seconds=300,
        )
        assert claim is not None and claim.job_id == source.job.job_id
        assert await jobs.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
        )
        await PrivateRunRepository(session).begin_execution(
            scope=seed.owner_a_scope,
            run_id=source_run_id,
            job_id=claim.job_id,
            lease_token=claim.lease_token,
            origin_trace_id=claim.origin_trace_id,
        )
    port = WorkerHostExecutionApprovalPort(
        seed.factory,
        context=seed.owner_a,
        claim=claim,
        thread_id=thread_id,
        request_ttl_seconds=300,
        provider_policy=_provider_policy(),
        execution_domain=_execution_domain(),
    )
    approval_id = None
    if stage:
        staged = await port.request_host_execution(
            HostExecutionPlan(
                source_tool_call_id=f"call-clock-{uuid.uuid4().hex}",
                source_run_id=source_run_id,
                source_thread_id=thread_id,
                description="exercise authoritative clock",
                requested_command="python /mnt/user-data/workspace/clock.py",
                effective_command="cd /private/workspace && python clock.py",
                shell="/bin/zsh",
                cwd="/private/workspace",
                timeout_seconds=60,
                agent_path=("lead",),
            )
        )
        assert staged.approval_id is not None
        approval_id = uuid.UUID(staged.approval_id)
    return SimpleNamespace(
        seed=seed,
        thread_id=thread_id,
        source_run_id=source_run_id,
        worker_id=worker_id,
        claim=claim,
        port=port,
        approval_id=approval_id,
    )


async def _settle_clock_scenario_pending(scenario) -> None:
    async with scenario.seed.factory() as session, session.begin():
        await PrivateRunRepository(session).settle_execution(
            scope=scenario.seed.owner_a_scope,
            run_id=scenario.source_run_id,
            job_id=scenario.claim.job_id,
            lease_token=scenario.claim.lease_token,
            outcome="succeeded",
        )
        await settle_staged_execution_approvals(
            session,
            claim=scenario.claim,
            succeeded=True,
            suspended_approval_id=str(scenario.approval_id),
            request_ttl_seconds=300,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_durable_success_recovers_only_exact_attempt_staged_approval(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    try:
        async with scenario.seed.factory() as session, session.begin():
            assert (
                await recover_staged_execution_approval_id(
                    session,
                    claim=scenario.claim,
                )
                is None
            )

        await scenario.port.seal_suspended_approval_marker(
            str(scenario.approval_id),
        )

        async with scenario.seed.factory() as session, session.begin():
            assert await recover_staged_execution_approval_id(
                session,
                claim=scenario.claim,
            ) == str(scenario.approval_id)

            wrong_attempt = replace(
                scenario.claim,
                attempt_id=uuid.uuid4(),
            )
            assert await recover_staged_execution_approval_id(
                session,
                claim=wrong_attempt,
            ) == str(scenario.approval_id)
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_durable_success_attempt_takeover_activates_marker_without_rerun(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    try:
        async with scenario.seed.factory() as session, session.begin():
            await _add_source_output(
                session,
                scenario,
                logical_path="outputs/takeover.txt",
                sha256="8" * 64,
            )
        await scenario.port.seal_suspended_approval_marker(
            str(scenario.approval_id),
        )
        async with scenario.seed.factory() as session, session.begin():
            await DbRunEventStore(
                scenario.seed.factory,
                run_event_notify_enabled=False,
            ).append_stream_frame(
                session,
                scope=scenario.seed.owner_a_scope,
                thread_id=scenario.thread_id,
                run_id=scenario.source_run_id,
                frame=StreamFrame.end(status="completed"),
                lease=StreamLeaseProof(
                    job_id=scenario.claim.job_id,
                    lease_token=scenario.claim.lease_token,
                ),
            )

        expired_at = datetime.now(UTC) - timedelta(seconds=1)
        async with scenario.seed.factory() as session, session.begin():
            job = await session.get(
                JobRow,
                scenario.claim.job_id,
                with_for_update=True,
            )
            run = await session.get(
                RunRow,
                scenario.source_run_id,
                with_for_update=True,
            )
            assert job is not None and run is not None
            job.lease_expires_at = expired_at
            run.execution_lease_expires_at = expired_at

        async with scenario.seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            takeover_claim = await jobs.claim_next(
                worker_id=scenario.worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
            )
            assert takeover_claim is not None
            assert takeover_claim.job_id == scenario.claim.job_id
            assert takeover_claim.attempt_id != scenario.claim.attempt_id
            assert await jobs.mark_running(
                takeover_claim.job_id,
                lease_token=takeover_claim.lease_token,
            )

        handler = PrivateRunJobHandler(
            scenario.seed.factory,
            executor=SimpleNamespace(),
        )
        settlement = await handler._handle_with_trace(
            takeover_claim,
            SimpleNamespace(),
        )
        await settlement.commit()

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            run = await session.get(RunRow, scenario.source_run_id)
            job = await session.get(JobRow, scenario.claim.job_id)
            assert approval is not None and approval.status == "pending"
            assert obligation is not None and obligation.status == "deferred"
            assert run is not None and run.status == "success"
            assert job is not None and job.status == "succeeded"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_marker_before_stream_terminal_repairs_success_without_graph_rerun(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)

    class _ExecutorMustNotRun:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("recovery must not invoke the Agent graph")

    try:
        async with scenario.seed.factory() as session, session.begin():
            await _add_source_output(
                session,
                scenario,
                logical_path="outputs/pre-terminal-crash.txt",
                sha256="a" * 64,
            )
        await scenario.port.seal_suspended_approval_marker(
            str(scenario.approval_id),
        )

        expired_at = datetime.now(UTC) - timedelta(seconds=1)
        async with scenario.seed.factory() as session, session.begin():
            job = await session.get(
                JobRow,
                scenario.claim.job_id,
                with_for_update=True,
            )
            run = await session.get(
                RunRow,
                scenario.source_run_id,
                with_for_update=True,
            )
            assert job is not None and run is not None
            job.lease_expires_at = expired_at
            run.execution_lease_expires_at = expired_at

        async with scenario.seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            takeover_claim = await jobs.claim_next(
                worker_id=scenario.worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
            )
            assert takeover_claim is not None
            assert takeover_claim.job_id == scenario.claim.job_id
            assert takeover_claim.attempt_id != scenario.claim.attempt_id
            assert await jobs.mark_running(
                takeover_claim.job_id,
                lease_token=takeover_claim.lease_token,
            )

        settlement = await PrivateRunJobHandler(
            scenario.seed.factory,
            executor=_ExecutorMustNotRun(),
        )._handle_with_trace(
            takeover_claim,
            SimpleNamespace(),
        )
        await settlement.commit()

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            run = await session.get(RunRow, scenario.source_run_id)
            job = await session.get(JobRow, scenario.claim.job_id)
            terminal = await DbRunEventStore(
                scenario.seed.factory,
                run_event_notify_enabled=False,
            ).get_stream_terminal(
                session,
                scope=scenario.seed.owner_a_scope,
                thread_id=scenario.thread_id,
                run_id=scenario.source_run_id,
            )
            assert approval is not None and approval.status == "pending"
            assert obligation is not None and obligation.status == "deferred"
            assert run is not None and run.status == "success"
            assert job is not None and job.status == "succeeded"
            assert terminal is not None
            assert terminal.data["status"] == "completed"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_marker",
    ["wrong_producing_attempt", "terminal_approval"],
)
async def test_suspension_marker_mismatch_fails_closed(
    migrated_postgres_database_url: str,
    stale_marker: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    try:
        await scenario.port.seal_suspended_approval_marker(
            str(scenario.approval_id),
        )
        async with scenario.seed.factory() as session, session.begin():
            run = await session.get(
                RunRow,
                scenario.source_run_id,
                with_for_update=True,
            )
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
                with_for_update=True,
            )
            assert run is not None and approval is not None
            if stale_marker == "wrong_producing_attempt":
                metadata = dict(run.metadata_json)
                marker = dict(metadata[RUN_HOST_EXECUTION_SUSPENSION_KEY])
                marker["producing_attempt_id"] = str(uuid.uuid4())
                metadata[RUN_HOST_EXECUTION_SUSPENSION_KEY] = marker
                run.metadata_json = metadata
            else:
                terminal_at = datetime.now(UTC)
                approval.status = "cancelled"
                approval.version += 1
                approval.terminal_at = terminal_at
                approval.updated_at = terminal_at

        async with scenario.seed.factory() as session, session.begin():
            with pytest.raises(ExecutionApprovalPrivateLifecycleConflict):
                await recover_staged_execution_approval_id(
                    session,
                    claim=scenario.claim,
                )
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_publish_end_failure_after_marker_repairs_success_in_settlement(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)

    class _PublishEndFailure:
        async def execute(self, *_args, **_kwargs):
            await scenario.port.seal_suspended_approval_marker(
                str(scenario.approval_id),
            )
            raise RuntimeError("durable publish_end acknowledgement was lost")

    try:
        async with scenario.seed.factory() as session, session.begin():
            await _add_source_output(
                session,
                scenario,
                logical_path="outputs/publish-ack-failure.txt",
                sha256="b" * 64,
            )
        authority = SimpleNamespace(
            bind_heartbeat_callback=lambda _callback: None,
            cancel_requested=False,
        )
        settlement = await PrivateRunJobHandler(
            scenario.seed.factory,
            executor=_PublishEndFailure(),
        )._handle_with_trace(
            scenario.claim,
            authority,
        )
        await settlement.commit()

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            run = await session.get(RunRow, scenario.source_run_id)
            job = await session.get(JobRow, scenario.claim.job_id)
            terminal = await DbRunEventStore(
                scenario.seed.factory,
                run_event_notify_enabled=False,
            ).get_stream_terminal(
                session,
                scope=scenario.seed.owner_a_scope,
                thread_id=scenario.thread_id,
                run_id=scenario.source_run_id,
            )
            assert approval is not None and approval.status == "pending"
            assert obligation is not None and obligation.status == "deferred"
            assert run is not None and run.status == "success"
            assert job is not None and job.status == "succeeded"
            assert terminal is not None and terminal.data["status"] == "completed"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_public_cancel_after_marker_rolls_back_then_repairs_success(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    try:
        async with scenario.seed.factory() as session, session.begin():
            await _add_source_output(
                session,
                scenario,
                logical_path="outputs/cancel-race.txt",
                sha256="c" * 64,
            )
        await scenario.port.seal_suspended_approval_marker(
            str(scenario.approval_id),
        )
        with pytest.raises(PrivateWorkConflict):
            await PrivateRunService(scenario.seed.factory).cancel(
                scenario.seed.owner_a,
                scenario.thread_id,
                scenario.source_run_id,
            )

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            run = await session.get(RunRow, scenario.source_run_id)
            job = await session.get(JobRow, scenario.claim.job_id)
            assert approval is not None and approval.status == "staged"
            assert run is not None and run.cancel_requested_at is None
            assert job is not None and job.cancel_requested_at is None

        settlement = await PrivateRunJobHandler(
            scenario.seed.factory,
            executor=SimpleNamespace(),
        )._handle_with_trace(
            scenario.claim,
            SimpleNamespace(),
        )
        await settlement.commit()

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            run = await session.get(RunRow, scenario.source_run_id)
            job = await session.get(JobRow, scenario.claim.job_id)
            assert approval is not None and approval.status == "pending"
            assert obligation is not None and obligation.status == "deferred"
            assert run is not None and run.status == "success"
            assert job is not None and job.status == "succeeded"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_lazy_reconcile_preserves_marker_for_takeover_recovery(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    try:
        async with scenario.seed.factory() as session, session.begin():
            await _add_source_output(
                session,
                scenario,
                logical_path="outputs/reconcile-race.txt",
                sha256="d" * 64,
            )
        await scenario.port.seal_suspended_approval_marker(
            str(scenario.approval_id),
        )
        expired_at = datetime.now(UTC) - timedelta(seconds=1)
        async with scenario.seed.factory() as session, session.begin():
            job = await session.get(
                JobRow,
                scenario.claim.job_id,
                with_for_update=True,
            )
            run = await session.get(
                RunRow,
                scenario.source_run_id,
                with_for_update=True,
            )
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
                with_for_update=True,
            )
            assert job is not None and run is not None and approval is not None
            job.lease_expires_at = expired_at
            run.execution_lease_expires_at = expired_at
            await reconcile_locked_execution_approval(
                session,
                approval,
                now=datetime.now(UTC),
            )
            assert approval.status == "staged"

        async with scenario.seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            takeover_claim = await jobs.claim_next(
                worker_id=scenario.worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
            )
            assert takeover_claim is not None
            assert takeover_claim.job_id == scenario.claim.job_id
            assert takeover_claim.attempt_id != scenario.claim.attempt_id
            assert await jobs.mark_running(
                takeover_claim.job_id,
                lease_token=takeover_claim.lease_token,
            )

        settlement = await PrivateRunJobHandler(
            scenario.seed.factory,
            executor=SimpleNamespace(),
        )._handle_with_trace(
            takeover_claim,
            SimpleNamespace(),
        )
        await settlement.commit()

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            run = await session.get(RunRow, scenario.source_run_id)
            job = await session.get(JobRow, scenario.claim.job_id)
            assert approval is not None and approval.status == "pending"
            assert obligation is not None and obligation.status == "deferred"
            assert run is not None and run.status == "success"
            assert job is not None and job.status == "succeeded"
    finally:
        await scenario.seed.engine.dispose()


async def _add_source_output(
    session,
    scenario,
    *,
    logical_path: str,
    sha256: str,
) -> PrivateFileRow:
    row = PrivateFileRow(
        project_id=scenario.seed.owner_a.project_id,
        owner_user_id=str(scenario.seed.owner_a.user_id),
        thread_id=scenario.thread_id,
        kind="output",
        logical_path=logical_path,
        media_type="text/plain",
        size=1,
        sha256=sha256,
        status="ready",
        version=1,
        created_by_run_id=scenario.source_run_id,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_derives_markdown_line_counts_from_authoritative_db_chunks(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(
        migrated_postgres_database_url,
        stage=False,
    )
    try:
        old_content = b"alpha\nbeta\n"
        modified_content = b"alpha\ngamma\n"
        created_content = b"one\ntwo\n"
        old_sha256 = hashlib.sha256(old_content).hexdigest()
        modified_sha256 = hashlib.sha256(modified_content).hexdigest()
        created_sha256 = hashlib.sha256(created_content).hexdigest()
        modified_id = uuid.uuid4()
        created_id = uuid.uuid4()

        async with scenario.seed.factory() as session, session.begin():
            old = PrivateFileRow(
                project_id=scenario.seed.owner_a.project_id,
                owner_user_id=str(scenario.seed.owner_a.user_id),
                thread_id=scenario.thread_id,
                kind="output",
                logical_path="outputs/report.md",
                media_type="text/markdown",
                size=len(old_content),
                sha256=old_sha256,
                status="ready",
                version=1,
                created_by_run_id=scenario.source_run_id,
            )
            session.add(old)
            await session.flush()
            session.add(
                PrivateFileChunkRow(
                    file_id=old.id,
                    chunk_index=0,
                    content=old_content,
                    size=len(old_content),
                    sha256=old_sha256,
                )
            )
            repository = PrivateFileRepository(session)
            for file_id, logical_path, content, sha256 in (
                (
                    modified_id,
                    "outputs/.deerflow-staging-report",
                    modified_content,
                    modified_sha256,
                ),
                (
                    created_id,
                    "outputs/.deerflow-staging-new",
                    created_content,
                    created_sha256,
                ),
            ):
                await repository.stage(
                    scope=scenario.seed.owner_a_scope,
                    thread_id=scenario.thread_id,
                    kind="output",
                    logical_path=logical_path,
                    media_type="text/markdown",
                    created_by_run_id=scenario.source_run_id,
                    file_id=file_id,
                )
                await repository.append_chunk(
                    scope=scenario.seed.owner_a_scope,
                    thread_id=scenario.thread_id,
                    file_id=file_id,
                    chunk_index=0,
                    content=content,
                    size=len(content),
                    sha256=sha256,
                )
            run = await session.get(
                RunRow,
                scenario.source_run_id,
                with_for_update=True,
            )
            assert run is not None
            run.finalization_status = "finalizing"

        manifest = AuthorityManifest(
            entries=(
                AuthorityManifestEntry(
                    file_id=old.id,
                    logical_path=old.logical_path,
                    kind=old.kind,
                    media_type=old.media_type,
                    size=old.size,
                    sha256=old.sha256,
                    version=old.version,
                ),
            ),
            run_id=scenario.source_run_id,
        )
        modified_after = _AfterFile(
            logical_path="outputs/report.md",
            virtual_path="/mnt/user-data/outputs/report.md",
            kind="output",
            size=len(modified_content),
            media_type="text/markdown",
        )
        created_after = _AfterFile(
            logical_path="outputs/new.md",
            virtual_path="/mnt/user-data/outputs/new.md",
            kind="output",
            size=len(created_content),
            media_type="text/markdown",
        )
        boundary = PrivateRunExecutionBoundary(
            scenario.seed.factory,
            context=scenario.seed.owner_a,
            claim=scenario.claim,
        )
        result = await PrivateFileFinalizer(scenario.seed.factory)._commit(
            PrivateFileRunScope(
                scenario.seed.owner_a,
                thread_id=scenario.thread_id,
                run_id=scenario.source_run_id,
                authorization_boundary=boundary,
            ),
            manifest,
            (modified_after, created_after),
            (
                _StagedFile(
                    id=modified_id,
                    after=modified_after,
                    size=len(modified_content),
                    sha256=modified_sha256,
                ),
                _StagedFile(
                    id=created_id,
                    after=created_after,
                    size=len(created_content),
                    sha256=created_sha256,
                ),
            ),
            (),
        )

        assert result.workspace_changes is not None
        assert result.workspace_changes.version == 2
        assert result.workspace_changes.summary.created == 1
        assert result.workspace_changes.summary.modified == 1
        assert result.workspace_changes.summary.additions == 3
        assert result.workspace_changes.summary.deletions == 1
        by_path = {change.path: change for change in result.workspace_changes.files}
        assert by_path["/mnt/user-data/outputs/report.md"].additions == 1
        assert by_path["/mnt/user-data/outputs/report.md"].deletions == 1
        assert by_path["/mnt/user-data/outputs/new.md"].additions == 2
        assert by_path["/mnt/user-data/outputs/new.md"].deletions == 0
    finally:
        await scenario.seed.engine.dispose()


async def _prepare_assigned_output_obligation(database_url: str):
    scenario = await _prepare_clock_scenario(database_url)
    hooks = _ApprovalLifecycleHooks()
    async with scenario.seed.factory() as session, session.begin():
        await _add_source_output(
            session,
            scenario,
            logical_path="outputs/required.txt",
            sha256="7" * 64,
        )
    await _settle_clock_scenario_pending(scenario)
    projection = await ExecutionApprovalService(
        scenario.seed.factory,
        admission=_QueuedAffinityContinuationAdmission(
            scenario.seed.factory,
            hooks=hooks,
        ),
        provider_policy=_provider_policy(),
        quota=hooks,
        run_audit=hooks,
    ).decide(
        scenario.seed.owner_a,
        thread_id=scenario.thread_id,
        source_run_id=scenario.source_run_id,
        approval_id=scenario.approval_id,
        decision="allow_once",
        expected_version=2,
        idempotency_key=uuid.uuid4(),
    )
    assert projection.approval is not None
    continuation = projection.approval["continuation_run"]
    assert isinstance(continuation, dict)
    continuation_run_id = continuation["run_id"]
    async with scenario.seed.factory() as session:
        continuation_run = await session.get(RunRow, continuation_run_id)
        assert continuation_run is not None
        assert continuation_run.job_id is not None
        continuation_job_id = continuation_run.job_id
    return SimpleNamespace(
        scenario=scenario,
        hooks=hooks,
        continuation_run_id=continuation_run_id,
        continuation_job_id=continuation_job_id,
    )


async def _claim_assigned_output_obligation(prepared):
    scenario = prepared.scenario
    async with scenario.seed.factory() as session, session.begin():
        jobs = JobRepository(session)
        claim = await jobs.claim_next(
            worker_id=scenario.worker_id,
            capabilities=frozenset({"private_run"}),
            lease_seconds=300,
            execution_domain_affinity=_execution_domain().affinity,
        )
        assert claim is not None
        assert claim.job_id == prepared.continuation_job_id
        assert await jobs.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
        )
        await PrivateRunRepository(session).begin_execution(
            scope=scenario.seed.owner_a_scope,
            run_id=prepared.continuation_run_id,
            job_id=claim.job_id,
            lease_token=claim.lease_token,
            origin_trace_id=claim.origin_trace_id,
        )
        approval = await session.get(
            ExecutionApprovalRequestRow,
            scenario.approval_id,
            with_for_update=True,
        )
        job = await session.get(JobRow, claim.job_id, with_for_update=True)
        assert approval is not None and approval.status == "approved"
        assert job is not None
        claimed_at = datetime.now(UTC)
        approval.status = "claimed"
        approval.execution_job_attempt_id = claim.attempt_id
        approval.claimed_at = claimed_at
        approval.version += 1
        approval.updated_at = claimed_at
        job.retry_safety = "unknown"
        job.updated_at = claimed_at
    return claim


async def _claimed_output_delivery_runtime(prepared):
    claim = await _claim_assigned_output_obligation(prepared)
    scenario = prepared.scenario
    boundary = PrivateRunExecutionBoundary(
        scenario.seed.factory,
        context=scenario.seed.owner_a,
        claim=claim,
    )
    port = WorkerHostExecutionApprovalPort(
        scenario.seed.factory,
        context=scenario.seed.owner_a,
        claim=claim,
        thread_id=scenario.thread_id,
        request_ttl_seconds=300,
        provider_policy=_provider_policy(),
        execution_domain=_execution_domain(),
        continuation_approval_id=str(scenario.approval_id),
        retry_safety_boundary=boundary,
    )
    return claim, boundary, port


async def _authorize_claimed_spawn(
    port: WorkerHostExecutionApprovalPort,
    approval_id: uuid.UUID,
) -> None:
    remaining = await port.authorize_claimed_host_execution_spawn(
        str(approval_id),
    )
    assert remaining is not None and remaining > 0


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_status", ["finished", "launch_failed"])
async def test_host_receipt_resolves_only_spawn_fence_and_transient_failure_retries(
    migrated_postgres_database_url: str,
    receipt_status: str,
) -> None:
    prepared = await _prepare_assigned_output_obligation(
        migrated_postgres_database_url,
    )
    scenario = prepared.scenario
    try:
        claim, boundary, port = await _claimed_output_delivery_runtime(prepared)
        fence = await boundary.before_sandbox_exec()
        outcome = (
            HostExecutionOutcome(
                status="finished",
                exit_code=0,
                result_text="durable receipt",
            )
            if receipt_status == "finished"
            else HostExecutionOutcome(
                status="launch_failed",
                reason_code="pre_spawn_authorization_failed",
            )
        )
        if receipt_status == "finished":
            await _authorize_claimed_spawn(port, scenario.approval_id)
        await port.complete_host_execution_with_retry_safety_fence(
            str(scenario.approval_id),
            outcome,
            fence,
        )
        assert boundary.ambiguous_side_effect is False

        async with scenario.seed.factory() as session, session.begin():
            settled = await PrivateRunRepository(
                session,
                jobs=JobRepository(
                    session,
                    owner_ref_hasher=lambda _owner: JobOwnerRef(
                        "test",
                        "0" * 64,
                    ),
                    terminal_port=PrivateRunJobTerminalPort(),
                ),
            ).settle_execution(
                scope=scenario.seed.owner_a_scope,
                run_id=prepared.continuation_run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                outcome="failed",
                public_error_code="PRIVATE_RUN_EXECUTION_FAILED",
                ambiguous_side_effect=boundary.ambiguous_side_effect,
                retryable_failure=True,
                retry_initial_seconds=2,
                retry_max_seconds=300,
            )
            assert settled.run.status == "pending"

        async with scenario.seed.factory() as session:
            job = await session.get(JobRow, claim.job_id)
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            assert job is not None
            assert job.status == "retry_wait"
            assert job.retry_safety == "safe"
            assert approval is not None and approval.status == receipt_status
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("other_fence_position", ["before_receipt", "after_receipt"])
async def test_host_receipt_never_clears_another_unresolved_side_effect(
    migrated_postgres_database_url: str,
    other_fence_position: str,
) -> None:
    prepared = await _prepare_assigned_output_obligation(
        migrated_postgres_database_url,
    )
    scenario = prepared.scenario
    try:
        claim, boundary, port = await _claimed_output_delivery_runtime(prepared)
        if other_fence_position == "before_receipt":
            await boundary.before_sandbox_write()
        spawn_fence = await boundary.before_sandbox_exec()
        await _authorize_claimed_spawn(port, scenario.approval_id)
        await port.complete_host_execution_with_retry_safety_fence(
            str(scenario.approval_id),
            HostExecutionOutcome(
                status="finished",
                exit_code=0,
                result_text="durable receipt",
            ),
            spawn_fence,
        )
        if other_fence_position == "after_receipt":
            await boundary.before_sandbox_write()
        assert boundary.ambiguous_side_effect is True

        async with scenario.seed.factory() as session, session.begin():
            settled = await PrivateRunRepository(
                session,
                jobs=JobRepository(
                    session,
                    owner_ref_hasher=lambda _owner: JobOwnerRef(
                        "test",
                        "0" * 64,
                    ),
                    terminal_port=PrivateRunJobTerminalPort(),
                ),
            ).settle_execution(
                scope=scenario.seed.owner_a_scope,
                run_id=prepared.continuation_run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                outcome="failed",
                public_error_code="SIDE_EFFECT_STATE_UNKNOWN",
                ambiguous_side_effect=True,
                retryable_failure=True,
                retry_initial_seconds=2,
                retry_max_seconds=300,
            )
            assert settled.run.status == "error"

        async with scenario.seed.factory() as session:
            job = await session.get(JobRow, claim.job_id)
            assert job is not None
            assert job.status == "dead"
            assert job.retry_safety == "unknown"
            assert job.public_error_code == "SIDE_EFFECT_STATE_UNKNOWN"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_state", ["intent_recorded", "delivered"])
async def test_receipt_and_output_delivery_survive_lease_loss_without_respawn(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    delivery_state: str,
) -> None:
    prepared = await _prepare_assigned_output_obligation(
        migrated_postgres_database_url,
    )
    scenario = prepared.scenario
    try:
        first_claim, boundary, port = await _claimed_output_delivery_runtime(
            prepared,
        )
        spawn_fence = await boundary.before_sandbox_exec()
        await _authorize_claimed_spawn(port, scenario.approval_id)
        await port.complete_host_execution_with_retry_safety_fence(
            str(scenario.approval_id),
            HostExecutionOutcome(
                status="finished",
                exit_code=0,
                result_text="durable receipt",
            ),
            spawn_fence,
        )
        required_path = "/mnt/user-data/outputs/required.txt"
        await port.record_output_delivery_intent(
            (required_path,),
            tool_call_id="present-before-crash",
        )
        if delivery_state == "delivered":
            async with scenario.seed.factory() as session, session.begin():
                candidate = await session.scalar(
                    sa.select(PrivateFileRow).where(
                        PrivateFileRow.project_id == scenario.seed.owner_a.project_id,
                        PrivateFileRow.owner_user_id == str(scenario.seed.owner_a.user_id),
                        PrivateFileRow.thread_id == scenario.thread_id,
                        PrivateFileRow.logical_path == "outputs/required.txt",
                        PrivateFileRow.status == "ready",
                    ),
                )
                assert candidate is not None
                artifact = PrivateArtifactRow(
                    project_id=scenario.seed.owner_a.project_id,
                    owner_user_id=str(scenario.seed.owner_a.user_id),
                    thread_id=scenario.thread_id,
                    run_id=prepared.continuation_run_id,
                    file_id=candidate.id,
                    display_name="required.txt",
                    media_type="text/plain",
                    artifact_metadata={"logical_path": "outputs/required.txt"},
                )
                session.add(artifact)
                await session.flush()
                assert await port.deliver_output_obligation_in_session(
                    session,
                    artifact_id=artifact.id,
                    logical_path="outputs/required.txt",
                )

        assert await port.output_delivery_status() == delivery_state
        expired_at = datetime.now(UTC) - timedelta(seconds=1)
        async with scenario.seed.factory() as session, session.begin():
            job = await session.get(JobRow, first_claim.job_id, with_for_update=True)
            run = await session.get(
                RunRow,
                prepared.continuation_run_id,
                with_for_update=True,
            )
            assert job is not None and run is not None
            assert job.retry_safety == "safe"
            job.lease_expires_at = expired_at
            run.execution_lease_expires_at = expired_at

        async with scenario.seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            replay_claim = await jobs.claim_next(
                worker_id=scenario.worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
                execution_domain_affinity=_execution_domain().affinity,
            )
            assert replay_claim is not None
            assert replay_claim.job_id == first_claim.job_id
            assert replay_claim.attempt_id != first_claim.attempt_id
            assert await jobs.mark_running(
                replay_claim.job_id,
                lease_token=replay_claim.lease_token,
            )
            await PrivateRunRepository(session).begin_execution(
                scope=scenario.seed.owner_a_scope,
                run_id=prepared.continuation_run_id,
                job_id=replay_claim.job_id,
                lease_token=replay_claim.lease_token,
                origin_trace_id=replay_claim.origin_trace_id,
            )

        replay_port = WorkerHostExecutionApprovalPort(
            scenario.seed.factory,
            context=scenario.seed.owner_a,
            claim=replay_claim,
            thread_id=scenario.thread_id,
            request_ttl_seconds=300,
            provider_policy=_provider_policy(),
            execution_domain=_execution_domain(),
            continuation_approval_id=str(scenario.approval_id),
        )

        def provider_must_not_be_read() -> object:
            raise AssertionError("receipt replay must never access a sandbox")

        monkeypatch.setattr(
            "deerflow.runtime.host_execution_runner.get_sandbox_provider",
            provider_must_not_be_read,
        )
        app_config = AppConfig(
            sandbox=SandboxConfig(
                use="deerflow.sandbox.local:LocalSandboxProvider",
                allow_host_bash=False,
                host_execution_approval={
                    "mode": "approval_required",
                    "execution_domain_id": "mac-primary",
                },
                bash_command_timeout=60,
            ),
        )
        replay_input = await execute_frozen_host_execution_continuation(
            approval_port=replay_port,
            app_config=app_config,
            runtime_context={
                "thread_id": scenario.thread_id,
                "run_id": prepared.continuation_run_id,
            },
            file_authority=None,
            graph_input={"messages": []},
            continuation_required=True,
        )
        assert "durable receipt" in replay_input["messages"][0]["content"]
        if delivery_state == "intent_recorded":
            assert required_path in replay_input["messages"][0]["content"]
        else:
            assert "must call present_files" not in replay_input["messages"][0]["content"]
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_public_cancel_atomically_closes_queued_continuation_obligation(
    migrated_postgres_database_url: str,
) -> None:
    prepared = await _prepare_assigned_output_obligation(
        migrated_postgres_database_url,
    )
    scenario = prepared.scenario
    try:
        await PrivateRunService(
            scenario.seed.factory,
            quota=prepared.hooks,
            audit=prepared.hooks,
        ).cancel(
            scenario.seed.owner_a,
            scenario.thread_id,
            prepared.continuation_run_id,
        )

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            continuation_run = await session.get(
                RunRow,
                prepared.continuation_run_id,
            )
            continuation_job = await session.get(
                JobRow,
                prepared.continuation_job_id,
            )
            assert approval is not None and approval.status == "cancelled"
            assert obligation is not None and obligation.status == "cancelled"
            assert continuation_run is not None
            assert continuation_run.status == "interrupted"
            assert continuation_job is not None
            assert continuation_job.status == "cancelled"
        assert prepared.hooks.cancel_requested_runs == [
            prepared.continuation_run_id,
        ]
        assert prepared.hooks.released_runs == [prepared.continuation_run_id]
        assert prepared.hooks.terminal_runs == [prepared.continuation_run_id]
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_public_cancel_closes_approved_obligation_behind_leased_job(
    migrated_postgres_database_url: str,
) -> None:
    prepared = await _prepare_assigned_output_obligation(
        migrated_postgres_database_url,
    )
    scenario = prepared.scenario
    try:
        async with scenario.seed.factory() as session, session.begin():
            claim = await JobRepository(session).claim_next(
                worker_id=scenario.worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
                execution_domain_affinity=_execution_domain().affinity,
            )
            assert claim is not None
            assert claim.job_id == prepared.continuation_job_id

        await PrivateRunService(
            scenario.seed.factory,
            quota=prepared.hooks,
            audit=prepared.hooks,
        ).cancel(
            scenario.seed.owner_a,
            scenario.thread_id,
            prepared.continuation_run_id,
        )

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            continuation_run = await session.get(
                RunRow,
                prepared.continuation_run_id,
            )
            continuation_job = await session.get(
                JobRow,
                prepared.continuation_job_id,
            )
            assert approval is not None and approval.status == "cancelled"
            assert obligation is not None and obligation.status == "cancelled"
            assert continuation_run is not None
            assert continuation_run.status == "pending"
            assert continuation_run.cancel_requested_at is not None
            assert continuation_job is not None
            assert continuation_job.status == "leased"
            assert continuation_job.cancel_requested_at is not None
        assert prepared.hooks.cancel_requested_runs == [
            prepared.continuation_run_id,
        ]
        assert prepared.hooks.released_runs == []
        assert prepared.hooks.terminal_runs == []
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow_once", "deny"])
async def test_decision_uses_clock_after_approval_lock_wait(
    migrated_postgres_database_url: str,
    decision: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    admission = _NeverContinuationAdmission()
    hooks = _ApprovalLifecycleHooks()
    try:
        await _settle_clock_scenario_pending(scenario)
        service = ExecutionApprovalService(
            scenario.seed.factory,
            admission=admission,
            provider_policy=_provider_policy(),
            quota=hooks,
            run_audit=hooks,
        )
        async with scenario.seed.factory() as blocker:
            async with blocker.begin():
                row = await blocker.get(
                    ExecutionApprovalRequestRow,
                    scenario.approval_id,
                    with_for_update=True,
                )
                assert row is not None and row.status == "pending"
                row.expires_at = datetime.now(UTC) + timedelta(milliseconds=250)
                await blocker.flush()
                decision_task = asyncio.create_task(
                    service.decide(
                        scenario.seed.owner_a,
                        thread_id=scenario.thread_id,
                        source_run_id=scenario.source_run_id,
                        approval_id=scenario.approval_id,
                        decision=decision,
                        expected_version=row.version,
                        idempotency_key=uuid.uuid4(),
                    )
                )
                await asyncio.sleep(0.4)
                assert not decision_task.done()
        projection = await asyncio.wait_for(decision_task, timeout=5)
        assert projection.approval is not None
        assert projection.approval["status"] == "expired"
        assert admission.calls == 0
        async with scenario.seed.factory() as session:
            row = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            assert row is not None and row.status == "expired"
            assert row.decision is None
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_approved_unlinked_recovery_uses_clock_after_lock_wait(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    hooks = _ApprovalLifecycleHooks()
    try:
        await _settle_clock_scenario_pending(scenario)
        failing_service = ExecutionApprovalService(
            scenario.seed.factory,
            admission=_FailingContinuationAdmission(),
            provider_policy=_provider_policy(),
            quota=hooks,
            run_audit=hooks,
        )
        with pytest.raises(PrivateWorkUnavailable):
            await failing_service.decide(
                scenario.seed.owner_a,
                thread_id=scenario.thread_id,
                source_run_id=scenario.source_run_id,
                approval_id=scenario.approval_id,
                decision="allow_once",
                expected_version=2,
                idempotency_key=uuid.uuid4(),
            )
        admission = _NeverContinuationAdmission()
        recovery_service = ExecutionApprovalService(
            scenario.seed.factory,
            admission=admission,
            provider_policy=_provider_policy(),
            quota=hooks,
            run_audit=hooks,
        )
        async with scenario.seed.factory() as blocker:
            async with blocker.begin():
                row = await blocker.get(
                    ExecutionApprovalRequestRow,
                    scenario.approval_id,
                    with_for_update=True,
                )
                assert row is not None and row.status == "approved"
                assert row.continuation_run_id is None
                row.expires_at = datetime.now(UTC) + timedelta(milliseconds=250)
                await blocker.flush()
                recovery_task = asyncio.create_task(
                    recovery_service.decide(
                        scenario.seed.owner_a,
                        thread_id=scenario.thread_id,
                        source_run_id=scenario.source_run_id,
                        approval_id=scenario.approval_id,
                        decision="allow_once",
                        expected_version=row.version,
                        idempotency_key=uuid.uuid4(),
                    )
                )
                await asyncio.sleep(0.4)
                assert not recovery_task.done()
        projection = await asyncio.wait_for(recovery_task, timeout=5)
        assert projection.approval is not None
        assert projection.approval["status"] == "expired"
        assert admission.calls == 0
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["active", "get"])
async def test_reader_uses_clock_after_approval_lock_wait(
    migrated_postgres_database_url: str,
    reader: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    hooks = _ApprovalLifecycleHooks()
    try:
        await _settle_clock_scenario_pending(scenario)
        service = ExecutionApprovalService(
            scenario.seed.factory,
            admission=_NeverContinuationAdmission(),
            provider_policy=_provider_policy(),
            quota=hooks,
            run_audit=hooks,
        )
        async with scenario.seed.factory() as blocker:
            async with blocker.begin():
                row = await blocker.get(
                    ExecutionApprovalRequestRow,
                    scenario.approval_id,
                    with_for_update=True,
                )
                assert row is not None and row.status == "pending"
                row.expires_at = datetime.now(UTC) + timedelta(milliseconds=250)
                await blocker.flush()
                if reader == "active":
                    read_task = asyncio.create_task(
                        service.active(
                            scenario.seed.owner_a,
                            scenario.thread_id,
                        )
                    )
                else:
                    read_task = asyncio.create_task(
                        service.get(
                            scenario.seed.owner_a,
                            scenario.thread_id,
                            scenario.approval_id,
                        )
                    )
                await asyncio.sleep(0.4)
                assert not read_task.done()
        projection = await asyncio.wait_for(read_task, timeout=5)
        if reader == "active":
            assert projection.approval is None
        else:
            assert projection.approval is not None
            assert projection.approval["status"] == "expired"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stage_rechecks_source_lease_after_scope_lock_wait(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(
        migrated_postgres_database_url,
        stage=False,
    )
    try:
        async with scenario.seed.factory() as blocker:
            async with blocker.begin():
                await blocker.execute(sa.select(ProjectRow.id).where(ProjectRow.id == scenario.seed.owner_a.project_id).with_for_update(of=ProjectRow))
                expires_at = datetime.now(UTC) + timedelta(milliseconds=250)
                job = await blocker.get(JobRow, scenario.claim.job_id)
                run = await blocker.get(RunRow, scenario.source_run_id)
                assert job is not None and run is not None
                job.lease_expires_at = expires_at
                run.execution_lease_expires_at = expires_at
                await blocker.flush()
                stage_task = asyncio.create_task(
                    scenario.port.request_host_execution(
                        HostExecutionPlan(
                            source_tool_call_id="call-expired-stage-clock",
                            source_run_id=scenario.source_run_id,
                            source_thread_id=scenario.thread_id,
                            description="must not stage after lease expiry",
                            requested_command="python stale.py",
                            effective_command="cd /private/workspace && python stale.py",
                            shell="/bin/zsh",
                            cwd="/private/workspace",
                            timeout_seconds=60,
                            agent_path=("lead",),
                        )
                    )
                )
                await asyncio.sleep(0.4)
                assert not stage_task.done()
        result = await asyncio.wait_for(stage_task, timeout=5)
        assert result.status == "denied"
        async with scenario.seed.factory() as session:
            count = await session.scalar(sa.select(sa.func.count()).select_from(ExecutionApprovalRequestRow).where(ExecutionApprovalRequestRow.source_run_id == scenario.source_run_id))
            assert count == 0
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_settle_staged_uses_clock_after_approval_lock_wait(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    try:
        async with scenario.seed.factory() as session, session.begin():
            await PrivateRunRepository(session).settle_execution(
                scope=scenario.seed.owner_a_scope,
                run_id=scenario.source_run_id,
                job_id=scenario.claim.job_id,
                lease_token=scenario.claim.lease_token,
                outcome="succeeded",
            )

        async def settle() -> None:
            async with scenario.seed.factory() as session, session.begin():
                await settle_staged_execution_approvals(
                    session,
                    claim=scenario.claim,
                    succeeded=True,
                    suspended_approval_id=str(scenario.approval_id),
                    request_ttl_seconds=10,
                )

        async with scenario.seed.factory() as blocker:
            async with blocker.begin():
                row = await blocker.get(
                    ExecutionApprovalRequestRow,
                    scenario.approval_id,
                    with_for_update=True,
                )
                assert row is not None and row.status == "staged"
                settle_task = asyncio.create_task(settle())
                await asyncio.sleep(0.4)
                assert not settle_task.done()
                released_at = datetime.now(UTC)
        await asyncio.wait_for(settle_task, timeout=5)
        async with scenario.seed.factory() as session:
            row = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            assert row is not None and row.status == "pending"
            assert row.updated_at >= released_at
            assert row.expires_at - row.updated_at == timedelta(seconds=10)
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_source_settlement_rejects_lease_expired_during_job_lock_wait(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    try:

        async def settle_source() -> None:
            async with scenario.seed.factory() as session, session.begin():
                await PrivateRunRepository(session).settle_execution(
                    scope=scenario.seed.owner_a_scope,
                    run_id=scenario.source_run_id,
                    job_id=scenario.claim.job_id,
                    lease_token=scenario.claim.lease_token,
                    outcome="succeeded",
                )
                await settle_staged_execution_approvals(
                    session,
                    claim=scenario.claim,
                    succeeded=True,
                    suspended_approval_id=str(scenario.approval_id),
                    request_ttl_seconds=300,
                )

        async with scenario.seed.factory() as blocker:
            async with blocker.begin():
                job = await blocker.get(
                    JobRow,
                    scenario.claim.job_id,
                    with_for_update=True,
                )
                run = await blocker.get(
                    RunRow,
                    scenario.source_run_id,
                    with_for_update=True,
                )
                assert job is not None and run is not None
                expires_at = datetime.now(UTC) + timedelta(milliseconds=250)
                job.lease_expires_at = expires_at
                run.execution_lease_expires_at = expires_at
                await blocker.flush()
                settlement_task = asyncio.create_task(settle_source())
                await asyncio.sleep(0.4)
                assert not settlement_task.done()
        with pytest.raises(PrivateRunExecutionLeaseLost):
            await asyncio.wait_for(settlement_task, timeout=5)
        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            source_run = await session.get(RunRow, scenario.source_run_id)
            assert approval is not None and approval.status == "staged"
            assert source_run is not None and source_run.status == "running"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_source_suspension_seals_unpresented_output_obligation(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    try:
        async with scenario.seed.factory() as session, session.begin():
            first = await _add_source_output(
                session,
                scenario,
                logical_path="outputs/first.txt",
                sha256="1" * 64,
            )
            second = await _add_source_output(
                session,
                scenario,
                logical_path="outputs/second.txt",
                sha256="2" * 64,
            )
            await PrivateRunRepository(session).settle_execution(
                scope=scenario.seed.owner_a_scope,
                run_id=scenario.source_run_id,
                job_id=scenario.claim.job_id,
                lease_token=scenario.claim.lease_token,
                outcome="succeeded",
            )
            await settle_staged_execution_approvals(
                session,
                claim=scenario.claim,
                succeeded=True,
                suspended_approval_id=str(scenario.approval_id),
                request_ttl_seconds=300,
            )

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            candidates = tuple(
                (
                    await session.scalars(
                        sa.select(
                            ExecutionApprovalOutputDeliveryCandidateRow,
                        )
                        .where(
                            ExecutionApprovalOutputDeliveryCandidateRow.approval_id == scenario.approval_id,
                        )
                        .order_by(
                            ExecutionApprovalOutputDeliveryCandidateRow.logical_path,
                        )
                    )
                ).all()
            )
            assert approval is not None and approval.status == "pending"
            assert obligation is not None
            assert obligation.status == "deferred"
            assert obligation.mode == "any_one"
            assert obligation.continuation_run_id is None
            assert [candidate.file_id for candidate in candidates] == [
                first.id,
                second.id,
            ]
            assert [candidate.sha256 for candidate in candidates] == [
                "1" * 64,
                "2" * 64,
            ]
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_source_suspension_needs_no_obligation_after_source_artifact(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    try:
        async with scenario.seed.factory() as session, session.begin():
            output = await _add_source_output(
                session,
                scenario,
                logical_path="outputs/already-presented.txt",
                sha256="3" * 64,
            )
            session.add(
                PrivateArtifactRow(
                    project_id=scenario.seed.owner_a.project_id,
                    owner_user_id=str(scenario.seed.owner_a.user_id),
                    thread_id=scenario.thread_id,
                    run_id=scenario.source_run_id,
                    file_id=output.id,
                    display_name="already-presented.txt",
                    media_type="text/plain",
                    artifact_metadata={
                        "logical_path": "outputs/already-presented.txt",
                    },
                )
            )
            await PrivateRunRepository(session).settle_execution(
                scope=scenario.seed.owner_a_scope,
                run_id=scenario.source_run_id,
                job_id=scenario.claim.job_id,
                lease_token=scenario.claim.lease_token,
                outcome="succeeded",
            )
            await settle_staged_execution_approvals(
                session,
                claim=scenario.claim,
                succeeded=True,
                suspended_approval_id=str(scenario.approval_id),
                request_ttl_seconds=300,
            )

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            assert approval is not None and approval.status == "pending"
            assert obligation is None
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_continuation_intent_restore_and_artifact_delivery_are_idempotent(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    hooks = _ApprovalLifecycleHooks()
    try:
        async with scenario.seed.factory() as session, session.begin():
            await _add_source_output(
                session,
                scenario,
                logical_path="outputs/candidate.txt",
                sha256="5" * 64,
            )
        await _settle_clock_scenario_pending(scenario)
        projection = await ExecutionApprovalService(
            scenario.seed.factory,
            admission=_QueuedAffinityContinuationAdmission(
                scenario.seed.factory,
                hooks=hooks,
            ),
            provider_policy=_provider_policy(),
            quota=hooks,
            run_audit=hooks,
        ).decide(
            scenario.seed.owner_a,
            thread_id=scenario.thread_id,
            source_run_id=scenario.source_run_id,
            approval_id=scenario.approval_id,
            decision="allow_once",
            expected_version=2,
            idempotency_key=uuid.uuid4(),
        )
        assert projection.approval is not None
        continuation_run_id = projection.approval["continuation_run"]["run_id"]

        async with scenario.seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            continuation_claim = await jobs.claim_next(
                worker_id=scenario.worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
                execution_domain_affinity=_execution_domain().affinity,
            )
            assert continuation_claim is not None
            assert continuation_claim.run_id == continuation_run_id
            assert await jobs.mark_running(
                continuation_claim.job_id,
                lease_token=continuation_claim.lease_token,
            )
            await PrivateRunRepository(session).begin_execution(
                scope=scenario.seed.owner_a_scope,
                run_id=continuation_run_id,
                job_id=continuation_claim.job_id,
                lease_token=continuation_claim.lease_token,
                origin_trace_id=continuation_claim.origin_trace_id,
            )
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
                with_for_update=True,
            )
            assert approval is not None
            await assign_output_delivery_obligation(
                session,
                approval=approval,
                continuation_run_id=continuation_run_id,
                continuation_job_id=continuation_claim.job_id,
                now=datetime.now(UTC),
            )

        port = WorkerHostExecutionApprovalPort(
            scenario.seed.factory,
            context=scenario.seed.owner_a,
            claim=continuation_claim,
            thread_id=scenario.thread_id,
            request_ttl_seconds=300,
            provider_policy=_provider_policy(),
            execution_domain=_execution_domain(),
            continuation_approval_id=str(scenario.approval_id),
        )
        candidate_path = "/mnt/user-data/outputs/candidate.txt"
        extra_path = "/mnt/user-data/outputs/extra.txt"
        await port.record_output_delivery_intent(
            (candidate_path, extra_path),
            tool_call_id="present-continuation-1",
        )
        await port.record_output_delivery_intent(
            (extra_path, candidate_path),
            tool_call_id="present-continuation-1",
        )
        await port.record_output_delivery_intent(
            (candidate_path, extra_path),
            tool_call_id="present-continuation-replay",
        )
        with pytest.raises(ValueError, match="output delivery intent is unavailable"):
            await port.record_output_delivery_intent(
                (candidate_path,),
                tool_call_id="present-continuation-1",
            )
        with pytest.raises(ValueError, match="output delivery intent is unavailable"):
            await port.record_output_delivery_intent(
                (candidate_path,),
                tool_call_id="present-continuation-conflict",
            )
        assert await port.output_delivery_status() == "intent_recorded"
        assert await port.restore_output_delivery_intent_paths() == (
            candidate_path,
            extra_path,
        )
        assert await port.output_delivery_requirement_paths() == (candidate_path,)

        extra_file_id = uuid.uuid4()
        extra_content = b"x"
        extra_sha256 = hashlib.sha256(extra_content).hexdigest()
        async with scenario.seed.factory() as session, session.begin():
            file_repository = PrivateFileRepository(session)
            await file_repository.stage(
                scope=scenario.seed.owner_a_scope,
                thread_id=scenario.thread_id,
                kind="output",
                logical_path=(f"outputs/.deerflow-staging-{extra_file_id.hex}"),
                media_type="text/plain",
                created_by_run_id=continuation_run_id,
                file_id=extra_file_id,
            )
            await file_repository.append_chunk(
                scope=scenario.seed.owner_a_scope,
                thread_id=scenario.thread_id,
                file_id=extra_file_id,
                chunk_index=0,
                content=extra_content,
                size=len(extra_content),
                sha256=extra_sha256,
            )
            ready_files = tuple(
                (
                    await session.scalars(
                        sa.select(PrivateFileRow)
                        .where(
                            PrivateFileRow.project_id == scenario.seed.owner_a.project_id,
                            PrivateFileRow.owner_user_id == str(scenario.seed.owner_a.user_id),
                            PrivateFileRow.thread_id == scenario.thread_id,
                            PrivateFileRow.status == "ready",
                        )
                        .order_by(
                            PrivateFileRow.logical_path,
                            PrivateFileRow.id,
                        ),
                    )
                ).all(),
            )
            run = await session.get(
                RunRow,
                continuation_run_id,
                with_for_update=True,
            )
            assert run is not None
            run.finalization_status = "finalizing"

        manifest = AuthorityManifest(
            entries=tuple(
                AuthorityManifestEntry(
                    file_id=row.id,
                    logical_path=row.logical_path,
                    kind=row.kind,
                    media_type=row.media_type,
                    size=row.size,
                    sha256=row.sha256,
                    version=row.version,
                )
                for row in ready_files
            ),
            run_id=continuation_run_id,
        )
        after_files = tuple(
            _AfterFile(
                logical_path=row.logical_path,
                virtual_path=f"/mnt/user-data/{row.logical_path}",
                kind=row.kind,
                size=row.size,
                media_type=row.media_type,
            )
            for row in ready_files
        ) + (
            _AfterFile(
                logical_path="outputs/extra.txt",
                virtual_path="/mnt/user-data/outputs/extra.txt",
                kind="output",
                size=1,
                media_type="text/plain",
            ),
        )
        staged_extra = _StagedFile(
            id=extra_file_id,
            after=after_files[-1],
            size=1,
            sha256=extra_sha256,
        )

        class _FinalizationQuotaProbe:
            def __init__(self) -> None:
                self.reservations: list[tuple[uuid.UUID, int]] = []
                self.releases: list[tuple[uuid.UUID, int]] = []

            async def reserve_file(
                self,
                _session,
                _context,
                *,
                file_id: uuid.UUID,
                size: int,
            ) -> None:
                self.reservations.append((file_id, size))

            async def release_file(
                self,
                _session,
                _scope,
                *,
                file_id: uuid.UUID,
                size: int,
                request_id: str,
            ) -> None:
                del request_id
                self.releases.append((file_id, size))

        quota_probe = _FinalizationQuotaProbe()
        finalization_boundary = PrivateRunExecutionBoundary(
            scenario.seed.factory,
            context=scenario.seed.owner_a,
            claim=continuation_claim,
        )
        run_scope = PrivateFileRunScope(
            scenario.seed.owner_a,
            thread_id=scenario.thread_id,
            run_id=continuation_run_id,
            authorization_boundary=finalization_boundary,
        )
        finalizer = PrivateFileFinalizer(
            scenario.seed.factory,
            quota=quota_probe,
            output_delivery_port=port,
        )
        presented_logical_paths = (
            "outputs/candidate.txt",
            "outputs/extra.txt",
        )
        first_finalization = await finalizer._commit(
            run_scope,
            manifest,
            after_files,
            (staged_extra,),
            presented_logical_paths,
        )
        first_artifact_ids = tuple(artifact.id for artifact in first_finalization.artifacts)
        candidate_artifact = next(artifact for artifact in first_finalization.artifacts if artifact.metadata["logical_path"] == "outputs/candidate.txt")
        assert finalization_boundary.ambiguous_side_effect is False
        assert first_finalization.workspace_changes is not None
        assert first_finalization.workspace_changes.summary.created == 1
        assert first_finalization.workspace_changes.summary.additions == 1
        assert first_finalization.workspace_changes.summary.deletions == 0
        assert first_finalization.workspace_changes.files[0].size_after == 1
        assert first_finalization.workspace_changes.files[0].sha256_after == extra_sha256

        assert quota_probe.reservations == [(extra_file_id, 1)]
        assert quota_probe.releases == []

        # Simulate commit ACK loss after file promotion, Artifact delivery, and
        # quota reservation commit. A new Worker rebuilds its manifest from DB
        # authority, then must reuse every durable object without a new quota
        # mutation.
        async with scenario.seed.factory() as session, session.begin():
            replay_ready_files = tuple(
                (
                    await session.scalars(
                        sa.select(PrivateFileRow)
                        .where(
                            PrivateFileRow.project_id == scenario.seed.owner_a.project_id,
                            PrivateFileRow.owner_user_id == str(scenario.seed.owner_a.user_id),
                            PrivateFileRow.thread_id == scenario.thread_id,
                            PrivateFileRow.status == "ready",
                        )
                        .order_by(
                            PrivateFileRow.logical_path,
                            PrivateFileRow.id,
                        ),
                    )
                ).all(),
            )
            run = await session.get(
                RunRow,
                continuation_run_id,
                with_for_update=True,
            )
            assert run is not None
            run.finalization_status = "finalizing"
        replay_manifest = AuthorityManifest(
            entries=tuple(
                AuthorityManifestEntry(
                    file_id=row.id,
                    logical_path=row.logical_path,
                    kind=row.kind,
                    media_type=row.media_type,
                    size=row.size,
                    sha256=row.sha256,
                    version=row.version,
                )
                for row in replay_ready_files
            ),
            run_id=continuation_run_id,
        )
        replay_after_files = tuple(
            _AfterFile(
                logical_path=row.logical_path,
                virtual_path=f"/mnt/user-data/{row.logical_path}",
                kind=row.kind,
                size=row.size,
                media_type=row.media_type,
            )
            for row in replay_ready_files
        )
        replayed_finalization = await finalizer._commit(
            run_scope,
            replay_manifest,
            replay_after_files,
            (),
            presented_logical_paths,
        )
        assert tuple(artifact.id for artifact in replayed_finalization.artifacts) == first_artifact_ids
        assert finalization_boundary.ambiguous_side_effect is False
        assert quota_probe.reservations == [(extra_file_id, 1)]
        assert quota_probe.releases == []

        assert await port.output_delivery_status() == "delivered"
        assert await port.output_delivery_requirement_paths() == ()
        async with scenario.seed.factory() as session:
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            assert obligation is not None
            assert obligation.status == "delivered"
            assert obligation.satisfied_artifact_id == candidate_artifact.id
            artifact_count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(PrivateArtifactRow)
                .where(
                    PrivateArtifactRow.project_id == scenario.seed.owner_a.project_id,
                    PrivateArtifactRow.owner_user_id == str(scenario.seed.owner_a.user_id),
                    PrivateArtifactRow.thread_id == scenario.thread_id,
                    PrivateArtifactRow.run_id == continuation_run_id,
                    PrivateArtifactRow.deleted_at.is_(None),
                ),
            )
            assert artifact_count == 2
            promoted_extra = tuple(
                (
                    await session.scalars(
                        sa.select(PrivateFileRow).where(
                            PrivateFileRow.project_id == scenario.seed.owner_a.project_id,
                            PrivateFileRow.owner_user_id == str(scenario.seed.owner_a.user_id),
                            PrivateFileRow.thread_id == scenario.thread_id,
                            PrivateFileRow.logical_path == "outputs/extra.txt",
                            PrivateFileRow.status == "ready",
                        ),
                    )
                ).all(),
            )
            assert len(promoted_extra) == 1
            assert promoted_extra[0].id == extra_file_id
            job = await session.get(JobRow, continuation_claim.job_id)
            assert job is not None and job.retry_safety == "safe"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_typed_suspension_coordinate_mismatch_rolls_back_source_settlement(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    try:
        with pytest.raises(ExecutionApprovalPrivateLifecycleConflict):
            async with scenario.seed.factory() as session, session.begin():
                await PrivateRunRepository(session).settle_execution(
                    scope=scenario.seed.owner_a_scope,
                    run_id=scenario.source_run_id,
                    job_id=scenario.claim.job_id,
                    lease_token=scenario.claim.lease_token,
                    outcome="succeeded",
                )
                await settle_staged_execution_approvals(
                    session,
                    claim=scenario.claim,
                    succeeded=True,
                    suspended_approval_id=str(uuid.uuid4()),
                    request_ttl_seconds=300,
                )

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            source_run = await session.get(RunRow, scenario.source_run_id)
            assert approval is not None and approval.status == "staged"
            assert source_run is not None and source_run.status == "running"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_deny_cancels_deferred_output_delivery_obligation(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    hooks = _ApprovalLifecycleHooks()
    try:
        async with scenario.seed.factory() as session, session.begin():
            await _add_source_output(
                session,
                scenario,
                logical_path="outputs/denied.txt",
                sha256="4" * 64,
            )
        await _settle_clock_scenario_pending(scenario)
        projection = await ExecutionApprovalService(
            scenario.seed.factory,
            admission=_NeverContinuationAdmission(),
            provider_policy=_provider_policy(),
            quota=hooks,
            run_audit=hooks,
        ).decide(
            scenario.seed.owner_a,
            thread_id=scenario.thread_id,
            source_run_id=scenario.source_run_id,
            approval_id=scenario.approval_id,
            decision="deny",
            expected_version=2,
            idempotency_key=uuid.uuid4(),
        )
        assert projection.approval is not None
        assert projection.approval["status"] == "denied"
        async with scenario.seed.factory() as session:
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            assert obligation is not None
            assert obligation.status == "cancelled"
            assert obligation.terminal_at is not None
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pending_expiry_cancels_output_delivery_obligation(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    hooks = _ApprovalLifecycleHooks()
    try:
        async with scenario.seed.factory() as session, session.begin():
            await _add_source_output(
                session,
                scenario,
                logical_path="outputs/expired.txt",
                sha256="8" * 64,
            )
        await _settle_clock_scenario_pending(scenario)
        async with scenario.seed.factory() as session, session.begin():
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
                with_for_update=True,
            )
            assert approval is not None
            approval.expires_at = approval.created_at + timedelta(microseconds=1)

        projection = await ExecutionApprovalService(
            scenario.seed.factory,
            admission=_NeverContinuationAdmission(),
            provider_policy=_provider_policy(),
            quota=hooks,
            run_audit=hooks,
        ).active(
            scenario.seed.owner_a,
            scenario.thread_id,
        )
        assert projection.approval is None
        async with scenario.seed.factory() as session:
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            assert obligation is not None
            assert obligation.status == "cancelled"
            assert obligation.terminal_at is not None
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_approved_expiry_cancels_queued_continuation_and_obligation(
    migrated_postgres_database_url: str,
) -> None:
    prepared = await _prepare_assigned_output_obligation(
        migrated_postgres_database_url,
    )
    scenario = prepared.scenario
    try:
        async with scenario.seed.factory() as session, session.begin():
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
                with_for_update=True,
            )
            assert approval is not None and approval.status == "approved"
            approval.expires_at = approval.created_at + timedelta(microseconds=1)

        projection = await ExecutionApprovalService(
            scenario.seed.factory,
            admission=_NeverContinuationAdmission(),
            provider_policy=_provider_policy(),
            quota=prepared.hooks,
            run_audit=prepared.hooks,
        ).active(
            scenario.seed.owner_a,
            scenario.thread_id,
        )
        assert projection.approval is None
        async with scenario.seed.factory() as session:
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            continuation_run = await session.get(
                RunRow,
                prepared.continuation_run_id,
            )
            continuation_job = await session.get(
                JobRow,
                prepared.continuation_job_id,
            )
            assert obligation is not None and obligation.status == "cancelled"
            assert continuation_run is not None
            assert continuation_run.status == "interrupted"
            assert continuation_job is not None
            assert continuation_job.status == "cancelled"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_claimed_unknown_blocks_output_delivery_obligation(
    migrated_postgres_database_url: str,
) -> None:
    prepared = await _prepare_assigned_output_obligation(
        migrated_postgres_database_url,
    )
    scenario = prepared.scenario
    try:
        async with scenario.seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=scenario.worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
                execution_domain_affinity=_execution_domain().affinity,
            )
            assert claim is not None
            assert claim.job_id == prepared.continuation_job_id
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )
            await PrivateRunRepository(session).begin_execution(
                scope=scenario.seed.owner_a_scope,
                run_id=prepared.continuation_run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                origin_trace_id=claim.origin_trace_id,
            )
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
                with_for_update=True,
            )
            assert approval is not None
            claimed_at = max(
                approval.created_at + timedelta(seconds=1),
                datetime.now(UTC),
            )
            approval.status = "claimed"
            approval.execution_job_attempt_id = claim.attempt_id
            approval.claimed_at = claimed_at
            approval.updated_at = claimed_at
            approval.version += 1
            job = await session.get(JobRow, claim.job_id)
            run = await session.get(RunRow, prepared.continuation_run_id)
            assert job is not None and run is not None
            job.retry_safety = "unknown"
            job.lease_expires_at = claimed_at + timedelta(seconds=1)
            run.execution_lease_expires_at = claimed_at + timedelta(seconds=1)

        async with scenario.seed.factory() as session, session.begin():
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
                with_for_update=True,
            )
            assert approval is not None
            await reconcile_locked_execution_approval(
                session,
                approval,
                now=claimed_at + timedelta(seconds=91),
            )

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            assert approval is not None and approval.status == "unknown"
            assert obligation is not None
            assert obligation.status == "blocked_unknown"
            assert obligation.terminal_at is not None
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settled_status", "expected_status"),
    [("error", "failed"), ("interrupted", "cancelled")],
)
async def test_continuation_terminal_converges_output_delivery_obligation(
    migrated_postgres_database_url: str,
    settled_status: str,
    expected_status: str,
) -> None:
    prepared = await _prepare_assigned_output_obligation(
        migrated_postgres_database_url,
    )
    scenario = prepared.scenario
    try:
        async with scenario.seed.factory() as session, session.begin():
            await settle_continuation_output_delivery(
                session,
                approval_id_value=str(scenario.approval_id),
                project_id=scenario.seed.owner_a.project_id,
                owner_user_id=str(scenario.seed.owner_a.user_id),
                thread_id=scenario.thread_id,
                continuation_run_id=prepared.continuation_run_id,
                continuation_job_id=prepared.continuation_job_id,
                settled_status=settled_status,
                now=datetime.now(UTC),
            )
        async with scenario.seed.factory() as session:
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            assert obligation is not None
            assert obligation.status == expected_status
            assert obligation.terminal_at is not None
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_continuation_success_rejects_incomplete_output_delivery(
    migrated_postgres_database_url: str,
) -> None:
    prepared = await _prepare_assigned_output_obligation(
        migrated_postgres_database_url,
    )
    scenario = prepared.scenario
    try:
        with pytest.raises(OutputDeliveryObligationConflict):
            async with scenario.seed.factory() as session, session.begin():
                await settle_continuation_output_delivery(
                    session,
                    approval_id_value=str(scenario.approval_id),
                    project_id=scenario.seed.owner_a.project_id,
                    owner_user_id=str(scenario.seed.owner_a.user_id),
                    thread_id=scenario.thread_id,
                    continuation_run_id=prepared.continuation_run_id,
                    continuation_job_id=prepared.continuation_job_id,
                    settled_status="success",
                    now=datetime.now(UTC),
                )
        async with scenario.seed.factory() as session:
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            assert obligation is not None and obligation.status == "assigned"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_handler_rolls_back_success_with_incomplete_output_delivery(
    migrated_postgres_database_url: str,
) -> None:
    prepared = await _prepare_assigned_output_obligation(
        migrated_postgres_database_url,
    )
    scenario = prepared.scenario
    try:
        async with scenario.seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=scenario.worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
                execution_domain_affinity=_execution_domain().affinity,
            )
            assert claim is not None
            assert claim.job_id == prepared.continuation_job_id
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )
            await PrivateRunRepository(session).begin_execution(
                scope=scenario.seed.owner_a_scope,
                run_id=prepared.continuation_run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                origin_trace_id=claim.origin_trace_id,
            )

        handler = PrivateRunJobHandler(
            scenario.seed.factory,
            executor=SimpleNamespace(),
        )
        settlement = handler._settlement(
            claim,
            AgentExecutionResult.succeeded(),
            scope=scenario.seed.owner_a_scope,
        )
        with pytest.raises(LeaseLost):
            await settlement.commit()

        async with scenario.seed.factory() as session:
            continuation_run = await session.get(
                RunRow,
                prepared.continuation_run_id,
            )
            continuation_job = await session.get(
                JobRow,
                prepared.continuation_job_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            assert continuation_run is not None
            assert continuation_run.status == "running"
            assert continuation_job is not None
            assert continuation_job.status == "running"
            assert obligation is not None and obligation.status == "assigned"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "ambiguous_side_effect"),
    [
        (
            AgentExecutionResult.failed(
                "AGENT_EXECUTION_FAILED",
                retryable=False,
            ),
            True,
        ),
        (
            AgentExecutionResult.failed(
                "MODEL_OUTPUT_LIMIT",
                retryable=False,
            ),
            False,
        ),
        (AgentExecutionResult.cancelled(), False),
    ],
)
async def test_handler_claimed_terminal_keeps_obligation_blocked_unknown(
    migrated_postgres_database_url: str,
    result: AgentExecutionResult,
    ambiguous_side_effect: bool,
) -> None:
    prepared = await _prepare_assigned_output_obligation(
        migrated_postgres_database_url,
    )
    scenario = prepared.scenario
    try:
        claim = await _claim_assigned_output_obligation(prepared)
        handler = PrivateRunJobHandler(
            scenario.seed.factory,
            executor=SimpleNamespace(),
            job_repository_builder=lambda session: JobRepository(
                session,
                owner_ref_hasher=lambda _owner: JobOwnerRef(
                    "test",
                    "0" * 64,
                ),
                terminal_port=PrivateRunJobTerminalPort(),
            ),
        )
        settlement = handler._settlement(
            claim,
            result,
            scope=scenario.seed.owner_a_scope,
            ambiguous_side_effect=ambiguous_side_effect,
        )
        await settlement.commit()

        async with scenario.seed.factory() as session, session.begin():
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
                with_for_update=True,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            assert approval is not None and approval.status == "claimed"
            assert obligation is not None
            assert obligation.status == "blocked_unknown"
            await reconcile_locked_execution_approval(
                session,
                approval,
                now=claimed_execution_absolute_deadline(approval) + timedelta(microseconds=1),
            )

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            assert approval is not None and approval.status == "unknown"
            assert obligation is not None
            assert obligation.status == "blocked_unknown"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "execution_status",
    ["finished", "launch_failed"],
)
async def test_handler_dead_settlement_fails_finished_delivery_obligation(
    migrated_postgres_database_url: str,
    execution_status: str,
) -> None:
    prepared = await _prepare_assigned_output_obligation(
        migrated_postgres_database_url,
    )
    scenario = prepared.scenario
    try:
        claim = await _claim_assigned_output_obligation(prepared)
        async with scenario.seed.factory() as session, session.begin():
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
                with_for_update=True,
            )
            job = await session.get(JobRow, claim.job_id, with_for_update=True)
            assert approval is not None and approval.status == "claimed"
            assert job is not None
            completed_at = datetime.now(UTC)
            if execution_status == "finished":
                approval.spawn_authorized_at = approval.claimed_at
            approval.status = execution_status
            approval.version += 1
            approval.terminal_at = completed_at
            approval.updated_at = completed_at
            job.retry_safety = "safe"
            job.updated_at = completed_at
        handler = PrivateRunJobHandler(
            scenario.seed.factory,
            executor=SimpleNamespace(),
            job_repository_builder=lambda session: JobRepository(
                session,
                owner_ref_hasher=lambda _owner: JobOwnerRef(
                    "test",
                    "0" * 64,
                ),
                terminal_port=PrivateRunJobTerminalPort(),
            ),
        )
        settlement = handler._settlement(
            claim,
            AgentExecutionResult.failed(
                "AGENT_EXECUTION_FAILED",
                retryable=False,
            ),
            scope=scenario.seed.owner_a_scope,
        )
        await settlement.commit()

        async with scenario.seed.factory() as session:
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                scenario.approval_id,
            )
            run = await session.get(RunRow, prepared.continuation_run_id)
            job = await session.get(JobRow, prepared.continuation_job_id)
            assert approval is not None
            assert approval.status == execution_status
            assert obligation is not None and obligation.status == "failed"
            assert run is not None and run.status == "error"
            assert job is not None and job.status == "dead"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_approval_delete_cascades_obligation_but_preserves_candidate_file(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    try:
        async with scenario.seed.factory() as session, session.begin():
            candidate = await _add_source_output(
                session,
                scenario,
                logical_path="outputs/retained.txt",
                sha256="9" * 64,
            )
            candidate_id = candidate.id
        await _settle_clock_scenario_pending(scenario)
        async with scenario.seed.factory() as session, session.begin():
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
                with_for_update=True,
            )
            assert approval is not None
            await session.delete(approval)

        async with scenario.seed.factory() as session:
            assert (
                await session.get(
                    ExecutionApprovalOutputDeliveryObligationRow,
                    scenario.approval_id,
                )
                is None
            )
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(ExecutionApprovalOutputDeliveryCandidateRow)
                    .where(
                        ExecutionApprovalOutputDeliveryCandidateRow.approval_id == scenario.approval_id,
                    )
                )
                == 0
            )
            retained = await session.get(PrivateFileRow, candidate_id)
            assert retained is not None and retained.status == "ready"
    finally:
        await scenario.seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_ordinary_admission_uses_database_clock_for_recent_claim_gate(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    hooks = _ApprovalLifecycleHooks()
    try:
        await _settle_clock_scenario_pending(scenario)
        approved = await ExecutionApprovalService(
            scenario.seed.factory,
            admission=_QueuedAffinityContinuationAdmission(
                scenario.seed.factory,
                hooks=hooks,
            ),
            provider_policy=_provider_policy(),
            quota=hooks,
            run_audit=hooks,
        ).decide(
            scenario.seed.owner_a,
            thread_id=scenario.thread_id,
            source_run_id=scenario.source_run_id,
            approval_id=scenario.approval_id,
            decision="allow_once",
            expected_version=2,
            idempotency_key=uuid.uuid4(),
        )
        assert approved.approval is not None
        continuation_run_id = approved.approval["continuation_run"]["run_id"]

        async with scenario.seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            continuation_claim = await jobs.claim_next(
                worker_id=scenario.worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
                execution_domain_affinity=_execution_domain().affinity,
            )
            assert continuation_claim is not None
            assert continuation_claim.run_id == continuation_run_id
            assert await jobs.mark_running(
                continuation_claim.job_id,
                lease_token=continuation_claim.lease_token,
            )
            await PrivateRunRepository(session).begin_execution(
                scope=scenario.seed.owner_a_scope,
                run_id=continuation_run_id,
                job_id=continuation_claim.job_id,
                lease_token=continuation_claim.lease_token,
                origin_trace_id=continuation_claim.origin_trace_id,
            )

        async with scenario.seed.factory() as session, session.begin():
            approval = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            job = await session.get(JobRow, continuation_claim.job_id)
            run = await session.get(RunRow, continuation_run_id)
            assert approval is not None and approval.status == "approved"
            assert job is not None and run is not None
            claimed_at = datetime.now(UTC)
            approval.status = "claimed"
            approval.execution_job_attempt_id = continuation_claim.attempt_id
            approval.claimed_at = claimed_at
            approval.updated_at = claimed_at
            approval.version += 1
            job.retry_safety = "unknown"
            expired_lease_at = datetime.now(UTC) - timedelta(seconds=1)
            job.lease_expires_at = expired_lease_at
            run.execution_lease_expires_at = expired_lease_at

        monkeypatch.setattr(
            "app.private_work.run_admission.datetime",
            _HostClockOneDayAhead,
        )
        with pytest.raises(PrivateWorkConflict):
            await PrivateRunAdmissionService(
                scenario.seed.factory,
                quota=hooks,
                audit=hooks,
            ).admit(
                scenario.seed.owner_a,
                scenario.thread_id,
                PrivateRunCreate(
                    run_id=str(uuid.uuid4()),
                    kwargs={"input": {"messages": []}},
                ),
            )

        async with scenario.seed.factory() as session:
            retained = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            assert retained is not None and retained.status == "claimed"
            assert retained.terminal_at is None
    finally:
        await scenario.seed.engine.dispose()


class _ApprovalLifecycleHooks:
    def __init__(self) -> None:
        self.reserved_runs: list[str] = []
        self.admitted_runs: list[str] = []
        self.released_runs: list[str] = []
        self.cancel_requested_runs: list[str] = []
        self.terminal_runs: list[str] = []

    async def reserve_concurrent_run(
        self,
        _session,
        _context,
        run,
    ) -> None:
        self.reserved_runs.append(run.run_id)

    async def run_admitted(
        self,
        _session,
        _context,
        run,
        _job,
    ) -> None:
        self.admitted_runs.append(run.run_id)

    async def release_concurrent_run(
        self,
        _session,
        _scope,
        *,
        run_id: str,
        request_id: str,
    ) -> None:
        assert request_id
        self.released_runs.append(run_id)

    async def run_cancel_requested(
        self,
        _session,
        _context,
        *,
        run_id: str,
        job_id: uuid.UUID,
    ) -> None:
        assert job_id
        self.cancel_requested_runs.append(run_id)

    async def run_terminal(
        self,
        _session,
        _scope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        job_type: str,
        status: str,
        public_error_code: str | None,
        request_id: str,
    ) -> None:
        assert job_id and job_type == "private_run"
        assert status == "interrupted"
        assert public_error_code is None and request_id
        self.terminal_runs.append(run_id)


class _QueuedAffinityContinuationAdmission:
    """Persist a real queued continuation without exercising asset resolution."""

    def __init__(self, factory, *, hooks: _ApprovalLifecycleHooks) -> None:
        self._factory = factory
        self._hooks = hooks

    async def admit(self, context, thread_id, request, *, server_context):
        affinity = server_context.host_execution_domain_affinity
        approval_id = server_context.host_execution_approval_id
        assert affinity is not None and approval_id is not None
        request = replace(
            request,
            kwargs={
                **request.kwargs,
                "host_execution_approval_id": str(approval_id),
                "host_execution_decision_digest": (server_context.host_execution_decision_digest),
                "host_execution_domain_affinity": affinity,
            },
        )
        async with self._factory() as session, session.begin():
            approval = await session.get(
                ExecutionApprovalRequestRow,
                approval_id,
                with_for_update=True,
            )
            assert approval is not None and approval.status == "approved"
            runs = PrivateRunRepository(session)
            run = await runs.create(
                scope=context.resource_scope,
                thread_id=thread_id,
                request=request,
            )
            job = await PrivateRunJobRepository(session).enqueue(
                scope=JobScope(context.project_id, str(context.user_id)),
                run_id=run.run_id,
                origin_trace_id=run.origin_trace_id,
                execution_domain_affinity=affinity,
            )
            run = await runs.attach_job(
                scope=context.resource_scope,
                run_id=run.run_id,
                job_id=job.job_id,
            )
            approval.continuation_run_id = run.run_id
            approval.continuation_job_id = job.job_id
            approval.version += 1
            linked_at = datetime.now(UTC)
            approval.updated_at = linked_at
            await assign_output_delivery_obligation(
                session,
                approval=approval,
                continuation_run_id=run.run_id,
                continuation_job_id=job.job_id,
                now=linked_at,
            )
            await self._hooks.reserve_concurrent_run(session, context, run)
            await self._hooks.run_admitted(session, context, run, job)
            return SimpleNamespace(run=run, job=job)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_is_live", "expected_status"),
    [(True, "approved"), (False, "cancelled")],
)
async def test_approved_running_continuation_with_pending_run_uses_job_lease(
    migrated_postgres_database_url: str,
    *,
    lease_is_live: bool,
    expected_status: str,
) -> None:
    scenario = await _prepare_clock_scenario(migrated_postgres_database_url)
    hooks = _ApprovalLifecycleHooks()
    try:
        await _settle_clock_scenario_pending(scenario)
        service = ExecutionApprovalService(
            scenario.seed.factory,
            admission=_QueuedAffinityContinuationAdmission(
                scenario.seed.factory,
                hooks=hooks,
            ),
            provider_policy=_provider_policy(),
            quota=hooks,
            run_audit=hooks,
        )
        approved = await service.decide(
            scenario.seed.owner_a,
            thread_id=scenario.thread_id,
            source_run_id=scenario.source_run_id,
            approval_id=scenario.approval_id,
            decision="allow_once",
            expected_version=2,
            idempotency_key=uuid.uuid4(),
        )
        assert approved.approval is not None
        continuation_run_id = approved.approval["continuation_run"]["run_id"]

        async with scenario.seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            continuation_claim = await jobs.claim_next(
                worker_id=scenario.worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
                execution_domain_affinity=_execution_domain().affinity,
            )
            assert continuation_claim is not None
            assert continuation_claim.run_id == continuation_run_id
            assert await jobs.mark_running(
                continuation_claim.job_id,
                lease_token=continuation_claim.lease_token,
            )
            job = await session.get(JobRow, continuation_claim.job_id)
            run = await session.get(RunRow, continuation_run_id)
            latest_attempt = await session.scalar(sa.select(JobAttemptRow).where(JobAttemptRow.job_id == continuation_claim.job_id).order_by(JobAttemptRow.attempt_number.desc()).limit(1))
            assert job is not None and run is not None and latest_attempt is not None
            assert job.status == "running"
            assert job.retry_safety == "safe"
            assert run.status == "pending"
            assert job.lease_token_hash == latest_attempt.lease_token_hash
            assert latest_attempt.finished_at is None
            if lease_is_live:
                assert job.lease_expires_at is not None
                assert job.lease_expires_at > datetime.now(UTC)
            else:
                job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

        projection = await service.get(
            scenario.seed.owner_a,
            scenario.thread_id,
            scenario.approval_id,
        )
        assert projection.approval is not None
        assert projection.approval["status"] == expected_status

        async with scenario.seed.factory() as session:
            row = await session.get(
                ExecutionApprovalRequestRow,
                scenario.approval_id,
            )
            assert row is not None
            assert row.status == expected_status
    finally:
        await scenario.seed.engine.dispose()


def test_provider_policy_snapshot_is_derived_from_typed_app_config() -> None:
    config = AppConfig(
        sandbox=SandboxConfig(
            use="deerflow.sandbox.local:LocalSandboxProvider",
            allow_host_bash=False,
            bash_command_timeout=60,
            host_execution_approval={
                "mode": "approval_required",
                "request_ttl_seconds": 300,
                "max_timeout_seconds": 60,
                "execution_domain_id": "mac-primary",
            },
        ),
    )

    assert HostExecutionProviderPolicySnapshot.from_app_config(config) == (_provider_policy())


def test_provider_policy_snapshot_binds_local_mount_and_skill_mapping(
    tmp_path,
) -> None:
    host_path = tmp_path / "mounted-input"
    host_path.mkdir()
    config = AppConfig(
        sandbox=SandboxConfig(
            use="deerflow.sandbox.local:LocalSandboxProvider",
            allow_host_bash=False,
            bash_command_timeout=60,
            host_execution_approval={
                "mode": "approval_required",
                "request_ttl_seconds": 300,
                "max_timeout_seconds": 60,
                "execution_domain_id": "mac-primary",
            },
            mounts=[
                {
                    "host_path": str(host_path),
                    "container_path": "/mnt/custom/",
                    "read_only": True,
                },
            ],
        ),
        skills={"container_path": "/mnt/project-skills/"},
    )

    snapshot = HostExecutionProviderPolicySnapshot.from_app_config(config)

    assert snapshot.local_mounts == ((str(host_path.resolve()), "/mnt/custom", True),)
    assert snapshot.skills_container_path == "/mnt/project-skills"
    assert snapshot.execution_domain_id == "mac-primary"
    assert (
        HostExecutionProviderPolicySnapshot.from_payload(
            snapshot.to_payload(),
        )
        == snapshot
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_run_admission_atomically_expires_abandoned_approval(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    source_run_id = str(uuid.uuid4())
    worker_id = uuid.uuid4()
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="approval-admission-test",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=1,
                )
            )

        source = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(
                run_id=source_run_id,
                kwargs={"input": {"messages": []}},
            ),
        )
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
            )
            assert claim is not None
            assert claim.job_id == source.job.job_id
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )
            await PrivateRunRepository(session).begin_execution(
                scope=seed.owner_a_scope,
                run_id=source_run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                origin_trace_id=claim.origin_trace_id,
            )

        audit_keyring = AuditHmacKeyring(
            active_key_id="approval-atomic-test",
            _keys={"approval-atomic-test": b"a" * 32},
        )
        worker_audit_service = AuditService(None, audit_keyring)
        worker_audit = OperationalAuditSink(
            worker_audit_service,
            process_context=_bind_worker_audit_process(worker_audit_service),
        )
        gateway_audit_service = AuditService(None, audit_keyring)
        gateway_audit = OperationalAuditSink(
            gateway_audit_service,
            process_context=_bind_gateway_audit_process(
                gateway_audit_service,
            ),
        )
        plan = HostExecutionPlan(
            source_tool_call_id="call-expired-admission",
            source_run_id=source_run_id,
            source_thread_id=thread_id,
            description="stage before source completion",
            requested_command="python /mnt/user-data/workspace/expired.py",
            effective_command="cd /private/workspace && python expired.py",
            shell="/bin/zsh",
            cwd="/private/workspace",
            timeout_seconds=60,
            agent_path=("lead",),
        )
        failing_source_port = WorkerHostExecutionApprovalPort(
            seed.factory,
            context=seed.owner_a,
            claim=claim,
            thread_id=thread_id,
            request_ttl_seconds=300,
            provider_policy=_provider_policy(),
            execution_domain=_execution_domain(),
            audit=_FailingHostExecutionAudit("requested"),
        )
        with pytest.raises(RuntimeError, match="requested audit unavailable"):
            await failing_source_port.request_host_execution(plan)
        async with seed.factory() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(ExecutionApprovalRequestRow)
                    .where(
                        ExecutionApprovalRequestRow.project_id == seed.owner_a.project_id,
                        ExecutionApprovalRequestRow.owner_user_id == str(seed.owner_a.user_id),
                        ExecutionApprovalRequestRow.source_run_id == source_run_id,
                    ),
                )
                == 0
            )

        source_port = WorkerHostExecutionApprovalPort(
            seed.factory,
            context=seed.owner_a,
            claim=claim,
            thread_id=thread_id,
            request_ttl_seconds=300,
            provider_policy=_provider_policy(),
            execution_domain=_execution_domain(),
            audit=worker_audit,
        )
        staged = await source_port.request_host_execution(plan)
        assert staged.approval_id is not None
        approval_id = uuid.UUID(staged.approval_id)

        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).settle_execution(
                scope=seed.owner_a_scope,
                run_id=source_run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                outcome="succeeded",
            )
            await settle_staged_execution_approvals(
                session,
                claim=claim,
                succeeded=True,
                suspended_approval_id=str(approval_id),
                request_ttl_seconds=300,
                audit=worker_audit,
            )

        failing_decision_service = ExecutionApprovalService(
            seed.factory,
            admission=SimpleNamespace(),
            provider_policy=_provider_policy(),
            quota=_ApprovalLifecycleHooks(),
            run_audit=_ApprovalLifecycleHooks(),
            audit=_FailingHostExecutionAudit("decided"),
        )
        with pytest.raises(RuntimeError, match="decided audit unavailable"):
            await failing_decision_service.decide(
                seed.owner_a,
                thread_id=thread_id,
                source_run_id=source_run_id,
                approval_id=approval_id,
                decision="deny",
                expected_version=2,
                idempotency_key=uuid.uuid4(),
            )
        async with seed.factory() as session:
            pending = await session.get(
                ExecutionApprovalRequestRow,
                approval_id,
            )
            assert pending is not None
            assert pending.status == "pending"
            assert pending.decision is None

        async with seed.factory() as session, session.begin():
            abandoned = await session.get(
                ExecutionApprovalRequestRow,
                approval_id,
                with_for_update=True,
            )
            assert abandoned is not None and abandoned.status == "pending"
            # Stay inside the database timestamp constraint while making the
            # expiry unambiguously older than the later decision transaction.
            expired_at = abandoned.created_at + timedelta(microseconds=1)
            abandoned.expires_at = expired_at

        expired_projection = await ExecutionApprovalService(
            seed.factory,
            admission=SimpleNamespace(),
            provider_policy=_provider_policy(),
            quota=_ApprovalLifecycleHooks(),
            run_audit=_ApprovalLifecycleHooks(),
            audit=gateway_audit,
        ).decide(
            seed.owner_a,
            thread_id=thread_id,
            source_run_id=source_run_id,
            approval_id=approval_id,
            decision="deny",
            expected_version=2,
            idempotency_key=uuid.uuid4(),
        )
        assert expired_projection.approval is not None
        assert expired_projection.approval["status"] == "expired"
        async with seed.factory() as session:
            committed_expiry = await session.get(
                ExecutionApprovalRequestRow,
                approval_id,
            )
            committed_terminal_audit = await session.scalar(
                sa.select(AuditLogRow).where(
                    AuditLogRow.project_id == seed.owner_a.project_id,
                    AuditLogRow.action == AuditAction.HOST_EXECUTION_APPROVAL_TERMINAL.value,
                ),
            )
            assert committed_expiry is not None
            assert committed_expiry.status == "expired"
            assert committed_expiry.terminal_at is not None
            assert committed_terminal_audit is not None
            assert committed_terminal_audit.metadata_json == {
                "status": "expired",
            }

        admitted = await PrivateRunAdmissionService(
            seed.factory,
            audit=gateway_audit,
        ).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(
                run_id=str(uuid.uuid4()),
                kwargs={"input": {"messages": []}},
            ),
        )

        assert admitted.run.status == "pending"
        async with seed.factory() as session:
            abandoned = await session.get(
                ExecutionApprovalRequestRow,
                approval_id,
            )
            assert abandoned is not None
            assert abandoned.status == "expired"
            assert abandoned.terminal_at is not None
            approval_audits = (
                await session.scalars(
                    sa.select(AuditLogRow)
                    .where(
                        AuditLogRow.project_id == seed.owner_a.project_id,
                        AuditLogRow.action.in_(
                            (
                                AuditAction.HOST_EXECUTION_APPROVAL_REQUESTED.value,
                                AuditAction.HOST_EXECUTION_APPROVAL_AVAILABLE.value,
                                AuditAction.HOST_EXECUTION_APPROVAL_DECIDED.value,
                                AuditAction.HOST_EXECUTION_APPROVAL_TERMINAL.value,
                            ),
                        ),
                    )
                    .order_by(AuditLogRow.occurred_at, AuditLogRow.id),
                )
            ).all()
            assert [row.action for row in approval_audits] == [
                AuditAction.HOST_EXECUTION_APPROVAL_REQUESTED.value,
                AuditAction.HOST_EXECUTION_APPROVAL_AVAILABLE.value,
                AuditAction.HOST_EXECUTION_APPROVAL_TERMINAL.value,
            ]
            assert approval_audits[-1].metadata_json == {"status": "expired"}
            assert all(row.job_id is None for row in approval_audits)
            assert all(row.attempt_id is None for row in approval_audits)
            assert all(str(source_run_id) not in repr(row.metadata_json) for row in approval_audits)
            assert all(row.target_ref_hmac != source_run_id for row in approval_audits)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_source_retry_conflict_cancels_staged_approval_before_settlement(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    source_run_id = str(uuid.uuid4())
    worker_a = uuid.uuid4()
    worker_b = uuid.uuid4()
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            session.add_all(
                [
                    WorkerNodeRow(
                        id=worker_id,
                        version="approval-source-retry-test",
                        capabilities_json=["private_run"],
                        max_concurrent_jobs=1,
                    )
                    for worker_id in (worker_a, worker_b)
                ],
            )

        source = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(
                run_id=source_run_id,
                kwargs={"input": {"messages": []}},
            ),
        )
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim_a = await jobs.claim_next(
                worker_id=worker_a,
                capabilities=frozenset({"private_run"}),
                lease_seconds=60,
            )
            assert claim_a is not None and claim_a.job_id == source.job.job_id
            assert await jobs.mark_running(
                claim_a.job_id,
                lease_token=claim_a.lease_token,
            )
            await PrivateRunRepository(session).begin_execution(
                scope=seed.owner_a_scope,
                run_id=source_run_id,
                job_id=claim_a.job_id,
                lease_token=claim_a.lease_token,
                origin_trace_id=claim_a.origin_trace_id,
            )

        plan_a = HostExecutionPlan(
            source_tool_call_id="call-source-retry",
            source_run_id=source_run_id,
            source_thread_id=thread_id,
            description="first frozen plan",
            requested_command="python /mnt/user-data/workspace/a.py",
            effective_command="cd /private/workspace && python a.py",
            shell="/bin/zsh",
            cwd="/private/workspace",
            timeout_seconds=60,
        )
        port_a = WorkerHostExecutionApprovalPort(
            seed.factory,
            context=seed.owner_a,
            claim=claim_a,
            thread_id=thread_id,
            request_ttl_seconds=300,
            provider_policy=_provider_policy(),
            execution_domain=_execution_domain(device_fingerprint="a" * 64),
        )
        staged = await port_a.request_host_execution(plan_a)
        assert staged.approval_id is not None
        approval_id = uuid.UUID(staged.approval_id)

        retry_at = datetime.now(UTC)
        async with seed.factory() as session, session.begin():
            source_job = await session.get(JobRow, claim_a.job_id)
            source_run = await session.get(RunRow, source_run_id)
            assert source_job is not None and source_run is not None
            source_job.lease_expires_at = retry_at - timedelta(seconds=1)
            source_run.execution_lease_expires_at = retry_at - timedelta(
                seconds=1,
            )

        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim_b = await jobs.claim_next(
                worker_id=worker_b,
                capabilities=frozenset({"private_run"}),
                lease_seconds=60,
                now=retry_at,
            )
            assert claim_b is not None and claim_b.job_id == claim_a.job_id
            assert claim_b.attempt_id != claim_a.attempt_id
            assert await jobs.mark_running(
                claim_b.job_id,
                lease_token=claim_b.lease_token,
                now=retry_at,
            )
            await PrivateRunRepository(session).begin_execution(
                scope=seed.owner_a_scope,
                run_id=source_run_id,
                job_id=claim_b.job_id,
                lease_token=claim_b.lease_token,
                origin_trace_id=claim_b.origin_trace_id,
            )

        plan_b = HostExecutionPlan(
            source_tool_call_id=plan_a.source_tool_call_id,
            source_run_id=source_run_id,
            source_thread_id=thread_id,
            description="conflicting retry plan",
            requested_command="python /mnt/user-data/workspace/b.py",
            effective_command="cd /private/workspace && python b.py",
            shell="/bin/zsh",
            cwd="/private/workspace",
            timeout_seconds=60,
        )
        port_b = WorkerHostExecutionApprovalPort(
            seed.factory,
            context=seed.owner_a,
            claim=claim_b,
            thread_id=thread_id,
            request_ttl_seconds=300,
            provider_policy=_provider_policy(),
            execution_domain=_execution_domain(device_fingerprint="b" * 64),
        )
        conflict = await port_b.request_host_execution(plan_b)
        assert conflict.status == "denied"
        assert conflict.reason_code == "approval_request_conflict"

        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).settle_execution(
                scope=seed.owner_a_scope,
                run_id=source_run_id,
                job_id=claim_b.job_id,
                lease_token=claim_b.lease_token,
                outcome="succeeded",
            )
            await settle_staged_execution_approvals(
                session,
                claim=claim_b,
                succeeded=True,
                suspended_approval_id=None,
                request_ttl_seconds=300,
            )

        async with seed.factory() as session:
            row = await session.get(ExecutionApprovalRequestRow, approval_id)
            assert row is not None
            assert row.status == "cancelled"
            assert row.terminal_at is not None
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(ExecutionApprovalRequestRow)
                    .where(
                        ExecutionApprovalRequestRow.project_id == seed.owner_a.project_id,
                        ExecutionApprovalRequestRow.owner_user_id == str(seed.owner_a.user_id),
                        ExecutionApprovalRequestRow.thread_id == thread_id,
                        ExecutionApprovalRequestRow.status.in_(
                            ("staged", "pending", "approved", "claimed"),
                        ),
                    ),
                )
                == 0
            )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_wrong_affinity_continuation_is_unclaimable_and_ttl_cleanup_releases_it(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    source_run_id = str(uuid.uuid4())
    worker_id = uuid.uuid4()
    wrong_worker_id = uuid.uuid4()
    hooks = _ApprovalLifecycleHooks()
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            session.add_all(
                [
                    WorkerNodeRow(
                        id=value,
                        version="approval-affinity-ttl-test",
                        capabilities_json=["private_run"],
                        max_concurrent_jobs=1,
                    )
                    for value in (worker_id, wrong_worker_id)
                ],
            )

        source = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(
                run_id=source_run_id,
                kwargs={"input": {"messages": []}},
            ),
        )
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            source_claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
            )
            assert source_claim is not None
            assert source_claim.job_id == source.job.job_id
            assert await jobs.mark_running(
                source_claim.job_id,
                lease_token=source_claim.lease_token,
            )
            await PrivateRunRepository(session).begin_execution(
                scope=seed.owner_a_scope,
                run_id=source_run_id,
                job_id=source_claim.job_id,
                lease_token=source_claim.lease_token,
                origin_trace_id=source_claim.origin_trace_id,
            )

        domain = _execution_domain()
        plan = HostExecutionPlan(
            source_tool_call_id="call-affinity-ttl",
            source_run_id=source_run_id,
            source_thread_id=thread_id,
            description="queue exact-domain continuation",
            requested_command="python /mnt/user-data/workspace/ttl.py",
            effective_command="cd /private/workspace && python ttl.py",
            shell="/bin/zsh",
            cwd="/private/workspace",
            timeout_seconds=60,
        )
        source_port = WorkerHostExecutionApprovalPort(
            seed.factory,
            context=seed.owner_a,
            claim=source_claim,
            thread_id=thread_id,
            request_ttl_seconds=300,
            provider_policy=_provider_policy(),
            execution_domain=domain,
        )
        staged = await source_port.request_host_execution(plan)
        assert staged.approval_id is not None
        approval_id = uuid.UUID(staged.approval_id)
        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).settle_execution(
                scope=seed.owner_a_scope,
                run_id=source_run_id,
                job_id=source_claim.job_id,
                lease_token=source_claim.lease_token,
                outcome="succeeded",
            )
            await settle_staged_execution_approvals(
                session,
                claim=source_claim,
                succeeded=True,
                suspended_approval_id=str(approval_id),
                request_ttl_seconds=300,
            )

        admission = PrivateRunAdmissionService(
            seed.factory,
            quota=hooks,
            audit=hooks,
        )
        service = ExecutionApprovalService(
            seed.factory,
            admission=_QueuedAffinityContinuationAdmission(
                seed.factory,
                hooks=hooks,
            ),
            provider_policy=_provider_policy(),
            quota=hooks,
            run_audit=hooks,
        )
        approved = await service.decide(
            seed.owner_a,
            thread_id=thread_id,
            source_run_id=source_run_id,
            approval_id=approval_id,
            decision="allow_once",
            expected_version=2,
            idempotency_key=uuid.uuid4(),
        )
        assert approved.approval is not None
        continuation_run_id = approved.approval["continuation_run"]["run_id"]

        async with seed.factory() as session, session.begin():
            approval = await session.get(ExecutionApprovalRequestRow, approval_id)
            assert approval is not None and approval.continuation_job_id is not None
            continuation_job_id = approval.continuation_job_id
            continuation_job = await session.get(JobRow, continuation_job_id)
            assert continuation_job is not None
            assert continuation_job.status == "queued"
            assert continuation_job.execution_domain_affinity == domain.affinity
            wrong_claim = await JobRepository(session).claim_next(
                worker_id=wrong_worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=60,
                execution_domain_affinity=_execution_domain(
                    device_fingerprint="e" * 64,
                ).affinity,
            )
            assert wrong_claim is None
            # Stay inside the database timestamp constraint while making the
            # expiry unambiguously older than the later admission transaction.
            expired_at = approval.created_at + timedelta(microseconds=1)
            approval.expires_at = expired_at

        replacement_run_id = str(uuid.uuid4())
        replacement = await admission.admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(
                run_id=replacement_run_id,
                kwargs={"input": {"messages": []}},
            ),
        )
        assert replacement.run.status == "pending"
        projection = await service.get(seed.owner_a, thread_id, approval_id)
        assert projection.approval is not None
        assert projection.approval["status"] == "expired"
        async with seed.factory() as session:
            approval = await session.get(ExecutionApprovalRequestRow, approval_id)
            continuation_job = await session.get(JobRow, continuation_job_id)
            continuation_run = await session.get(RunRow, continuation_run_id)
            assert approval is not None and approval.status == "expired"
            assert continuation_job is not None
            assert continuation_job.status == "cancelled"
            assert continuation_job.lease_owner_id is None
            assert continuation_run is not None
            assert continuation_run.status == "interrupted"
        assert hooks.released_runs == [continuation_run_id]
        assert hooks.terminal_runs == [continuation_run_id]
        assert hooks.cancel_requested_runs == []
        assert replacement_run_id in hooks.reserved_runs
        assert replacement_run_id in hooks.admitted_runs
        assert (await service.active(seed.owner_a, thread_id)).approval is None
    finally:
        await seed.engine.dispose()


class _AtomicContinuationAdmission:
    """Small real-PostgreSQL admission seam for the domain-service test."""

    def __init__(
        self,
        factory,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        agent_id: uuid.UUID,
        agent_version_id: uuid.UUID,
        model_config_id: uuid.UUID,
        model_payload_checksum: str,
        runtime_policy_version_id: uuid.UUID,
        runtime_policy_schema_version: int,
        runtime_policy_checksum: str,
        worker_id: uuid.UUID,
        lease_token: str,
    ) -> None:
        self._factory = factory
        self._project_id = project_id
        self._owner_user_id = owner_user_id
        self._agent_id = agent_id
        self._agent_version_id = agent_version_id
        self._model_config_id = model_config_id
        self._model_payload_checksum = model_payload_checksum
        self._runtime_policy_version_id = runtime_policy_version_id
        self._runtime_policy_schema_version = runtime_policy_schema_version
        self._runtime_policy_checksum = runtime_policy_checksum
        self._worker_id = worker_id
        self.lease_token = lease_token
        self.calls = 0
        self.job: JobRow | None = None
        self.attempt: JobAttemptRow | None = None
        self.first_admission_entered = asyncio.Event()
        self.release_first_admission = asyncio.Event()
        self.fail_next = False
        self.channel_user_ids: list[str | None] = []

    async def admit(self, context, thread_id, request, *, server_context):
        self.calls += 1
        if self.calls == 1:
            self.first_admission_entered.set()
            await self.release_first_admission.wait()
        if self.fail_next:
            self.fail_next = False
            raise PrivateWorkUnavailable("continuation-admission-failed")
        assert request.metadata == {"execution_approval_continuation": True}
        assert request.kwargs["input"] == {"messages": []}
        assert "retry" not in str(request.kwargs).lower()
        assert server_context.host_execution_approval_id is not None
        assert server_context.host_execution_decision_digest is not None
        self.channel_user_ids.append(server_context.channel_user_id)
        async with self._factory() as session, session.begin():
            approval = await session.scalar(
                sa.select(ExecutionApprovalRequestRow)
                .where(
                    ExecutionApprovalRequestRow.id == server_context.host_execution_approval_id,
                )
                .with_for_update(),
            )
            assert approval is not None
            assert approval.status == "approved"
            assert approval.decision == "allow_once"
            assert approval.decision_request_digest == server_context.host_execution_decision_digest
            assert server_context.host_execution_domain_affinity is not None
            assert approval.execution_domain_affinity == server_context.host_execution_domain_affinity
            existing = await session.get(RunRow, request.run_id)
            if existing is None:
                job, attempt = await _running_job(
                    session,
                    project_id=self._project_id,
                    owner_user_id=self._owner_user_id,
                    thread_id=thread_id,
                    agent_id=self._agent_id,
                    worker_id=self._worker_id,
                    run_id=request.run_id,
                    lease_token=self.lease_token,
                    model_ref=str(self._model_config_id),
                    execution_domain_affinity=(server_context.host_execution_domain_affinity),
                )
                session.add(
                    RunAssetVersionRow(
                        project_id=self._project_id,
                        owner_user_id=self._owner_user_id,
                        thread_id=thread_id,
                        run_id=request.run_id,
                        asset_kind="agent",
                        dependency_order=0,
                        asset_scope="project",
                        asset_id=self._agent_id,
                        version_id=self._agent_version_id,
                        payload_checksum="a" * 64,
                        catalog_generation=1,
                        snapshot_json={},
                    ),
                )
                session.add(
                    RunModelConfigSnapshotRow(
                        project_id=self._project_id,
                        owner_user_id=self._owner_user_id,
                        thread_id=thread_id,
                        run_id=request.run_id,
                        purpose="chat",
                        model_config_id=self._model_config_id,
                        provider_payload={
                            "model_ref": str(self._model_config_id),
                            "max_input_tokens": 64_000,
                        },
                        payload_checksum=self._model_payload_checksum,
                        secret_generation_id=None,
                        secret_envelope_digest=None,
                    ),
                )
                session.add(
                    RunRuntimePolicySnapshotRow(
                        project_id=self._project_id,
                        owner_user_id=self._owner_user_id,
                        thread_id=thread_id,
                        run_id=request.run_id,
                        section="agent_runtime",
                        policy_version_id=self._runtime_policy_version_id,
                        schema_version=self._runtime_policy_schema_version,
                        payload_checksum=self._runtime_policy_checksum,
                    ),
                )
                approval.continuation_run_id = request.run_id
                approval.continuation_job_id = job.id
                approval.version += 1
                approval.updated_at = datetime.now(UTC)
                self.job = job
                self.attempt = attempt
            else:
                assert approval.continuation_run_id == request.run_id
                assert approval.continuation_job_id is not None
                self.job = await session.get(
                    JobRow,
                    approval.continuation_job_id,
                )
                self.attempt = await session.scalar(
                    sa.select(JobAttemptRow).where(
                        JobAttemptRow.job_id == approval.continuation_job_id,
                    ),
                )
            await session.flush()
            assert self.job is not None and self.attempt is not None
            return SimpleNamespace(
                run=SimpleNamespace(run_id=request.run_id),
                job=SimpleNamespace(job_id=self.job.id),
            )


@pytest.mark.parametrize(
    "snapshot_drift",
    [None, "model", "runtime", "execution_domain"],
)
@pytest.mark.asyncio
async def test_local_host_execution_approval_is_consumed_once_with_receipt(
    migrated_postgres_database_url: str,
    snapshot_drift: str | None,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()
    model_config_id = uuid.uuid4()
    model_payload_checksum = "b" * 64
    drifted_model_payload_checksum = "c" * 64
    drifted_runtime_policy_version_id = uuid.uuid4()
    drifted_runtime_policy_checksum = "d" * 64
    thread_id = str(uuid.uuid4())
    source_run_id = str(uuid.uuid4())
    source_token = uuid.uuid4().hex
    continuation_token = uuid.uuid4().hex
    try:
        async with factory() as session, session.begin():
            runtime_policy = await session.get(
                SystemRuntimePolicyRow,
                "agent_runtime",
            )
            assert runtime_policy is not None
            runtime_policy_version = await session.get(
                SystemRuntimePolicyVersionRow,
                runtime_policy.current_version_id,
            )
            assert runtime_policy_version is not None
            session.add(
                UserRow(
                    id=str(owner_id),
                    email="approval-owner@example.test",
                    username="approval_owner",
                    password_hash=None,
                    system_role="user",
                ),
            )
            await session.flush()
            session.add(
                ProjectRow(
                    id=project_id,
                    slug=f"approval-{uuid.uuid4().hex[:8]}",
                    display_name="Approval lifecycle",
                    created_by_user_id=str(owner_id),
                ),
            )
            await session.flush()
            session.add(
                ProjectMembershipRow(
                    id=membership_id,
                    project_id=project_id,
                    user_id=str(owner_id),
                    role="admin",
                ),
            )
            agent = AgentRow(
                id=agent_id,
                scope="project",
                project_id=project_id,
                slug="approval-agent",
                display_name="Approval Agent",
                created_by_user_id=str(owner_id),
            )
            session.add(agent)
            await session.flush()
            session.add(
                AgentVersionRow(
                    id=agent_version_id,
                    agent_id=agent_id,
                    version_number=1,
                    description="",
                    soul="test",
                    model_ref=str(model_config_id),
                    model_settings={},
                    tool_groups=[],
                    payload_checksum="a" * 64,
                    created_by_user_id=str(owner_id),
                ),
            )
            await session.flush()
            agent.current_version_id = agent_version_id
            model_config = SystemModelConfigRow(
                id=model_config_id,
                display_name="Test model",
                status="active",
                provider_adapter="openai",
                provider_model="test-model",
                max_input_tokens=64_000,
                settings={},
                supports_thinking=False,
                supports_reasoning_effort=False,
                supports_vision=False,
                payload_checksum=model_payload_checksum,
                current_secret_generation_id=None,
                secret_revision=0,
                revision=1,
                created_by_user_id=str(owner_id),
                updated_by_user_id=str(owner_id),
            )
            session.add(model_config)
            await session.flush()
            session.add(
                SystemRuntimePolicyVersionRow(
                    id=drifted_runtime_policy_version_id,
                    section="agent_runtime",
                    version_number=runtime_policy_version.version_number + 1,
                    schema_version=runtime_policy_version.schema_version,
                    value=runtime_policy_version.value,
                    payload_checksum=drifted_runtime_policy_checksum,
                    supersedes_version_id=runtime_policy_version.id,
                    created_by_user_id=str(owner_id),
                ),
            )
            await session.flush()
            session.add(
                ThreadMetaRow(
                    thread_id=thread_id,
                    assistant_id=str(agent_id),
                    owner_user_id=str(owner_id),
                    display_name="Approval lifecycle",
                    status="idle",
                    metadata_json={},
                    project_id=project_id,
                    agent_asset_id=agent_id,
                    agent_scope="project",
                ),
            )
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="test",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=2,
                ),
            )
            await session.flush()
            source_job, source_attempt = await _running_job(
                session,
                project_id=project_id,
                owner_user_id=str(owner_id),
                thread_id=thread_id,
                agent_id=agent_id,
                worker_id=worker_id,
                run_id=source_run_id,
                lease_token=source_token,
                model_ref=str(model_config_id),
            )
            session.add(
                RunAssetVersionRow(
                    project_id=project_id,
                    owner_user_id=str(owner_id),
                    thread_id=thread_id,
                    run_id=source_run_id,
                    asset_kind="agent",
                    dependency_order=0,
                    asset_scope="project",
                    asset_id=agent_id,
                    version_id=agent_version_id,
                    payload_checksum="a" * 64,
                    catalog_generation=1,
                    snapshot_json={},
                ),
            )
            session.add(
                RunModelConfigSnapshotRow(
                    project_id=project_id,
                    owner_user_id=str(owner_id),
                    thread_id=thread_id,
                    run_id=source_run_id,
                    purpose="chat",
                    model_config_id=model_config_id,
                    provider_payload={
                        "model_ref": str(model_config_id),
                        "max_input_tokens": 64_000,
                    },
                    payload_checksum=model_payload_checksum,
                    secret_generation_id=None,
                    secret_envelope_digest=None,
                ),
            )
            session.add(
                RunRuntimePolicySnapshotRow(
                    project_id=project_id,
                    owner_user_id=str(owner_id),
                    thread_id=thread_id,
                    run_id=source_run_id,
                    section="agent_runtime",
                    policy_version_id=runtime_policy_version.id,
                    schema_version=runtime_policy_version.schema_version,
                    payload_checksum=runtime_policy_version.payload_checksum,
                ),
            )

        project_context = ProjectContext(
            user_id=owner_id,
            project_id=project_id,
            membership_id=membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="approval-lifecycle",
        )
        context = PrivateWorkContext.from_project(project_context)
        source_claim = JobClaim(
            job_id=source_job.id,
            attempt_id=source_attempt.id,
            lease_token=source_token,
            job_type="private_run",
            scope=JobScope(project_id, str(owner_id)),
            run_id=source_run_id,
            occurrence_id=None,
            retry_safety="safe",
            cancel_requested=False,
            origin_trace_id=source_job.origin_trace_id,
        )
        source_plan = HostExecutionPlan(
            source_tool_call_id="call-approval",
            source_run_id=source_run_id,
            source_thread_id=thread_id,
            description="write a marker",
            requested_command="python /mnt/user-data/workspace/marker.py",
            effective_command="cd /private/workspace && python marker.py",
            shell="/bin/zsh",
            cwd="/private/workspace",
            timeout_seconds=60,
            agent_path=("lead",),
            channel_identity_mode="set",
            channel_user_id="source-channel-user",
        )
        source_port = WorkerHostExecutionApprovalPort(
            factory,
            context=context,
            claim=source_claim,
            thread_id=thread_id,
            request_ttl_seconds=300,
            provider_policy=_provider_policy(),
            execution_domain=_execution_domain(),
        )
        assert (await source_port.claim_frozen_host_execution()).status == ("not_applicable")
        staged = await source_port.request_host_execution(source_plan)
        assert staged.status == "pending"
        assert staged.approval_id is not None
        approval_id = uuid.UUID(staged.approval_id)

        staged_reader = ExecutionApprovalService(
            factory,
            admission=SimpleNamespace(),
            provider_policy=_provider_policy(),
            quota=_ApprovalLifecycleHooks(),
            run_audit=_ApprovalLifecycleHooks(),
        )
        staged_active_projection = await staged_reader.active(context, thread_id)
        staged_by_id_projection = await staged_reader.get(
            context,
            thread_id,
            approval_id,
        )
        assert staged_active_projection.approval is None
        assert staged_by_id_projection.approval is None

        async with factory() as session, session.begin():
            row = await session.get(ExecutionApprovalRequestRow, approval_id)
            assert row is not None and row.status == "staged"
            await settle_staged_execution_approvals(
                session,
                claim=source_claim,
                succeeded=True,
                suspended_approval_id=str(approval_id),
                request_ttl_seconds=300,
            )
            source_run = await session.get(RunRow, source_run_id)
            assert source_run is not None
            source_run.status = "success"

        pending_active_projection = await staged_reader.active(context, thread_id)
        pending_by_id_projection = await staged_reader.get(
            context,
            thread_id,
            approval_id,
        )
        assert pending_active_projection.approval is not None
        assert pending_active_projection.approval["status"] == "pending"
        assert pending_active_projection.approval["can_decide"] is True
        assert pending_by_id_projection.approval == pending_active_projection.approval

        admission = _AtomicContinuationAdmission(
            factory,
            project_id=project_id,
            owner_user_id=str(owner_id),
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            model_config_id=model_config_id,
            model_payload_checksum=(drifted_model_payload_checksum if snapshot_drift == "model" else model_payload_checksum),
            runtime_policy_version_id=(drifted_runtime_policy_version_id if snapshot_drift == "runtime" else runtime_policy_version.id),
            runtime_policy_schema_version=runtime_policy_version.schema_version,
            runtime_policy_checksum=(drifted_runtime_policy_checksum if snapshot_drift == "runtime" else runtime_policy_version.payload_checksum),
            worker_id=worker_id,
            lease_token=continuation_token,
        )
        service = ExecutionApprovalService(
            factory,
            admission=admission,
            provider_policy=_provider_policy(),
            quota=_ApprovalLifecycleHooks(),
            run_audit=_ApprovalLifecycleHooks(),
        )
        drifted_service = ExecutionApprovalService(
            factory,
            admission=admission,
            provider_policy=HostExecutionProviderPolicySnapshot(
                provider_use="deerflow.sandbox.local:LocalSandboxProvider",
                host_execution_mode="local_approval_required",
                allow_host_bash=False,
                bash_command_timeout=61,
                approval_max_timeout_seconds=60,
                request_ttl_seconds=300,
                execution_domain_id="mac-primary",
            ),
            quota=_ApprovalLifecycleHooks(),
            run_audit=_ApprovalLifecycleHooks(),
        )
        with pytest.raises(ExecutionApprovalConflict):
            await drifted_service.decide(
                context,
                thread_id=thread_id,
                source_run_id=source_run_id,
                approval_id=approval_id,
                decision="allow_once",
                expected_version=2,
                idempotency_key=uuid.uuid4(),
            )
        async with factory() as session:
            still_pending = await session.get(
                ExecutionApprovalRequestRow,
                approval_id,
            )
            assert still_pending is not None
            assert still_pending.status == "pending"
            assert still_pending.decision is None

        decision_key = uuid.uuid4()
        allow_task = asyncio.create_task(
            service.decide(
                context,
                thread_id=thread_id,
                source_run_id=source_run_id,
                approval_id=approval_id,
                decision="allow_once",
                expected_version=2,
                idempotency_key=decision_key,
            ),
        )
        await asyncio.wait_for(admission.first_admission_entered.wait(), timeout=5)
        with pytest.raises(ExecutionApprovalConflict):
            await service.decide(
                context,
                thread_id=thread_id,
                source_run_id=source_run_id,
                approval_id=approval_id,
                decision="deny",
                expected_version=2,
                idempotency_key=uuid.uuid4(),
            )
        admission.release_first_admission.set()
        approved = await allow_task
        assert approved.approval is not None
        assert approved.approval["status"] == "approved"
        assert admission.calls == 1
        assert admission.channel_user_ids == ["source-channel-user"]
        repeated = await service.decide(
            context,
            thread_id=thread_id,
            source_run_id=source_run_id,
            approval_id=approval_id,
            decision="allow_once",
            expected_version=2,
            idempotency_key=decision_key,
        )
        assert repeated.approval == approved.approval
        assert admission.calls == 1
        assert admission.job is not None and admission.attempt is not None
        continuation_job = admission.job
        continuation_attempt = admission.attempt
        continuation_run_id = continuation_job.run_id
        assert continuation_run_id is not None

        continuation_claim = JobClaim(
            job_id=continuation_job.id,
            attempt_id=continuation_attempt.id,
            lease_token=continuation_token,
            job_type="private_run",
            scope=JobScope(project_id, str(owner_id)),
            run_id=continuation_run_id,
            occurrence_id=None,
            retry_safety="safe",
            cancel_requested=False,
            origin_trace_id=continuation_job.origin_trace_id,
            execution_domain_affinity=(continuation_job.execution_domain_affinity),
        )
        continuation_port = WorkerHostExecutionApprovalPort(
            factory,
            context=context,
            claim=continuation_claim,
            thread_id=thread_id,
            request_ttl_seconds=300,
            continuation_approval_id=str(approval_id),
            provider_policy=_provider_policy(),
            execution_domain=(_execution_domain(device_fingerprint="e" * 64) if snapshot_drift == "execution_domain" else _execution_domain()),
        )
        if snapshot_drift is None:
            failing_claim_port = WorkerHostExecutionApprovalPort(
                factory,
                context=context,
                claim=continuation_claim,
                thread_id=thread_id,
                request_ttl_seconds=300,
                continuation_approval_id=str(approval_id),
                provider_policy=_provider_policy(),
                execution_domain=_execution_domain(),
                audit=_FailingHostExecutionAudit("claimed"),
            )
            with pytest.raises(RuntimeError, match="claimed audit unavailable"):
                await failing_claim_port.claim_frozen_host_execution()
            async with factory() as session:
                rolled_back_approval = await session.get(
                    ExecutionApprovalRequestRow,
                    approval_id,
                )
                rolled_back_job = await session.get(
                    JobRow,
                    continuation_job.id,
                )
                assert rolled_back_approval is not None
                assert rolled_back_approval.status == "approved"
                assert rolled_back_approval.execution_job_attempt_id is None
                assert rolled_back_job is not None
                assert rolled_back_job.retry_safety == "safe"
        frozen = await continuation_port.claim_frozen_host_execution()
        if snapshot_drift == "execution_domain":
            assert frozen.status == "denied"
            assert frozen.reason_code == "host_execution_domain_mismatch"
            async with factory() as session:
                row = await session.get(ExecutionApprovalRequestRow, approval_id)
                job = await session.get(JobRow, continuation_job.id)
                assert row is not None and row.status == "approved"
                assert job is not None and job.retry_safety == "safe"
            return
        if snapshot_drift is not None:
            assert frozen.status == "denied"
            assert frozen.reason_code == "host_execution_asset_closure_drift"
            async with factory() as session:
                row = await session.get(ExecutionApprovalRequestRow, approval_id)
                job = await session.get(JobRow, continuation_job.id)
                assert row is not None and row.status == "cancelled"
                assert job is not None and job.retry_safety == "safe"
            return
        assert frozen.status == "claimed"
        assert frozen.plan == source_plan
        spawn_window = await continuation_port.authorize_claimed_host_execution_spawn(
            str(approval_id),
        )
        assert spawn_window is not None and spawn_window > 0
        failing_completion_port = WorkerHostExecutionApprovalPort(
            factory,
            context=context,
            claim=continuation_claim,
            thread_id=thread_id,
            request_ttl_seconds=300,
            continuation_approval_id=str(approval_id),
            provider_policy=_provider_policy(),
            execution_domain=_execution_domain(),
            audit=_FailingHostExecutionAudit("terminal"),
        )
        finished_outcome = HostExecutionOutcome(
            status="finished",
            exit_code=0,
            result_text="marker written",
        )
        with pytest.raises(RuntimeError, match="terminal audit unavailable"):
            await failing_completion_port.complete_host_execution(
                str(approval_id),
                finished_outcome,
            )
        async with factory() as session:
            rolled_back_approval = await session.get(
                ExecutionApprovalRequestRow,
                approval_id,
            )
            rolled_back_receipt = await session.scalar(
                sa.select(ExecutionApprovalResultReceiptRow).where(
                    ExecutionApprovalResultReceiptRow.approval_id == approval_id,
                ),
            )
            rolled_back_job = await session.get(JobRow, continuation_job.id)
            assert rolled_back_approval is not None
            assert rolled_back_approval.status == "claimed"
            assert rolled_back_receipt is None
            assert rolled_back_job is not None
            assert rolled_back_job.retry_safety == "unknown"
        await continuation_port.complete_host_execution(
            str(approval_id),
            finished_outcome,
        )

        async with factory() as session:
            row = await session.get(ExecutionApprovalRequestRow, approval_id)
            receipt = await session.scalar(
                sa.select(ExecutionApprovalResultReceiptRow).where(
                    ExecutionApprovalResultReceiptRow.approval_id == approval_id,
                ),
            )
            job = await session.get(JobRow, continuation_job.id)
            assert row is not None and row.status == "finished"
            assert row.execution_job_attempt_id == continuation_attempt.id
            assert receipt is not None and receipt.exit_code == 0
            assert receipt.result_private_json["result_text"] == "marker written"
            assert job is not None and job.retry_safety == "safe"

        replay = await continuation_port.claim_frozen_host_execution()
        assert replay.status == "replay"
        assert replay.plan == source_plan
        assert replay.outcome is not None
        assert replay.outcome.status == "finished"
        assert replay.outcome.result_text == "marker written"

        next_plan = HostExecutionPlan(
            source_tool_call_id="call-next-command",
            source_run_id=continuation_run_id,
            source_thread_id=thread_id,
            description="run a newly requested command",
            requested_command="python /mnt/user-data/workspace/next.py",
            effective_command="cd /private/workspace && python next.py",
            shell="/bin/zsh",
            cwd="/private/workspace",
            timeout_seconds=60,
            agent_path=("lead",),
        )
        next_staged = await continuation_port.request_host_execution(next_plan)
        assert next_staged.status == "pending"
        assert next_staged.approval_id != str(approval_id)
        async with factory() as session:
            next_row = await session.get(
                ExecutionApprovalRequestRow,
                uuid.UUID(next_staged.approval_id or ""),
            )
            assert next_row is not None
            assert next_row.status == "staged"
            assert next_row.source_run_id == continuation_run_id
            assert next_row.source_job_id == continuation_job.id
            assert next_row.source_job_attempt_id == continuation_attempt.id

        next_approval_id = uuid.UUID(next_staged.approval_id or "")
        async with factory() as session, session.begin():
            await settle_staged_execution_approvals(
                session,
                claim=continuation_claim,
                succeeded=True,
                suspended_approval_id=str(next_approval_id),
                request_ttl_seconds=300,
            )
            continuation_run = await session.get(RunRow, continuation_run_id)
            assert continuation_run is not None
            continuation_run.status = "success"

        admission.fail_next = True
        with pytest.raises(PrivateWorkUnavailable):
            await service.decide(
                context,
                thread_id=thread_id,
                source_run_id=continuation_run_id,
                approval_id=next_approval_id,
                decision="allow_once",
                expected_version=2,
                idempotency_key=uuid.uuid4(),
            )
        async with factory() as session:
            approved_unlinked = await session.get(
                ExecutionApprovalRequestRow,
                next_approval_id,
            )
            assert approved_unlinked is not None
            assert approved_unlinked.status == "approved"
            assert approved_unlinked.continuation_run_id is None
            recovery_version = approved_unlinked.version

        # A browser refresh no longer has the original UUID. The durable
        # approved decision may still resume deterministic admission with a
        # new request idempotency key and its current CAS version.
        crash_approved = await service.decide(
            context,
            thread_id=thread_id,
            source_run_id=continuation_run_id,
            approval_id=next_approval_id,
            decision="allow_once",
            expected_version=recovery_version,
            idempotency_key=uuid.uuid4(),
        )
        assert admission.channel_user_ids[-1] is None
        assert crash_approved.approval is not None
        assert crash_approved.approval["status"] == "approved"
        assert admission.job is not None and admission.attempt is not None
        crash_job = admission.job
        crash_attempt = admission.attempt
        crash_run_id = crash_job.run_id
        assert crash_run_id is not None
        crash_claim = JobClaim(
            job_id=crash_job.id,
            attempt_id=crash_attempt.id,
            lease_token=continuation_token,
            job_type="private_run",
            scope=JobScope(project_id, str(owner_id)),
            run_id=crash_run_id,
            occurrence_id=None,
            retry_safety="safe",
            cancel_requested=False,
            origin_trace_id=crash_job.origin_trace_id,
            execution_domain_affinity=crash_job.execution_domain_affinity,
        )
        crash_port = WorkerHostExecutionApprovalPort(
            factory,
            context=context,
            claim=crash_claim,
            thread_id=thread_id,
            request_ttl_seconds=300,
            continuation_approval_id=str(next_approval_id),
            provider_policy=_provider_policy(),
            execution_domain=_execution_domain(),
        )
        crash_frozen = await crash_port.claim_frozen_host_execution()
        assert crash_frozen.status == "claimed"

        async with factory() as session:
            claimed_job = await session.get(JobRow, crash_job.id)
            assert claimed_job is not None
            assert claimed_job.retry_safety == "unknown"

        async with factory() as session, session.begin():
            receipt = await session.scalar(
                sa.select(ExecutionApprovalResultReceiptRow).where(
                    ExecutionApprovalResultReceiptRow.approval_id == approval_id,
                ),
            )
            assert receipt is not None
            await session.delete(receipt)
        with pytest.raises(PrivateWorkUnavailable):
            await service.get(context, thread_id, approval_id)

        lost_at = datetime.now(UTC) - timedelta(seconds=1)
        async with factory() as session, session.begin():
            claimed_job = await session.get(JobRow, crash_job.id)
            claimed_run = await session.get(RunRow, crash_run_id)
            assert claimed_job is not None and claimed_run is not None
            claimed_job.lease_expires_at = lost_at
            claimed_run.execution_lease_expires_at = lost_at

        reconciled = await service.get(context, thread_id, next_approval_id)
        assert reconciled.approval is not None
        assert reconciled.approval["status"] == "claimed"
        async with factory() as session:
            next_row = await session.get(ExecutionApprovalRequestRow, next_approval_id)
            crash_receipt = await session.scalar(
                sa.select(ExecutionApprovalResultReceiptRow).where(
                    ExecutionApprovalResultReceiptRow.approval_id == next_approval_id,
                ),
            )
            claimed_job = await session.get(JobRow, crash_job.id)
            assert next_row is not None and next_row.status == "claimed"
            assert next_row.claimed_at is not None
            claim_deadline = next_row.claimed_at + timedelta(seconds=91)
            assert crash_receipt is None
            assert claimed_job is not None
            assert claimed_job.retry_safety == "unknown"

        # A dead DB lease does not prove the Local subprocess ended. The same
        # row converges only after its frozen timeout plus settlement grace.
        async with factory() as session, session.begin():
            active = await lock_and_reconcile_active_execution_approval(
                session,
                project_id=project_id,
                owner_user_id=str(owner_id),
                thread_id=thread_id,
                now=claim_deadline,
            )
            assert active is None
        async with factory() as session:
            next_row = await session.get(
                ExecutionApprovalRequestRow,
                next_approval_id,
            )
            assert next_row is not None and next_row.status == "unknown"

        # A command staged during a real Job that subsequently dies is hidden
        # while the source Job is live, then becomes visible by id only after
        # terminalization in the same transaction as the Run.
        revived_until = datetime.now(UTC) + timedelta(minutes=5)
        async with factory() as session, session.begin():
            claimed_job = await session.get(JobRow, crash_job.id)
            claimed_run = await session.get(RunRow, crash_run_id)
            assert claimed_job is not None and claimed_run is not None
            claimed_job.lease_expires_at = revived_until
            claimed_run.execution_lease_expires_at = revived_until

        terminal_plan = HostExecutionPlan(
            source_tool_call_id="call-terminal-convergence",
            source_run_id=crash_run_id,
            source_thread_id=thread_id,
            description="command staged before worker death",
            requested_command="python /mnt/user-data/workspace/terminal.py",
            effective_command="cd /private/workspace && python terminal.py",
            shell="/bin/zsh",
            cwd="/private/workspace",
            timeout_seconds=60,
            agent_path=("lead",),
        )
        terminal_staged = await crash_port.request_host_execution(terminal_plan)
        assert terminal_staged.approval_id is not None
        terminal_approval_id = uuid.UUID(terminal_staged.approval_id)
        hidden_staged = await service.get(
            context,
            thread_id,
            terminal_approval_id,
        )
        assert hidden_staged.approval is None

        terminal_at = datetime.now(UTC)
        async with factory() as session, session.begin():
            claimed_job = await session.get(JobRow, crash_job.id)
            claimed_run = await session.get(RunRow, crash_run_id)
            assert claimed_job is not None and claimed_run is not None
            claimed_job.lease_expires_at = terminal_at - timedelta(seconds=1)
            claimed_run.execution_lease_expires_at = terminal_at - timedelta(
                seconds=1,
            )
            claim = await JobRepository(
                session,
                owner_ref_hasher=lambda _owner: JobOwnerRef("test", "0" * 64),
                terminal_port=PrivateRunJobTerminalPort(),
            ).claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=60,
                now=terminal_at,
                execution_domain_affinity=_execution_domain().affinity,
            )
            assert claim is None

        async with factory() as session:
            terminal_row = await session.get(
                ExecutionApprovalRequestRow,
                terminal_approval_id,
            )
            terminal_run = await session.get(RunRow, crash_run_id)
            assert terminal_row is not None
            assert terminal_row.status == "cancelled"
            assert terminal_row.terminal_at == terminal_at
            assert terminal_run is not None and terminal_run.status == "error"

        cancelled_projection = await service.get(
            context,
            thread_id,
            terminal_approval_id,
        )
        assert cancelled_projection.approval is not None
        assert cancelled_projection.approval["status"] == "cancelled"

        # The same terminal hook also closes a continuation that died before
        # claim, while preserving ambiguity once a claim could have spawned
        # the host process.  Reuse the valid persisted row to exercise both
        # database-constrained shapes without introducing another fixture.
        for active_status, expected_status, terminal_expected in (
            ("approved", "cancelled", True),
            ("claimed", "claimed", False),
        ):
            reactivated_at = datetime.now(UTC)
            async with factory() as session, session.begin():
                terminal_row = await session.get(
                    ExecutionApprovalRequestRow,
                    terminal_approval_id,
                    with_for_update=True,
                )
                assert terminal_row is not None
                terminal_row.status = active_status
                terminal_row.decision = "allow_once"
                terminal_row.decision_idempotency_key = "e" * 64
                terminal_row.decision_request_digest = "f" * 64
                terminal_row.decided_by_user_id = str(owner_id)
                terminal_row.decided_at = reactivated_at
                terminal_row.continuation_run_id = crash_run_id
                terminal_row.continuation_job_id = crash_job.id
                terminal_row.execution_job_attempt_id = crash_attempt.id if active_status == "claimed" else None
                terminal_row.claimed_at = reactivated_at if active_status == "claimed" else None
                terminal_row.expires_at = reactivated_at + timedelta(minutes=5)
                terminal_row.terminal_at = None
                terminal_row.version += 1
                terminal_row.updated_at = reactivated_at
                await session.flush()
                await PrivateRunJobTerminalPort().job_terminalized(
                    session,
                    JobTerminalEvent(
                        job_id=crash_job.id,
                        project_id=project_id,
                        owner_user_id=str(owner_id),
                        run_id=crash_run_id,
                        occurrence_id=None,
                        job_type="private_run",
                        status="dead",
                        retry_safety="unknown",
                        public_error_code="WORKER_LEASE_EXPIRED",
                        cancel_reason=None,
                        occurred_at=reactivated_at,
                        attempt_count=crash_job.attempt_count,
                        origin_trace_id=crash_job.origin_trace_id,
                    ),
                )

            async with factory() as session:
                converged = await session.get(
                    ExecutionApprovalRequestRow,
                    terminal_approval_id,
                )
            assert converged is not None
            assert converged.status == expected_status
            assert converged.terminal_at == (reactivated_at if terminal_expected else None)

        lazy_staged_at = datetime.now(UTC)
        async with factory() as session, session.begin():
            abandoned = await session.get(
                ExecutionApprovalRequestRow,
                terminal_approval_id,
                with_for_update=True,
            )
            assert abandoned is not None
            abandoned.status = "staged"
            abandoned.decision = None
            abandoned.decision_idempotency_key = None
            abandoned.decision_request_digest = None
            abandoned.decided_by_user_id = None
            abandoned.decided_at = None
            abandoned.continuation_run_id = None
            abandoned.continuation_job_id = None
            abandoned.execution_job_attempt_id = None
            abandoned.claimed_at = None
            abandoned.expires_at = lazy_staged_at + timedelta(minutes=5)
            abandoned.terminal_at = None
            abandoned.version += 1
            abandoned.updated_at = lazy_staged_at

        async with factory() as session, session.begin():
            active = await lock_and_reconcile_active_execution_approval(
                session,
                project_id=project_id,
                owner_user_id=str(owner_id),
                thread_id=thread_id,
                now=datetime.now(UTC),
            )
            assert active is None
            abandoned = await session.get(
                ExecutionApprovalRequestRow,
                terminal_approval_id,
            )
            assert abandoned is not None
            assert abandoned.status == "cancelled"

        # Ordinary admission lazily expires an abandoned active approval in
        # the same transaction, so the partial unique index cannot cause a
        # permanent 409 for this Thread.
        expired_at = datetime.now(UTC)
        async with factory() as session, session.begin():
            abandoned = await session.get(
                ExecutionApprovalRequestRow,
                terminal_approval_id,
                with_for_update=True,
            )
            assert abandoned is not None
            abandoned.status = "pending"
            abandoned.decision = None
            abandoned.decision_idempotency_key = None
            abandoned.decision_request_digest = None
            abandoned.decided_by_user_id = None
            abandoned.decided_at = None
            abandoned.continuation_run_id = None
            abandoned.continuation_job_id = None
            abandoned.execution_job_attempt_id = None
            abandoned.claimed_at = None
            abandoned.expires_at = expired_at
            abandoned.terminal_at = None
            abandoned.version += 1
            abandoned.updated_at = expired_at

        async with factory() as session, session.begin():
            active = await lock_and_reconcile_active_execution_approval(
                session,
                project_id=project_id,
                owner_user_id=str(owner_id),
                thread_id=thread_id,
                now=datetime.now(UTC),
            )
            assert active is None
            abandoned = await session.get(
                ExecutionApprovalRequestRow,
                terminal_approval_id,
            )
            assert abandoned is not None
            assert abandoned.status == "expired"
            assert abandoned.terminal_at is not None
    finally:
        await engine.dispose()
