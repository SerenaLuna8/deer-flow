from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from app.automations.dispatcher import (
    AutomationDefinitionRef,
    AutomationDispatcher,
)
from app.automations.errors import AutomationActiveRun, AutomationConcurrencyLimit
from app.automations.occurrences import AutomationOccurrenceService
from app.automations.reconciliation import AutomationReconciler
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.reliability.execution import (
    AgentExecutionResult,
    PrivateRunJobHandler,
    PrivateRunJobTerminalPort,
)
from app.reliability.owner_refs import AuditHmacKeyring
from app.reliability.workers import WorkerRegistry
from app.worker.service import JobLeaseAuthority
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.jobs.sql import JobRepository, JobTerminalEvent
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskCreate,
    ScheduledTaskRepository,
)

NOW = datetime(2026, 7, 16, 13, 0, tzinfo=UTC)


async def _create_due_task(
    seed,
    *,
    task_id: str,
    schedule_type: str = "cron",
):
    async with seed.factory() as session, session.begin():
        return await ScheduledTaskRepository(session).create(
            seed.owner_a_scope,
            ScheduledTaskCreate(
                task_id=task_id,
                thread_id=None,
                context_mode="fresh_thread_per_run",
                agent_asset_id=seed.project_agent_id,
                agent_scope="project",
                title="M6 atomic automation",
                prompt="Process this project in the background.",
                schedule_type=schedule_type,
                schedule_spec=({"cron": "0 * * * *"} if schedule_type == "cron" else {"run_at": NOW.isoformat()}),
                timezone="UTC",
                next_run_at=NOW,
            ),
        )


async def _create_reuse_task(seed, *, task_id: str, thread_id: str):
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        return await ScheduledTaskRepository(session).create(
            seed.owner_a_scope,
            ScheduledTaskCreate(
                task_id=task_id,
                thread_id=thread_id,
                context_mode="reuse_thread",
                agent_asset_id=seed.project_agent_id,
                agent_scope="project",
                title="M6 reuse-thread automation",
                prompt="Process this project in the background.",
                schedule_type="cron",
                schedule_spec={"cron": "0 * * * *"},
                timezone="UTC",
                next_run_at=NOW,
            ),
        )


def _definition(seed, task) -> AutomationDefinitionRef:
    return AutomationDefinitionRef(
        project_id=seed.owner_a.project_id,
        owner_user_id=str(seed.owner_a.user_id),
        task_id=task.id,
        membership_version=seed.owner_a.membership_version,
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_occurrence_run_job_commit_together(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        task = await _create_due_task(
            seed,
            task_id=f"m6-atomic-{uuid.uuid4().hex[:20]}",
        )
        admitted = await AutomationDispatcher(seed.factory).admit_occurrence(
            _definition(seed, task),
            scheduled_for=NOW,
        )

        assert admitted.created is True
        assert admitted.occurrence.run_id == admitted.run.run_id
        assert admitted.occurrence.thread_id == admitted.run.thread_id
        assert admitted.occurrence.job_id == admitted.job.job_id
        assert admitted.run.job_id == admitted.job.job_id
        assert admitted.job.job_type == "automation_run"

        async with seed.factory() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT o.status AS occurrence_status,
                                  o.run_id AS occurrence_run_id,
                                  o.job_id AS occurrence_job_id,
                                  r.status AS run_status,
                                  r.job_id AS run_job_id,
                                  j.status AS job_status,
                                  j.job_type,
                                  j.automation_occurrence_id
                           FROM scheduled_task_runs o
                           JOIN runs r ON r.run_id=o.run_id
                            AND r.project_id=o.project_id
                            AND r.owner_user_id=o.owner_user_id
                           JOIN jobs j ON j.id=o.job_id
                           WHERE o.id=:occurrence_id"""
                    ),
                    {"occurrence_id": admitted.occurrence.id},
                )
            ).one()
        assert tuple(row) == (
            "running",
            admitted.run.run_id,
            admitted.job.job_id,
            "pending",
            admitted.job.job_id,
            "queued",
            "automation_run",
            admitted.occurrence.id,
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_automation_admission_enforces_shared_project_run_quota_atomically(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    keyring = AuditHmacKeyring.from_environment()
    quota = ProjectQuotaEnforcer(
        QuotaService(
            seed.factory,
            QuotaConfig(),
            source_ref_hasher=keyring.quota_source_ref,
        )
    )
    dispatcher = AutomationDispatcher(seed.factory, quota=quota)
    try:
        tasks = [
            await _create_due_task(
                seed,
                task_id=f"m6-quota-{uuid.uuid4().hex[:20]}",
            )
            for _ in range(4)
        ]
        for task in tasks[:3]:
            await dispatcher.admit_occurrence(
                _definition(seed, task),
                scheduled_for=NOW,
            )
        with pytest.raises(AutomationConcurrencyLimit):
            await dispatcher.admit_occurrence(
                _definition(seed, tasks[3]),
                scheduled_for=NOW,
            )

        async with seed.factory() as session:
            reserved = await session.scalar(
                text(
                    """SELECT reserved FROM project_usage_counters
                       WHERE project_id=:project_id
                         AND dimension='concurrent_runs'
                         AND bucket='lifetime'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
            run_count = await session.scalar(
                text("SELECT count(*) FROM runs WHERE project_id=:project_id"),
                {"project_id": seed.owner_a.project_id},
            )
            rejected_occurrence = await session.scalar(
                text("SELECT count(*) FROM scheduled_task_runs WHERE task_id=:task_id"),
                {"task_id": tasks[3].id},
            )
        assert reserved == 3
        assert run_count == 3
        assert rejected_occurrence == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_scheduled_retry_and_lock_competition_return_one_occurrence_run_job(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        task = await _create_due_task(
            seed,
            task_id=f"m6-race-{uuid.uuid4().hex[:20]}",
        )
        results = await asyncio.gather(
            *(
                AutomationDispatcher(seed.factory).admit_occurrence(
                    _definition(seed, task),
                    scheduled_for=NOW,
                )
                for _ in range(2)
            )
        )

        assert results[0].occurrence.id == results[1].occurrence.id
        assert results[0].run.run_id == results[1].run.run_id
        assert results[0].job.job_id == results[1].job.job_id
        assert sorted(result.created for result in results) == [False, True]
        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                            (SELECT count(*) FROM scheduled_task_runs WHERE task_id=:task_id),
                            (SELECT count(*) FROM runs WHERE metadata_json->>'scheduled_task_id'=:task_id),
                            (SELECT count(*) FROM jobs WHERE automation_occurrence_id IS NOT NULL
                              AND run_id IN (SELECT run_id FROM runs WHERE metadata_json->>'scheduled_task_id'=:task_id))"""
                    ),
                    {"task_id": task.id},
                )
            ).one()
        assert tuple(counts) == (1, 1, 1)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_scheduled_retry_adopts_committed_triple_after_definition_pause(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        task = await _create_due_task(
            seed,
            task_id=f"m6-paused-retry-{uuid.uuid4().hex[:18]}",
        )
        dispatcher = AutomationDispatcher(seed.factory, clock=lambda: NOW)
        admitted = await dispatcher.admit_occurrence(
            _definition(seed, task),
            scheduled_for=NOW,
        )
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE scheduled_tasks
                       SET status='paused',next_run_at=NULL,updated_at=:now
                       WHERE id=:task_id"""
                ),
                {"task_id": task.id, "now": NOW},
            )

        replay = await dispatcher.admit_occurrence(
            _definition(seed, task),
            scheduled_for=NOW,
        )

        assert replay.created is False
        assert replay.occurrence.id == admitted.occurrence.id
        assert replay.run.run_id == admitted.run.run_id
        assert replay.job.job_id == admitted.job.job_id
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_due_definition_keyset_pages_past_earlier_task(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        first_task = await _create_due_task(seed, task_id="m6-page-a")
        second_task = await _create_due_task(seed, task_id="m6-page-b")
        occurrences = AutomationOccurrenceService(
            seed.factory,
            max_concurrent_runs=1,
        )

        first_page = await occurrences.due_definitions(
            now=NOW,
            limit=1,
        )
        definition, scheduled_for = first_page[0]
        second_page = await occurrences.due_definitions(
            now=NOW,
            limit=1,
            after=(
                scheduled_for,
                definition.project_id,
                definition.owner_user_id,
                definition.task_id,
            ),
        )

        assert definition.task_id == first_task.id
        assert [item[0].task_id for item in second_page] == [second_task.id]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_late_scheduled_admission_preserves_tick_but_uses_admission_time(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    admitted_at = NOW + timedelta(minutes=17)
    try:
        task = await _create_due_task(
            seed,
            task_id=f"m6-late-{uuid.uuid4().hex[:20]}",
        )

        admitted = await AutomationDispatcher(
            seed.factory,
            clock=lambda: admitted_at,
        ).admit_occurrence(
            _definition(seed, task),
            scheduled_for=NOW,
        )

        assert admitted.occurrence.scheduled_for == NOW
        assert admitted.occurrence.created_at == admitted_at
        assert admitted.occurrence.started_at == admitted_at
        async with seed.factory() as session:
            row = (
                await session.execute(
                    text("SELECT next_run_at,updated_at FROM scheduled_tasks WHERE id=:task_id"),
                    {"task_id": task.id},
                )
            ).one()
        assert row.next_run_at > admitted_at
        assert row.updated_at == admitted_at
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_manual_trigger_is_idempotent_and_uses_same_atomic_admission(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    key = uuid.uuid4()
    try:
        task = await _create_due_task(
            seed,
            task_id=f"m6-manual-{uuid.uuid4().hex[:20]}",
        )
        dispatcher = AutomationDispatcher(seed.factory)
        first = await dispatcher.admit_manual(
            seed.owner_a,
            task.id,
            key,
            scheduled_for=NOW,
        )
        replay = await dispatcher.admit_manual(
            seed.owner_a,
            task.id,
            key,
            scheduled_for=NOW,
        )

        assert first.created is True
        assert replay.created is False
        assert replay.occurrence.id == first.occurrence.id
        assert replay.run.run_id == first.run.run_id
        assert replay.job.job_id == first.job.job_id
        assert first.occurrence.trigger == "manual"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_admission_failure_rolls_back_occurrence_thread_run_snapshot_and_job(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    task_id = f"m6-rollback-{uuid.uuid4().hex[:20]}"
    try:
        task = await _create_due_task(seed, task_id=task_id)

        class FailingDispatcher(AutomationDispatcher):
            async def _after_job_attached(self, *_args) -> None:
                raise RuntimeError("injected atomic rollback")

        with pytest.raises(RuntimeError, match="injected atomic rollback"):
            await FailingDispatcher(seed.factory).admit_occurrence(
                _definition(seed, task),
                scheduled_for=NOW,
            )

        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                            (SELECT count(*) FROM scheduled_task_runs WHERE task_id=:task_id),
                            (SELECT count(*) FROM threads_meta WHERE metadata_json->>'scheduled_task_id'=:task_id),
                            (SELECT count(*) FROM runs WHERE metadata_json->>'scheduled_task_id'=:task_id),
                            (SELECT count(*) FROM jobs WHERE automation_occurrence_id IS NOT NULL
                              AND run_id IN (SELECT run_id FROM runs WHERE metadata_json->>'scheduled_task_id'=:task_id)),
                            (SELECT count(*) FROM run_asset_versions
                              WHERE run_id IN (SELECT run_id FROM runs WHERE metadata_json->>'scheduled_task_id'=:task_id))"""
                    ),
                    {"task_id": task_id},
                )
            ).one()
        assert tuple(counts) == (0, 0, 0, 0, 0)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_scheduled_reuse_thread_overlap_skips_atomically_and_advances(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-reuse-scheduled-{uuid.uuid4()}"
    try:
        task = await _create_reuse_task(
            seed,
            task_id=f"m6-reuse-scheduled-{uuid.uuid4().hex[:16]}",
            thread_id=thread_id,
        )
        await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(run_id=str(uuid.uuid4())),
        )

        skipped = await AutomationDispatcher(seed.factory).admit_occurrence(
            _definition(seed, task),
            scheduled_for=NOW,
        )

        assert skipped.created is True
        assert skipped.occurrence.status == "skipped"
        assert skipped.occurrence.error_code == "AUTOMATION_OVERLAP_SKIPPED"
        assert skipped.occurrence.thread_id is None
        assert skipped.occurrence.run_id is None
        assert skipped.occurrence.job_id is None
        async with seed.factory() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT t.next_run_at,t.run_count,t.last_outcome,
                                  count(r.id) FILTER (WHERE r.task_id=t.id) AS occurrences
                           FROM scheduled_tasks t
                           LEFT JOIN scheduled_task_runs r
                             ON r.task_id=t.id
                            AND r.project_id=t.project_id
                            AND r.owner_user_id=t.owner_user_id
                           WHERE t.id=:task_id
                           GROUP BY t.id"""
                    ),
                    {"task_id": task.id},
                )
            ).one()
        assert row.next_run_at > NOW
        assert row.run_count == 1
        assert row.last_outcome == "skipped"
        assert row.occurrences == 1
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_manual_reuse_thread_overlap_rejects_without_partial_admission(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-reuse-manual-{uuid.uuid4()}"
    try:
        task = await _create_reuse_task(
            seed,
            task_id=f"m6-reuse-manual-{uuid.uuid4().hex[:16]}",
            thread_id=thread_id,
        )
        await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(run_id=str(uuid.uuid4())),
        )

        with pytest.raises(AutomationActiveRun):
            await AutomationDispatcher(seed.factory).admit_manual(
                seed.owner_a,
                task.id,
                uuid.uuid4(),
                scheduled_for=NOW,
            )

        async with seed.factory() as session:
            count = await session.scalar(
                text("SELECT count(*) FROM scheduled_task_runs WHERE task_id=:task_id"),
                {"task_id": task.id},
            )
        assert count == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_dead_once_automation_job_terminalizes_occurrence_and_definition(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    occurred_at = NOW.replace(minute=5)
    try:
        task = await _create_due_task(
            seed,
            task_id=f"m6-once-dead-{uuid.uuid4().hex[:16]}",
            schedule_type="once",
        )
        admitted = await AutomationDispatcher(seed.factory).admit_occurrence(
            _definition(seed, task),
            scheduled_for=NOW,
        )

        async with seed.factory() as session, session.begin():
            await PrivateRunJobTerminalPort().job_terminalized(
                session,
                JobTerminalEvent(
                    job_id=admitted.job.job_id,
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    run_id=admitted.run.run_id,
                    occurrence_id=admitted.occurrence.id,
                    job_type="automation_run",
                    status="dead",
                    retry_safety="safe",
                    public_error_code="WORKER_ATTEMPTS_EXHAUSTED",
                    cancel_reason=None,
                    occurred_at=occurred_at,
                ),
            )

        async with seed.factory() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT o.status,r.status,t.status,t.next_run_at,
                                  t.run_count,t.last_outcome,t.last_error_code
                           FROM scheduled_task_runs o
                           JOIN runs r ON r.run_id=o.run_id
                            AND r.project_id=o.project_id
                            AND r.owner_user_id=o.owner_user_id
                           JOIN scheduled_tasks t ON t.id=o.task_id
                            AND t.project_id=o.project_id
                            AND t.owner_user_id=o.owner_user_id
                           WHERE o.id=:occurrence_id"""
                    ),
                    {"occurrence_id": admitted.occurrence.id},
                )
            ).one()
        assert tuple(row) == (
            "failed",
            "error",
            "failed",
            None,
            1,
            "failed",
            "WORKER_ATTEMPTS_EXHAUSTED",
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_gateway_cancel_queued_automation_terminalizes_occurrence_once(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        task = await _create_due_task(
            seed,
            task_id=f"m6-gateway-cancel-{uuid.uuid4().hex[:16]}",
        )
        admitted = await AutomationDispatcher(seed.factory).admit_occurrence(
            _definition(seed, task),
            scheduled_for=NOW,
        )
        service = PrivateRunService(seed.factory)

        await service.cancel(
            seed.owner_a,
            admitted.run.thread_id,
            admitted.run.run_id,
        )
        await service.cancel(
            seed.owner_a,
            admitted.run.thread_id,
            admitted.run.run_id,
        )

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT o.status,o.error_code,r.status,j.status,
                                  t.run_count,t.last_outcome,t.last_error_code
                           FROM scheduled_task_runs o
                           JOIN runs r ON r.run_id=o.run_id
                            AND r.project_id=o.project_id
                            AND r.owner_user_id=o.owner_user_id
                           JOIN jobs j ON j.id=o.job_id
                           JOIN scheduled_tasks t ON t.id=o.task_id
                            AND t.project_id=o.project_id
                            AND t.owner_user_id=o.owner_user_id
                           WHERE o.id=:occurrence_id"""
                    ),
                    {"occurrence_id": admitted.occurrence.id},
                )
            ).one()
        assert tuple(state) == (
            "interrupted",
            "AUTOMATION_RUN_INTERRUPTED",
            "interrupted",
            "cancelled",
            1,
            "interrupted",
            "AUTOMATION_RUN_INTERRUPTED",
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_automation_terminal_port_never_waits_on_locked_parent_definition(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    occurred_at = NOW.replace(minute=9)
    try:
        task = await _create_due_task(
            seed,
            task_id=f"m6-terminal-lock-{uuid.uuid4().hex[:16]}",
        )
        admitted = await AutomationDispatcher(
            seed.factory,
            clock=lambda: NOW,
        ).admit_occurrence(
            _definition(seed, task),
            scheduled_for=NOW,
        )
        terminal_port = PrivateRunJobTerminalPort()

        async with seed.factory() as locking_session, locking_session.begin():
            await locking_session.execute(
                text("SELECT id FROM scheduled_tasks WHERE id=:task_id FOR UPDATE"),
                {"task_id": task.id},
            )

            async def terminalize() -> None:
                async with seed.factory() as session, session.begin():
                    await terminal_port.job_terminalized(
                        session,
                        JobTerminalEvent(
                            job_id=admitted.job.job_id,
                            project_id=seed.owner_a.project_id,
                            owner_user_id=str(seed.owner_a.user_id),
                            run_id=admitted.run.run_id,
                            occurrence_id=admitted.occurrence.id,
                            job_type="automation_run",
                            status="dead",
                            retry_safety="safe",
                            public_error_code="WORKER_ATTEMPTS_EXHAUSTED",
                            cancel_reason=None,
                            occurred_at=occurred_at,
                        ),
                    )

            await asyncio.wait_for(terminalize(), timeout=2)

        assert terminal_port.take_automation_reconciliation_pending() is True

        async with seed.factory() as session:
            before_reconcile = (
                await session.execute(
                    text(
                        """SELECT o.status,r.status,t.run_count
                           FROM scheduled_task_runs o
                           JOIN runs r ON r.run_id=o.run_id
                            AND r.project_id=o.project_id
                            AND r.owner_user_id=o.owner_user_id
                           JOIN scheduled_tasks t ON t.id=o.task_id
                            AND t.project_id=o.project_id
                            AND t.owner_user_id=o.owner_user_id
                           WHERE o.id=:occurrence_id"""
                    ),
                    {"occurrence_id": admitted.occurrence.id},
                )
            ).one()
        assert tuple(before_reconcile) == ("running", "error", 0)

        await AutomationReconciler(
            seed.factory,
            clock=lambda: occurred_at,
        ).handle_run_completion(
            SimpleNamespace(run_id=admitted.run.run_id),
        )

        async with seed.factory() as session:
            after_reconcile = (
                await session.execute(
                    text(
                        """SELECT o.status,t.run_count,t.last_outcome
                           FROM scheduled_task_runs o
                           JOIN scheduled_tasks t ON t.id=o.task_id
                            AND t.project_id=o.project_id
                            AND t.owner_user_id=o.owner_user_id
                           WHERE o.id=:occurrence_id"""
                    ),
                    {"occurrence_id": admitted.occurrence.id},
                )
            ).one()
        assert tuple(after_reconcile) == ("failed", 1, "failed")
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_automation_job_executes_in_worker_and_reconciles_occurrence(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)

    class Executor:
        async def execute(self, execution, _authority):
            assert execution.run.metadata["scheduled_trigger"] == "scheduled"
            return AgentExecutionResult.succeeded()

    try:
        task = await _create_due_task(
            seed,
            task_id=f"m6-worker-auto-{uuid.uuid4().hex[:16]}",
        )
        admitted = await AutomationDispatcher(seed.factory).admit_occurrence(
            _definition(seed, task),
            scheduled_for=NOW,
        )
        worker_id = uuid.uuid4()
        await WorkerRegistry(
            seed.factory,
            version="test-m6-automation",
        ).register(worker_id, frozenset({"automation_run"}), 1)
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"automation_run"}),
                lease_seconds=90,
            )
            assert claim is not None
            assert claim.job_id == admitted.job.job_id
            assert claim.occurrence_id == admitted.occurrence.id
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )

        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT o.status,r.status,j.status,t.run_count,t.last_outcome
                           FROM scheduled_task_runs o
                           JOIN runs r ON r.run_id=o.run_id
                            AND r.project_id=o.project_id
                            AND r.owner_user_id=o.owner_user_id
                           JOIN jobs j ON j.id=o.job_id
                           JOIN scheduled_tasks t ON t.id=o.task_id
                            AND t.project_id=o.project_id
                            AND t.owner_user_id=o.owner_user_id
                           WHERE o.id=:occurrence_id"""
                    ),
                    {"occurrence_id": admitted.occurrence.id},
                )
            ).one()
        assert tuple(state) == (
            "success",
            "success",
            "succeeded",
            1,
            "success",
        )
    finally:
        await seed.engine.dispose()
