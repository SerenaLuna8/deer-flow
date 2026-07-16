from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.automations.errors import (
    AutomationActiveRun,
    AutomationConflict,
    AutomationForbidden,
    AutomationInvalid,
    AutomationNotFound,
    AutomationOnceExpired,
    AutomationVersionConflict,
)
from app.automations.models import AutomationChanges, AutomationCreate
from app.automations.service import ProjectAutomationService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.shared_assets.agent_repository import AgentRepository
from app.shared_assets.agent_service import AgentService
from app.shared_assets.contexts import SystemAssetGovernanceContext
from deerflow.persistence.scheduled_task_runs import (
    ScheduledTaskRunCreate,
    ScheduledTaskRunRepository,
)
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskCreate,
    ScheduledTaskRepository,
)
from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
from deerflow.persistence.shared_assets import AgentRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow

NOW = datetime(2026, 7, 16, 10, 30, tzinfo=UTC)
MAX_POSTGRES_BIGINT = 2**63 - 1


@dataclass
class MutableClock:
    now: datetime = NOW
    calls: int = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.now


@dataclass
class FailIfCalledSessionFactory:
    calls: int = 0

    def __call__(self) -> None:
        self.calls += 1
        raise AssertionError("database session must not be opened")


@dataclass(frozen=True)
class AutomationServiceSeed:
    database: M4ThreadSeed
    clock: MutableClock
    owner_thread_id: str
    outsider_thread_id: str

    @property
    def factory(self):
        return self.database.factory

    @property
    def owner_context(self):
        return self.database.owner_a

    @property
    def outsider_context(self):
        return self.database.owner_b

    @property
    def viewer_context(self):
        return self.database.viewer

    def create_command(self, **changes: object) -> AutomationCreate:
        command = AutomationCreate(
            title="Morning summary",
            prompt="Summarize the private project activity.",
            context_mode="fresh_thread_per_run",
            thread_id=None,
            agent_asset_id=self.database.project_agent_id,
            agent_scope="project",
            schedule_type="cron",
            schedule_spec={"cron": "0 * * * *"},
            timezone="UTC",
        )
        return replace(command, **changes)

    async def create_task(
        self,
        *,
        status: str = "enabled",
        context=None,
        **changes: object,
    ):
        service = ProjectAutomationService(self.factory, self.clock)
        task = await service.create(context or self.owner_context, self.create_command(**changes))
        if status != "enabled":
            async with self.factory() as session, session.begin():
                updated = await ScheduledTaskRepository(session).update(
                    (context or self.owner_context).resource_scope,
                    task.id,
                    expected_version=task.version,
                    values={
                        "status": status,
                        "next_run_at": None if status == "paused" else task.next_run_at,
                    },
                )
                assert updated is not None
            return await service.get(context or self.owner_context, task.id)
        return task

    async def create_occurrence(self, task, *, status: str, suffix: str = "one"):
        occurrence_id = f"occurrence-{suffix}-{uuid.uuid4().hex[:12]}"
        async with self.factory() as session, session.begin():
            return await ScheduledTaskRunRepository(session).create(
                self.owner_context.resource_scope,
                ScheduledTaskRunCreate(
                    occurrence_id=occurrence_id,
                    task_id=task.id,
                    task_version=task.version,
                    occurrence_key=uuid.uuid4().hex + uuid.uuid4().hex,
                    manual_idempotency_hash=None,
                    scheduled_for=self.clock.now,
                    trigger="scheduled",
                    status=status,
                ),
            )


@pytest_asyncio.fixture()
async def automation_service_seed(
    migrated_postgres_database_url: str,
) -> AutomationServiceSeed:
    database = await seed_m4_thread_database(migrated_postgres_database_url)
    owner_thread_id = f"owner-thread-{uuid.uuid4().hex[:12]}"
    outsider_thread_id = f"outsider-thread-{uuid.uuid4().hex[:12]}"
    try:
        async with database.factory() as session, session.begin():
            repository = PrivateThreadRepository(session)
            await repository.create(
                scope=database.owner_a_scope,
                thread_id=owner_thread_id,
                agent=ThreadAgentRef(database.project_agent_id, "project"),
            )
            await repository.create(
                scope=database.owner_b_scope,
                thread_id=outsider_thread_id,
                agent=ThreadAgentRef(database.project_agent_id, "project"),
            )
        yield AutomationServiceSeed(
            database=database,
            clock=MutableClock(),
            owner_thread_id=owner_thread_id,
            outsider_thread_id=outsider_thread_id,
        )
    finally:
        await database.engine.dispose()


def _repository_create(seed: AutomationServiceSeed, task_id: str) -> ScheduledTaskCreate:
    return ScheduledTaskCreate(
        task_id=task_id,
        thread_id=None,
        context_mode="fresh_thread_per_run",
        agent_asset_id=seed.database.system_agent_id,
        agent_scope="system",
        title="Viewer task",
        prompt="Read-only private prompt",
        schedule_type="cron",
        schedule_spec={"cron": "0 * * * *"},
        timezone="UTC",
        next_run_at=NOW + timedelta(minutes=30),
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_viewer_can_read_own_definition_but_cannot_create(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    task_id = f"viewer-task-{uuid.uuid4().hex[:12]}"
    async with seed.factory() as session, session.begin():
        await ScheduledTaskRepository(session).create(
            seed.viewer_context.resource_scope,
            _repository_create(seed, task_id),
        )

    service = ProjectAutomationService(seed.factory, seed.clock)
    assert [item.id for item in await service.list(seed.viewer_context, limit=50, offset=0)] == [task_id]
    assert (await service.get(seed.viewer_context, task_id)).prompt == "Read-only private prompt"
    with pytest.raises(AutomationForbidden) as raised:
        await service.create(seed.viewer_context, seed.create_command())
    assert raised.value.request_id == seed.viewer_context.request_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_definition_reads_are_project_and_owner_scoped(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    task = await service.create(seed.owner_context, seed.create_command())

    assert await service.list(seed.outsider_context, limit=50, offset=0) == ()
    assert await service.list(seed.owner_context, limit=50, offset=0, thread_id=seed.owner_thread_id) == ()
    with pytest.raises(AutomationNotFound):
        await service.get(seed.outsider_context, task.id)
    with pytest.raises(AutomationNotFound):
        await service.get(seed.database.project_b_owner_a, task.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_create_uses_prefixed_uuid_hex_task_id(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    task = await ProjectAutomationService(seed.factory, seed.clock).create(
        seed.owner_context,
        seed.create_command(),
    )

    assert task.id == f"task-{uuid.UUID(task.id.removeprefix('task-')).hex}"


@pytest.mark.parametrize("invalid_delay", [-1, True, 1.5])
def test_min_once_delay_requires_nonnegative_plain_integer(
    invalid_delay: object,
) -> None:
    with pytest.raises(ValueError, match="min_once_delay_seconds"):
        ProjectAutomationService(
            FailIfCalledSessionFactory(),  # type: ignore[arg-type]
            min_once_delay_seconds=invalid_delay,  # type: ignore[arg-type]
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_create_requires_server_issued_context_and_executable_exact_agent(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)

    with pytest.raises(AutomationNotFound):
        await service.create(object(), seed.create_command())  # type: ignore[arg-type]
    with pytest.raises(AutomationNotFound):
        await service.create(
            seed.owner_context,
            seed.create_command(agent_asset_id=seed.database.project_b_agent_id),
        )
    with pytest.raises(AutomationNotFound):
        await service.create(
            seed.owner_context,
            seed.create_command(agent_scope="system"),
        )


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("agent_scope", ["project", "system"])
async def test_create_rejects_archived_agent(
    automation_service_seed: AutomationServiceSeed,
    agent_scope: str,
) -> None:
    seed = automation_service_seed
    agent_asset_id = seed.database.project_agent_id if agent_scope == "project" else seed.database.system_agent_id
    async with seed.factory() as session, session.begin():
        await session.execute(update(AgentRow).where(AgentRow.id == agent_asset_id).values(status="archived"))

    service = ProjectAutomationService(seed.factory, seed.clock)
    with pytest.raises(AutomationNotFound) as raised:
        await service.create(
            seed.owner_context,
            seed.create_command(
                agent_asset_id=agent_asset_id,
                agent_scope=agent_scope,
            ),
        )
    assert raised.value.request_id == seed.owner_context.request_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_system_agent_archive_waits_for_create_target_validation(
    automation_service_seed: AutomationServiceSeed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    archive_service = AgentService(seed.factory)
    system_admin = SystemAssetGovernanceContext(
        user_id=seed.owner_context.user_id,
        request_id="req-system-archive",
    )
    active_checked = asyncio.Event()
    archive_lock_attempted = asyncio.Event()
    release_resolution = asyncio.Event()
    completion_order: list[str] = []
    resolve = service._resolver.resolve_project_asset_snapshot_in_session
    get_system_asset = AgentRepository.get_system_asset

    async def pause_after_active_check(session, context, selection):
        active_checked.set()
        await release_resolution.wait()
        return await resolve(session, context, selection)

    async def observe_archive_lock(
        repository,
        context,
        asset_id,
        *,
        for_update=False,
    ):
        if for_update:
            archive_lock_attempted.set()
        return await get_system_asset(
            repository,
            context,
            asset_id,
            for_update=for_update,
        )

    monkeypatch.setattr(
        service._resolver,
        "resolve_project_asset_snapshot_in_session",
        pause_after_active_check,
    )
    monkeypatch.setattr(
        AgentRepository,
        "get_system_asset",
        observe_archive_lock,
    )

    async def create_automation():
        created = await service.create(
            seed.owner_context,
            seed.create_command(
                agent_asset_id=seed.database.system_agent_id,
                agent_scope="system",
            ),
        )
        completion_order.append("create")
        return created

    async def archive_agent():
        archived = await archive_service.archive(
            system_admin,
            seed.database.system_agent_id,
            expected_asset_version=1,
        )
        completion_order.append("archive")
        return archived

    create_task = asyncio.create_task(create_automation())
    await asyncio.wait_for(active_checked.wait(), timeout=5)
    archive_task = asyncio.create_task(archive_agent())
    await asyncio.wait_for(archive_lock_attempted.wait(), timeout=5)
    archive_completed_while_validation_paused = False
    try:
        await asyncio.wait_for(asyncio.shield(archive_task), timeout=0.2)
        archive_completed_while_validation_paused = True
    except TimeoutError:
        pass
    finally:
        release_resolution.set()

    created, archived = await asyncio.wait_for(
        asyncio.gather(create_task, archive_task),
        timeout=5,
    )
    assert created.agent_asset_id == seed.database.system_agent_id
    assert archived.status == "archived"
    assert archive_completed_while_validation_paused is False
    assert completion_order == ["create", "archive"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_system_agent_archive_wins_before_create_active_lock(
    automation_service_seed: AutomationServiceSeed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.automations.service as automation_service_module

    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    archive_updated = asyncio.Event()
    release_archive = asyncio.Event()
    active_lock_attempted = asyncio.Event()
    require_executable = automation_service_module.require_executable_agent

    async def observe_active_lock(session, context, agent):
        active_lock_attempted.set()
        await require_executable(session, context, agent)

    monkeypatch.setattr(
        automation_service_module,
        "require_executable_agent",
        observe_active_lock,
    )

    async def hold_archived_agent_write_lock() -> None:
        async with seed.factory() as session, session.begin():
            agent = (await session.execute(select(AgentRow).where(AgentRow.id == seed.database.system_agent_id).with_for_update(of=AgentRow))).scalar_one()
            agent.status = "archived"
            agent.version += 1
            await session.flush()
            archive_updated.set()
            await release_archive.wait()

    archive_task = asyncio.create_task(hold_archived_agent_write_lock())
    await asyncio.wait_for(archive_updated.wait(), timeout=5)
    create_task = asyncio.create_task(
        service.create(
            seed.owner_context,
            seed.create_command(
                agent_asset_id=seed.database.system_agent_id,
                agent_scope="system",
            ),
        )
    )
    try:
        await asyncio.wait_for(active_lock_attempted.wait(), timeout=5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(create_task), timeout=0.2)
    finally:
        release_archive.set()
        await asyncio.wait_for(archive_task, timeout=5)

    with pytest.raises(AutomationNotFound) as raised:
        await asyncio.wait_for(create_task, timeout=5)
    assert raised.value.request_id == seed.owner_context.request_id
    assert await service.list(seed.owner_context, limit=50, offset=0) == ()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_reuse_thread_must_match_scope_and_agent(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)

    with pytest.raises(AutomationNotFound):
        await service.create(
            seed.owner_context,
            seed.create_command(
                context_mode="reuse_thread",
                thread_id=seed.outsider_thread_id,
            ),
        )
    with pytest.raises(AutomationNotFound):
        await service.create(
            seed.owner_context,
            seed.create_command(
                context_mode="reuse_thread",
                thread_id=seed.owner_thread_id,
                agent_asset_id=seed.database.system_agent_id,
                agent_scope="system",
            ),
        )

    created = await service.create(
        seed.owner_context,
        seed.create_command(
            context_mode="reuse_thread",
            thread_id=seed.owner_thread_id,
        ),
    )
    assert created.thread_id == seed.owner_thread_id
    assert created.agent_asset_id == seed.database.project_agent_id


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "Mars/Base"},
        {"schedule_spec": {"cron": "not a cron"}},
        {
            "schedule_type": "once",
            "schedule_spec": {"run_at": "2026-07-16T10:29:00+00:00"},
        },
        {"context_mode": "fresh_thread_per_run", "thread_id": "client-thread"},
    ],
)
async def test_create_rejects_invalid_schedule_and_thread_shapes(
    automation_service_seed: AutomationServiceSeed,
    changes: dict[str, object],
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    with pytest.raises(AutomationInvalid) as raised:
        await service.create(seed.owner_context, seed.create_command(**changes))
    assert raised.value.request_id == seed.owner_context.request_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_once_create_enforces_configured_minimum_delay_boundary(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(
        seed.factory,
        seed.clock,
        min_once_delay_seconds=60,
    )

    with pytest.raises(AutomationInvalid) as raised:
        await service.create(
            seed.owner_context,
            seed.create_command(
                schedule_type="once",
                schedule_spec={"run_at": (NOW + timedelta(seconds=59)).isoformat()},
            ),
        )
    assert raised.value.request_id == seed.owner_context.request_id
    assert await service.list(seed.owner_context, limit=50, offset=0) == ()

    created = await service.create(
        seed.owner_context,
        seed.create_command(
            schedule_type="once",
            schedule_spec={"run_at": (NOW + timedelta(seconds=60)).isoformat()},
        ),
    )
    assert created.next_run_at == NOW + timedelta(seconds=60)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_once_schedule_update_validates_delay_before_any_write(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(
        seed.factory,
        seed.clock,
        min_once_delay_seconds=60,
    )
    task = await seed.create_task(
        schedule_type="once",
        schedule_spec={"run_at": (NOW + timedelta(hours=2)).isoformat()},
    )
    queued = await seed.create_occurrence(task, status="queued")
    async with seed.factory() as session:
        task_before = await ScheduledTaskRepository(session).get(
            seed.owner_context.resource_scope,
            task.id,
        )
        occurrence_before = await ScheduledTaskRunRepository(session).get(
            seed.owner_context.resource_scope,
            queued.id,
        )
    assert task_before is not None
    assert occurrence_before is not None

    with pytest.raises(AutomationInvalid):
        await service.update(
            seed.owner_context,
            task.id,
            AutomationChanges(
                expected_version=task.version,
                schedule_spec={"run_at": (NOW + timedelta(seconds=59)).isoformat()},
            ),
        )

    async with seed.factory() as session:
        task_after = await ScheduledTaskRepository(session).get(
            seed.owner_context.resource_scope,
            task.id,
        )
        occurrence_after = await ScheduledTaskRunRepository(session).get(
            seed.owner_context.resource_scope,
            queued.id,
        )
    assert task_after == task_before
    assert occurrence_after == occurrence_before

    updated = await service.update(
        seed.owner_context,
        task.id,
        AutomationChanges(
            expected_version=task.version,
            schedule_spec={"run_at": (NOW + timedelta(seconds=60)).isoformat()},
        ),
    )
    assert updated.next_run_at == NOW + timedelta(seconds=60)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_once_title_only_update_near_existing_run_is_not_delay_validated(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(
        seed.factory,
        seed.clock,
        min_once_delay_seconds=60,
    )
    run_at = NOW + timedelta(hours=2)
    task = await service.create(
        seed.owner_context,
        seed.create_command(
            schedule_type="once",
            schedule_spec={"run_at": run_at.isoformat()},
        ),
    )
    seed.clock.now = run_at - timedelta(seconds=30)

    updated = await service.update(
        seed.owner_context,
        task.id,
        AutomationChanges(expected_version=task.version, title="Near run"),
    )

    assert updated.title == "Near run"
    assert updated.next_run_at == task.next_run_at


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_update_cancels_queued_and_uses_version_cas(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    task = await seed.create_task()
    occurrence = await seed.create_occurrence(task, status="queued")

    updated = await service.update(
        seed.owner_context,
        task.id,
        AutomationChanges(expected_version=task.version, title="Changed"),
    )
    assert updated.title == "Changed"
    assert updated.version == task.version + 1
    assert updated.next_run_at == datetime(2026, 7, 16, 11, 0, tzinfo=UTC)

    async with seed.factory() as session:
        cancelled = await ScheduledTaskRunRepository(session).get(
            seed.owner_context.resource_scope,
            occurrence.id,
        )
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.error_code == "AUTOMATION_UPDATED"

    with pytest.raises(AutomationVersionConflict):
        await service.update(
            seed.owner_context,
            task.id,
            AutomationChanges(expected_version=task.version, title="Stale"),
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_expected_version_accepts_postgres_bigint_boundaries(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    task = await seed.create_task()
    assert task.version == 1

    updated = await service.update(
        seed.owner_context,
        task.id,
        AutomationChanges(expected_version=1, title="Lower boundary"),
    )
    assert updated.version == 2

    with pytest.raises(AutomationVersionConflict) as raised:
        await service.pause(
            seed.owner_context,
            task.id,
            MAX_POSTGRES_BIGINT,
        )
    assert raised.value.request_id == seed.owner_context.request_id


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "pause", "resume", "delete"])
@pytest.mark.parametrize(
    "expected_version",
    [0, MAX_POSTGRES_BIGINT + 1],
    ids=["below-lower-bound", "above-upper-bound"],
)
async def test_out_of_range_expected_version_is_invalid_before_database(
    automation_service_seed: AutomationServiceSeed,
    operation: str,
    expected_version: int,
) -> None:
    seed = automation_service_seed
    factory = FailIfCalledSessionFactory()
    service = ProjectAutomationService(factory, seed.clock)  # type: ignore[arg-type]

    with pytest.raises(AutomationInvalid) as raised:
        if operation == "update":
            await service.update(
                seed.owner_context,
                "valid-task-id",
                AutomationChanges(
                    expected_version=expected_version,
                    title="Invalid version",
                ),
            )
        elif operation == "pause":
            await service.pause(
                seed.owner_context,
                "valid-task-id",
                expected_version,
            )
        elif operation == "resume":
            await service.resume(
                seed.owner_context,
                "valid-task-id",
                expected_version,
            )
        else:
            await service.delete(
                seed.owner_context,
                "valid-task-id",
                expected_version,
            )

    assert raised.value.request_id == seed.owner_context.request_id
    assert factory.calls == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_update_uses_one_clock_snapshot_for_atomic_mutation(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    task = await seed.create_task()
    await seed.create_occurrence(task, status="queued")
    seed.clock.calls = 0

    await service.update(
        seed.owner_context,
        task.id,
        AutomationChanges(expected_version=task.version, title="Changed"),
    )

    assert seed.clock.calls == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_update_rejects_empty_schedule_spec_without_mutation(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    task = await seed.create_task()

    with pytest.raises(AutomationInvalid):
        await service.update(
            seed.owner_context,
            task.id,
            AutomationChanges(
                expected_version=task.version,
                schedule_spec={},
            ),
        )

    persisted = await service.get(seed.owner_context, task.id)
    assert persisted.version == task.version
    assert persisted.schedule_spec == task.schedule_spec


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["launching", "running"])
async def test_update_rejects_active_execution_without_partial_mutation(
    automation_service_seed: AutomationServiceSeed,
    status: str,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    task = await seed.create_task()
    queued = await seed.create_occurrence(task, status="queued", suffix="queued")
    await seed.create_occurrence(task, status=status, suffix=status)

    with pytest.raises(AutomationActiveRun):
        await service.update(
            seed.owner_context,
            task.id,
            AutomationChanges(expected_version=task.version, title="Changed"),
        )

    persisted = await service.get(seed.owner_context, task.id)
    assert persisted.title == task.title
    assert persisted.version == task.version
    async with seed.factory() as session:
        still_queued = await ScheduledTaskRunRepository(session).get(
            seed.owner_context.resource_scope,
            queued.id,
        )
    assert still_queued is not None and still_queued.status == "queued"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_update_rejects_active_execution_before_target_revalidation(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    task = await seed.create_task(
        context_mode="reuse_thread",
        thread_id=seed.owner_thread_id,
    )
    await seed.create_occurrence(task, status="running")
    async with seed.factory() as session, session.begin():
        thread = await PrivateThreadRepository(session).get(
            scope=seed.owner_context.resource_scope,
            thread_id=seed.owner_thread_id,
        )
        assert thread is not None
        await session.execute(ThreadMetaRow.__table__.update().where(ThreadMetaRow.thread_id == seed.owner_thread_id).values(frozen_at=seed.clock.now))

    with pytest.raises(AutomationActiveRun):
        await service.update(
            seed.owner_context,
            task.id,
            AutomationChanges(expected_version=task.version, title="Changed"),
        )


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "task_status", "occurrence_status"),
    [
        ("pause", "enabled", "launching"),
        ("resume", "paused", "running"),
        ("delete", "enabled", "running"),
    ],
)
async def test_state_mutations_reject_launching_or_running_occurrence(
    automation_service_seed: AutomationServiceSeed,
    operation: str,
    task_status: str,
    occurrence_status: str,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    task = await seed.create_task(status=task_status)
    await seed.create_occurrence(task, status=occurrence_status)

    with pytest.raises(AutomationActiveRun):
        if operation == "pause":
            await service.pause(seed.owner_context, task.id, task.version)
        elif operation == "resume":
            await service.resume(seed.owner_context, task.id, task.version)
        else:
            await service.delete(seed.owner_context, task.id, task.version)

    persisted = await service.get(seed.owner_context, task.id)
    assert persisted.status == task.status
    assert persisted.version == task.version


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "source_status"),
    [
        ("pause", "paused"),
        ("pause", "completed"),
        ("pause", "failed"),
        ("pause", "cancelled"),
        ("resume", "enabled"),
        ("resume", "completed"),
        ("resume", "failed"),
        ("resume", "cancelled"),
    ],
)
async def test_pause_resume_reject_invalid_source_status_without_any_write(
    automation_service_seed: AutomationServiceSeed,
    operation: str,
    source_status: str,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    task = await seed.create_task(status=source_status)
    occurrence = await seed.create_occurrence(task, status="queued")
    async with seed.factory() as session:
        task_before = await ScheduledTaskRepository(session).get(
            seed.owner_context.resource_scope,
            task.id,
        )
        occurrence_before = await ScheduledTaskRunRepository(session).get(
            seed.owner_context.resource_scope,
            occurrence.id,
        )
    assert task_before is not None
    assert occurrence_before is not None
    seed.clock.calls = 0

    conflict = None
    try:
        if operation == "pause":
            await service.pause(seed.owner_context, task.id, task.version)
        else:
            await service.resume(seed.owner_context, task.id, task.version)
    except AutomationConflict as error:
        conflict = error

    async with seed.factory() as session:
        task_after = await ScheduledTaskRepository(session).get(
            seed.owner_context.resource_scope,
            task.id,
        )
        occurrence_after = await ScheduledTaskRunRepository(session).get(
            seed.owner_context.resource_scope,
            occurrence.id,
        )
    assert task_after == task_before
    assert occurrence_after == occurrence_before
    assert seed.clock.calls == 0
    assert conflict is not None
    assert conflict.code == "AUTOMATION_CONFLICT"
    assert conflict.request_id == seed.owner_context.request_id


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "pause", "resume", "delete"])
async def test_viewer_cannot_mutate_own_definition(
    automation_service_seed: AutomationServiceSeed,
    operation: str,
) -> None:
    seed = automation_service_seed
    task_id = f"viewer-mutation-{operation}-{uuid.uuid4().hex[:8]}"
    async with seed.factory() as session, session.begin():
        task = await ScheduledTaskRepository(session).create(
            seed.viewer_context.resource_scope,
            _repository_create(seed, task_id),
        )
        if operation == "resume":
            task = await ScheduledTaskRepository(session).update(
                seed.viewer_context.resource_scope,
                task.id,
                expected_version=task.version,
                values={"status": "paused", "next_run_at": None},
            )
            assert task is not None

    service = ProjectAutomationService(seed.factory, seed.clock)
    with pytest.raises(AutomationForbidden):
        if operation == "update":
            await service.update(
                seed.viewer_context,
                task.id,
                AutomationChanges(expected_version=task.version, title="Forbidden"),
            )
        elif operation == "pause":
            await service.pause(seed.viewer_context, task.id, task.version)
        elif operation == "resume":
            await service.resume(seed.viewer_context, task.id, task.version)
        else:
            await service.delete(seed.viewer_context, task.id, task.version)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pause_resume_replans_from_now_and_once_expiry_is_stable_conflict(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    task = await seed.create_task()
    queued = await seed.create_occurrence(task, status="queued")

    paused = await service.pause(seed.owner_context, task.id, task.version)
    assert paused.status == "paused"
    assert paused.next_run_at is None
    async with seed.factory() as session:
        occurrence = await ScheduledTaskRunRepository(session).get(
            seed.owner_context.resource_scope,
            queued.id,
        )
    assert occurrence is not None and occurrence.status == "cancelled"

    seed.clock.now = datetime(2026, 7, 16, 11, 15, tzinfo=UTC)
    seed.clock.calls = 0
    resumed = await service.resume(seed.owner_context, task.id, paused.version)
    assert resumed.status == "enabled"
    assert resumed.next_run_at == datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    assert seed.clock.calls == 1

    once = await seed.create_task(
        schedule_type="once",
        schedule_spec={"run_at": "2026-07-16T12:30:00+00:00"},
    )
    once_paused = await service.pause(seed.owner_context, once.id, once.version)
    seed.clock.now = datetime(2026, 7, 16, 12, 30, tzinfo=UTC)
    with pytest.raises(AutomationOnceExpired) as raised:
        await service.resume(seed.owner_context, once.id, once_paused.version)
    assert raised.value.request_id == seed.owner_context.request_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_update_rejects_expired_once_schedule_while_paused(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    task = await seed.create_task(
        schedule_type="once",
        schedule_spec={"run_at": "2026-07-16T12:30:00+00:00"},
    )
    paused = await service.pause(seed.owner_context, task.id, task.version)

    with pytest.raises(AutomationOnceExpired):
        await service.update(
            seed.owner_context,
            task.id,
            AutomationChanges(
                expected_version=paused.version,
                schedule_spec={"run_at": "2026-07-16T10:00:00+00:00"},
            ),
        )

    persisted = await service.get(seed.owner_context, task.id)
    assert persisted.version == paused.version
    assert persisted.schedule_spec == paused.schedule_spec


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_resume_revalidates_reuse_thread_and_execution_capabilities(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    task = await seed.create_task(
        context_mode="reuse_thread",
        thread_id=seed.owner_thread_id,
    )
    paused = await service.pause(seed.owner_context, task.id, task.version)

    async with seed.factory() as session, session.begin():
        thread = await PrivateThreadRepository(session).get(
            scope=seed.owner_context.resource_scope,
            thread_id=seed.owner_thread_id,
        )
        assert thread is not None
        await session.execute(ThreadMetaRow.__table__.update().where(ThreadMetaRow.thread_id == seed.owner_thread_id).values(frozen_at=seed.clock.now))

    with pytest.raises(AutomationNotFound):
        await service.resume(seed.owner_context, task.id, paused.version)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_delete_soft_deletes_paused_definition_and_preserves_history(
    automation_service_seed: AutomationServiceSeed,
) -> None:
    seed = automation_service_seed
    service = ProjectAutomationService(seed.factory, seed.clock)
    task = await seed.create_task()
    occurrence = await seed.create_occurrence(task, status="queued")

    await service.delete(seed.owner_context, task.id, task.version)
    with pytest.raises(AutomationNotFound):
        await service.get(seed.owner_context, task.id)

    async with seed.factory() as session:
        row = (await session.execute(select(ScheduledTaskRow).where(ScheduledTaskRow.id == task.id))).scalar_one()
        history = await ScheduledTaskRunRepository(session).list_by_task(
            seed.owner_context.resource_scope,
            task.id,
            limit=50,
            offset=0,
        )
    assert row.status == "paused"
    assert row.next_run_at is None
    assert row.deleted_at == seed.clock.now
    assert [item.id for item in history] == [occurrence.id]
    assert history[0].status == "cancelled"
    assert history[0].error_code == "AUTOMATION_DELETED"
