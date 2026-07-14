from __future__ import annotations

import asyncio
import copy
import inspect
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.private_work.errors import PrivateWorkNotFound, PrivateWorkUnavailable


def _config(thread_id: str, **configurable: object) -> dict[str, object]:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
            **configurable,
        }
    }


@pytest_asyncio.fixture
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


async def _create_thread(seed: M4ThreadSeed, thread_id: str = "checkpoint-thread"):
    from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef

    async with seed.factory() as session:
        async with session.begin():
            return await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )


async def _raw_checkpoint(
    raw: InMemorySaver,
    thread_id: str,
    marker: object = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {"source": "input", "step": -1, "parents": {}}
    if marker is not None:
        metadata["deerflow_private_scope"] = marker
    return await raw.aput(_config(thread_id), empty_checkpoint(), metadata, {})


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.parametrize(
    "marker",
    [
        None,
        {"project_id": "forged", "owner_user_id": "forged"},
    ],
)
async def test_scoped_checkpointer_rejects_missing_or_mismatched_marker(
    seed: M4ThreadSeed,
    marker: object,
) -> None:
    from app.private_work.checkpointer import ProjectScopedCheckpointer

    await _create_thread(seed)
    raw = InMemorySaver()
    await _raw_checkpoint(raw, "checkpoint-thread", marker)
    wrapper = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)

    with pytest.raises(PrivateWorkNotFound):
        await wrapper.aget_tuple(_config("checkpoint-thread"))


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_scoped_checkpointer_overwrites_client_scope_and_marker(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.checkpointer import PRIVATE_SCOPE_MARKER, ProjectScopedCheckpointer

    await _create_thread(seed)
    raw = InMemorySaver()
    wrapper = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
    client_config = _config(
        "checkpoint-thread",
        project_id=str(seed.project_b_owner_a.project_id),
        owner_user_id=str(seed.owner_b.user_id),
        deerflow_private_scope={"project_id": "forged", "owner_user_id": "forged"},
    )
    client_config["metadata"] = {
        "project_id": str(seed.project_b_owner_a.project_id),
        "__private_marker": "forged",
    }
    written_config = await wrapper.aput(
        client_config,
        empty_checkpoint(),
        {
            "source": "input",
            "step": -1,
            "parents": {},
            PRIVATE_SCOPE_MARKER: {"project_id": "forged", "owner_user_id": "forged"},
        },
        {},
    )

    raw_tuple = await raw.aget_tuple(written_config)
    assert raw_tuple is not None
    assert raw_tuple.metadata[PRIVATE_SCOPE_MARKER] == {
        "project_id": str(seed.owner_a.project_id),
        "owner_user_id": str(seed.owner_a.user_id),
    }
    serialized_config = repr(raw_tuple.config)
    assert str(seed.project_b_owner_a.project_id) not in serialized_config
    assert str(seed.owner_b.user_id) not in serialized_config
    assert "__private_marker" not in serialized_config
    assert await wrapper.aget_tuple(_config("checkpoint-thread")) is not None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_scoped_checkpointer_rejects_cross_owner_project_deleted_and_frozen(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.checkpointer import ProjectScopedCheckpointer

    await _create_thread(seed)
    raw = InMemorySaver()
    await _raw_checkpoint(
        raw,
        "checkpoint-thread",
        {
            "project_id": str(seed.owner_a.project_id),
            "owner_user_id": str(seed.owner_a.user_id),
        },
    )
    for context in (seed.owner_b, seed.project_b_owner_a):
        with pytest.raises(PrivateWorkNotFound):
            await ProjectScopedCheckpointer(raw, seed.factory).for_context(context).aget_tuple(_config("checkpoint-thread"))

    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE threads_meta SET frozen_at=now()
                WHERE thread_id='checkpoint-thread'"""
            )
        )
    with pytest.raises(PrivateWorkNotFound):
        await ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a).aget_tuple(_config("checkpoint-thread"))


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_scoped_checkpointer_validates_every_list_item_marker(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.checkpointer import ProjectScopedCheckpointer

    await _create_thread(seed)
    raw = InMemorySaver()
    config = await _raw_checkpoint(
        raw,
        "checkpoint-thread",
        {
            "project_id": str(seed.owner_a.project_id),
            "owner_user_id": str(seed.owner_a.user_id),
        },
    )
    await raw.aput(
        config,
        empty_checkpoint(),
        {
            "source": "loop",
            "step": 0,
            "parents": {},
            "deerflow_private_scope": {
                "project_id": str(seed.project_b_owner_a.project_id),
                "owner_user_id": str(seed.owner_a.user_id),
            },
        },
        {},
    )

    wrapper = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
    with pytest.raises(PrivateWorkNotFound):
        _ = [item async for item in wrapper.alist(_config("checkpoint-thread"))]


class _FailingDeleteSaver(InMemorySaver):
    def __init__(self, seed: M4ThreadSeed) -> None:
        super().__init__()
        self._seed = seed
        self.observed_invisible = False

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._seed.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """SELECT deleted_at,checkpoint_delete_status
                        FROM threads_meta WHERE thread_id=:thread_id"""
                    ),
                    {"thread_id": thread_id},
                )
            ).one()
        self.observed_invisible = row.deleted_at is not None and row.checkpoint_delete_status == "pending"
        raise RuntimeError("raw saver unavailable")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_scoped_checkpointer_delete_hides_thread_before_raw_delete_and_marks_retry(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.checkpointer import ProjectScopedCheckpointer

    record = await _create_thread(seed)
    raw = _FailingDeleteSaver(seed)
    await _raw_checkpoint(
        raw,
        "checkpoint-thread",
        {
            "project_id": str(seed.owner_a.project_id),
            "owner_user_id": str(seed.owner_a.user_id),
        },
    )
    wrapper = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)

    with pytest.raises(PrivateWorkUnavailable):
        await wrapper.adelete_thread("checkpoint-thread", expected_version=record.version)

    assert raw.observed_invisible is True
    async with seed.engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    """SELECT deleted_at,checkpoint_delete_status
                    FROM threads_meta WHERE thread_id='checkpoint-thread'"""
                )
            )
        ).one()
    assert row.deleted_at is not None
    assert row.checkpoint_delete_status == "retry_required"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_scoped_checkpointer_covers_sync_and_async_saver_surface(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.checkpointer import ProjectScopedCheckpointer

    await _create_thread(seed)
    raw = InMemorySaver()
    wrapper = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
    config = _config("checkpoint-thread")
    checkpoint = empty_checkpoint()
    async_config = await wrapper.aput(
        config,
        checkpoint,
        {"source": "input", "step": -1, "parents": {}},
        {},
    )
    await wrapper.aput_writes(async_config, [("messages", "async")], "async-task")
    assert await wrapper.aget(async_config) is not None
    assert await wrapper.aget_tuple(async_config) is not None
    assert [item async for item in wrapper.alist(config)]

    sync_tuple = await asyncio.to_thread(wrapper.get_tuple, async_config)
    assert sync_tuple is not None
    assert await asyncio.to_thread(wrapper.get, async_config) is not None
    assert await asyncio.to_thread(lambda: list(wrapper.list(config)))
    sync_config = await asyncio.to_thread(
        wrapper.put,
        async_config,
        copy.deepcopy(checkpoint),
        {"source": "loop", "step": 0, "parents": {}},
        {},
    )
    await asyncio.to_thread(
        wrapper.put_writes,
        sync_config,
        [("messages", "sync")],
        "sync-task",
    )
    raw.get_next_version = MagicMock(return_value="delegated-version")
    assert wrapper.get_next_version(None, None) == "delegated-version"
    raw.get_next_version.assert_called_once_with(None, None)


def test_project_modules_cannot_import_raw_checkpointer() -> None:
    from app.gateway import deps
    from app.private_work import checkpointer, thread_service

    assert "get_checkpointer" not in inspect.getsource(checkpointer)
    assert "get_checkpointer" not in inspect.getsource(thread_service)
    deps_source = inspect.getsource(deps)
    assert "def get_project_checkpointer" in deps_source
    assert "project_scoped_checkpointer.for_context" in deps_source
