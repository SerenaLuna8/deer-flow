from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from deerflow.persistence.scheduled_task_runs import (
    ScheduledTaskRunCreate,
    ScheduledTaskRunRecord,
    ScheduledTaskRunRepository,
)
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskCreate,
    ScheduledTaskPatch,
    ScheduledTaskRecord,
    ScheduledTaskRepository,
)


@pytest_asyncio.fixture()
async def automation_seed(migrated_postgres_database_url: str):
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield seed
    finally:
        await seed.engine.dispose()


def _task_create(seed: M4ThreadSeed, task_id: str) -> ScheduledTaskCreate:
    return ScheduledTaskCreate(
        task_id=task_id,
        thread_id=None,
        context_mode="fresh_thread_per_run",
        agent_asset_id=seed.system_agent_id,
        agent_scope="system",
        title="Owner task",
        prompt="private prompt",
        schedule_type="cron",
        schedule_spec={"cron": "0 9 * * *"},
        timezone="Asia/Shanghai",
        next_run_at=datetime(2026, 7, 17, 1, 0, tzinfo=UTC),
    )


async def _create_task(seed: M4ThreadSeed, task_id: str = "task-owner"):
    async with seed.factory() as session, session.begin():
        return await ScheduledTaskRepository(session).create(
            seed.owner_a_scope,
            _task_create(seed, task_id),
        )


def test_repository_requires_exact_private_resource_scope() -> None:
    with pytest.raises(TypeError, match="PrivateResourceScope"):
        ScheduledTaskRepository.predicates({"project_id": "forged"})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="PrivateResourceScope"):
        ScheduledTaskRunRepository.predicates({"project_id": "forged"})  # type: ignore[arg-type]


def test_commands_and_records_are_immutable_and_occurrence_record_is_safe() -> None:
    assert ScheduledTaskCreate.__dataclass_params__.frozen is True
    assert ScheduledTaskPatch.__dataclass_params__.frozen is True
    assert ScheduledTaskRecord.__dataclass_params__.frozen is True
    assert ScheduledTaskRunCreate.__dataclass_params__.frozen is True
    assert ScheduledTaskRunRecord.__dataclass_params__.frozen is True
    run_fields = {field.name for field in fields(ScheduledTaskRunRecord)}
    assert "lease_owner" not in run_fields
    assert "lease_expires_at" not in run_fields
    assert "manual_idempotency_hash" not in run_fields


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_task_repository_never_returns_or_mutates_cross_owner(
    automation_seed: M4ThreadSeed,
) -> None:
    seed = automation_seed
    created = await _create_task(seed)
    assert created.id == "task-owner"
    assert created.version == 1

    async with seed.factory() as session, session.begin():
        repository = ScheduledTaskRepository(session)
        assert await repository.get(seed.owner_b_scope, created.id) is None
        assert await repository.list(seed.owner_b_scope, limit=50, offset=0) == ()
        assert (
            await repository.update(
                seed.owner_b_scope,
                created.id,
                expected_version=1,
                values={"title": "forged"},
            )
            is None
        )
        assert not await repository.soft_delete(
            seed.owner_b_scope,
            created.id,
            expected_version=1,
            deleted_at=datetime(2026, 7, 17, 2, 0, tzinfo=UTC),
        )

    async with seed.factory() as session:
        owner = await ScheduledTaskRepository(session).get(seed.owner_a_scope, created.id)
        assert owner is not None
        assert owner.title == "Owner task"
        assert owner.deleted_at is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_task_repository_enforces_project_agent_ownership_and_allows_system_agents(
    automation_seed: M4ThreadSeed,
) -> None:
    seed = automation_seed
    async with seed.factory() as session, session.begin():
        repository = ScheduledTaskRepository(session)
        system_task = await repository.create(
            seed.owner_a_scope,
            _task_create(seed, "task-system-agent"),
        )
        project_task = await repository.create(
            seed.owner_a_scope,
            replace(
                _task_create(seed, "task-project-agent"),
                agent_asset_id=seed.project_agent_id,
                agent_scope="project",
            ),
        )
        assert system_task.agent_scope == "system"
        assert project_task.agent_asset_id == seed.project_agent_id

    with pytest.raises(IntegrityError):
        async with seed.factory() as session, session.begin():
            await ScheduledTaskRepository(session).create(
                seed.owner_a_scope,
                replace(
                    _task_create(seed, "task-cross-project-agent"),
                    agent_asset_id=seed.project_b_agent_id,
                    agent_scope="project",
                ),
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_task_repository_rejects_server_owned_update_fields(
    automation_seed: M4ThreadSeed,
) -> None:
    seed = automation_seed
    created = await _create_task(seed, "task-protected-fields")
    protected_values = {
        "id": "forged-id",
        "project_id": seed.project_b_owner_a.project_id,
        "owner_user_id": seed.owner_b.user_id,
        "thread_id": "forged-thread",
        "context_mode": "reuse_thread",
        "agent_asset_id": seed.project_b_agent_id,
        "agent_scope": "project",
        "schedule_type": "once",
        "overlap_policy": "parallel",
        "last_run_at": datetime(2026, 7, 17, 2, 0, tzinfo=UTC),
        "last_outcome": "success",
        "last_error_code": "FORGED",
        "run_count": 99,
        "version": 99,
        "frozen_at": datetime(2026, 7, 17, 2, 0, tzinfo=UTC),
        "deleted_at": datetime(2026, 7, 17, 2, 0, tzinfo=UTC),
        "created_at": datetime(2026, 7, 17, 2, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 17, 2, 0, tzinfo=UTC),
    }

    async with seed.factory() as session, session.begin():
        repository = ScheduledTaskRepository(session)
        for field_name, value in protected_values.items():
            with pytest.raises(ValueError, match="patchable"):
                await repository.update(
                    seed.owner_a_scope,
                    created.id,
                    expected_version=1,
                    values={field_name: value},
                )
        updated = await repository.update(
            seed.owner_a_scope,
            created.id,
            expected_version=1,
            values={"title": "Legitimate patch"},
        )
        assert updated is not None
        assert updated.title == "Legitimate patch"
        assert updated.version == 2

    async with seed.factory() as session:
        persisted = await ScheduledTaskRepository(session).get(
            seed.owner_a_scope,
            created.id,
        )
        assert persisted is not None
        assert persisted.id == created.id
        assert persisted.project_id == seed.owner_a.project_id
        assert persisted.owner_user_id == str(seed.owner_a.user_id)
        assert persisted.run_count == 0
        assert persisted.frozen_at is None
        assert persisted.deleted_at is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_task_repository_uses_version_cas_locking_and_soft_delete(
    automation_seed: M4ThreadSeed,
) -> None:
    seed = automation_seed
    created = await _create_task(seed, "task-cas")

    async with seed.factory() as session, session.begin():
        repository = ScheduledTaskRepository(session)
        locked = await repository.lock_active(seed.owner_a_scope, created.id)
        assert locked is not None
        assert locked.id == created.id
        updated = await repository.update(
            seed.owner_a_scope,
            created.id,
            expected_version=1,
            values={"title": "Updated"},
        )
        assert updated is not None
        assert updated.title == "Updated"
        assert updated.version == 2
        assert (
            await repository.update(
                seed.owner_a_scope,
                created.id,
                expected_version=1,
                values={"title": "Stale"},
            )
            is None
        )
        assert await repository.soft_delete(
            seed.owner_a_scope,
            created.id,
            expected_version=2,
            deleted_at=datetime(2026, 7, 17, 3, 0, tzinfo=UTC),
        )

    async with seed.factory() as session:
        repository = ScheduledTaskRepository(session)
        assert await repository.get(seed.owner_a_scope, created.id) is None
        assert await repository.lock_active(seed.owner_a_scope, created.id) is None
        assert await repository.list(seed.owner_a_scope, limit=50, offset=0) == ()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_task_repository_lists_deterministically_and_enforces_hard_limit(
    automation_seed: M4ThreadSeed,
) -> None:
    seed = automation_seed
    await _create_task(seed, "task-a")
    await _create_task(seed, "task-b")

    async with seed.factory() as session:
        repository = ScheduledTaskRepository(session)
        rows = await repository.list(seed.owner_a_scope, limit=50, offset=0)
        assert [row.id for row in rows] == ["task-b", "task-a"]
        for invalid in (0, 1001):
            with pytest.raises(ValueError, match="limit"):
                await repository.list(seed.owner_a_scope, limit=invalid, offset=0)
        with pytest.raises(ValueError, match="offset"):
            await repository.list(seed.owner_a_scope, limit=50, offset=-1)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_occurrence_history_and_agent_run_lookup_are_parent_scoped(
    automation_seed: M4ThreadSeed,
) -> None:
    seed = automation_seed
    await _create_task(seed)
    now = datetime(2026, 7, 17, 1, 0, tzinfo=UTC)
    async with seed.factory() as session, session.begin():
        repository = ScheduledTaskRunRepository(session)
        created = await repository.create(
            seed.owner_a_scope,
            ScheduledTaskRunCreate(
                occurrence_id="task-run-owner",
                task_id="task-owner",
                task_version=1,
                occurrence_key="a" * 64,
                manual_idempotency_hash=None,
                scheduled_for=now,
                trigger="scheduled",
                status="queued",
            ),
        )
        assert created.id == "task-run-owner"
        assert created.run_id is None

    async with seed.factory() as session, session.begin():
        repository = ScheduledTaskRunRepository(session)
        assert (
            await repository.list_by_task(
                seed.owner_b_scope,
                "task-owner",
                limit=50,
                offset=0,
            )
            == ()
        )
        assert await repository.get(seed.owner_b_scope, created.id) is None
        assert not await repository.has_active(seed.owner_b_scope, "task-owner")
        assert await repository.get_by_agent_run_id(seed.owner_b_scope, "agent-run") is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_occurrence_terminal_cas_and_queued_cancellation_are_scoped(
    automation_seed: M4ThreadSeed,
) -> None:
    seed = automation_seed
    await _create_task(seed)
    now = datetime(2026, 7, 17, 1, 0, tzinfo=UTC)
    async with seed.factory() as session, session.begin():
        repository = ScheduledTaskRunRepository(session)
        for occurrence_id, occurrence_key in (
            ("run-finish", "b" * 64),
            ("run-cancel", "c" * 64),
        ):
            await repository.create(
                seed.owner_a_scope,
                ScheduledTaskRunCreate(
                    occurrence_id=occurrence_id,
                    task_id="task-owner",
                    task_version=1,
                    occurrence_key=occurrence_key,
                    manual_idempotency_hash=None,
                    scheduled_for=now,
                    trigger="scheduled",
                    status="queued",
                ),
            )
        assert not await repository.finish(
            seed.owner_b_scope,
            "run-finish",
            status="success",
            error_code=None,
            error_message=None,
            finished_at=now + timedelta(minutes=1),
        )
        assert await repository.finish(
            seed.owner_a_scope,
            "run-finish",
            status="success",
            error_code=None,
            error_message=None,
            finished_at=now + timedelta(minutes=1),
        )
        assert not await repository.finish(
            seed.owner_a_scope,
            "run-finish",
            status="failed",
            error_code="LATE",
            error_message="late completion",
            finished_at=now + timedelta(minutes=2),
        )
        assert (
            await repository.cancel_queued(
                seed.owner_b_scope,
                "task-owner",
                now=now + timedelta(minutes=2),
                error_code="OUTSIDER",
            )
            == 0
        )
        assert (
            await repository.cancel_queued(
                seed.owner_a_scope,
                "task-owner",
                now=now + timedelta(minutes=2),
                error_code="AUTOMATION_PAUSED",
            )
            == 1
        )

    async with seed.factory() as session:
        repository = ScheduledTaskRunRepository(session)
        finished = await repository.get(seed.owner_a_scope, "run-finish")
        cancelled = await repository.get(seed.owner_a_scope, "run-cancel")
        assert finished is not None and finished.status == "success"
        assert cancelled is not None and cancelled.status == "cancelled"
        assert cancelled.error_code == "AUTOMATION_PAUSED"
