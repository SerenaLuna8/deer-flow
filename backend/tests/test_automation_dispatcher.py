from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select, text
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

import app.automations.dispatcher as dispatcher_module
from app.automations.dispatcher import AutomationDispatcher, AutomationDispatchResult
from app.automations.errors import (
    AutomationConflict,
    AutomationForbidden,
    AutomationNotFound,
    AutomationUnavailable,
    AutomationVersionConflict,
)
from app.automations.occurrences import (
    AutomationOccurrenceService,
    deterministic_run_id,
    deterministic_thread_id,
)
from app.automations.reconciliation import AutomationReconciler
from app.private_work.authorization import PrivateRunAuthorizationService
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import is_issued_private_work_context
from app.private_work.errors import PrivateWorkUnavailable
from app.private_work.retention import PrivateWorkRetentionService
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.private_work.thread_service import PrivateThreadService
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.membership_repository import MembershipRepository
from app.projects.membership_service import MembershipService
from app.projects.models import ProjectRole
from deerflow.persistence.private_work.model import RunAssetVersionRow
from deerflow.persistence.projects.model import ProjectMembershipRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.scheduled_task_runs import (
    ScheduledTaskRunCreate,
    ScheduledTaskRunRecord,
    ScheduledTaskRunRepository,
)
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskCreate,
    ScheduledTaskRecord,
    ScheduledTaskRepository,
)
from deerflow.runtime import DisconnectMode, RunRecord, RunStatus

NOW = datetime(2026, 7, 16, 11, 30, tzinfo=UTC)


class _AsyncNullContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class _UnitSession:
    def begin(self):
        return _AsyncNullContext()


class _UnitSessionFactory:
    def __init__(self):
        self.session = _UnitSession()

    def __call__(self):
        return _AsyncNullContext(self.session)


@dataclass(frozen=True)
class DispatchSeed:
    database: M4ThreadSeed
    thread_service: PrivateThreadService

    @property
    def factory(self):
        return self.database.factory

    @property
    def context(self):
        return self.database.owner_a

    async def claimed_occurrence(
        self,
        *,
        context=None,
        context_mode: str = "fresh_thread_per_run",
        thread_id: str | None = None,
        trigger: str = "scheduled",
        status: str = "enabled",
        agent_asset_id: uuid.UUID | None = None,
        agent_scope: str = "system",
        schedule_type: str = "cron",
        next_run_at: datetime | None = NOW,
    ) -> tuple[ScheduledTaskRecord, ScheduledTaskRunRecord]:
        context = context or self.context
        task_id = f"task-{uuid.uuid4().hex[:20]}"
        occurrence_id = str(uuid.uuid4())
        async with self.factory() as session, session.begin():
            tasks = ScheduledTaskRepository(session)
            task = await tasks.create(
                context.resource_scope,
                ScheduledTaskCreate(
                    task_id=task_id,
                    thread_id=thread_id,
                    context_mode=context_mode,
                    agent_asset_id=agent_asset_id or self.database.system_agent_id,
                    agent_scope=agent_scope,
                    title="Private automation",
                    prompt="Process private project work.",
                    schedule_type=schedule_type,
                    schedule_spec=({"cron": "0 * * * *"} if schedule_type == "cron" else {"run_at": NOW.isoformat()}),
                    timezone="UTC",
                    next_run_at=next_run_at,
                ),
            )
            if status != "enabled":
                updated = await tasks.update(
                    context.resource_scope,
                    task.id,
                    expected_version=task.version,
                    values={"status": status, "next_run_at": None},
                )
                assert updated is not None
                task = updated
            occurrences = ScheduledTaskRunRepository(session)
            await occurrences.create(
                context.resource_scope,
                ScheduledTaskRunCreate(
                    occurrence_id=occurrence_id,
                    task_id=task.id,
                    task_version=task.version,
                    occurrence_key=hashlib.sha256(occurrence_id.encode("ascii")).hexdigest(),
                    manual_idempotency_hash=(hashlib.sha256(occurrence_id.encode("ascii")).hexdigest() if trigger == "manual" else None),
                    scheduled_for=NOW,
                    trigger=trigger,
                    status="queued",
                    created_at=NOW,
                ),
            )
            claimed = await occurrences.claim(
                context.resource_scope,
                occurrence_id,
                now=NOW,
                lease_owner="dispatcher-test",
                lease_expires_at=NOW + timedelta(minutes=1),
            )
        assert claimed is not None
        assert claimed.thread_id is None
        assert claimed.run_id is None
        return task, claimed

    async def persisted_occurrence(self, occurrence_id: str) -> ScheduledTaskRunRecord:
        async with self.factory() as session:
            record = await ScheduledTaskRunRepository(session).get(
                self.context.resource_scope,
                occurrence_id,
            )
        assert record is not None
        return record

    async def persisted_task(self, task_id: str) -> ScheduledTaskRecord:
        async with self.factory() as session:
            record = await ScheduledTaskRepository(session).get(
                self.context.resource_scope,
                task_id,
            )
        assert record is not None
        return record


@pytest_asyncio.fixture()
async def dispatch_seed(migrated_postgres_database_url: str) -> DispatchSeed:
    database = await seed_m4_thread_database(migrated_postgres_database_url)
    scoped = ProjectScopedCheckpointer(InMemorySaver(), database.factory)
    try:
        yield DispatchSeed(
            database=database,
            thread_service=PrivateThreadService(database.factory, scoped),
        )
    finally:
        await database.engine.dispose()


def _runtime_record(admitted) -> RunRecord:
    return RunRecord(
        run_id=admitted.run.run_id,
        thread_id=admitted.run.thread_id,
        assistant_id=admitted.run.assistant_id,
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.continue_,
        metadata=admitted.run.metadata,
        kwargs=admitted.run.kwargs,
        model_name=admitted.run.model_name,
        created_at=admitted.run.created_at.isoformat(),
        updated_at=admitted.run.updated_at.isoformat(),
        scope=admitted.opaque_runtime_scope,
    )


def _real_admission_launcher(seed: DispatchSeed, calls: list[dict[str, object]]):
    async def launch_private_run(**kwargs):
        calls.append(kwargs)
        assert is_issued_private_work_context(kwargs["context"])
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            kwargs["context"],
            kwargs["thread_id"],
            PrivateRunCreate(
                run_id=kwargs["run_id"],
                metadata=dict(kwargs["metadata"]),
                kwargs={
                    "input": {"messages": [{"role": "user", "content": kwargs["prompt"]}]},
                    "config": {"context": {"non_interactive": True}},
                },
            ),
        )
        return _runtime_record(admitted)

    return launch_private_run


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_dispatcher_launches_through_real_private_admission_and_backfills_immediate_fks(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence()
    calls: list[dict[str, object]] = []
    app = SimpleNamespace(state=SimpleNamespace())
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=_real_admission_launcher(seed, calls),
        clock=lambda: NOW,
    )

    result = await dispatcher.dispatch(occurrence.id, app=app)

    expected_thread_id = deterministic_thread_id(occurrence.id)
    expected_run_id = deterministic_run_id(occurrence.id)
    assert result.thread_id == expected_thread_id
    assert result.run_id == expected_run_id
    assert len(calls) == 1
    assert calls[0]["app"] is app
    assert calls[0]["run_id"] == expected_run_id
    assert calls[0]["prompt"] == task.prompt
    assert calls[0]["metadata"] == {
        "scheduled_task_id": task.id,
        "scheduled_task_run_id": occurrence.id,
        "scheduled_trigger": "scheduled",
    }

    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "running"
    assert persisted.thread_id == expected_thread_id
    assert persisted.run_id == expected_run_id
    assert persisted.resolved_membership_id == seed.context.membership_id
    assert persisted.resolved_membership_version == seed.context.membership_version

    async with seed.factory() as session:
        thread = await PrivateThreadRepository(session).get(
            scope=seed.context.resource_scope,
            thread_id=expected_thread_id,
        )
        run = await PrivateRunRepository(session).get(
            scope=seed.context.resource_scope,
            run_id=expected_run_id,
        )
        snapshot_count = await session.scalar(select(func.count()).select_from(RunAssetVersionRow).where(RunAssetVersionRow.run_id == expected_run_id))
    assert thread is not None
    assert thread.agent_asset_id == task.agent_asset_id
    assert thread.agent_scope == task.agent_scope
    assert thread.metadata["scheduled_task_run_id"] == occurrence.id
    assert run is not None
    assert run.thread_id == expected_thread_id
    assert run.metadata == calls[0]["metadata"]
    assert int(snapshot_count or 0) >= 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_dispatchers_converge_after_deterministic_admission_conflict(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence()
    barrier = asyncio.Barrier(2)
    launch_calls: list[dict[str, object]] = []

    async def launch_private_run(**kwargs):
        launch_calls.append(kwargs)
        await asyncio.wait_for(barrier.wait(), timeout=5)
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            kwargs["context"],
            kwargs["thread_id"],
            PrivateRunCreate(
                run_id=kwargs["run_id"],
                metadata=dict(kwargs["metadata"]),
                kwargs={
                    "input": {
                        "messages": [
                            {
                                "role": "user",
                                "content": kwargs["prompt"],
                            }
                        ]
                    },
                    "config": {"context": {"non_interactive": True}},
                },
            ),
        )
        return _runtime_record(admitted)

    dispatchers = tuple(
        AutomationDispatcher(
            seed.factory,
            thread_service=seed.thread_service,
            launch_private_run=launch_private_run,
            clock=lambda: NOW,
        )
        for _ in range(2)
    )

    outcomes = await asyncio.wait_for(
        asyncio.gather(
            *(dispatcher.dispatch(occurrence.id, app=SimpleNamespace()) for dispatcher in dispatchers),
            return_exceptions=True,
        ),
        timeout=10,
    )

    expected = AutomationDispatchResult(
        occurrence_id=occurrence.id,
        thread_id=deterministic_thread_id(occurrence.id),
        run_id=deterministic_run_id(occurrence.id),
    )
    assert (
        launch_calls[0]["metadata"]
        == launch_calls[1]["metadata"]
        == {
            "scheduled_task_id": task.id,
            "scheduled_task_run_id": occurrence.id,
            "scheduled_trigger": occurrence.trigger,
        }
    )
    assert outcomes == [expected, expected]

    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "running"
    assert persisted.thread_id == expected.thread_id
    assert persisted.run_id == expected.run_id
    async with seed.factory() as session:
        run_count = await session.scalar(
            text(
                """SELECT count(*) FROM runs
                WHERE project_id=:project_id
                  AND owner_user_id=:owner_user_id
                  AND run_id=:run_id"""
            ),
            {
                "project_id": seed.context.project_id,
                "owner_user_id": seed.context.resource_scope.owner_user_id,
                "run_id": expected.run_id,
            },
        )
        run = await PrivateRunRepository(session).get(
            scope=seed.context.resource_scope,
            run_id=expected.run_id,
        )
    assert run_count == 1
    assert run is not None
    assert run.thread_id == expected.thread_id
    assert run.metadata == launch_calls[0]["metadata"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_dispatcher_retry_adopts_matching_existing_private_run_without_relaunch(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence()
    thread_id = deterministic_thread_id(occurrence.id)
    run_id = deterministic_run_id(occurrence.id)
    await seed.thread_service.create(
        seed.context,
        thread_id=thread_id,
        agent=ThreadAgentRef(task.agent_asset_id, task.agent_scope),
        metadata={
            "scheduled_task_id": task.id,
            "scheduled_task_run_id": occurrence.id,
            "scheduled_trigger": occurrence.trigger,
        },
    )
    metadata = {
        "scheduled_task_id": task.id,
        "scheduled_task_run_id": occurrence.id,
        "scheduled_trigger": occurrence.trigger,
    }
    admitted = await PrivateRunAdmissionService(seed.factory).admit(
        seed.context,
        thread_id,
        PrivateRunCreate(run_id=run_id, metadata=metadata),
    )
    launcher = AsyncMock()
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=launcher,
        clock=lambda: NOW,
    )

    result = await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())

    assert result.thread_id == admitted.run.thread_id
    assert result.run_id == admitted.run.run_id
    launcher.assert_not_awaited()
    assert (await seed.persisted_occurrence(occurrence.id)).status == "running"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_dispatcher_settles_matching_terminal_run_from_durable_outcome(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence()
    thread_id = deterministic_thread_id(occurrence.id)
    run_id = deterministic_run_id(occurrence.id)
    metadata = {
        "scheduled_task_id": task.id,
        "scheduled_task_run_id": occurrence.id,
        "scheduled_trigger": occurrence.trigger,
    }
    await seed.thread_service.create(
        seed.context,
        thread_id=thread_id,
        agent=ThreadAgentRef(task.agent_asset_id, task.agent_scope),
        metadata=metadata,
    )
    await PrivateRunAdmissionService(seed.factory).admit(
        seed.context,
        thread_id,
        PrivateRunCreate(run_id=run_id, metadata=metadata),
    )
    async with seed.factory() as session, session.begin():
        updated = await PrivateRunRepository(session).update_status(
            scope=seed.context.resource_scope,
            run_id=run_id,
            status="error",
            error="terminal runtime failure",
        )
    assert updated is True
    launcher = AsyncMock()
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=launcher,
        clock=lambda: NOW,
    )

    with pytest.raises(AutomationConflict):
        await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())

    launcher.assert_not_awaited()
    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "failed"
    assert persisted.error_code == "AUTOMATION_RUN_FAILED"
    assert persisted.error_message == "The automation run failed."
    assert persisted.thread_id == thread_id
    assert persisted.run_id == run_id
    parent = await seed.persisted_task(task.id)
    assert parent.last_outcome == "failed"
    assert parent.last_error_code == "AUTOMATION_RUN_FAILED"
    assert parent.run_count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch_path", ["normal_launch", "adoption"])
async def test_locked_run_status_gate_settles_pending_to_error_race(
    dispatch_seed: DispatchSeed,
    monkeypatch: pytest.MonkeyPatch,
    dispatch_path: str,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence()
    thread_id = deterministic_thread_id(occurrence.id)
    run_id = deterministic_run_id(occurrence.id)
    metadata = {
        "scheduled_task_id": task.id,
        "scheduled_task_run_id": occurrence.id,
        "scheduled_trigger": occurrence.trigger,
    }
    launch_calls: list[dict[str, object]] = []
    if dispatch_path == "adoption":
        await seed.thread_service.create(
            seed.context,
            thread_id=thread_id,
            agent=ThreadAgentRef(task.agent_asset_id, task.agent_scope),
            metadata=metadata,
        )
        await PrivateRunAdmissionService(seed.factory).admit(
            seed.context,
            thread_id,
            PrivateRunCreate(run_id=run_id, metadata=metadata),
        )
        launcher = AsyncMock()
    else:
        launcher = _real_admission_launcher(seed, launch_calls)

    barrier = asyncio.Barrier(2)
    status_committed = asyncio.Event()
    locked_read_consumed = False

    class BarrierPrivateRunRepository(PrivateRunRepository):
        async def get(self, *, scope, run_id, lock=False):
            nonlocal locked_read_consumed
            if lock and not locked_read_consumed:
                locked_read_consumed = True
                await asyncio.wait_for(barrier.wait(), timeout=5)
                await asyncio.wait_for(status_committed.wait(), timeout=5)
            return await super().get(scope=scope, run_id=run_id, lock=lock)

    monkeypatch.setattr(
        "app.automations.dispatcher.PrivateRunRepository",
        BarrierPrivateRunRepository,
    )

    async def terminalize_before_locked_read() -> None:
        await asyncio.wait_for(barrier.wait(), timeout=5)
        try:
            async with seed.factory() as session, session.begin():
                updated = await PrivateRunRepository(session).update_status(
                    scope=seed.context.resource_scope,
                    run_id=run_id,
                    status="error",
                    error="failed before locked status gate",
                )
            assert updated is True
        finally:
            status_committed.set()

    updater = asyncio.create_task(terminalize_before_locked_read())
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=launcher,
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(AutomationConflict):
            await asyncio.wait_for(
                dispatcher.dispatch(occurrence.id, app=SimpleNamespace()),
                timeout=10,
            )
        await asyncio.wait_for(updater, timeout=5)
    finally:
        if not updater.done():
            updater.cancel()
            await asyncio.gather(updater, return_exceptions=True)

    if dispatch_path == "adoption":
        launcher.assert_not_awaited()
    else:
        assert len(launch_calls) == 1
    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "failed"
    assert persisted.error_code == "AUTOMATION_RUN_FAILED"
    assert persisted.error_message == "The automation run failed."
    assert persisted.thread_id == thread_id
    assert persisted.run_id == run_id
    async with seed.factory() as session:
        run = await PrivateRunRepository(session).get(
            scope=seed.context.resource_scope,
            run_id=run_id,
        )
    assert run is not None
    assert run.status == "error"
    assert run.thread_id == thread_id
    assert run.metadata == metadata
    parent = await seed.persisted_task(task.id)
    assert parent.last_outcome == "failed"
    assert parent.last_error_code == "AUTOMATION_RUN_FAILED"
    assert parent.run_count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_fresh_thread_adoption_rejects_mismatched_automation_metadata(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence()
    await seed.thread_service.create(
        seed.context,
        thread_id=deterministic_thread_id(occurrence.id),
        agent=ThreadAgentRef(task.agent_asset_id, task.agent_scope),
        metadata={
            "scheduled_task_id": task.id,
            "scheduled_task_run_id": "another-occurrence",
            "scheduled_trigger": occurrence.trigger,
        },
    )
    launcher = AsyncMock()
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=launcher,
        clock=lambda: NOW,
    )

    with pytest.raises(AutomationConflict):
        await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())

    launcher.assert_not_awaited()
    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "rejected"
    assert persisted.thread_id is None
    assert persisted.run_id is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_dispatch_rejects_viewer_downgrade_before_thread_create(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    _task, occurrence = await seed.claimed_occurrence()
    async with seed.factory() as session, session.begin():
        await session.execute(
            text("UPDATE project_memberships SET role='viewer', version=version+1 WHERE id=:membership_id"),
            {"membership_id": seed.context.membership_id},
        )
    thread_service = AsyncMock(wraps=seed.thread_service)
    launcher = AsyncMock()
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=thread_service,
        launch_private_run=launcher,
        clock=lambda: NOW,
    )

    with pytest.raises(AutomationForbidden):
        await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())

    thread_service.create.assert_not_awaited()
    launcher.assert_not_awaited()
    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "rejected"
    assert persisted.error_code == "AUTOMATION_FORBIDDEN"
    assert persisted.thread_id is None
    assert persisted.run_id is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_dispatch_rejects_frozen_definition_without_runtime_parents(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence()
    async with seed.factory() as session, session.begin():
        await session.execute(
            text("UPDATE scheduled_tasks SET frozen_at=:now WHERE id=:task_id"),
            {"now": NOW, "task_id": task.id},
        )
    launcher = AsyncMock()
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=launcher,
        clock=lambda: NOW,
    )

    with pytest.raises(AutomationNotFound):
        await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())

    launcher.assert_not_awaited()
    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "rejected"
    assert persisted.thread_id is None
    assert persisted.run_id is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_reuse_thread_revalidates_active_scope_and_agent(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    reuse_thread_id = str(uuid.uuid4())
    await seed.thread_service.create(
        seed.context,
        thread_id=reuse_thread_id,
        agent=ThreadAgentRef(seed.database.project_agent_id, "project"),
    )
    _task, occurrence = await seed.claimed_occurrence(
        context_mode="reuse_thread",
        thread_id=reuse_thread_id,
        agent_asset_id=seed.database.system_agent_id,
        agent_scope="system",
    )
    launcher = AsyncMock()
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=launcher,
        clock=lambda: NOW,
    )

    with pytest.raises(AutomationConflict):
        await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())

    launcher.assert_not_awaited()
    assert (await seed.persisted_occurrence(occurrence.id)).status == "rejected"


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["cron", "once"])
async def test_scheduled_reuse_overlap_skips_and_settles_parent_once(
    dispatch_seed: DispatchSeed,
    schedule_type: str,
) -> None:
    seed = dispatch_seed
    reuse_thread_id = f"reuse-overlap-{uuid.uuid4().hex}"
    await seed.thread_service.create(
        seed.context,
        thread_id=reuse_thread_id,
        agent=ThreadAgentRef(seed.database.system_agent_id, "system"),
    )
    next_run_at = NOW + timedelta(hours=1) if schedule_type == "cron" else None
    task, occurrence = await seed.claimed_occurrence(
        context_mode="reuse_thread",
        thread_id=reuse_thread_id,
        schedule_type=schedule_type,
        next_run_at=next_run_at,
    )
    unrelated_run_id = str(uuid.uuid4())
    await PrivateRunAdmissionService(seed.factory).admit(
        seed.context,
        reuse_thread_id,
        PrivateRunCreate(run_id=unrelated_run_id, metadata={"source": "unrelated"}),
    )
    launch_calls: list[dict[str, object]] = []
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=_real_admission_launcher(seed, launch_calls),
        clock=lambda: NOW,
    )

    for _ in range(2):
        with pytest.raises(AutomationConflict):
            await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())

    assert len(launch_calls) == 1
    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "skipped"
    assert persisted.error_code == "AUTOMATION_OVERLAP_SKIPPED"
    assert persisted.thread_id is None
    assert persisted.run_id is None
    parent = await seed.persisted_task(task.id)
    assert parent.status == ("enabled" if schedule_type == "cron" else "cancelled")
    assert parent.next_run_at == next_run_at
    assert parent.last_run_at == NOW
    assert parent.last_outcome == "skipped"
    assert parent.last_error_code == "AUTOMATION_OVERLAP_SKIPPED"
    assert parent.run_count == 1
    async with seed.factory() as session:
        deterministic = await PrivateRunRepository(session).get(
            scope=seed.context.resource_scope,
            run_id=deterministic_run_id(occurrence.id),
        )
    assert deterministic is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_manual_reuse_overlap_remains_rejected_and_settles_parent(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    reuse_thread_id = f"reuse-manual-overlap-{uuid.uuid4().hex}"
    await seed.thread_service.create(
        seed.context,
        thread_id=reuse_thread_id,
        agent=ThreadAgentRef(seed.database.system_agent_id, "system"),
    )
    task, occurrence = await seed.claimed_occurrence(
        context_mode="reuse_thread",
        thread_id=reuse_thread_id,
        trigger="manual",
        next_run_at=NOW + timedelta(hours=1),
    )
    await PrivateRunAdmissionService(seed.factory).admit(
        seed.context,
        reuse_thread_id,
        PrivateRunCreate(run_id=str(uuid.uuid4()), metadata={"source": "manual-unrelated"}),
    )
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=_real_admission_launcher(seed, []),
        clock=lambda: NOW,
    )

    with pytest.raises(AutomationConflict):
        await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())

    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "rejected"
    assert persisted.error_code == "AUTOMATION_CONFLICT"
    parent = await seed.persisted_task(task.id)
    assert parent.status == "enabled"
    assert parent.last_run_at == NOW
    assert parent.last_outcome == "rejected"
    assert parent.last_error_code == "AUTOMATION_CONFLICT"
    assert parent.run_count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_dispatch_rejects_definition_version_drift_with_version_conflict(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence()
    async with seed.factory() as session, session.begin():
        await session.execute(
            text("UPDATE scheduled_tasks SET version=version+1 WHERE id=:task_id"),
            {"task_id": task.id},
        )
    launcher = AsyncMock()
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=launcher,
        clock=lambda: NOW,
    )

    with pytest.raises(AutomationVersionConflict):
        await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())

    launcher.assert_not_awaited()
    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "rejected"
    assert persisted.error_code == "AUTOMATION_VERSION_CONFLICT"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pre_admission_unavailable_requeues_without_runtime_references(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    _task, occurrence = await seed.claimed_occurrence()
    launcher = AsyncMock(side_effect=PrivateWorkUnavailable("private-launch"))
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=launcher,
        clock=lambda: NOW,
        retry_delay=timedelta(seconds=30),
    )

    with pytest.raises(AutomationUnavailable):
        await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())

    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "queued"
    assert persisted.next_attempt_at == NOW + timedelta(seconds=30)
    assert persisted.thread_id is None
    assert persisted.run_id is None


@pytest.mark.asyncio
async def test_failure_settlement_does_not_requeue_frozen_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    owner_user_id = str(uuid.uuid4())
    occurrence_id = str(uuid.uuid4())
    task_id = "frozen-retry-task"
    coordinates = dispatcher_module._DispatchCoordinates(
        occurrence_id=occurrence_id,
        project_id=project_id,
        owner_user_id=owner_user_id,
        task_id=task_id,
        trigger="scheduled",
        context_mode="fresh_thread_per_run",
        reuse_thread_id=None,
    )
    authority = SimpleNamespace(
        project_status="active",
        project_is_suspended=False,
        membership_status="active",
        membership_role="runner",
        can_execute=True,
    )
    task = SimpleNamespace(
        id=task_id,
        status="paused",
        schedule_type="cron",
        version=2,
        frozen_at=NOW,
        deleted_at=None,
    )
    occurrence = SimpleNamespace(
        id=occurrence_id,
        task_id=task_id,
        task_version=1,
        trigger="scheduled",
        status="launching",
    )
    tasks = AsyncMock()
    tasks.lock_for_automation_outcome.return_value = task
    occurrences = AsyncMock()
    occurrences.get.return_value = occurrence
    runs = AsyncMock()
    runs.get.return_value = None
    lock_authority = AsyncMock(return_value=authority)
    monkeypatch.setattr(
        dispatcher_module,
        "lock_automation_execution_authority",
        lock_authority,
        raising=False,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "ScheduledTaskRepository",
        lambda _session: tasks,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "ScheduledTaskRunRepository",
        lambda _session: occurrences,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "PrivateRunRepository",
        lambda _session: runs,
    )

    await AutomationDispatcher(
        _UnitSessionFactory(),
        thread_service=AsyncMock(),
        clock=lambda: NOW,
    )._settle_failure(
        coordinates,
        AutomationUnavailable("unit-frozen-retry"),
    )

    lock_authority.assert_awaited_once()
    tasks.lock_for_automation_outcome.assert_awaited_once()
    occurrences.requeue_launch.assert_not_awaited()
    occurrences.finish.assert_awaited_once_with(
        coordinates.scope,
        occurrence_id,
        status="cancelled",
        error_code="AUTOMATION_AUTHORIZATION_REVOKED",
        error_message=None,
        finished_at=NOW,
    )


@pytest.mark.asyncio
async def test_failure_settlement_requeues_current_definition_in_fixed_lock_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    owner_user_id = str(uuid.uuid4())
    occurrence_id = str(uuid.uuid4())
    task_id = "current-retry-task"
    coordinates = dispatcher_module._DispatchCoordinates(
        occurrence_id=occurrence_id,
        project_id=project_id,
        owner_user_id=owner_user_id,
        task_id=task_id,
        trigger="scheduled",
        context_mode="fresh_thread_per_run",
        reuse_thread_id=None,
    )
    authority = SimpleNamespace(can_execute=True)
    task = SimpleNamespace(
        id=task_id,
        status="enabled",
        version=1,
        frozen_at=None,
        deleted_at=None,
    )
    occurrence = SimpleNamespace(
        id=occurrence_id,
        task_id=task_id,
        task_version=1,
        trigger="scheduled",
        status="launching",
    )
    lock_order: list[str] = []

    async def lock_authority(*_args, **_kwargs):
        lock_order.append("project-membership")
        return authority

    tasks = AsyncMock()

    async def lock_task(*_args, **_kwargs):
        lock_order.append("definition")
        return task

    tasks.lock_for_automation_outcome.side_effect = lock_task
    occurrences = AsyncMock()

    async def lock_occurrence(*_args, **_kwargs):
        lock_order.append("occurrence")
        return occurrence

    occurrences.get.side_effect = lock_occurrence
    runs = AsyncMock()

    async def lock_run(*_args, **_kwargs):
        lock_order.append("run")
        return None

    runs.get.side_effect = lock_run
    monkeypatch.setattr(
        dispatcher_module,
        "lock_automation_execution_authority",
        lock_authority,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "ScheduledTaskRepository",
        lambda _session: tasks,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "ScheduledTaskRunRepository",
        lambda _session: occurrences,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "PrivateRunRepository",
        lambda _session: runs,
    )

    await AutomationDispatcher(
        _UnitSessionFactory(),
        thread_service=AsyncMock(),
        clock=lambda: NOW,
        retry_delay=timedelta(seconds=30),
    )._settle_failure(
        coordinates,
        AutomationUnavailable("unit-current-retry"),
    )

    assert lock_order == [
        "project-membership",
        "definition",
        "occurrence",
        "run",
    ]
    occurrences.finish.assert_not_awaited()
    occurrences.requeue_launch.assert_awaited_once_with(
        coordinates.scope,
        occurrence_id,
        next_attempt_at=NOW + timedelta(seconds=30),
        error_code="AUTOMATION_UNAVAILABLE",
        updated_at=NOW,
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_transient_failure_after_concurrent_freeze_is_terminal_and_releases_global_cap(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence()
    launch_entered = asyncio.Event()
    freeze_committed = asyncio.Event()

    async def unavailable_after_freeze(**_kwargs):
        launch_entered.set()
        await asyncio.wait_for(freeze_committed.wait(), timeout=5)
        raise PrivateWorkUnavailable("private-launch")

    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=unavailable_after_freeze,
        clock=lambda: NOW,
        retry_delay=timedelta(seconds=30),
    )
    dispatch = asyncio.create_task(dispatcher.dispatch(occurrence.id, app=SimpleNamespace()))
    await asyncio.wait_for(launch_entered.wait(), timeout=5)
    async with seed.factory() as session, session.begin():
        await PrivateWorkRetentionService.freeze_owner(
            session,
            project_id=seed.context.project_id,
            owner_user_id=str(seed.context.user_id),
            now=NOW,
        )
    freeze_committed.set()

    with pytest.raises(AutomationUnavailable):
        await asyncio.wait_for(dispatch, timeout=5)

    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "cancelled"
    assert persisted.error_code == "AUTOMATION_AUTHORIZATION_REVOKED"
    assert persisted.next_attempt_at is None
    assert persisted.finished_at == NOW

    async with seed.factory() as session, session.begin():
        replacement = await ScheduledTaskRepository(session).create(
            seed.database.project_b_owner_a.resource_scope,
            ScheduledTaskCreate(
                task_id=f"replacement-{uuid.uuid4().hex[:20]}",
                thread_id=None,
                context_mode="fresh_thread_per_run",
                agent_asset_id=seed.database.system_agent_id,
                agent_scope="system",
                title="Replacement automation",
                prompt="Run after the frozen launch settles.",
                schedule_type="cron",
                schedule_spec={"cron": "0 * * * *"},
                timezone="UTC",
                next_run_at=NOW,
            ),
        )
    occurrences = AutomationOccurrenceService(
        seed.factory,
        max_concurrent_runs=1,
    )
    reserved = await occurrences.reserve_due(now=NOW, limit=10)
    assert tuple(row.task_id for row in reserved) == (replacement.id,)
    claimed = await occurrences.claim_next(
        now=NOW,
        lease_owner="replacement-claim",
        lease_seconds=60,
    )
    assert claimed is not None and claimed.task_id == replacement.id


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("first_locker", ["governance", "settlement"])
async def test_governance_and_dispatch_failure_settlement_follow_one_lock_order(
    dispatch_seed: DispatchSeed,
    monkeypatch: pytest.MonkeyPatch,
    first_locker: str,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence(context=seed.database.owner_b)
    thread_id = deterministic_thread_id(occurrence.id)
    run_id = deterministic_run_id(occurrence.id)
    metadata = {
        "scheduled_task_id": task.id,
        "scheduled_task_run_id": occurrence.id,
        "scheduled_trigger": occurrence.trigger,
    }
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.database.owner_b.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(task.agent_asset_id, task.agent_scope),
            metadata=metadata,
        )
        await PrivateRunRepository(session).create(
            scope=seed.database.owner_b.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(
                run_id=run_id,
                status="pending",
                metadata=metadata,
            ),
        )

    actor = ProjectContext(
        user_id=seed.context.user_id,
        project_id=seed.context.project_id,
        membership_id=seed.context.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=seed.context.membership_version,
        request_id=f"governance-dispatch-{first_locker}",
    )
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        clock=lambda: NOW,
    )
    coordinates = await dispatcher._load_coordinates(occurrence.id)
    release_first = asyncio.Event()
    first_domain_locked = asyncio.Event()
    second_domain_attempted = asyncio.Event()

    class SignallingMembershipRepository(MembershipRepository):
        async def lock_project_and_member(self, context, membership_id):
            second_domain_attempted.set()
            return await super().lock_project_and_member(context, membership_id)

    if first_locker == "governance":
        actual_lock_authority = dispatcher_module.lock_automation_execution_authority

        async def signal_settlement_authority_attempt(session, scope):
            second_domain_attempted.set()
            return await actual_lock_authority(session, scope)

        monkeypatch.setattr(
            dispatcher_module,
            "lock_automation_execution_authority",
            signal_settlement_authority_attempt,
        )

        class PausingAuthorization:
            @staticmethod
            async def mark_revoked(*args, **kwargs):
                first_domain_locked.set()
                await asyncio.wait_for(release_first.wait(), timeout=5)
                return await PrivateRunAuthorizationService.mark_revoked(
                    *args,
                    **kwargs,
                )

        repository_type = MembershipRepository
        authorization = PausingAuthorization
    else:

        class PausingPrivateRunRepository(PrivateRunRepository):
            async def get(self, *, scope, run_id, lock=False):
                if lock:
                    first_domain_locked.set()
                    await asyncio.wait_for(release_first.wait(), timeout=5)
                return await super().get(scope=scope, run_id=run_id, lock=lock)

        monkeypatch.setattr(
            dispatcher_module,
            "PrivateRunRepository",
            PausingPrivateRunRepository,
        )
        repository_type = SignallingMembershipRepository
        authorization = PrivateRunAuthorizationService

    async def govern() -> None:
        async with seed.factory() as session:
            await MembershipService(
                repository_type(session),
                clock=lambda: NOW,
                authorization=authorization,
            ).change_role(
                actor,
                seed.database.owner_b.membership_id,
                ProjectRole.VIEWER,
                expected_version=seed.database.owner_b.membership_version,
            )

    async def settle() -> None:
        await dispatcher._settle_failure(
            coordinates,
            AutomationUnavailable("interleaved-settlement"),
        )

    if first_locker == "governance":
        first = asyncio.create_task(govern())
        await asyncio.wait_for(first_domain_locked.wait(), timeout=5)
        second = asyncio.create_task(settle())
    else:
        first = asyncio.create_task(settle())
        await asyncio.wait_for(first_domain_locked.wait(), timeout=5)
        second = asyncio.create_task(govern())
    try:
        await asyncio.wait_for(second_domain_attempted.wait(), timeout=5)
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
    finally:
        release_first.set()
        for running in (first, second):
            if not running.done():
                running.cancel()
        await asyncio.gather(first, second, return_exceptions=True)

    async with seed.factory() as session, session.begin():
        persisted_task = await ScheduledTaskRepository(session).get(
            seed.database.owner_b.resource_scope,
            task.id,
        )
        persisted_occurrence = await ScheduledTaskRunRepository(session).get(
            seed.database.owner_b.resource_scope,
            occurrence.id,
        )
        authorization_cancel_requested_at = (
            await session.execute(
                select(RunRow.authorization_cancel_requested_at).where(
                    RunRow.run_id == run_id,
                    *PrivateRunRepository.predicates(
                        seed.database.owner_b.resource_scope,
                    ),
                )
            )
        ).scalar_one()
        membership = await session.get(
            ProjectMembershipRow,
            seed.database.owner_b.membership_id,
        )
    assert persisted_task is not None
    assert persisted_task.status == "paused"
    assert persisted_task.frozen_at == NOW
    assert persisted_occurrence is not None
    assert persisted_occurrence.status == "running"
    assert persisted_occurrence.thread_id == thread_id
    assert persisted_occurrence.run_id == run_id
    assert persisted_task.run_count == 0
    assert authorization_cancel_requested_at == NOW
    assert membership is not None and membership.role == "viewer"


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["cron", "once"])
async def test_post_admission_terminal_failure_uses_safe_durable_run_outcome(
    dispatch_seed: DispatchSeed,
    schedule_type: str,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence(
        schedule_type=schedule_type,
        next_run_at=NOW + timedelta(hours=1) if schedule_type == "cron" else None,
    )

    async def fail_after_admission(**kwargs):
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            kwargs["context"],
            kwargs["thread_id"],
            PrivateRunCreate(
                run_id=kwargs["run_id"],
                metadata=dict(kwargs["metadata"]),
            ),
        )
        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).update_status(
                scope=kwargs["context"].resource_scope,
                run_id=admitted.run.run_id,
                status="error",
                error="secret private runtime launch failed",
            )
        raise PrivateWorkUnavailable(kwargs["context"].request_id)

    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=fail_after_admission,
        clock=lambda: NOW,
    )

    with pytest.raises(AutomationUnavailable):
        await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())

    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "failed"
    assert persisted.thread_id == deterministic_thread_id(occurrence.id)
    assert persisted.run_id == deterministic_run_id(occurrence.id)
    assert persisted.error_code == "AUTOMATION_RUN_FAILED"
    assert persisted.error_message == "The automation run failed."
    assert "secret" not in persisted.error_message
    async with seed.factory() as session:
        run = await PrivateRunRepository(session).get(
            scope=seed.context.resource_scope,
            run_id=persisted.run_id,
        )
    assert run is not None
    assert run.thread_id == persisted.thread_id
    assert run.metadata["scheduled_task_id"] == task.id
    parent = await seed.persisted_task(task.id)
    assert parent.status == ("enabled" if schedule_type == "cron" else "failed")
    assert parent.last_outcome == "failed"
    assert parent.last_error_code == "AUTOMATION_RUN_FAILED"
    assert parent.run_count == 1

    with pytest.raises(AutomationConflict):
        await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())
    assert (await seed.persisted_task(task.id)).run_count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_post_admission_active_run_keeps_occurrence_and_global_cap(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence(
        next_run_at=NOW + timedelta(hours=1),
    )
    admitted_run = None

    async def fail_after_admission(**kwargs):
        nonlocal admitted_run
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            kwargs["context"],
            kwargs["thread_id"],
            PrivateRunCreate(
                run_id=kwargs["run_id"],
                metadata=dict(kwargs["metadata"]),
            ),
        )
        admitted_run = admitted.run
        raise PrivateWorkUnavailable(kwargs["context"].request_id)

    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=fail_after_admission,
        clock=lambda: NOW,
    )

    with pytest.raises(AutomationUnavailable):
        await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())

    assert admitted_run is not None
    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "running"
    assert persisted.thread_id == admitted_run.thread_id
    assert persisted.run_id == admitted_run.run_id
    assert persisted.started_at == admitted_run.created_at
    assert persisted.error_code is None
    parent = await seed.persisted_task(task.id)
    assert parent.run_count == 0
    assert parent.last_outcome is None

    async with seed.factory() as session, session.begin():
        blocked = await ScheduledTaskRepository(session).create(
            seed.context.resource_scope,
            ScheduledTaskCreate(
                task_id=f"blocked-{uuid.uuid4().hex[:20]}",
                thread_id=None,
                context_mode="fresh_thread_per_run",
                agent_asset_id=seed.database.system_agent_id,
                agent_scope="system",
                title="Blocked by admitted run",
                prompt="Must wait for the active admitted run.",
                schedule_type="cron",
                schedule_spec={"cron": "0 * * * *"},
                timezone="UTC",
                next_run_at=NOW,
            ),
        )
    reserved = await AutomationOccurrenceService(
        seed.factory,
        max_concurrent_runs=1,
    ).reserve_due(now=NOW, limit=10)
    assert reserved == ()
    assert (await seed.persisted_task(blocked.id)).next_run_at == NOW


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_completion_before_backfill_wins_without_double_settlement(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence()

    async def complete_before_launcher_returns(**kwargs):
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            kwargs["context"],
            kwargs["thread_id"],
            PrivateRunCreate(
                run_id=kwargs["run_id"],
                metadata=dict(kwargs["metadata"]),
            ),
        )
        async with seed.factory() as session, session.begin():
            assert await PrivateRunRepository(session).update_status(
                scope=kwargs["context"].resource_scope,
                run_id=admitted.run.run_id,
                status="error",
                error="secret completion failure",
            )
        await AutomationReconciler(
            seed.factory,
            clock=lambda: NOW,
        ).handle_run_completion(_runtime_record(admitted))
        raise PrivateWorkUnavailable(kwargs["context"].request_id)

    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=complete_before_launcher_returns,
        clock=lambda: NOW,
    )

    with pytest.raises(AutomationUnavailable):
        await dispatcher.dispatch(occurrence.id, app=SimpleNamespace())

    persisted = await seed.persisted_occurrence(occurrence.id)
    assert persisted.status == "failed"
    assert persisted.error_code == "AUTOMATION_RUN_FAILED"
    assert persisted.error_message == "The automation run failed."
    assert persisted.thread_id == deterministic_thread_id(occurrence.id)
    assert persisted.run_id == deterministic_run_id(occurrence.id)
    parent = await seed.persisted_task(task.id)
    assert parent.last_outcome == "failed"
    assert parent.last_error_code == "AUTOMATION_RUN_FAILED"
    assert parent.run_count == 1
