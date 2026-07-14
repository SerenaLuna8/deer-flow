from __future__ import annotations

import uuid
from dataclasses import fields

import pytest
import pytest_asyncio
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.persistence.feedback import FeedbackRepository
from deerflow.persistence.run import RunRepository
from deerflow.runtime import RunManager, RunStatus
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.runs.manager import RunRecord


@pytest_asyncio.fixture()
async def private_runs(migrated_postgres_database_url):
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_ids = {
        "owner_a": f"thread-{uuid.uuid4()}",
        "owner_b": f"thread-{uuid.uuid4()}",
        "project_b": f"thread-{uuid.uuid4()}",
    }
    async with seed.factory() as session, session.begin():
        threads = PrivateThreadRepository(session)
        await threads.create(
            scope=seed.owner_a_scope,
            thread_id=thread_ids["owner_a"],
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        await threads.create(
            scope=seed.owner_b_scope,
            thread_id=thread_ids["owner_b"],
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        await threads.create(
            scope=seed.project_b_owner_a_scope,
            thread_id=thread_ids["project_b"],
            agent=ThreadAgentRef(seed.project_b_agent_id, "project"),
        )
    try:
        yield seed, thread_ids
    finally:
        await seed.engine.dispose()


def _scope() -> PrivateResourceScope:
    return PrivateResourceScope(
        project_id=str(uuid.uuid4()),
        owner_user_id=str(uuid.uuid4()),
        membership_version=1,
    )


def test_run_record_carries_private_resource_scope() -> None:
    assert "scope" in {field.name for field in fields(RunRecord)}


@pytest.mark.anyio
async def test_run_manager_rejects_cross_scope_memory_hit() -> None:
    owner_scope = _scope()
    other_owner_scope = PrivateResourceScope(
        project_id=owner_scope.project_id,
        owner_user_id=str(uuid.uuid4()),
        membership_version=1,
    )
    manager = RunManager()

    created = await manager.create("thread-private", scope=owner_scope)

    assert await manager.get(created.run_id, scope=owner_scope) is created
    assert await manager.get(created.run_id, scope=other_owner_scope) is None
    assert await manager.get(created.run_id) is None


@pytest.mark.postgres
@pytest.mark.anyio
async def test_private_run_repository_isolates_read_pagination_update_and_delete(
    private_runs: tuple[M4ThreadSeed, dict[str, str]],
) -> None:
    seed, thread_ids = private_runs
    run_ids = [str(uuid.uuid4()) for _ in range(3)]
    async with seed.factory() as session, session.begin():
        repository = PrivateRunRepository(session)
        for run_id in run_ids:
            await repository.create(
                scope=seed.owner_a_scope,
                thread_id=thread_ids["owner_a"],
                request=PrivateRunCreate(run_id=run_id),
            )

        assert await repository.get(scope=seed.owner_b_scope, run_id=run_ids[0]) is None
        assert await repository.get(scope=seed.project_b_owner_a_scope, run_id=run_ids[0]) is None
        assert await repository.get(scope=seed.owner_a_scope, run_id=str(uuid.uuid4())) is None

        page_one = await repository.list_by_thread(
            scope=seed.owner_a_scope,
            thread_id=thread_ids["owner_a"],
            limit=2,
        )
        page_two = await repository.list_by_thread(
            scope=seed.owner_a_scope,
            thread_id=thread_ids["owner_a"],
            limit=2,
            offset=2,
        )
        assert len(page_one) == 2
        assert len(page_two) == 1
        assert {row.run_id for row in (*page_one, *page_two)} == set(run_ids)
        assert not await repository.list_by_thread(
            scope=seed.owner_b_scope,
            thread_id=thread_ids["owner_a"],
        )

        assert not await repository.update_status(
            scope=seed.owner_b_scope,
            run_id=run_ids[0],
            status="running",
        )
        assert not await repository.delete(
            scope=seed.project_b_owner_a_scope,
            run_id=run_ids[0],
        )
        assert await repository.update_status(
            scope=seed.owner_a_scope,
            run_id=run_ids[0],
            status="running",
        )
        assert (await repository.get(scope=seed.owner_a_scope, run_id=run_ids[0])).status == "running"
        assert await repository.delete(scope=seed.owner_a_scope, run_id=run_ids[0])


@pytest.mark.postgres
@pytest.mark.anyio
async def test_run_manager_uses_record_scope_for_real_store_updates(
    private_runs: tuple[M4ThreadSeed, dict[str, str]],
) -> None:
    seed, thread_ids = private_runs
    store = RunRepository(seed.factory)
    manager = RunManager(store=store)

    record = await manager.create(thread_ids["owner_a"], scope=seed.owner_a_scope)
    await manager.set_status(record.run_id, RunStatus.running)
    await manager.update_model_name(record.run_id, "scoped-model")

    assert await store.get(record.run_id, scope=seed.owner_b_scope) is None
    stored = await store.get(record.run_id, scope=seed.owner_a_scope)
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["model_name"] == "scoped-model"


@pytest.mark.postgres
@pytest.mark.anyio
async def test_event_feedback_and_history_derive_scope_from_parent_run(
    private_runs: tuple[M4ThreadSeed, dict[str, str]],
) -> None:
    seed, thread_ids = private_runs
    run_id = str(uuid.uuid4())
    async with seed.factory() as session, session.begin():
        await PrivateRunRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_ids["owner_a"],
            request=PrivateRunCreate(run_id=run_id),
        )

    events = DbRunEventStore(seed.factory)
    event = await events.put(
        scope=seed.owner_a_scope,
        thread_id=thread_ids["owner_a"],
        run_id=run_id,
        event_type="llm.ai.response",
        category="message",
        content="private answer",
    )
    assert event["project_id"] == seed.owner_a_scope.project_id
    assert event["owner_user_id"] == seed.owner_a_scope.owner_user_id
    assert (
        await events.list_messages(
            thread_ids["owner_a"],
            scope=seed.owner_b_scope,
        )
        == []
    )
    assert [
        row["content"]
        for row in await events.list_messages(
            thread_ids["owner_a"],
            scope=seed.owner_a_scope,
        )
    ] == ["private answer"]

    feedback = FeedbackRepository(seed.factory)
    created = await feedback.create(
        scope=seed.owner_a_scope,
        run_id=run_id,
        thread_id=thread_ids["owner_a"],
        rating=1,
    )
    assert created["project_id"] == seed.owner_a_scope.project_id
    assert await feedback.get(created["feedback_id"], scope=seed.owner_b_scope) is None
    assert await feedback.get(created["feedback_id"], scope=seed.project_b_owner_a_scope) is None
    assert await feedback.get(created["feedback_id"], scope=seed.owner_a_scope) is not None
    assert not await feedback.delete(created["feedback_id"], scope=seed.owner_b_scope)
