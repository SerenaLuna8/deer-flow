from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.automations.occurrences import deterministic_run_id, deterministic_thread_id
from app.automations.reconciliation import AutomationReconciler
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.persistence.run import RunRepository
from deerflow.persistence.scheduled_task_runs import (
    ScheduledTaskRunCreate,
    ScheduledTaskRunRepository,
)
from deerflow.persistence.scheduled_tasks import ScheduledTaskCreate, ScheduledTaskRepository
from deerflow.runtime import DisconnectMode, RunManager, RunRecord, RunStatus
from deerflow.runtime.runs.worker import RunContext, run_agent

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Scenario:
    task_id: str
    occurrence_id: str
    thread_id: str
    run_id: str


@pytest_asyncio.fixture()
async def reconciliation_seed(migrated_postgres_database_url: str):
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield seed
    finally:
        await seed.engine.dispose()


async def _create_scenario(
    seed: M4ThreadSeed,
    *,
    context=None,
    schedule_type: str = "cron",
    occurrence_status: str = "running",
    run_status: str | None = "pending",
    attach_run: bool = True,
) -> Scenario:
    context = context or seed.owner_a
    occurrence_id = str(uuid.uuid4())
    task_id = f"task-{uuid.uuid4().hex[:20]}"
    thread_id = deterministic_thread_id(occurrence_id)
    run_id = deterministic_run_id(occurrence_id)
    scope = context.resource_scope
    async with seed.factory() as session, session.begin():
        task = await ScheduledTaskRepository(session).create(
            scope,
            ScheduledTaskCreate(
                task_id=task_id,
                thread_id=None,
                context_mode="fresh_thread_per_run",
                agent_asset_id=seed.system_agent_id,
                agent_scope="system",
                title="Private automation",
                prompt="Process private project work.",
                schedule_type=schedule_type,
                schedule_spec=({"cron": "0 * * * *"} if schedule_type == "cron" else {"run_at": NOW.isoformat()}),
                timezone="UTC",
                next_run_at=NOW,
            ),
        )
        occurrences = ScheduledTaskRunRepository(session)
        await occurrences.create(
            scope,
            ScheduledTaskRunCreate(
                occurrence_id=occurrence_id,
                task_id=task.id,
                task_version=task.version,
                occurrence_key=hashlib.sha256(occurrence_id.encode()).hexdigest(),
                manual_idempotency_hash=None,
                scheduled_for=NOW,
                trigger="scheduled",
                status="queued",
                created_at=NOW,
            ),
        )
        if occurrence_status != "queued":
            claimed = await occurrences.claim(
                scope,
                occurrence_id,
                now=NOW,
                lease_owner="seed",
                lease_expires_at=NOW - timedelta(seconds=1),
            )
            assert claimed is not None
        if run_status is not None:
            await PrivateThreadRepository(session).create(
                scope=scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.system_agent_id, "system"),
                metadata={"scheduled_task_run_id": occurrence_id},
            )
            await PrivateRunRepository(session).create(
                scope=scope,
                thread_id=thread_id,
                request=PrivateRunCreate(
                    run_id=run_id,
                    status=run_status,
                    metadata={
                        "scheduled_task_id": task_id,
                        "scheduled_task_run_id": occurrence_id,
                        "scheduled_trigger": "scheduled",
                    },
                ),
            )
        if occurrence_status == "running":
            if attach_run:
                running = await occurrences.mark_running(
                    scope,
                    occurrence_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    started_at=NOW,
                    updated_at=NOW,
                )
                assert running is not None
            else:
                await session.execute(
                    text("UPDATE scheduled_task_runs SET status='running', lease_owner=NULL, lease_expires_at=NULL WHERE id=:id AND project_id=:project_id AND owner_user_id=:owner"),
                    {
                        "id": occurrence_id,
                        "project_id": uuid.UUID(scope.project_id),
                        "owner": scope.owner_user_id,
                    },
                )
    return Scenario(task_id, occurrence_id, thread_id, run_id)


def _callback(scenario: Scenario, *, status: RunStatus, scope, metadata) -> RunRecord:
    return RunRecord(
        run_id=scenario.run_id,
        thread_id=scenario.thread_id,
        assistant_id="lead_agent",
        status=status,
        on_disconnect=DisconnectMode.continue_,
        metadata=metadata,
        scope=scope,
        error="forged callback error",
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_completion_uses_persisted_run_scope_and_terminal_cas(
    reconciliation_seed: M4ThreadSeed,
) -> None:
    seed = reconciliation_seed
    target = await _create_scenario(seed, schedule_type="once")
    decoy = await _create_scenario(seed, context=seed.owner_b, schedule_type="once")
    async with seed.factory() as session, session.begin():
        assert await PrivateRunRepository(session).update_status(
            scope=seed.owner_a.resource_scope,
            run_id=target.run_id,
            status="success",
        )

    reconciler = AutomationReconciler(seed.factory, clock=lambda: NOW)
    forged = _callback(
        target,
        status=RunStatus.error,
        scope=seed.owner_b.resource_scope,
        metadata={
            "scheduled_task_id": decoy.task_id,
            "scheduled_task_run_id": decoy.occurrence_id,
            "scheduled_trigger": "scheduled",
        },
    )
    await reconciler.handle_run_completion(forged)
    await reconciler.handle_run_completion(forged)

    async with seed.factory() as session:
        target_run = await ScheduledTaskRunRepository(session).get(seed.owner_a.resource_scope, target.occurrence_id)
        target_task = await ScheduledTaskRepository(session).get(seed.owner_a.resource_scope, target.task_id)
        decoy_run = await ScheduledTaskRunRepository(session).get(seed.owner_b.resource_scope, decoy.occurrence_id)
        decoy_task = await ScheduledTaskRepository(session).get(seed.owner_b.resource_scope, decoy.task_id)

    assert target_run is not None and target_run.status == "success"
    assert target_task is not None
    assert target_task.status == "completed"
    assert target_task.run_count == 1
    assert decoy_run is not None and decoy_run.status == "running"
    assert decoy_task is not None and decoy_task.run_count == 0


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_status", "occurrence_status", "task_status"),
    [
        ("error", "failed", "failed"),
        ("timeout", "failed", "failed"),
        ("interrupted", "interrupted", "cancelled"),
    ],
)
async def test_completion_maps_terminal_run_outcomes_for_once_tasks(
    reconciliation_seed: M4ThreadSeed,
    run_status: str,
    occurrence_status: str,
    task_status: str,
) -> None:
    seed = reconciliation_seed
    scenario = await _create_scenario(seed, schedule_type="once")
    async with seed.factory() as session, session.begin():
        assert await PrivateRunRepository(session).update_status(
            scope=seed.owner_a.resource_scope,
            run_id=scenario.run_id,
            status=run_status,
            error="safe persisted error",
        )
    await AutomationReconciler(seed.factory, clock=lambda: NOW).handle_run_completion(
        _callback(
            scenario,
            status=RunStatus.success,
            scope=seed.owner_a.resource_scope,
            metadata={},
        )
    )
    async with seed.factory() as session:
        occurrence = await ScheduledTaskRunRepository(session).get(seed.owner_a.resource_scope, scenario.occurrence_id)
        task = await ScheduledTaskRepository(session).get(seed.owner_a.resource_scope, scenario.task_id)
    assert occurrence is not None and occurrence.status == occurrence_status
    assert task is not None and task.status == task_status and task.run_count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_fast_completion_backfills_missing_occurrence_run_links(
    reconciliation_seed: M4ThreadSeed,
) -> None:
    seed = reconciliation_seed
    scenario = await _create_scenario(
        seed,
        occurrence_status="launching",
        run_status="success",
        attach_run=False,
    )
    await AutomationReconciler(seed.factory, clock=lambda: NOW).handle_run_completion(
        _callback(
            scenario,
            status=RunStatus.success,
            scope=seed.owner_a.resource_scope,
            metadata={},
        )
    )
    async with seed.factory() as session:
        occurrence = await ScheduledTaskRunRepository(session).get(seed.owner_a.resource_scope, scenario.occurrence_id)
    assert occurrence is not None
    assert occurrence.status == "success"
    assert occurrence.thread_id == scenario.thread_id
    assert occurrence.run_id == scenario.run_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cron_completion_keeps_definition_enabled(
    reconciliation_seed: M4ThreadSeed,
) -> None:
    seed = reconciliation_seed
    scenario = await _create_scenario(seed, schedule_type="cron")
    async with seed.factory() as session, session.begin():
        assert await PrivateRunRepository(session).update_status(
            scope=seed.owner_a.resource_scope,
            run_id=scenario.run_id,
            status="success",
        )
    await AutomationReconciler(seed.factory, clock=lambda: NOW).handle_run_completion(
        _callback(
            scenario,
            status=RunStatus.success,
            scope=seed.owner_a.resource_scope,
            metadata={},
        )
    )
    async with seed.factory() as session:
        task = await ScheduledTaskRepository(session).get(seed.owner_a.resource_scope, scenario.task_id)
    assert task is not None
    assert task.status == "enabled"
    assert task.run_count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_restart_copies_terminal_private_run_outcome(
    reconciliation_seed: M4ThreadSeed,
) -> None:
    seed = reconciliation_seed
    scenario = await _create_scenario(
        seed,
        occurrence_status="launching",
        run_status="success",
        attach_run=False,
    )

    report = await AutomationReconciler(seed.factory).reconcile_restart(NOW)

    async with seed.factory() as session:
        occurrence = await ScheduledTaskRunRepository(session).get(seed.owner_a.resource_scope, scenario.occurrence_id)
    assert report.succeeded == 1
    assert occurrence is not None
    assert occurrence.status == "success"
    assert occurrence.run_id == scenario.run_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_restart_reconciliation_recovers_lease_and_never_replays_admitted_run(
    reconciliation_seed: M4ThreadSeed,
) -> None:
    seed = reconciliation_seed
    expired = await _create_scenario(seed, occurrence_status="launching", run_status=None)
    admitted = await _create_scenario(
        seed,
        occurrence_status="launching",
        run_status="pending",
        attach_run=False,
    )
    missing = await _create_scenario(seed, occurrence_status="running", run_status=None, attach_run=False)

    report = await AutomationReconciler(seed.factory, clock=lambda: NOW).reconcile_restart(NOW)

    async with seed.factory() as session:
        expired_row = await ScheduledTaskRunRepository(session).get(seed.owner_a.resource_scope, expired.occurrence_id)
        admitted_row = await ScheduledTaskRunRepository(session).get(seed.owner_a.resource_scope, admitted.occurrence_id)
        admitted_run = await PrivateRunRepository(session).get(scope=seed.owner_a.resource_scope, run_id=admitted.run_id)
        missing_row = await ScheduledTaskRunRepository(session).get(seed.owner_a.resource_scope, missing.occurrence_id)

    assert report.requeued == 1
    assert report.interrupted == 1
    assert report.failed == 1
    assert expired_row is not None
    assert expired_row.status == "queued"
    assert expired_row.next_attempt_at == NOW
    assert admitted_row is not None
    assert admitted_row.status == "interrupted"
    assert admitted_row.run_id == admitted.run_id
    assert admitted_run is not None and admitted_run.status == "interrupted"
    assert missing_row is not None
    assert missing_row.status == "failed"
    assert missing_row.error_code == "AUTOMATION_RUN_MISSING"

    again = await AutomationReconciler(seed.factory).reconcile_restart(NOW)
    assert again.requeued == again.interrupted == again.failed == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_cleanup_exhaustion_settles_automation_from_final_run_error(
    reconciliation_seed: M4ThreadSeed,
) -> None:
    seed = reconciliation_seed
    scenario = await _create_scenario(seed, schedule_type="once")
    reconciler = AutomationReconciler(seed.factory, clock=lambda: NOW)
    manager = RunManager(store=RunRepository(seed.factory))
    record = await manager.register_persisted(
        run_id=scenario.run_id,
        thread_id=scenario.thread_id,
        assistant_id=None,
        metadata={
            "scheduled_task_id": scenario.task_id,
            "scheduled_task_run_id": scenario.occurrence_id,
            "scheduled_trigger": "scheduled",
        },
        scope=seed.owner_a.resource_scope,
    )
    authority = SimpleNamespace(
        restore=AsyncMock(),
        finalize=AsyncMock(),
        mark_failed=AsyncMock(),
        release=AsyncMock(side_effect=RuntimeError("persistent cleanup failure")),
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield {"messages": []}

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            file_authority=authority,
            on_run_completed=reconciler.handle_run_completion,
        ),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={},
        config={},
    )

    async with seed.factory() as session:
        private_run = await PrivateRunRepository(session).get(
            scope=seed.owner_a.resource_scope,
            run_id=scenario.run_id,
        )
        occurrence = await ScheduledTaskRunRepository(session).get(
            seed.owner_a.resource_scope,
            scenario.occurrence_id,
        )
        task = await ScheduledTaskRepository(session).get(
            seed.owner_a.resource_scope,
            scenario.task_id,
        )

    assert authority.release.await_count == 3
    assert private_run is not None and private_run.status == "error"
    assert occurrence is not None and occurrence.status == "failed"
    assert occurrence.error_code == "AUTOMATION_RUN_FAILED"
    assert task is not None and task.status == "failed"
    assert task.last_outcome == "failed"
    assert task.run_count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_normal_success_calls_completion_once_after_cleanup(
    reconciliation_seed: M4ThreadSeed,
) -> None:
    seed = reconciliation_seed
    scenario = await _create_scenario(seed, schedule_type="once")
    reconciler = AutomationReconciler(seed.factory, clock=lambda: NOW)
    completion_hook = AsyncMock(side_effect=reconciler.handle_run_completion)
    manager = RunManager(store=RunRepository(seed.factory))
    record = await manager.register_persisted(
        run_id=scenario.run_id,
        thread_id=scenario.thread_id,
        assistant_id=None,
        metadata={
            "scheduled_task_id": scenario.task_id,
            "scheduled_task_run_id": scenario.occurrence_id,
            "scheduled_trigger": "scheduled",
        },
        scope=seed.owner_a.resource_scope,
    )
    authority = SimpleNamespace(
        restore=AsyncMock(),
        finalize=AsyncMock(),
        mark_failed=AsyncMock(),
        release=AsyncMock(),
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield {"messages": []}

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            file_authority=authority,
            on_run_completed=completion_hook,
        ),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={},
        config={},
    )

    async with seed.factory() as session:
        occurrence = await ScheduledTaskRunRepository(session).get(
            seed.owner_a.resource_scope,
            scenario.occurrence_id,
        )
        task = await ScheduledTaskRepository(session).get(
            seed.owner_a.resource_scope,
            scenario.task_id,
        )

    completion_hook.assert_awaited_once_with(record)
    authority.release.assert_awaited_once_with()
    assert record.status is RunStatus.success
    assert occurrence is not None and occurrence.status == "success"
    assert task is not None and task.status == "completed"
    assert task.run_count == 1
