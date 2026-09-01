from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from support.agent_definition_seed import direct_agent_definition_fields
from support.run_closure import add_sealed_test_run

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkNotFound
from app.private_work.run_execution_state import (
    RunExecutionState,
    RunExecutionStatePolicy,
    read_run_execution_state,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.jobs import automation_run_idempotency_key
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
from deerflow.persistence.shared_assets import AgentRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user import UserRow

POLICY = RunExecutionStatePolicy(worker_fresh_for_seconds=60)
AFFINITY = "a" * 64


@dataclass(frozen=True, slots=True)
class _Seed:
    context: PrivateWorkContext
    other_context: PrivateWorkContext
    thread_id: str
    run_id: str
    job_id: uuid.UUID
    worker_id: uuid.UUID


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    job_type: str = "private_run",
    worker_capabilities: list[str] | None = None,
) -> _Seed:
    owner_id = uuid.uuid4()
    other_owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    owner_membership_id = uuid.uuid4()
    other_membership_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    job_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    task_id = str(uuid.uuid4())
    occurrence_id = str(uuid.uuid4())
    trace_id = f"run-execution-state-{uuid.uuid4().hex}"
    created_at = datetime.now(UTC) - timedelta(seconds=10)
    definition = direct_agent_definition_fields(
        updated_by_user_id=str(owner_id),
        description="Execution state Agent",
    )
    automation_occurrence: ScheduledTaskRunRow | None = None
    async with factory() as session, session.begin():
        session.add_all(
            [
                UserRow(
                    id=str(owner_id),
                    email=f"execution-state-{owner_id}@example.com",
                    password_hash=None,
                    system_role="user",
                    needs_setup=False,
                    token_version=0,
                ),
                UserRow(
                    id=str(other_owner_id),
                    email=f"execution-state-{other_owner_id}@example.com",
                    password_hash=None,
                    system_role="user",
                    needs_setup=False,
                    token_version=0,
                ),
            ]
        )
        await session.flush()
        session.add(
            ProjectRow(
                id=project_id,
                slug=f"execution-state-{project_id.hex[:12]}",
                display_name="Run execution state",
                created_by_user_id=str(owner_id),
            )
        )
        await session.flush()
        session.add_all(
            [
                ProjectMembershipRow(
                    id=owner_membership_id,
                    project_id=project_id,
                    user_id=str(owner_id),
                    role="runner",
                    status="active",
                    version=1,
                    activation_generation=1,
                ),
                ProjectMembershipRow(
                    id=other_membership_id,
                    project_id=project_id,
                    user_id=str(other_owner_id),
                    role="runner",
                    status="active",
                    version=1,
                    activation_generation=1,
                ),
            ]
        )
        session.add(
            AgentRow(
                id=agent_id,
                scope="project",
                project_id=project_id,
                slug=f"execution-state-{agent_id.hex[:12]}",
                display_name="Execution state Agent",
                status="active",
                revision=1,
                created_by_user_id=str(owner_id),
                **definition,
            )
        )
        await session.flush()
        session.add(
            ThreadMetaRow(
                thread_id=thread_id,
                owner_user_id=str(owner_id),
                project_id=project_id,
                agent_asset_id=agent_id,
                agent_scope="project",
                thread_kind="chat",
                status="idle",
                metadata_json={},
                version=1,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        await session.flush()
        run = RunRow(
            run_id=run_id,
            thread_id=thread_id,
            owner_user_id=str(owner_id),
            project_id=project_id,
            status="pending",
            job_id=None,
            origin_trace_id=trace_id,
            metadata_json={},
            kwargs_json={},
            created_at=created_at,
            updated_at=created_at,
        )
        await add_sealed_test_run(session, run)
        if job_type == "automation_run":
            session.add(
                ScheduledTaskRow(
                    id=task_id,
                    project_id=project_id,
                    owner_user_id=str(owner_id),
                    thread_id=None,
                    context_mode="fresh_thread_per_run",
                    agent_asset_id=agent_id,
                    agent_scope="project",
                    title="Execution state automation",
                    prompt="Check execution state",
                    schedule_type="once",
                    schedule_spec={},
                    timezone="UTC",
                    status="enabled",
                    overlap_policy="skip",
                    run_count=0,
                    version=1,
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
            await session.flush()
            automation_occurrence = ScheduledTaskRunRow(
                id=occurrence_id,
                project_id=project_id,
                owner_user_id=str(owner_id),
                task_id=task_id,
                task_version=1,
                occurrence_key=uuid.uuid4().hex * 2,
                scheduled_for=created_at,
                trigger="scheduled",
                status="running",
                thread_id=thread_id,
                run_id=run_id,
                job_id=None,
                launch_attempt_count=0,
                created_at=created_at,
                updated_at=created_at,
            )
            session.add(automation_occurrence)
            await session.flush()
        session.add(
            JobRow(
                id=job_id,
                job_type=job_type,
                project_id=project_id,
                owner_user_id=str(owner_id),
                owner_private_generation=1,
                run_id=run_id,
                automation_occurrence_id=(occurrence_id if job_type == "automation_run" else None),
                origin_trace_id=trace_id,
                idempotency_key=(automation_run_idempotency_key(occurrence_id) if job_type == "automation_run" else uuid.uuid4().hex * 2),
                status="queued",
                available_at=created_at,
                attempt_count=0,
                max_attempts=3,
                retry_safety="safe",
                execution_domain_affinity=(AFFINITY if job_type == "private_run" else None),
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="test",
                capabilities_json=(["private_run"] if worker_capabilities is None else worker_capabilities),
                max_concurrent_jobs=1,
                execution_domain_affinity=None,
                draining=False,
                started_at=created_at,
                heartbeat_at=datetime.now(UTC),
            )
        )
        await session.flush()
        run.job_id = job_id
        if automation_occurrence is not None:
            automation_occurrence.job_id = job_id
        await session.flush()

    return _Seed(
        context=PrivateWorkContext.from_project(
            ProjectContext(
                user_id=owner_id,
                project_id=project_id,
                membership_id=owner_membership_id,
                role=ProjectRole.RUNNER,
                capabilities=capabilities_for(ProjectRole.RUNNER),
                membership_version=1,
                request_id="run-execution-state-postgres",
            )
        ),
        other_context=PrivateWorkContext.from_project(
            ProjectContext(
                user_id=other_owner_id,
                project_id=project_id,
                membership_id=other_membership_id,
                role=ProjectRole.RUNNER,
                capabilities=capabilities_for(ProjectRole.RUNNER),
                membership_version=1,
                request_id="run-execution-state-postgres-other",
            )
        ),
        thread_id=thread_id,
        run_id=run_id,
        job_id=job_id,
        worker_id=worker_id,
    )


async def _read(
    factory: async_sessionmaker[AsyncSession],
    seed: _Seed,
) -> RunExecutionState:
    async with factory() as session:
        projection = await read_run_execution_state(
            session,
            seed.context,
            seed.thread_id,
            seed.run_id,
            POLICY,
        )
    assert type(projection) is RunExecutionState
    return projection


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_reader_accepts_automation_run_and_requires_its_worker_capability(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        seed = await _seed(
            factory,
            job_type="automation_run",
            worker_capabilities=["automation_run"],
        )

        assert (await _read(factory, seed)).phase == "queued"

        async with factory() as session, session.begin():
            worker = await session.get(WorkerNodeRow, seed.worker_id)
            assert worker is not None
            worker.capabilities_json = {"automation_run": True}
        assert (await _read(factory, seed)).phase == "waiting_for_worker"

        async with factory() as session, session.begin():
            worker = await session.get(WorkerNodeRow, seed.worker_id)
            assert worker is not None
            worker.capabilities_json = ["private_run"]
        assert (await _read(factory, seed)).phase == "waiting_for_worker"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_reader_projects_worker_eligibility_and_execution_phase_matrix(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        seed = await _seed(factory)

        assert (await _read(factory, seed)).phase == "waiting_for_worker"

        async with factory() as session, session.begin():
            worker = await session.get(WorkerNodeRow, seed.worker_id)
            assert worker is not None
            worker.execution_domain_affinity = AFFINITY
            worker.draining = True
        assert (await _read(factory, seed)).phase == "waiting_for_worker"

        async with factory() as session, session.begin():
            worker = await session.get(WorkerNodeRow, seed.worker_id)
            assert worker is not None
            worker.draining = False
            worker.capabilities_json = ["memory_seal"]
        assert (await _read(factory, seed)).phase == "waiting_for_worker"

        async with factory() as session, session.begin():
            worker = await session.get(WorkerNodeRow, seed.worker_id)
            assert worker is not None
            worker.capabilities_json = ["private_run"]
            worker.heartbeat_at = datetime.now(UTC)
        assert (await _read(factory, seed)).phase == "queued"

        first_token = "b" * 64
        first_started_at = datetime.now(UTC)
        first_lease_expires_at = first_started_at + timedelta(seconds=90)
        first_attempt_id = uuid.uuid4()
        async with factory() as session, session.begin():
            job = await session.get(JobRow, seed.job_id)
            assert job is not None
            job.status = "leased"
            job.attempt_count = 1
            job.lease_owner_id = seed.worker_id
            job.lease_token_hash = first_token
            job.lease_expires_at = first_lease_expires_at
            job.heartbeat_at = first_started_at
            job.started_at = first_started_at
            job.updated_at = first_started_at
            session.add(
                JobAttemptRow(
                    id=first_attempt_id,
                    job_id=seed.job_id,
                    attempt_number=1,
                    worker_id=seed.worker_id,
                    lease_token_hash=first_token,
                    started_at=first_started_at,
                    heartbeat_at=first_started_at,
                )
            )
        starting = await _read(factory, seed)
        assert starting.phase == "starting"
        assert starting.phase_started_at == first_started_at

        first_execution_started_at = datetime.now(UTC)
        async with factory() as session, session.begin():
            job = await session.get(JobRow, seed.job_id)
            run = await session.get(RunRow, seed.run_id)
            attempt = await session.get(JobAttemptRow, first_attempt_id)
            assert job is not None and run is not None and attempt is not None
            job.status = "running"
            job.updated_at = first_execution_started_at
            run.status = "running"
            run.execution_lease_token_hash = first_token
            run.execution_lease_expires_at = first_lease_expires_at
            run.execution_started_at = first_execution_started_at
            run.updated_at = first_execution_started_at
            attempt.execution_started_at = first_execution_started_at
        executing = await _read(factory, seed)
        assert executing.phase == "executing"
        assert executing.execution_started_at == first_execution_started_at
        assert executing.phase_started_at == first_execution_started_at

        stale_heartbeat = datetime.now(UTC) - timedelta(seconds=120)
        async with factory() as session, session.begin():
            worker = await session.get(WorkerNodeRow, seed.worker_id)
            assert worker is not None
            worker.heartbeat_at = stale_heartbeat
        disconnected = await _read(factory, seed)
        assert disconnected.phase == "waiting_for_lease_expiry"
        assert disconnected.phase_started_at == stale_heartbeat + timedelta(seconds=60)
        assert disconnected.retry_at == first_lease_expires_at

        async with factory() as session, session.begin():
            database_now = await session.scalar(
                select(func.clock_timestamp()),
            )
            assert isinstance(database_now, datetime)
            assert database_now.tzinfo is not None
            expired_at = database_now - timedelta(seconds=1)
            job = await session.get(JobRow, seed.job_id)
            run = await session.get(RunRow, seed.run_id)
            worker = await session.get(WorkerNodeRow, seed.worker_id)
            assert job is not None and run is not None and worker is not None
            job.lease_expires_at = expired_at
            job.retry_safety = "unsafe"
            run.execution_lease_expires_at = expired_at
            worker.heartbeat_at = database_now
        terminalizing = await _read(factory, seed)
        assert terminalizing.phase == "waiting_for_terminalization"
        assert terminalizing.phase_started_at == expired_at

        async with factory() as session, session.begin():
            job = await session.get(JobRow, seed.job_id)
            assert job is not None
            job.retry_safety = "safe"
        recovering_wait = await _read(factory, seed)
        assert recovering_wait.phase == "waiting_for_recovery"
        assert recovering_wait.phase_started_at == expired_at

        second_token = "c" * 64
        second_started_at = datetime.now(UTC)
        second_lease_expires_at = second_started_at + timedelta(seconds=90)
        second_attempt_id = uuid.uuid4()
        async with factory() as session, session.begin():
            job = await session.get(JobRow, seed.job_id)
            first_attempt = await session.get(JobAttemptRow, first_attempt_id)
            assert job is not None and first_attempt is not None
            first_attempt.outcome = "lease_lost"
            first_attempt.finished_at = expired_at
            first_attempt.public_error_code = "LEASE_EXPIRED"
            job.status = "leased"
            job.attempt_count = 2
            job.lease_token_hash = second_token
            job.lease_expires_at = second_lease_expires_at
            job.heartbeat_at = second_started_at
            job.updated_at = second_started_at
            session.add(
                JobAttemptRow(
                    id=second_attempt_id,
                    job_id=seed.job_id,
                    attempt_number=2,
                    worker_id=seed.worker_id,
                    lease_token_hash=second_token,
                    started_at=second_started_at,
                    heartbeat_at=second_started_at,
                )
            )
        recovering = await _read(factory, seed)
        assert recovering.phase == "recovering"
        assert recovering.phase_started_at == second_started_at
        assert recovering.execution_started_at == first_execution_started_at

        second_execution_started_at = datetime.now(UTC)
        async with factory() as session, session.begin():
            job = await session.get(JobRow, seed.job_id)
            run = await session.get(RunRow, seed.run_id)
            attempt = await session.get(JobAttemptRow, second_attempt_id)
            assert job is not None and run is not None and attempt is not None
            job.status = "running"
            job.updated_at = second_execution_started_at
            run.execution_lease_token_hash = second_token
            run.execution_lease_expires_at = second_lease_expires_at
            run.updated_at = second_execution_started_at
            attempt.execution_started_at = second_execution_started_at
        recovered_execution = await _read(factory, seed)
        assert recovered_execution.phase == "executing"
        assert recovered_execution.phase_started_at == second_execution_started_at
        assert recovered_execution.execution_started_at == first_execution_started_at

        completed_at = datetime.now(UTC)
        async with factory() as session, session.begin():
            job = await session.get(JobRow, seed.job_id)
            run = await session.get(RunRow, seed.run_id)
            attempt = await session.get(JobAttemptRow, second_attempt_id)
            assert job is not None and run is not None and attempt is not None
            attempt.outcome = "succeeded"
            attempt.finished_at = completed_at
            job.status = "succeeded"
            job.lease_owner_id = None
            job.lease_token_hash = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.completed_at = completed_at
            job.updated_at = completed_at
            run.status = "success"
            run.execution_lease_token_hash = None
            run.execution_lease_expires_at = None
            run.execution_heartbeat_at = None
            run.updated_at = completed_at
        terminal = await _read(factory, seed)
        assert terminal.phase == "terminal"
        assert terminal.phase_started_at == completed_at
        assert terminal.execution_started_at == first_execution_started_at
        assert terminal.run_status == "success"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_reader_hides_wrong_owner_thread_and_run_scope(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        seed = await _seed(factory)
        cases = (
            (seed.other_context, seed.thread_id, seed.run_id),
            (seed.context, str(uuid.uuid4()), seed.run_id),
            (seed.context, seed.thread_id, str(uuid.uuid4())),
        )
        for context, thread_id, run_id in cases:
            async with factory() as session:
                with pytest.raises(PrivateWorkNotFound):
                    await read_run_execution_state(
                        session,
                        context,
                        thread_id,
                        run_id,
                        POLICY,
                    )
    finally:
        await engine.dispose()
