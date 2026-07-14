from __future__ import annotations

import inspect

import pytest
import pytest_asyncio
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)


@pytest_asyncio.fixture
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


def _service(seed: M4ThreadSeed, raw=None, *, branch_copy_hook=None):
    from app.private_work.checkpointer import ProjectScopedCheckpointer
    from app.private_work.thread_service import PrivateThreadService

    raw_saver = raw or InMemorySaver()
    scoped = ProjectScopedCheckpointer(raw_saver, seed.factory)
    return (
        PrivateThreadService(
            seed.factory,
            scoped,
            branch_copy_hook=branch_copy_hook,
        ),
        raw_saver,
        scoped,
    )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_create_search_patch_and_delete(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    service, raw, _scoped = _service(seed)
    created = await service.create(
        seed.owner_a,
        thread_id="service-thread",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
        display_name="Service Thread",
        metadata={"topic": "private"},
    )
    assert created.version == 1
    assert created.agent_asset_id == seed.project_agent_id
    assert [item.thread_id for item in await service.search(seed.owner_a)] == ["service-thread"]
    assert await service.get(seed.owner_b, "service-thread") is None

    raw_tuple = await raw.aget_tuple({"configurable": {"thread_id": "service-thread", "checkpoint_ns": ""}})
    assert raw_tuple is not None
    assert raw_tuple.metadata["deerflow_private_scope"] == {
        "project_id": str(seed.owner_a.project_id),
        "owner_user_id": str(seed.owner_a.user_id),
    }

    patched = await service.patch(
        seed.owner_a,
        "service-thread",
        expected_version=created.version,
        display_name="Renamed Thread",
    )
    assert patched.display_name == "Renamed Thread"
    with pytest.raises(PrivateWorkConflict):
        await service.patch(
            seed.owner_a,
            "service-thread",
            expected_version=created.version,
            display_name="Stale",
        )

    await service.delete(
        seed.owner_a,
        "service-thread",
        expected_version=patched.version,
    )
    assert await service.get(seed.owner_a, "service-thread") is None
    async with seed.engine.connect() as connection:
        status = await connection.scalar(
            text(
                """SELECT checkpoint_delete_status FROM threads_meta
                WHERE thread_id='service-thread'"""
            )
        )
    assert status == "complete"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_create_requires_capability_and_executable_agent(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import (
        PrivateThreadRepository,
        ThreadAgentRef,
    )

    service, _raw, _scoped = _service(seed)
    with pytest.raises(PrivateWorkForbidden):
        await service.create(
            seed.viewer,
            thread_id="viewer-thread",
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )

    async with seed.factory() as session:
        async with session.begin():
            viewer_thread = await PrivateThreadRepository(session).create(
                scope=seed.viewer.resource_scope,
                thread_id="viewer-owned-thread",
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
    await service.delete(
        seed.viewer,
        viewer_thread.thread_id,
        expected_version=viewer_thread.version,
    )
    assert await service.get(seed.viewer, viewer_thread.thread_id) is None

    owner_thread = await service.create(
        seed.owner_a,
        thread_id="owner-thread-hidden-from-viewer",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    with pytest.raises(PrivateWorkNotFound):
        await service.delete(
            seed.viewer,
            owner_thread.thread_id,
            expected_version=owner_thread.version,
        )

    with pytest.raises(PrivateWorkNotFound):
        await service.create(
            seed.owner_a,
            thread_id="wrong-project-agent-thread",
            agent=ThreadAgentRef(seed.project_b_agent_id, "project"),
        )

    system_thread = await service.create(
        seed.owner_a,
        thread_id="system-agent-thread",
        agent=ThreadAgentRef(seed.system_agent_id, "system"),
    )
    assert system_thread.agent_scope == "system"


class _FailingRootSaver(InMemorySaver):
    async def aput(self, *_args, **_kwargs):
        raise RuntimeError("root checkpoint unavailable")


class _WriteThenRaiseSaver(InMemorySaver):
    def __init__(self, *, cleanup_fails: bool = False) -> None:
        super().__init__()
        self.cleanup_fails = cleanup_fails

    async def aput(self, *args, **kwargs):
        await super().aput(*args, **kwargs)
        raise RuntimeError("checkpoint commit result was ambiguous")

    async def adelete_thread(self, thread_id: str) -> None:
        if self.cleanup_fails:
            raise RuntimeError("checkpoint cleanup unavailable")
        await super().adelete_thread(thread_id)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_compensates_row_when_root_checkpoint_fails(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    service, _raw, _scoped = _service(seed, _FailingRootSaver())
    with pytest.raises(PrivateWorkUnavailable):
        await service.create(
            seed.owner_a,
            thread_id="failed-root-thread",
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )

    async with seed.engine.connect() as connection:
        count = await connection.scalar(
            text(
                """SELECT count(*) FROM threads_meta
                WHERE thread_id='failed-root-thread'"""
            )
        )
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_cleans_ambiguous_root_checkpoint_before_row(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    raw = _WriteThenRaiseSaver()
    service, _raw, _scoped = _service(seed, raw)
    with pytest.raises(PrivateWorkUnavailable):
        await service.create(
            seed.owner_a,
            thread_id="ambiguous-root-thread",
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )

    assert await raw.aget_tuple({"configurable": {"thread_id": "ambiguous-root-thread", "checkpoint_ns": ""}}) is None
    async with seed.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM threads_meta WHERE thread_id='ambiguous-root-thread'")) == 0


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_keeps_retry_tombstone_when_ambiguous_cleanup_fails(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    raw = _WriteThenRaiseSaver(cleanup_fails=True)
    service, _raw, _scoped = _service(seed, raw)
    with pytest.raises(PrivateWorkUnavailable):
        await service.create(
            seed.owner_a,
            thread_id="ambiguous-cleanup-thread",
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )

    async with seed.engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    """SELECT deleted_at, checkpoint_delete_status
                    FROM threads_meta WHERE thread_id='ambiguous-cleanup-thread'"""
                )
            )
        ).one()
    assert row.deleted_at is not None
    assert row.checkpoint_delete_status == "retry_required"

    with pytest.raises(PrivateWorkConflict):
        await service.create(
            seed.owner_a,
            thread_id="ambiguous-cleanup-thread",
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )


class _BranchCopyHook:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str, str]] = []

    async def copy_branch_authority(
        self,
        scope,
        source_thread_id: str,
        target_thread_id: str,
    ) -> None:
        self.calls.append((scope, source_thread_id, target_thread_id))

    async def rollback_branch_authority(
        self,
        scope,
        source_thread_id: str,
        target_thread_id: str,
    ) -> None:
        return None


class _FailingBranchCopyHook(_BranchCopyHook):
    def __init__(self) -> None:
        super().__init__()
        self.rollback_calls: list[tuple[object, str, str]] = []

    async def copy_branch_authority(
        self,
        scope,
        source_thread_id: str,
        target_thread_id: str,
    ) -> None:
        await super().copy_branch_authority(scope, source_thread_id, target_thread_id)
        raise RuntimeError("authority copy failed after a partial copy")

    async def rollback_branch_authority(
        self,
        scope,
        source_thread_id: str,
        target_thread_id: str,
    ) -> None:
        self.rollback_calls.append((scope, source_thread_id, target_thread_id))


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_branch_uses_database_authority_copy_hook_only(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    hook = _BranchCopyHook()
    service, _raw, scoped = _service(seed, branch_copy_hook=hook)
    source = await service.create(
        seed.owner_a,
        thread_id="branch-source",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
        display_name="Source",
    )
    source_saver = scoped.for_context(seed.owner_a)
    source_config = await source_saver.aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        empty_checkpoint(),
        {"source": "loop", "step": 0, "parents": {}},
        {},
    )
    checkpoint_id = source_config["configurable"]["checkpoint_id"]

    branch = await service.branch(
        seed.owner_a,
        source_thread_id=source.thread_id,
        target_thread_id="branch-target",
        checkpoint_id=checkpoint_id,
        expected_source_version=source.version,
        display_name="Branch",
    )

    assert branch.thread_id == "branch-target"
    assert branch.metadata["branch_parent_thread_id"] == source.thread_id
    assert hook.calls == [(seed.owner_a_scope, source.thread_id, branch.thread_id)]
    assert await service.get(seed.owner_a, branch.thread_id) == branch


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_branch_rolls_back_checkpoint_and_authority_hook(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    hook = _FailingBranchCopyHook()
    service, raw, scoped = _service(seed, branch_copy_hook=hook)
    source = await service.create(
        seed.owner_a,
        thread_id="failed-branch-source",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    source_config = await scoped.for_context(seed.owner_a).aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        empty_checkpoint(),
        {"source": "loop", "step": 0, "parents": {}},
        {},
    )

    with pytest.raises(PrivateWorkUnavailable):
        await service.branch(
            seed.owner_a,
            source_thread_id=source.thread_id,
            target_thread_id="failed-branch-target",
            checkpoint_id=source_config["configurable"]["checkpoint_id"],
            expected_source_version=source.version,
        )

    assert hook.rollback_calls == [(seed.owner_a_scope, source.thread_id, "failed-branch-target")]
    assert await raw.aget_tuple({"configurable": {"thread_id": "failed-branch-target", "checkpoint_ns": ""}}) is None
    async with seed.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM threads_meta WHERE thread_id='failed-branch-target'")) == 0


def test_private_thread_service_does_not_read_host_thread_directories() -> None:
    from app.private_work.thread_service import PrivateThreadService

    source = inspect.getsource(PrivateThreadService)
    assert "get_paths" not in source
    assert "shutil" not in source
    assert "sandbox_user_data_dir" not in source
