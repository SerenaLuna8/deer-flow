from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from support.private_thread_seed import seed_private_thread_database
from support.run_closure import add_sealed_test_run

import app.private_work.run_repository as run_repository_module
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_repository import (
    PrivateRunExecutionLeaseLost,
    PrivateRunRepository,
)
from app.private_work.run_skill_tree_materializer import (
    MaterializationAttemptIdentity,
)
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.projects.capabilities import Capability
from app.reliability.run_execution.boundary import PrivateRunExecutionBoundary
from app.worker.service import JobLeaseAuthority, LeaseLost
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import JobClaim, JobRepository, JobScope
from deerflow.persistence.projects.model import ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.sandbox.sandbox import AuthorizationRevoked


@dataclass(frozen=True)
class _ActiveRun:
    run_id: str
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    worker_id: uuid.UUID
    lease_token: str
    lease_hash: str
    expires_at: datetime


async def _seed_active_run(seed, *, lease_seconds: float) -> _ActiveRun:
    thread_id = str(uuid.uuid4())
    worker_id = uuid.uuid4()
    run_id = str(uuid.uuid4())
    origin_trace_id = uuid.uuid4().hex
    lease_token = f"heartbeat-{uuid.uuid4().hex}"
    lease_hash = hashlib.sha256(lease_token.encode()).hexdigest()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=lease_seconds)
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="lease-heartbeat-test",
                capabilities_json=["private_run"],
                max_concurrent_jobs=1,
            ),
        )
        run = RunRow(
            run_id=run_id,
            thread_id=thread_id,
            assistant_id=str(seed.project_agent_id),
            owner_user_id=str(seed.owner_a.user_id),
            status="running",
            model_name="test-model",
            multitask_strategy="reject",
            metadata_json={},
            kwargs_json={},
            origin_trace_id=origin_trace_id,
            project_id=seed.owner_a.project_id,
            finalization_status="pending",
            execution_lease_token_hash=lease_hash,
            execution_lease_expires_at=expires_at,
            execution_heartbeat_at=now,
            execution_started_at=now,
        )
        await add_sealed_test_run(session, run)
        job = JobRow(
            job_type="private_run",
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            owner_private_generation=1,
            run_id=run_id,
            origin_trace_id=origin_trace_id,
            idempotency_key=hashlib.sha256(f"job:{run_id}".encode()).hexdigest(),
            status="running",
            max_attempts=3,
            attempt_count=1,
            lease_owner_id=worker_id,
            lease_token_hash=lease_hash,
            lease_expires_at=expires_at,
            heartbeat_at=now,
            retry_safety="unknown",
            started_at=now,
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
        )
        session.add(attempt)
        await session.flush()
        return _ActiveRun(
            run_id=run_id,
            job_id=job.id,
            attempt_id=attempt.id,
            worker_id=worker_id,
            lease_token=lease_token,
            lease_hash=lease_hash,
            expires_at=expires_at,
        )


async def _add_worker(seed) -> uuid.UUID:
    worker_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="lease-claim-test",
                capabilities_json=["private_run"],
                max_concurrent_jobs=1,
            ),
        )
    return worker_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_materialization_execution_suffix_uses_locked_context_and_exact_attempt_worker(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    statements: list[str] = []
    try:
        active = await _seed_active_run(seed, lease_seconds=30)
        async with seed.factory() as session:
            raw_ids = (
                await session.execute(
                    sa.text(
                        """SELECT job.id AS job_id, attempt.id AS attempt_id
                           FROM jobs AS job
                           JOIN job_attempts AS attempt
                             ON attempt.job_id=job.id
                           WHERE job.id=:job_id"""
                    ),
                    {"job_id": active.job_id},
                )
            ).one()

        def claim(*, attempt_id: object = raw_ids.attempt_id) -> JobClaim:
            return JobClaim(
                job_id=raw_ids.job_id,
                attempt_id=attempt_id,  # type: ignore[arg-type]
                lease_token=active.lease_token,
                job_type="private_run",
                scope=JobScope(
                    seed.owner_a.project_id,
                    str(seed.owner_a.user_id),
                ),
                run_id=active.run_id,
                occurrence_id=None,
                retry_safety="unknown",
                cancel_requested=False,
            )

        sa.event.listen(
            seed.engine.sync_engine,
            "before_cursor_execute",
            lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement),
        )
        boundary = PrivateRunExecutionBoundary(
            seed.factory,
            context=seed.owner_a,
            claim=claim(),
            expected_worker_id=active.worker_id,
        )
        async with seed.factory() as session, session.begin():
            locked_context = await PrivateWorkRevalidator().require(
                session,
                seed.owner_a,
                Capability.PRIVATE_WORK_CREATE,
                Capability.SHARED_ASSETS_EXECUTE,
                lock_mode="share",
            )
            identity = await boundary.lock_and_assert_materialization_active_in_session(
                session,
                locked_context,
            )

        assert type(identity) is MaterializationAttemptIdentity
        assert type(identity.job_id) is uuid.UUID
        assert type(identity.attempt_id) is uuid.UUID
        assert type(identity.worker_id) is uuid.UUID
        assert identity == MaterializationAttemptIdentity(
            job_id=active.job_id,
            attempt_id=active.attempt_id,
            worker_id=active.worker_id,
        )
        normalized_statements = [" ".join(statement.lower().split()) for statement in statements]
        reads = [statement for statement in normalized_statements if " from " in statement]
        lock_order = [
            next(index for index, statement in enumerate(reads) if table in statement)
            for table in (
                "from projects",
                "from project_memberships",
                "from jobs",
                "from runs",
                "from job_attempts",
            )
        ]
        assert lock_order == sorted(lock_order)
        assert all("from users" not in statement for statement in reads)

        wrong_attempt = PrivateRunExecutionBoundary(
            seed.factory,
            context=seed.owner_a,
            claim=claim(attempt_id=uuid.uuid4()),
            expected_worker_id=active.worker_id,
        )
        async with seed.factory() as session, session.begin():
            locked_context = await PrivateWorkRevalidator().require(
                session,
                seed.owner_a,
                Capability.PRIVATE_WORK_CREATE,
                Capability.SHARED_ASSETS_EXECUTE,
                lock_mode="share",
            )
            with pytest.raises(AuthorizationRevoked):
                await wrong_attempt.lock_and_assert_materialization_active_in_session(
                    session,
                    locked_context,
                )

        wrong_worker = PrivateRunExecutionBoundary(
            seed.factory,
            context=seed.owner_a,
            claim=claim(),
            expected_worker_id=uuid.uuid4(),
        )
        async with seed.factory() as session, session.begin():
            locked_context = await PrivateWorkRevalidator().require(
                session,
                seed.owner_a,
                Capability.PRIVATE_WORK_CREATE,
                Capability.SHARED_ASSETS_EXECUTE,
                lock_mode="share",
            )
            with pytest.raises(AuthorizationRevoked):
                await wrong_worker.lock_and_assert_materialization_active_in_session(
                    session,
                    locked_context,
                )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_job_heartbeat_cannot_revive_lease_after_authority_lock_wait(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed, lease_seconds=0.3)
        async with seed.factory() as blocker, blocker.begin():
            await blocker.execute(sa.text("SET LOCAL statement_timeout = '2s'"))
            job = await blocker.get(JobRow, active.job_id, with_for_update=True)
            run = await blocker.get(RunRow, active.run_id, with_for_update=True)
            assert job is not None and run is not None
            heartbeat_task = asyncio.create_task(
                _job_heartbeat(seed, active),
            )
            await asyncio.sleep(0.45)
            assert not heartbeat_task.done()

        assert await asyncio.wait_for(heartbeat_task, timeout=3) is False
        async with seed.factory() as session:
            persisted_job = await session.get(JobRow, active.job_id)
            persisted_run = await session.get(RunRow, active.run_id)
            assert persisted_job is not None and persisted_run is not None
            assert persisted_job.lease_expires_at == active.expires_at
            assert persisted_run.execution_lease_expires_at == active.expires_at
    finally:
        await seed.engine.dispose()


async def _job_heartbeat(seed, active: _ActiveRun):
    async with seed.factory() as session, session.begin():
        return await JobRepository(session).heartbeat(
            active.job_id,
            lease_token=active.lease_token,
            lease_seconds=30,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "expected_status"),
    (
        ("settle_success", "succeeded"),
        ("settle_cancelled", "cancelled"),
    ),
)
async def test_owned_terminal_settlement_clears_prior_retry_error(
    migrated_postgres_database_url: str,
    method_name: str,
    expected_status: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed, lease_seconds=5)
        async with seed.factory() as session, session.begin():
            job = await session.get(JobRow, active.job_id, with_for_update=True)
            assert job is not None
            job.public_error_code = "MEMORY_DREAM_MODEL_FAILED"

        async with seed.factory() as session, session.begin():
            repository = JobRepository(session)
            settled = await getattr(repository, method_name)(
                active.job_id,
                lease_token=active.lease_token,
            )
            assert settled is True

        async with seed.factory() as session:
            job = await session.get(JobRow, active.job_id)
            assert job is not None
            assert job.status == expected_status
            assert job.public_error_code is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_heartbeat_cannot_revive_run_after_authority_lock_wait(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed, lease_seconds=0.3)
        async with seed.factory() as blocker, blocker.begin():
            await blocker.execute(sa.text("SET LOCAL statement_timeout = '2s'"))
            job = await blocker.get(JobRow, active.job_id, with_for_update=True)
            run = await blocker.get(RunRow, active.run_id, with_for_update=True)
            assert job is not None and run is not None
            heartbeat_task = asyncio.create_task(
                _run_heartbeat(seed, active),
            )
            await asyncio.sleep(0.45)
            assert not heartbeat_task.done()

        with pytest.raises(PrivateRunExecutionLeaseLost):
            await asyncio.wait_for(heartbeat_task, timeout=3)
        async with seed.factory() as session:
            persisted_job = await session.get(JobRow, active.job_id)
            persisted_run = await session.get(RunRow, active.run_id)
            assert persisted_job is not None and persisted_run is not None
            assert persisted_job.lease_expires_at == active.expires_at
            assert persisted_run.execution_lease_expires_at == active.expires_at
    finally:
        await seed.engine.dispose()


async def _run_heartbeat(seed, active: _ActiveRun) -> None:
    async with seed.factory() as session, session.begin():
        await PrivateRunRepository(session).heartbeat_execution(
            scope=seed.owner_a.resource_scope,
            run_id=active.run_id,
            job_id=active.job_id,
            lease_token=active.lease_token,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_callback_chain_keeps_expired_run_host_claim_gate_closed(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed, lease_seconds=5)
        run_expires_at = datetime.now(UTC) + timedelta(seconds=0.35)
        async with seed.factory() as session, session.begin():
            await session.execute(
                sa.update(RunRow).where(RunRow.run_id == active.run_id).values(execution_lease_expires_at=run_expires_at),
            )
        claim = JobClaim(
            job_id=active.job_id,
            attempt_id=active.attempt_id,
            lease_token=active.lease_token,
            job_type="private_run",
            scope=JobScope(
                seed.owner_a.project_id,
                str(seed.owner_a.user_id),
            ),
            run_id=active.run_id,
            occurrence_id=None,
            retry_safety="unknown",
            cancel_requested=False,
            origin_trace_id=None,
        )
        authority = JobLeaseAuthority(
            seed.factory,
            claim,
            lease_seconds=30,
            repository_builder=JobRepository,
        )
        authority.bind_heartbeat_callback(lambda: _run_heartbeat(seed, active))

        async with seed.factory() as blocker, blocker.begin():
            await blocker.execute(sa.text("SET LOCAL statement_timeout = '2s'"))
            run = await blocker.get(RunRow, active.run_id, with_for_update=True)
            assert run is not None
            heartbeat_task = asyncio.create_task(authority.heartbeat())
            await asyncio.sleep(0.5)
            assert not heartbeat_task.done()

        with pytest.raises(LeaseLost):
            await asyncio.wait_for(heartbeat_task, timeout=3)
        with pytest.raises(LeaseLost):
            await authority.heartbeat()
        async with seed.factory() as session, session.begin():
            with pytest.raises(PrivateRunExecutionLeaseLost):
                await PrivateRunRepository(session).assert_execution_active(
                    scope=seed.owner_a.resource_scope,
                    run_id=active.run_id,
                    job_id=active.job_id,
                    lease_token=active.lease_token,
                )
            persisted_run = await session.get(RunRow, active.run_id)
            assert persisted_run is not None
            assert persisted_run.execution_lease_expires_at == run_expires_at
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_heartbeats_reject_job_attempt_authority_mismatch(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed, lease_seconds=5)
        async with seed.factory() as session, session.begin():
            job = await session.get(JobRow, active.job_id, with_for_update=True)
            assert job is not None
            job.attempt_count = 2

        with pytest.raises(RuntimeError, match="active job attempt authority"):
            await _job_heartbeat(seed, active)
        with pytest.raises(PrivateRunExecutionLeaseLost):
            await _run_heartbeat(seed, active)
        async with seed.factory() as session:
            persisted_job = await session.get(JobRow, active.job_id)
            persisted_run = await session.get(RunRow, active.run_id)
            assert persisted_job is not None and persisted_run is not None
            assert persisted_job.lease_expires_at == active.expires_at
            assert persisted_run.execution_lease_expires_at == active.expires_at
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["begin", "checkpoint"])
async def test_execution_entrypoints_cannot_revive_expired_run_lease(
    migrated_postgres_database_url: str,
    operation: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed, lease_seconds=5)
        expired_at = datetime.now(UTC) - timedelta(milliseconds=50)
        async with seed.factory() as session, session.begin():
            await session.execute(
                sa.update(RunRow).where(RunRow.run_id == active.run_id).values(execution_lease_expires_at=expired_at),
            )

        async with seed.factory() as session, session.begin():
            repository = PrivateRunRepository(session)
            with pytest.raises(PrivateRunExecutionLeaseLost):
                if operation == "begin":
                    await repository.begin_execution(
                        scope=seed.owner_a.resource_scope,
                        run_id=active.run_id,
                        job_id=active.job_id,
                        lease_token=active.lease_token,
                    )
                else:
                    await repository.prepare_checkpoint_takeover(
                        scope=seed.owner_a.resource_scope,
                        run_id=active.run_id,
                        job_id=active.job_id,
                        attempt_id=active.attempt_id,
                        lease_token=active.lease_token,
                        latest_checkpoint_id=None,
                    )

        async with seed.factory() as session:
            persisted_run = await session.get(RunRow, active.run_id)
            assert persisted_run is not None
            assert persisted_run.execution_lease_expires_at == expired_at
    finally:
        await seed.engine.dispose()


class _SkewedDateTimeMeta(type):
    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, datetime)


class _NegativelySkewedDateTime(
    datetime,
    metaclass=_SkewedDateTimeMeta,
):
    @classmethod
    def now(cls, tz=None):
        del cls, tz
        return datetime.now(UTC) - timedelta(days=1)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    [
        "assert_execution_active",
        "mark_execution_side_effect_unknown",
        "settle_execution",
    ],
)
async def test_authority_boundaries_use_database_clock_despite_negative_worker_skew(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed, lease_seconds=5)
        async with seed.factory() as session, session.begin():
            database_now = await session.scalar(
                sa.select(sa.func.clock_timestamp()),
            )
            assert isinstance(database_now, datetime)
            expired_at = database_now - timedelta(milliseconds=50)
            await session.execute(
                sa.update(JobRow)
                .where(JobRow.id == active.job_id)
                .values(
                    lease_expires_at=expired_at,
                    retry_safety="safe",
                ),
            )
            await session.execute(
                sa.update(RunRow).where(RunRow.run_id == active.run_id).values(execution_lease_expires_at=expired_at),
            )

        monkeypatch.setattr(
            run_repository_module,
            "datetime",
            _NegativelySkewedDateTime,
        )
        async with seed.factory() as session, session.begin():
            repository = PrivateRunRepository(session)
            method = getattr(repository, method_name)
            kwargs = {
                "scope": seed.owner_a.resource_scope,
                "run_id": active.run_id,
                "job_id": active.job_id,
                "lease_token": active.lease_token,
            }
            if method_name == "settle_execution":
                kwargs["outcome"] = "succeeded"
            with pytest.raises(PrivateRunExecutionLeaseLost):
                await method(**kwargs)

        async with seed.factory() as session:
            persisted_job = await session.get(JobRow, active.job_id)
            persisted_run = await session.get(RunRow, active.run_id)
            persisted_attempt = await session.get(
                JobAttemptRow,
                active.attempt_id,
            )
            assert persisted_job is not None and persisted_run is not None
            assert persisted_attempt is not None
            assert persisted_job.status == "running"
            assert persisted_job.retry_safety == "safe"
            assert persisted_job.lease_expires_at == expired_at
            assert persisted_run.status == "running"
            assert persisted_run.execution_lease_expires_at == expired_at
            assert persisted_attempt.outcome is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_before_sandbox_exec_has_no_side_effect_when_database_lease_expired(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed, lease_seconds=5)
        async with seed.factory() as session, session.begin():
            database_now = await session.scalar(
                sa.select(sa.func.clock_timestamp()),
            )
            assert isinstance(database_now, datetime)
            expired_at = database_now - timedelta(milliseconds=50)
            await session.execute(
                sa.update(JobRow)
                .where(JobRow.id == active.job_id)
                .values(
                    lease_expires_at=expired_at,
                    retry_safety="safe",
                ),
            )
            await session.execute(
                sa.update(RunRow).where(RunRow.run_id == active.run_id).values(execution_lease_expires_at=expired_at),
            )

        monkeypatch.setattr(
            run_repository_module,
            "datetime",
            _NegativelySkewedDateTime,
        )
        boundary = PrivateRunExecutionBoundary(
            seed.factory,
            context=seed.owner_a,
            claim=JobClaim(
                job_id=active.job_id,
                attempt_id=active.attempt_id,
                lease_token=active.lease_token,
                job_type="private_run",
                scope=JobScope(
                    seed.owner_a.project_id,
                    str(seed.owner_a.user_id),
                ),
                run_id=active.run_id,
                occurrence_id=None,
                retry_safety="safe",
                cancel_requested=False,
            ),
        )

        with pytest.raises(AuthorizationRevoked):
            await boundary.before_sandbox_exec()
        assert boundary.lease_lost is True
        assert boundary.ambiguous_side_effect is False

        async with seed.factory() as session:
            persisted_job = await session.get(JobRow, active.job_id)
            persisted_run = await session.get(RunRow, active.run_id)
            persisted_attempt = await session.get(
                JobAttemptRow,
                active.attempt_id,
            )
            assert persisted_job is not None and persisted_run is not None
            assert persisted_attempt is not None
            assert persisted_job.status == "running"
            assert persisted_job.retry_safety == "safe"
            assert persisted_job.lease_expires_at == expired_at
            assert persisted_run.status == "running"
            assert persisted_run.execution_lease_expires_at == expired_at
            assert persisted_attempt.outcome is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_claim_uses_database_clock_when_worker_clock_is_ahead(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed, lease_seconds=5)
        next_worker_id = await _add_worker(seed)
        async with seed.factory() as session, session.begin():
            database_now = await session.scalar(
                sa.select(sa.func.clock_timestamp()),
            )
            assert isinstance(database_now, datetime)
            lease_expires_at = database_now + timedelta(seconds=30)
            await session.execute(
                sa.update(JobRow)
                .where(JobRow.id == active.job_id)
                .values(
                    lease_expires_at=lease_expires_at,
                    retry_safety="safe",
                ),
            )

        original_now = JobRepository._now

        def positively_skewed_now(value):
            if value is None:
                return database_now + timedelta(days=1)
            return original_now(value)

        monkeypatch.setattr(
            JobRepository,
            "_now",
            staticmethod(positively_skewed_now),
        )
        async with seed.factory() as session, session.begin():
            claim = await JobRepository(session).claim_next(
                worker_id=next_worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=60,
            )
            assert claim is None

        async with seed.factory() as session:
            job = await session.get(JobRow, active.job_id)
            attempts = (
                await session.scalars(
                    sa.select(JobAttemptRow).where(JobAttemptRow.job_id == active.job_id).order_by(JobAttemptRow.attempt_number),
                )
            ).all()
            assert job is not None
            assert job.status == "running"
            assert job.attempt_count == 1
            assert job.lease_token_hash == active.lease_hash
            assert job.lease_expires_at == lease_expires_at
            assert len(attempts) == 1
            assert attempts[0].attempt_number == 1
            assert attempts[0].outcome is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_claim_rechecks_database_clock_after_authority_lock_wait(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed, lease_seconds=5)
        next_worker_id = await _add_worker(seed)
        async with seed.factory() as session, session.begin():
            database_now = await session.scalar(
                sa.select(sa.func.clock_timestamp()),
            )
            assert isinstance(database_now, datetime)
            await session.execute(
                sa.update(JobRow)
                .where(JobRow.id == active.job_id)
                .values(
                    lease_expires_at=database_now - timedelta(seconds=1),
                    retry_safety="safe",
                ),
            )

        original_now = JobRepository._now

        def positively_skewed_now(value):
            if value is None:
                return database_now + timedelta(days=1)
            return original_now(value)

        monkeypatch.setattr(
            JobRepository,
            "_now",
            staticmethod(positively_skewed_now),
        )

        async def claim_after_candidate_selection():
            async with seed.factory() as session, session.begin():
                await session.execute(
                    sa.text("SET LOCAL statement_timeout = '2s'"),
                )
                return await JobRepository(session).claim_next(
                    worker_id=next_worker_id,
                    capabilities=frozenset({"private_run"}),
                    lease_seconds=60,
                )

        async with seed.factory() as blocker, blocker.begin():
            await blocker.execute(sa.text("SET LOCAL statement_timeout = '2s'"))
            project = await blocker.get(
                ProjectRow,
                seed.owner_a.project_id,
                with_for_update=True,
            )
            job = await blocker.get(JobRow, active.job_id, with_for_update=True)
            assert project is not None and job is not None
            database_now = await blocker.scalar(
                sa.select(sa.func.clock_timestamp()),
            )
            assert isinstance(database_now, datetime)
            lease_expires_at = database_now + timedelta(seconds=30)
            job.lease_expires_at = lease_expires_at
            await blocker.flush()

            claim_task = asyncio.create_task(claim_after_candidate_selection())
            await asyncio.sleep(0.25)
            assert not claim_task.done()

        assert await asyncio.wait_for(claim_task, timeout=3) is None
        async with seed.factory() as session:
            job = await session.get(JobRow, active.job_id)
            attempts = (
                await session.scalars(
                    sa.select(JobAttemptRow).where(JobAttemptRow.job_id == active.job_id).order_by(JobAttemptRow.attempt_number),
                )
            ).all()
            assert job is not None
            assert job.status == "running"
            assert job.attempt_count == 1
            assert job.lease_token_hash == active.lease_hash
            assert job.lease_expires_at == lease_expires_at
            assert len(attempts) == 1
            assert attempts[0].attempt_number == 1
            assert attempts[0].outcome is None
    finally:
        await seed.engine.dispose()
