from __future__ import annotations

import asyncio
import hashlib
import hmac
import uuid
from datetime import UTC, datetime

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.errors import (
    PrivateWorkMcpQuotaExceeded,
    PrivateWorkRunQuotaExceeded,
    PrivateWorkStorageQuotaExceeded,
)
from app.private_work.file_service import PrivateFileService
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.context import ProjectContext
from app.projects.errors import ProjectMemberQuotaExceeded
from app.projects.invitation_repository import InvitationRepository
from app.projects.invitation_service import InvitationService
from app.projects.models import ProjectRole
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.models import (
    ProjectQuotaLimits,
    QuotaSourceRef,
    _issue_quota_reconciliation_authority,
)
from app.quotas.reconciliation import QuotaReconciler
from app.quotas.service import QuotaService
from app.reliability.execution import (
    AgentExecutionResult,
    LeaseAuthorizedStreamBridge,
    PrivateRunExecutionBoundary,
    PrivateRunJobHandler,
    PrivateRunJobTerminalPort,
    TransientExecutionError,
)
from app.reliability.workers import WorkerRegistry
from app.worker.service import JobLeaseAuthority
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.jobs.sql import JobRepository, JobTerminalEvent
from deerflow.runtime.events.stream import PostgresStreamBridge


def _source_ref(payload: bytes) -> QuotaSourceRef:
    return QuotaSourceRef(
        key_id="test-quota",
        hmac_hex=hmac.new(
            b"test-quota-integration-key" * 2,
            payload,
            hashlib.sha256,
        ).hexdigest(),
    )


def _project_context(private_context) -> ProjectContext:
    return ProjectContext(
        user_id=private_context.user_id,
        project_id=private_context.project_id,
        membership_id=private_context.membership_id,
        role=private_context.role,
        capabilities=private_context.capabilities,
        membership_version=private_context.membership_version,
        request_id=private_context.request_id,
    )


async def _chunks(value: bytes):
    yield value


async def _wait_for_project_lock_wait(factory) -> None:
    for _ in range(200):
        async with factory() as session:
            waiting = await session.scalar(
                text(
                    """SELECT EXISTS (
                        SELECT 1 FROM pg_stat_activity
                        WHERE datname=current_database()
                          AND pid<>pg_backend_pid()
                          AND wait_event_type='Lock'
                          AND query ILIKE '%projects%'
                    )"""
                )
            )
        if waiting:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("operation did not wait on the Project lock")


@pytest.mark.postgres
@pytest.mark.anyio
async def test_run_admission_enforces_limit_and_terminal_cancel_releases_once(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    enforcer = ProjectQuotaEnforcer(quotas)
    admission = PrivateRunAdmissionService(seed.factory, quota=enforcer)
    runs = PrivateRunService(seed.factory, quota=enforcer)
    thread_ids = tuple(str(uuid.uuid4()) for _ in range(4))
    try:
        async with seed.factory() as session, session.begin():
            repository = PrivateThreadRepository(session)
            for thread_id in thread_ids:
                await repository.create(
                    scope=seed.owner_a_scope,
                    thread_id=thread_id,
                    agent=ThreadAgentRef(seed.project_agent_id, "project"),
                )

        admitted = []
        for thread_id in thread_ids[:3]:
            admitted.append(
                await admission.admit(
                    seed.owner_a,
                    thread_id,
                    PrivateRunCreate(run_id=str(uuid.uuid4())),
                )
            )
        with pytest.raises(PrivateWorkRunQuotaExceeded):
            await admission.admit(
                seed.owner_a,
                thread_ids[3],
                PrivateRunCreate(run_id=str(uuid.uuid4())),
            )

        for thread_id, record in zip(thread_ids, admitted, strict=False):
            await runs.cancel(seed.owner_a, thread_id, record.run.run_id)

        async with seed.factory() as session:
            result = (
                await session.execute(
                    text(
                        """SELECT reserved,
                                  (SELECT count(*) FROM project_usage_ledger
                                   WHERE project_id=:project_id
                                     AND dimension='concurrent_runs'
                                     AND source_kind='release') AS releases
                           FROM project_usage_counters
                           WHERE project_id=:project_id
                             AND dimension='concurrent_runs'
                             AND bucket='lifetime'"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
            ).one()
            run_count = await session.scalar(
                text(
                    """SELECT count(*) FROM runs
                       WHERE project_id=:project_id"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
        assert result.reserved == 0
        assert result.releases == 3
        assert run_count == 3
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_worker_retry_retains_run_reservation_until_terminal_settlement(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    enforcer = ProjectQuotaEnforcer(quotas)
    thread_id = str(uuid.uuid4())
    attempts = 0

    class Executor:
        async def execute(self, _execution, _authority):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TransientExecutionError("MODEL_TEMPORARILY_UNAVAILABLE")
            return AgentExecutionResult.succeeded()

    async def claim(worker_id: uuid.UUID):
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claimed = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=90,
            )
            assert claimed is not None
            assert await jobs.mark_running(
                claimed.job_id,
                lease_token=claimed.lease_token,
            )
            return claimed

    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        admitted = await PrivateRunAdmissionService(
            seed.factory,
            quota=enforcer,
        ).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(),
        )
        worker_id = uuid.uuid4()
        await WorkerRegistry(seed.factory, version="quota-retry-test").register(
            worker_id,
            frozenset({"private_run"}),
            1,
        )
        handler = PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
            quota=enforcer,
            retry_initial_seconds=1,
        )

        first_claim = await claim(worker_id)
        first = await handler(
            first_claim,
            JobLeaseAuthority(seed.factory, first_claim, lease_seconds=90),
        )
        await first.commit()
        async with seed.factory() as session:
            assert (
                await session.scalar(
                    text(
                        """SELECT reserved FROM project_usage_counters
                       WHERE project_id=:project_id
                         AND dimension='concurrent_runs'
                         AND bucket='lifetime'"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
                == 1
            )
        async with seed.factory() as session, session.begin():
            await session.execute(
                text("UPDATE jobs SET available_at=now() WHERE id=:job_id"),
                {"job_id": admitted.job.job_id},
            )

        second_claim = await claim(worker_id)
        second = await handler(
            second_claim,
            JobLeaseAuthority(seed.factory, second_claim, lease_seconds=90),
        )
        await second.commit()
        await second.commit()

        async with seed.factory() as session:
            result = (
                await session.execute(
                    text(
                        """SELECT reserved,
                                  (SELECT count(*) FROM project_usage_ledger
                                   WHERE project_id=:project_id
                                     AND dimension='concurrent_runs'
                                     AND source_kind='release') AS releases
                           FROM project_usage_counters
                           WHERE project_id=:project_id
                             AND dimension='concurrent_runs'
                             AND bucket='lifetime'"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
            ).one()
        assert tuple(result) == (0, 1)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_revoked_member_terminal_releases_run_with_database_derived_scope(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    enforcer = ProjectQuotaEnforcer(quotas)
    thread_id = str(uuid.uuid4())

    class Executor:
        async def execute(self, _execution, _authority):
            raise AssertionError("revoked authority must not execute")

    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        admitted = await PrivateRunAdmissionService(
            seed.factory,
            quota=enforcer,
        ).admit(seed.owner_a, thread_id, PrivateRunCreate())
        worker_id = uuid.uuid4()
        await WorkerRegistry(seed.factory, version="quota-revoked-test").register(
            worker_id,
            frozenset({"private_run"}),
            1,
        )
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=90,
            )
            assert claim is not None
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE project_memberships
                       SET status='removed',version=version+1
                       WHERE id=:membership_id"""
                ),
                {"membership_id": seed.owner_a.membership_id},
            )

        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
            quota=enforcer,
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT r.status,j.status,c.reserved
                           FROM runs r
                           JOIN jobs j ON j.id=r.job_id
                           JOIN project_usage_counters c
                             ON c.project_id=r.project_id
                            AND c.dimension='concurrent_runs'
                            AND c.bucket='lifetime'
                           WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert tuple(state) == ("interrupted", "cancelled", 0)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_recovered_stream_terminal_releases_exact_run_reservation(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    enforcer = ProjectQuotaEnforcer(quotas)
    thread_id = str(uuid.uuid4())
    executor_calls = 0

    class Executor:
        async def execute(self, _execution, _authority):
            nonlocal executor_calls
            executor_calls += 1
            return AgentExecutionResult.succeeded()

    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        admitted = await PrivateRunAdmissionService(
            seed.factory,
            quota=enforcer,
        ).admit(seed.owner_a, thread_id, PrivateRunCreate())
        worker_id = uuid.uuid4()
        await WorkerRegistry(seed.factory, version="quota-terminal-test").register(
            worker_id,
            frozenset({"private_run"}),
            1,
        )
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=90,
            )
            assert claim is not None
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )
            await PrivateRunRepository(session, jobs=jobs).begin_execution(
                scope=seed.owner_a_scope,
                run_id=admitted.run.run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
            )
        await LeaseAuthorizedStreamBridge(
            PostgresStreamBridge(seed.factory),
            PrivateRunExecutionBoundary(
                seed.factory,
                context=seed.owner_a,
                claim=claim,
            ),
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            terminal_status=lambda: "success",
        ).publish_end(admitted.run.run_id)

        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
            quota=enforcer,
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        assert executor_calls == 0
        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT r.status,j.status,c.reserved
                           FROM runs r
                           JOIN jobs j ON j.id=r.job_id
                           JOIN project_usage_counters c
                             ON c.project_id=r.project_id
                            AND c.dimension='concurrent_runs'
                            AND c.bucket='lifetime'
                           WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert tuple(state) == ("success", "succeeded", 0)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_worker_terminal_waits_for_project_before_locking_job(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    enforcer = ProjectQuotaEnforcer(quotas)
    thread_id = str(uuid.uuid4())
    settlement_task = None

    class Executor:
        async def execute(self, _execution, _authority):
            return AgentExecutionResult.succeeded()

    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        admitted = await PrivateRunAdmissionService(
            seed.factory,
            quota=enforcer,
        ).admit(seed.owner_a, thread_id, PrivateRunCreate())
        worker_id = uuid.uuid4()
        await WorkerRegistry(seed.factory, version="quota-lock-order-test").register(
            worker_id,
            frozenset({"private_run"}),
            1,
        )
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=90,
            )
            assert claim is not None
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )
        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
            quota=enforcer,
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )

        async with seed.factory() as blocker, blocker.begin():
            await blocker.execute(
                text("SELECT id FROM projects WHERE id=:id FOR UPDATE"),
                {"id": seed.owner_a.project_id},
            )
            settlement_task = asyncio.create_task(settlement.commit())
            await _wait_for_project_lock_wait(seed.factory)
            async with seed.factory() as probe, probe.begin():
                locked = await probe.scalar(
                    text("SELECT id FROM jobs WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": admitted.job.job_id},
                )
                assert locked == admitted.job.job_id

        await settlement_task
        settlement_task = None
    finally:
        if settlement_task is not None and not settlement_task.done():
            settlement_task.cancel()
            await asyncio.gather(settlement_task, return_exceptions=True)
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_branch_rollback_waits_for_project_before_locking_file(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    enforcer = ProjectQuotaEnforcer(quotas)
    files = PrivateFileService(seed.factory, quota=enforcer)
    thread_id = str(uuid.uuid4())
    rollback_task = None
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        uploaded = await files.upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="rollback.bin",
            media_type="application/octet-stream",
            chunks=_chunks(b"rollback"),
        )

        async with seed.factory() as blocker, blocker.begin():
            await blocker.execute(
                text("SELECT id FROM projects WHERE id=:id FOR UPDATE"),
                {"id": seed.owner_a.project_id},
            )
            rollback_task = asyncio.create_task(
                files.rollback_branch_authority(
                    seed.owner_a_scope,
                    "source-thread",
                    thread_id,
                )
            )
            await _wait_for_project_lock_wait(seed.factory)
            async with seed.factory() as probe, probe.begin():
                locked = await probe.scalar(
                    text("SELECT id FROM files WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": uploaded.id},
                )
                assert locked == uploaded.id

        await rollback_task
        rollback_task = None
        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT count(*) FROM files WHERE id=:file_id),
                           (SELECT reserved FROM project_usage_counters
                            WHERE project_id=:project_id
                              AND dimension='storage_bytes'
                              AND bucket='lifetime')"""
                    ),
                    {
                        "file_id": uploaded.id,
                        "project_id": seed.owner_a.project_id,
                    },
                )
            ).one()
        assert tuple(state) == (0, 0)
    finally:
        if rollback_task is not None and not rollback_task.done():
            rollback_task.cancel()
            await asyncio.gather(rollback_task, return_exceptions=True)
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_unowned_dead_job_terminal_port_releases_run_reservation_once(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    enforcer = ProjectQuotaEnforcer(quotas)
    thread_id = str(uuid.uuid4())
    occurred_at = datetime(2026, 7, 16, 13, tzinfo=UTC)
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        admitted = await PrivateRunAdmissionService(
            seed.factory,
            quota=enforcer,
        ).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(),
        )
        event = JobTerminalEvent(
            job_id=admitted.job.job_id,
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            run_id=admitted.run.run_id,
            occurrence_id=None,
            job_type="private_run",
            status="dead",
            retry_safety="safe",
            public_error_code="MAX_ATTEMPTS_EXCEEDED",
            cancel_reason=None,
            occurred_at=occurred_at,
        )
        terminal = PrivateRunJobTerminalPort(quota=enforcer)
        async with seed.factory() as session, session.begin():
            await terminal.job_terminalized(session, event)
        async with seed.factory() as session, session.begin():
            await terminal.job_terminalized(session, event)

        async with seed.factory() as session:
            result = (
                await session.execute(
                    text(
                        """SELECT c.reserved,r.status,
                                  (SELECT count(*) FROM project_usage_ledger l
                                   WHERE l.project_id=:project_id
                                     AND l.dimension='concurrent_runs'
                                     AND l.source_kind='release') AS releases
                           FROM project_usage_counters c
                           JOIN runs r ON r.project_id=c.project_id
                           WHERE c.project_id=:project_id
                             AND c.dimension='concurrent_runs'
                             AND c.bucket='lifetime'
                             AND r.run_id=:run_id"""
                    ),
                    {
                        "project_id": seed.owner_a.project_id,
                        "run_id": admitted.run.run_id,
                    },
                )
            ).one()
        assert tuple(result) == (0, "error", 1)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_storage_finalize_rejects_atomically_and_delete_releases(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    config = QuotaConfig(default_storage_bytes_limit=4)
    quotas = QuotaService(seed.factory, config, source_ref_hasher=_source_ref)
    service = PrivateFileService(seed.factory, quota=ProjectQuotaEnforcer(quotas))
    thread_id = str(uuid.uuid4())
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )

        with pytest.raises(PrivateWorkStorageQuotaExceeded):
            await service.upload(
                seed.owner_a,
                thread_id=thread_id,
                logical_path="too-large.bin",
                media_type="application/octet-stream",
                chunks=_chunks(b"12345"),
            )
        ready = await service.upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="fits.bin",
            media_type="application/octet-stream",
            chunks=_chunks(b"1234"),
        )
        await service.delete_ready(
            seed.owner_a,
            thread_id=thread_id,
            file_id=ready.id,
        )

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT used,reserved FROM project_usage_counters
                           WHERE project_id=:project_id
                             AND dimension='storage_bytes'
                             AND bucket='lifetime'"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
            ).one()
            visible = await session.scalar(
                text(
                    """SELECT count(*) FROM files
                       WHERE project_id=:project_id AND status='ready'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
            rejected_rows = await session.scalar(
                text(
                    """SELECT count(*) FROM files
                       WHERE project_id=:project_id
                         AND owner_user_id=:owner_user_id
                         AND thread_id=:thread_id
                         AND logical_path='too-large.bin'"""
                ),
                {
                    "project_id": seed.owner_a.project_id,
                    "owner_user_id": str(seed.owner_a.user_id),
                    "thread_id": thread_id,
                },
            )
            net = await session.scalar(
                text(
                    """SELECT sum(delta) FROM project_usage_ledger
                       WHERE project_id=:project_id
                         AND dimension='storage_bytes'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
        assert tuple(state) == (0, 0)
        assert visible == 0
        assert rejected_rows == 0
        assert net == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_thread_delete_releases_all_ready_storage_reservations(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    enforcer = ProjectQuotaEnforcer(quotas)
    files = PrivateFileService(seed.factory, quota=enforcer)
    thread_id = str(uuid.uuid4())
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        await files.upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="one.bin",
            media_type="application/octet-stream",
            chunks=_chunks(b"123"),
        )
        await files.upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="two.bin",
            media_type="application/octet-stream",
            chunks=_chunks(b"4567"),
        )

        await (
            ProjectScopedCheckpointer(
                InMemorySaver(),
                seed.factory,
                quota=enforcer,
            )
            .for_context(seed.owner_a)
            .adelete_thread(
                thread_id,
                expected_version=1,
            )
        )

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT reserved,
                                  (SELECT count(*) FROM project_usage_ledger
                                   WHERE project_id=:project_id
                                     AND dimension='storage_bytes'
                                     AND source_kind='release') AS releases
                           FROM project_usage_counters
                           WHERE project_id=:project_id
                             AND dimension='storage_bytes'
                             AND bucket='lifetime'"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
            ).one()
        assert tuple(state) == (0, 2)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_member_redeem_at_limit_rolls_back_membership_and_invitation(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    enforcer = ProjectQuotaEnforcer(quotas)
    reconciler = QuotaReconciler(seed.factory, quotas)
    invited_user_id = uuid.uuid4()
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    try:
        await reconciler.execute(
            _issue_quota_reconciliation_authority(
                seed.owner_a.project_id,
                operation="quota_repair",
            ),
            now=now,
        )
        async with seed.factory() as session, session.begin():
            await quotas.set_limits(
                session,
                seed.owner_a,
                ProjectQuotaLimits(member_limit=3),
                expected_version=0,
            )
            await session.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'user',now(),false,0)"""
                ),
                {
                    "id": str(invited_user_id),
                    "email": f"{invited_user_id}@example.com",
                },
            )

        async with seed.factory() as session:
            invitations = InvitationService(
                InvitationRepository(session),
                quota=enforcer,
            )
            created = await invitations.create(
                _project_context(seed.owner_a),
                f"{invited_user_id}@example.com",
                ProjectRole.RUNNER,
                now,
            )
            claim = await invitations.claim(created.token, now)
            with pytest.raises(ProjectMemberQuotaExceeded):
                await invitations.redeem(
                    invited_user_id,
                    f"{invited_user_id}@example.com",
                    claim,
                    now,
                )

        async with seed.factory() as session:
            invitation_status = await session.scalar(
                text("SELECT status FROM project_invitations WHERE id=:id"),
                {"id": created.invitation.id},
            )
            membership_count = await session.scalar(
                text(
                    """SELECT count(*) FROM project_memberships
                       WHERE project_id=:project_id AND user_id=:user_id"""
                ),
                {
                    "project_id": seed.owner_a.project_id,
                    "user_id": str(invited_user_id),
                },
            )
        assert invitation_status == "pending"
        assert membership_count == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_mcp_actual_dispatch_is_counted_before_failure_and_hard_limited(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quotas = QuotaService(
        seed.factory,
        QuotaConfig(default_mcp_calls_daily_limit=1),
        source_ref_hasher=_source_ref,
    )
    enforcer = ProjectQuotaEnforcer(quotas)
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    try:
        await enforcer.consume_mcp_dispatch(
            seed.owner_a,
            dispatch_id=uuid.uuid4(),
            now=now,
        )
        with pytest.raises(PrivateWorkMcpQuotaExceeded):
            await enforcer.consume_mcp_dispatch(
                seed.owner_a,
                dispatch_id=uuid.uuid4(),
                now=now,
            )
        async with seed.factory() as session:
            used = await session.scalar(
                text(
                    """SELECT used FROM project_usage_counters
                       WHERE project_id=:project_id
                         AND dimension='mcp_calls_daily'
                         AND bucket='2026-07-16'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
        assert used == 1
    finally:
        await seed.engine.dispose()
