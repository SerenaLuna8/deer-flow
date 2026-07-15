from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.agents.memory.storage import ProjectMemoryStorage, create_empty_memory
from deerflow.persistence.private_work.memory_repository import (
    PrivateMemoryInvalid,
    PrivateMemoryVersionConflict,
)


@pytest_asyncio.fixture()
async def private_memories(migrated_postgres_database_url: str):
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield seed
    finally:
        await seed.engine.dispose()


def _memory(*, summary: str, facts: list[dict[str, object]] | None = None) -> dict[str, object]:
    memory = create_empty_memory()
    memory["lastUpdated"] = "2026-07-15T01:02:03Z"
    memory["user"]["workContext"] = {
        "summary": summary,
        "updatedAt": "2026-07-15T01:02:03Z",
    }
    memory["facts"] = facts or []
    return memory


async def _create_source_run(seed: M4ThreadSeed) -> tuple[str, str]:
    thread_id = f"memory-source-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        await PrivateRunRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(run_id=run_id),
        )
    return thread_id, run_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_storage_creates_saves_and_loads_existing_memory_json(private_memories: M4ThreadSeed) -> None:
    seed = private_memories
    storage = ProjectMemoryStorage(seed.factory)
    source_thread_id, source_run_id = await _create_source_run(seed)

    initial = await storage.create_if_needed(scope=seed.owner_a_scope, namespace="default")
    assert initial.version == 1
    assert initial.memory["facts"] == []

    saved = await storage.save(
        _memory(
            summary="Project A owner context",
            facts=[
                {
                    "id": "fact_legacy_shape",
                    "content": "User prefers runnable milestones.",
                    "category": "preference",
                    "confidence": 0.93,
                    "createdAt": "2026-07-14T10:00:00Z",
                    "source": source_thread_id,
                    "sourceThreadId": source_thread_id,
                    "sourceRunId": source_run_id,
                }
            ],
        ),
        scope=seed.owner_a_scope,
        namespace="default",
        expected_version=initial.version,
    )

    assert saved.version == 2
    assert saved.memory["version"] == "1.0"
    assert saved.memory["lastUpdated"] == "2026-07-15T01:02:03Z"
    assert saved.memory["user"]["workContext"]["summary"] == "Project A owner context"
    assert len(saved.memory["facts"]) == 1
    saved_fact = saved.memory["facts"][0]
    assert str(uuid.UUID(saved_fact["id"])) == saved_fact["id"]
    assert saved_fact["source"] == source_thread_id
    assert saved_fact["sourceThreadId"] == source_thread_id
    assert saved_fact["sourceRunId"] == source_run_id

    loaded = await storage.load(scope=seed.owner_a_scope, namespace="default")
    assert loaded == saved


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_storage_isolates_project_owner_and_namespace(private_memories: M4ThreadSeed) -> None:
    seed = private_memories
    storage = ProjectMemoryStorage(seed.factory)
    targets = (
        (seed.owner_a_scope, "default", "project-a-owner-a-default"),
        (seed.owner_a_scope, "agent:researcher", "project-a-owner-a-agent"),
        (seed.owner_b_scope, "default", "project-a-owner-b-default"),
        (seed.project_b_owner_a_scope, "default", "project-b-owner-a-default"),
    )

    for scope, namespace, summary in targets:
        current = await storage.load(scope=scope, namespace=namespace)
        await storage.save(
            _memory(summary=summary),
            scope=scope,
            namespace=namespace,
            expected_version=current.version,
        )

    for scope, namespace, summary in targets:
        loaded = await storage.load(scope=scope, namespace=namespace)
        assert loaded.memory["user"]["workContext"]["summary"] == summary


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_storage_rejects_stale_expected_version_without_losing_winner(private_memories: M4ThreadSeed) -> None:
    seed = private_memories
    storage = ProjectMemoryStorage(seed.factory)
    first_reader = await storage.load(scope=seed.owner_a_scope, namespace="default")
    stale_reader = await storage.load(scope=seed.owner_a_scope, namespace="default")

    winner = await storage.save(
        _memory(summary="winner"),
        scope=seed.owner_a_scope,
        namespace="default",
        expected_version=first_reader.version,
    )
    with pytest.raises(PrivateMemoryVersionConflict):
        await storage.save(
            _memory(summary="stale overwrite"),
            scope=seed.owner_a_scope,
            namespace="default",
            expected_version=stale_reader.version,
        )

    assert (await storage.load(scope=seed.owner_a_scope, namespace="default")) == winner


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_storage_clear_is_versioned_and_removes_fact_rows(private_memories: M4ThreadSeed) -> None:
    seed = private_memories
    storage = ProjectMemoryStorage(seed.factory)
    initial = await storage.load(scope=seed.owner_a_scope, namespace="default")
    populated = await storage.save(
        _memory(
            summary="temporary",
            facts=[
                {
                    "content": "Temporary fact",
                    "category": "context",
                    "confidence": 0.8,
                    "source": "manual",
                }
            ],
        ),
        scope=seed.owner_a_scope,
        namespace="default",
        expected_version=initial.version,
    )

    cleared = await storage.clear(
        scope=seed.owner_a_scope,
        namespace="default",
        expected_version=populated.version,
    )

    assert cleared.version == populated.version + 1
    assert cleared.memory["user"]["workContext"]["summary"] == ""
    assert cleared.memory["facts"] == []
    assert await storage.load(scope=seed.owner_a_scope, namespace="default") == cleared


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_storage_requires_valid_scope_and_namespace(private_memories: M4ThreadSeed) -> None:
    storage = ProjectMemoryStorage(private_memories.factory)

    with pytest.raises(PrivateMemoryInvalid):
        await storage.load(scope=None, namespace="default")  # type: ignore[arg-type]
    with pytest.raises(PrivateMemoryInvalid):
        await storage.load(scope=private_memories.owner_a_scope, namespace="")
