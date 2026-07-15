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

from app.automations.dispatcher import AutomationDispatcher, AutomationDispatchResult
from app.automations.errors import (
    AutomationConflict,
    AutomationForbidden,
    AutomationNotFound,
    AutomationUnavailable,
    AutomationVersionConflict,
)
from app.automations.occurrences import deterministic_run_id, deterministic_thread_id
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import is_issued_private_work_context
from app.private_work.errors import PrivateWorkUnavailable
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.private_work.thread_service import PrivateThreadService
from deerflow.persistence.private_work.model import RunAssetVersionRow
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
        context_mode: str = "fresh_thread_per_run",
        thread_id: str | None = None,
        trigger: str = "scheduled",
        status: str = "enabled",
        agent_asset_id: uuid.UUID | None = None,
        agent_scope: str = "system",
    ) -> tuple[ScheduledTaskRecord, ScheduledTaskRunRecord]:
        task_id = f"task-{uuid.uuid4().hex[:20]}"
        occurrence_id = str(uuid.uuid4())
        async with self.factory() as session, session.begin():
            tasks = ScheduledTaskRepository(session)
            task = await tasks.create(
                self.context.resource_scope,
                ScheduledTaskCreate(
                    task_id=task_id,
                    thread_id=thread_id,
                    context_mode=context_mode,
                    agent_asset_id=agent_asset_id or self.database.system_agent_id,
                    agent_scope=agent_scope,
                    title="Private automation",
                    prompt="Process private project work.",
                    schedule_type="cron",
                    schedule_spec={"cron": "0 * * * *"},
                    timezone="UTC",
                    next_run_at=NOW,
                ),
            )
            if status != "enabled":
                updated = await tasks.update(
                    self.context.resource_scope,
                    task.id,
                    expected_version=task.version,
                    values={"status": status, "next_run_at": None},
                )
                assert updated is not None
                task = updated
            occurrences = ScheduledTaskRunRepository(session)
            await occurrences.create(
                self.context.resource_scope,
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
                self.context.resource_scope,
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
async def test_dispatcher_rejects_matching_terminal_run_as_conflict(
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
    assert persisted.status == "rejected"
    assert persisted.error_code == "AUTOMATION_CONFLICT"
    assert persisted.thread_id == thread_id
    assert persisted.run_id == run_id


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch_path", ["normal_launch", "adoption"])
async def test_locked_run_status_gate_rejects_pending_to_error_race(
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
    assert persisted.status == "rejected"
    assert persisted.error_code == "AUTOMATION_CONFLICT"
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


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_post_admission_failure_never_requeues_existing_private_run(
    dispatch_seed: DispatchSeed,
) -> None:
    seed = dispatch_seed
    task, occurrence = await seed.claimed_occurrence()

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
                error="Private runtime launch failed",
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
    assert persisted.status == "rejected"
    assert persisted.thread_id == deterministic_thread_id(occurrence.id)
    assert persisted.run_id == deterministic_run_id(occurrence.id)
    assert persisted.error_code == "AUTOMATION_UNAVAILABLE"
    async with seed.factory() as session:
        run = await PrivateRunRepository(session).get(
            scope=seed.context.resource_scope,
            run_id=persisted.run_id,
        )
    assert run is not None
    assert run.thread_id == persisted.thread_id
    assert run.metadata["scheduled_task_id"] == task.id
