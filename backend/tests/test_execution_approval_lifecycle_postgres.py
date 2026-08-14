from __future__ import annotations

import asyncio
import hashlib
import uuid
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
    settle_staged_execution_approvals,
)
from app.private_work.execution_approval_audit import (
    NoopHostExecutionApprovalAudit,
)
from app.private_work.execution_approval_lifecycle import (
    lock_and_reconcile_active_execution_approval,
)
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import (
    PrivateRunCreate,
    PrivateRunExecutionLeaseLost,
    PrivateRunRepository,
)
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.jobs import PrivateRunJobRepository
from app.reliability.owner_refs import AuditHmacKeyring
from app.reliability.run_execution.settlement import PrivateRunJobTerminalPort
from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.execution_approvals import (
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
from deerflow.persistence.private_work import RunAssetVersionRow
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
    SystemModelConfigVersionRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow
from deerflow.runtime.host_execution_approval import (
    HostExecutionOutcome,
    HostExecutionPlan,
)
from deerflow.runtime.host_execution_domain import HostExecutionDomainSnapshot


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
        model_name="test-model",
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
            request_ttl_seconds=300,
        )


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
            approval.updated_at = datetime.now(UTC)
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

        expired_at = datetime.now(UTC)
        async with seed.factory() as session, session.begin():
            abandoned = await session.get(
                ExecutionApprovalRequestRow,
                approval_id,
                with_for_update=True,
            )
            assert abandoned is not None and abandoned.status == "pending"
            abandoned.expires_at = expired_at
            abandoned.updated_at = expired_at

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
            expired_at = datetime.now(UTC)
            approval.expires_at = expired_at
            approval.updated_at = expired_at

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
        model_config_version_id: uuid.UUID,
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
        self._model_config_version_id = model_config_version_id
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
                    ),
                )
                session.add(
                    RunModelConfigSnapshotRow(
                        project_id=self._project_id,
                        owner_user_id=self._owner_user_id,
                        thread_id=thread_id,
                        run_id=request.run_id,
                        purpose="chat",
                        logical_name="test-model",
                        model_config_id=self._model_config_id,
                        model_config_version_id=self._model_config_version_id,
                        payload_checksum=self._model_payload_checksum,
                        credential_id=None,
                        credential_version_id=None,
                        credential_env_key=None,
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
    model_config_version_id = uuid.uuid4()
    drifted_model_config_version_id = uuid.uuid4()
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
            session.add(
                AgentRow(
                    id=agent_id,
                    scope="project",
                    project_id=project_id,
                    slug="approval-agent",
                    display_name="Approval Agent",
                    created_by_user_id=str(owner_id),
                ),
            )
            await session.flush()
            session.add(
                AgentVersionRow(
                    id=agent_version_id,
                    agent_id=agent_id,
                    version_number=1,
                    workflow_status="published",
                    description="",
                    soul="test",
                    model_ref="test-model",
                    model_settings={},
                    tool_groups=[],
                    payload_checksum="a" * 64,
                    created_by_user_id=str(owner_id),
                ),
            )
            await session.flush()
            model_config = SystemModelConfigRow(
                id=model_config_id,
                logical_name="test-model",
                display_name="Test model",
                description="",
                status="active",
                current_version_id=None,
                revision=1,
                sort_order=0,
                created_by_user_id=str(owner_id),
                updated_by_user_id=str(owner_id),
            )
            session.add(model_config)
            await session.flush()
            session.add(
                SystemModelConfigVersionRow(
                    id=model_config_version_id,
                    model_config_id=model_config_id,
                    version_number=1,
                    provider_adapter="openai_compatible",
                    provider_model="test-model",
                    settings={},
                    supports_thinking=False,
                    supports_reasoning_effort=False,
                    supports_vision=False,
                    credential_id=None,
                    credential_version_id=None,
                    credential_env_key=None,
                    payload_checksum=model_payload_checksum,
                    supersedes_version_id=None,
                    created_by_user_id=str(owner_id),
                ),
            )
            await session.flush()
            session.add(
                SystemModelConfigVersionRow(
                    id=drifted_model_config_version_id,
                    model_config_id=model_config_id,
                    version_number=2,
                    provider_adapter="openai_compatible",
                    provider_model="test-model-v2",
                    settings={},
                    supports_thinking=False,
                    supports_reasoning_effort=False,
                    supports_vision=False,
                    credential_id=None,
                    credential_version_id=None,
                    credential_env_key=None,
                    payload_checksum=drifted_model_payload_checksum,
                    supersedes_version_id=model_config_version_id,
                    created_by_user_id=str(owner_id),
                ),
            )
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
            model_config.current_version_id = model_config_version_id
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
                ),
            )
            session.add(
                RunModelConfigSnapshotRow(
                    project_id=project_id,
                    owner_user_id=str(owner_id),
                    thread_id=thread_id,
                    run_id=source_run_id,
                    purpose="chat",
                    logical_name="test-model",
                    model_config_id=model_config_id,
                    model_config_version_id=model_config_version_id,
                    payload_checksum=model_payload_checksum,
                    credential_id=None,
                    credential_version_id=None,
                    credential_env_key=None,
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
        staged_projection = await staged_reader.active(context, thread_id)
        assert staged_projection.approval is not None
        assert staged_projection.approval["status"] == "pending"
        assert staged_projection.approval["can_decide"] is False

        async with factory() as session, session.begin():
            row = await session.get(ExecutionApprovalRequestRow, approval_id)
            assert row is not None and row.status == "staged"
            await settle_staged_execution_approvals(
                session,
                claim=source_claim,
                succeeded=True,
                request_ttl_seconds=300,
            )
            source_run = await session.get(RunRow, source_run_id)
            assert source_run is not None
            source_run.status = "success"

        admission = _AtomicContinuationAdmission(
            factory,
            project_id=project_id,
            owner_user_id=str(owner_id),
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            model_config_id=model_config_id,
            model_config_version_id=(drifted_model_config_version_id if snapshot_drift == "model" else model_config_version_id),
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

        # A command staged during a real Job that subsequently dies is
        # terminalized in the same transaction as the Run.  Its by-id shape
        # remains pollable while the source Job is still live.
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
        pollable = await service.get(
            context,
            thread_id,
            terminal_approval_id,
        )
        assert pollable.approval is not None
        assert pollable.approval["status"] == "pending"
        assert pollable.approval["can_decide"] is False

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
