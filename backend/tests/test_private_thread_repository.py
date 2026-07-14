from __future__ import annotations

import inspect
import uuid

import pytest
import pytest_asyncio
from langgraph.store.memory import InMemoryStore
from sqlalchemy import text
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.private_work.errors import PrivateWorkConflict


@pytest_asyncio.fixture
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


async def _create_thread(
    seed: M4ThreadSeed,
    *,
    scope=None,
    thread_id: str = "thread-a",
    agent_id: uuid.UUID | None = None,
):
    from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef

    async with seed.factory() as session:
        async with session.begin():
            return await PrivateThreadRepository(session).create(
                scope=scope or seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(
                    asset_id=agent_id or seed.project_agent_id,
                    scope="project",
                ),
                display_name="Scoped thread",
                metadata={"kind": "owned"},
            )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_repository_never_returns_same_project_other_owner(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import PrivateThreadRepository

    await _create_thread(seed)
    async with seed.factory() as session:
        repository = PrivateThreadRepository(session)
        assert await repository.get(scope=seed.owner_b_scope, thread_id="thread-a") is None
        assert await repository.get(scope=seed.project_b_owner_a_scope, thread_id="thread-a") is None
        assert await repository.check_access(scope=seed.owner_b_scope, thread_id="thread-a") is False
        assert await repository.check_access(scope=seed.project_b_owner_a_scope, thread_id="thread-a") is False


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_repository_scopes_search_patch_and_delete_in_sql(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import PrivateThreadRepository

    created = await _create_thread(seed)
    await _create_thread(
        seed,
        scope=seed.owner_b_scope,
        thread_id="thread-b",
    )

    async with seed.factory() as session:
        async with session.begin():
            repository = PrivateThreadRepository(session)
            results = await repository.search(scope=seed.owner_a_scope)
            assert [record.thread_id for record in results] == ["thread-a"]
            patched = await repository.patch(
                scope=seed.owner_a_scope,
                thread_id="thread-a",
                expected_version=created.version,
                display_name="Renamed",
            )
            assert patched.display_name == "Renamed"
            assert patched.version == created.version + 1

            with pytest.raises(PrivateWorkConflict):
                await repository.patch(
                    scope=seed.owner_a_scope,
                    thread_id="thread-a",
                    expected_version=created.version,
                    display_name="Stale write",
                )

            deleted = await repository.mark_deleted(
                scope=seed.owner_a_scope,
                thread_id="thread-a",
                expected_version=patched.version,
            )
            assert deleted.deleted_at is not None

    async with seed.factory() as session:
        repository = PrivateThreadRepository(session)
        assert await repository.get(scope=seed.owner_a_scope, thread_id="thread-a") is None
        assert await repository.check_access(scope=seed.owner_a_scope, thread_id="thread-a") is False
        assert await repository.search(scope=seed.owner_a_scope) == ()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_repository_global_uuid_collision_is_generic_conflict(
    seed: M4ThreadSeed,
) -> None:
    await _create_thread(seed, thread_id="globally-colliding-thread")

    with pytest.raises(PrivateWorkConflict) as exc_info:
        await _create_thread(
            seed,
            scope=seed.project_b_owner_a_scope,
            thread_id="globally-colliding-thread",
            agent_id=seed.project_b_agent_id,
        )

    assert str(exc_info.value) == "Private work conflict."
    assert exc_info.value.request_id == "unknown"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_repository_hides_frozen_threads(seed: M4ThreadSeed) -> None:
    from app.private_work.thread_repository import PrivateThreadRepository

    await _create_thread(seed, thread_id="frozen-thread")
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE threads_meta SET frozen_at=now()
                WHERE thread_id='frozen-thread'"""
            )
        )

    async with seed.factory() as session:
        repository = PrivateThreadRepository(session)
        assert await repository.get(scope=seed.owner_a_scope, thread_id="frozen-thread") is None
        assert await repository.check_access(scope=seed.owner_a_scope, thread_id="frozen-thread") is False


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_harness_thread_store_project_path_requires_full_scope_in_sql(
    seed: M4ThreadSeed,
) -> None:
    from deerflow.persistence.thread_meta.sql import ThreadMetaRepository

    repository = ThreadMetaRepository(seed.factory)
    await repository.create(
        "harness-scoped-thread",
        scope=seed.owner_a_scope,
        agent_asset_id=seed.project_agent_id,
        agent_scope="project",
    )
    assert (
        await repository.get(
            "harness-scoped-thread",
            scope=seed.owner_b_scope,
        )
        is None
    )
    await repository.update_status(
        "harness-scoped-thread",
        "busy",
        scope=seed.owner_b_scope,
    )
    owner_record = await repository.get(
        "harness-scoped-thread",
        scope=seed.owner_a_scope,
    )
    assert owner_record is not None
    assert owner_record["status"] == "idle"
    await repository.update_status(
        "harness-scoped-thread",
        "busy",
        scope=seed.owner_a_scope,
    )
    assert [
        item["thread_id"]
        for item in await repository.search(
            status="busy",
            scope=seed.owner_a_scope,
        )
    ] == ["harness-scoped-thread"]

    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE threads_meta SET frozen_at=now()
                WHERE thread_id='harness-scoped-thread'"""
            )
        )
    assert await repository.get("harness-scoped-thread", scope=seed.owner_a_scope) is None
    assert await repository.search(scope=seed.owner_a_scope) == []
    assert (
        await repository.check_access(
            "harness-scoped-thread",
            str(seed.owner_a.user_id),
            scope=seed.owner_a_scope,
        )
        is False
    )
    await repository.update_status(
        "harness-scoped-thread",
        "error",
        scope=seed.owner_a_scope,
    )
    await repository.delete(
        "harness-scoped-thread",
        scope=seed.owner_a_scope,
    )
    async with seed.engine.connect() as connection:
        frozen_status = (
            await connection.execute(
                text(
                    """SELECT status, deleted_at FROM threads_meta
                    WHERE thread_id='harness-scoped-thread'"""
                )
            )
        ).one()
    assert frozen_status.status == "busy"
    assert frozen_status.deleted_at is None

    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE threads_meta SET frozen_at=NULL
                WHERE thread_id='harness-scoped-thread'"""
            )
        )
    await repository.delete(
        "harness-scoped-thread",
        scope=seed.owner_b_scope,
    )
    assert (
        await repository.get(
            "harness-scoped-thread",
            scope=seed.owner_a_scope,
        )
        is not None
    )
    await repository.delete(
        "harness-scoped-thread",
        scope=seed.owner_a_scope,
    )
    assert (
        await repository.get(
            "harness-scoped-thread",
            scope=seed.owner_a_scope,
        )
        is None
    )


@pytest.mark.asyncio
async def test_memory_thread_store_scope_path_excludes_frozen_and_deleted_records() -> None:
    from deerflow.persistence.thread_meta.memory import THREADS_NS, MemoryThreadMetaStore
    from deerflow.runtime.private_scope import PrivateResourceScope

    owner_id = str(uuid.uuid4())
    scope = PrivateResourceScope(
        project_id=str(uuid.uuid4()),
        owner_user_id=owner_id,
        membership_version=1,
    )
    store = InMemoryStore()
    repository = MemoryThreadMetaStore(store)
    created = await repository.create(
        "memory-frozen-thread",
        scope=scope,
        agent_asset_id=uuid.uuid4(),
        agent_scope="project",
    )
    assert created["version"] == 1

    frozen = dict(created)
    frozen["frozen_at"] = "2026-07-14T00:00:00+00:00"
    await store.aput(THREADS_NS, "memory-frozen-thread", frozen)
    assert await repository.get("memory-frozen-thread", scope=scope) is None
    assert await repository.search(scope=scope) == []
    assert (
        await repository.check_access(
            "memory-frozen-thread",
            owner_id,
            scope=scope,
        )
        is False
    )
    await repository.update_status("memory-frozen-thread", "busy", scope=scope)
    await repository.delete("memory-frozen-thread", scope=scope)
    untouched = await store.aget(THREADS_NS, "memory-frozen-thread")
    assert untouched is not None
    assert untouched.value["status"] == "idle"

    deleted = dict(created)
    deleted["deleted_at"] = "2026-07-14T00:00:00+00:00"
    await store.aput(THREADS_NS, "memory-frozen-thread", deleted)
    assert await repository.get("memory-frozen-thread", scope=scope) is None
    assert await repository.search(scope=scope) == []


def test_private_thread_repository_has_no_fetch_then_python_scope_checks() -> None:
    from app.private_work.thread_repository import PrivateThreadRepository

    source = inspect.getsource(PrivateThreadRepository)
    assert ".get(ThreadMetaRow" not in source
    assert "ThreadMetaRow.project_id" in source
    assert "ThreadMetaRow.owner_user_id" in source
    assert "ThreadMetaRow.deleted_at" in source


def test_gateway_legacy_thread_store_is_explicitly_trusted_unscoped() -> None:
    from deerflow.persistence.thread_meta import TrustedUnscopedThreadMetaStore
    from deerflow.persistence.thread_meta.base import ThreadMetaStore

    assert not issubclass(TrustedUnscopedThreadMetaStore, ThreadMetaStore)
