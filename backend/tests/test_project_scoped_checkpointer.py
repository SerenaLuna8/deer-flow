from __future__ import annotations

import ast
import asyncio
import copy
import gc
import warnings
from pathlib import Path
from types import SimpleNamespace
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
    raw_config = await _raw_checkpoint(raw, "checkpoint-thread", marker)
    wrapper = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)

    with pytest.raises(PrivateWorkNotFound):
        await wrapper.aget_tuple(_config("checkpoint-thread"))
    with pytest.raises(PrivateWorkNotFound):
        await wrapper.aput_writes(
            raw_config,
            [("messages", "must not cross marker boundary")],
            "rejected-task",
        )


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
async def test_scoped_checkpointer_can_commit_inside_caller_held_authority_lock(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.checkpointer import PRIVATE_SCOPE_MARKER, ProjectScopedCheckpointer
    from app.private_work.thread_repository import PrivateThreadRepository

    record = await _create_thread(seed, "caller-authorized-checkpoint-thread")
    raw = InMemorySaver()
    wrapper = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)

    async with seed.factory() as session:
        async with session.begin():
            locked = await PrivateThreadRepository(session).get(
                scope=seed.owner_a.resource_scope,
                thread_id=record.thread_id,
                lock=True,
            )
            assert locked is not None
            written = await wrapper.aput_already_authorized(
                _config(
                    record.thread_id,
                    project_id="forged-project",
                    owner_user_id="forged-owner",
                ),
                empty_checkpoint(),
                {
                    "source": "update",
                    "step": 1,
                    "parents": {},
                    PRIVATE_SCOPE_MARKER: {
                        "project_id": "forged-project",
                        "owner_user_id": "forged-owner",
                    },
                },
                {},
                session=session,
            )

    item = await raw.aget_tuple(written)
    assert item is not None
    assert item.metadata[PRIVATE_SCOPE_MARKER] == {
        "project_id": str(seed.owner_a.project_id),
        "owner_user_id": str(seed.owner_a.user_id),
    }
    assert "forged-project" not in repr(item.config)

    async with seed.factory() as session:
        with pytest.raises(PrivateWorkUnavailable):
            await wrapper.aput_already_authorized(
                _config(record.thread_id),
                empty_checkpoint(),
                {"source": "update", "step": 2, "parents": {}},
                {},
                session=session,
            )


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


class _PauseableSaver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.pause_put = False
        self.pause_put_writes = False
        self.pause_get = False
        self.pause_list = False
        self.raw_entered = asyncio.Event()
        self.raw_release = asyncio.Event()
        self.delete_entered = asyncio.Event()

    async def aput(self, *args, **kwargs):
        if self.pause_put:
            self.raw_entered.set()
            await self.raw_release.wait()
        return await super().aput(*args, **kwargs)

    async def aput_writes(self, *args, **kwargs) -> None:
        if self.pause_put_writes:
            self.raw_entered.set()
            await self.raw_release.wait()
        await super().aput_writes(*args, **kwargs)

    async def aget_tuple(self, *args, **kwargs):
        if self.pause_get:
            self.raw_entered.set()
            await self.raw_release.wait()
        return await super().aget_tuple(*args, **kwargs)

    async def alist(self, *args, **kwargs):
        if self.pause_list:
            self.raw_entered.set()
            await self.raw_release.wait()
        async for item in super().alist(*args, **kwargs):
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_entered.set()
        await super().adelete_thread(thread_id)


class _MissingCheckpointBarrierSaver(InMemorySaver):
    """Expose LangGraph's valid pending-writes-before-checkpoint ordering."""

    def __init__(self, checkpoint_id: str) -> None:
        super().__init__()
        self._checkpoint_id = checkpoint_id
        self.missing_read = asyncio.Event()
        self.release_missing_read = asyncio.Event()

    async def aget_tuple(self, config, *args, **kwargs):
        item = await super().aget_tuple(config, *args, **kwargs)
        configurable = config.get("configurable", {})
        if item is None and configurable.get("checkpoint_id") == self._checkpoint_id:
            self.missing_read.set()
            await self.release_missing_read.wait()
        return item


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_scoped_put_writes_allows_pending_writes_before_checkpoint_put(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.checkpointer import ProjectScopedCheckpointer

    record = await _create_thread(seed, "checkpoint-handoff-thread")
    checkpoint = empty_checkpoint()
    checkpoint_id = checkpoint["id"]
    raw = _MissingCheckpointBarrierSaver(checkpoint_id)
    wrapper = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
    pending_config = _config(record.thread_id, checkpoint_id=checkpoint_id)

    writes_task = asyncio.create_task(
        wrapper.aput_writes(
            pending_config,
            [("messages", "concurrent")],
            "concurrent-task",
        )
    )
    await asyncio.wait_for(raw.missing_read.wait(), timeout=2)
    put_task = asyncio.create_task(
        wrapper.aput(
            _config(record.thread_id),
            checkpoint,
            {"source": "loop", "step": 0, "parents": {}},
            {},
        )
    )
    await asyncio.sleep(0)
    raw.release_missing_read.set()

    written = await asyncio.wait_for(put_task, timeout=2)
    await asyncio.wait_for(writes_task, timeout=2)
    assert await raw.aget_tuple(written) is not None


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
@pytest.mark.parametrize("operation", ["put", "put_writes"])
async def test_scoped_writer_and_delete_are_serialized_by_postgres_thread_lock(
    seed: M4ThreadSeed,
    operation: str,
) -> None:
    from app.private_work.checkpointer import ProjectScopedCheckpointer

    record = await _create_thread(seed, f"concurrent-{operation}-thread")
    raw = _PauseableSaver()
    config = await _raw_checkpoint(
        raw,
        record.thread_id,
        {
            "project_id": str(seed.owner_a.project_id),
            "owner_user_id": str(seed.owner_a.user_id),
        },
    )
    wrapper = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
    if operation == "put":
        raw.pause_put = True
        writer = asyncio.create_task(
            wrapper.aput(
                config,
                empty_checkpoint(),
                {"source": "loop", "step": 0, "parents": {}},
                {},
            )
        )
    else:
        raw.pause_put_writes = True
        writer = asyncio.create_task(
            wrapper.aput_writes(
                config,
                [("messages", "concurrent")],
                "concurrent-task",
            )
        )

    await asyncio.wait_for(raw.raw_entered.wait(), timeout=2)
    deleter = asyncio.create_task(wrapper.adelete_thread(record.thread_id, expected_version=record.version))
    await asyncio.sleep(0.05)
    delete_reached_raw_early = raw.delete_entered.is_set()
    raw.raw_release.set()
    await writer
    await deleter

    assert delete_reached_raw_early is False
    assert await raw.aget_tuple(_config(record.thread_id)) is None
    async with seed.engine.connect() as connection:
        status = await connection.scalar(
            text(
                """SELECT checkpoint_delete_status FROM threads_meta
                WHERE thread_id=:thread_id"""
            ),
            {"thread_id": record.thread_id},
        )
    assert status == "complete"


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.parametrize("operation", ["get", "list"])
async def test_scoped_read_and_membership_revoke_are_serialized_and_revalidated(
    seed: M4ThreadSeed,
    operation: str,
) -> None:
    from app.private_work.checkpointer import ProjectScopedCheckpointer

    record = await _create_thread(seed, f"concurrent-{operation}-thread")
    raw = _PauseableSaver()
    await _raw_checkpoint(
        raw,
        record.thread_id,
        {
            "project_id": str(seed.owner_a.project_id),
            "owner_user_id": str(seed.owner_a.user_id),
        },
    )
    wrapper = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
    if operation == "get":
        raw.pause_get = True
        reader = asyncio.create_task(wrapper.aget_tuple(_config(record.thread_id)))
    else:
        raw.pause_list = True

        async def collect():
            return [item async for item in wrapper.alist(_config(record.thread_id))]

        reader = asyncio.create_task(collect())

    await asyncio.wait_for(raw.raw_entered.wait(), timeout=2)

    async def revoke_membership() -> None:
        async with seed.engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE project_memberships
                    SET status='removed', version=version+1
                    WHERE id=:membership_id"""
                ),
                {"membership_id": seed.owner_a.membership_id},
            )

    revoker = asyncio.create_task(revoke_membership())
    await asyncio.sleep(0.05)
    revoke_committed_early = revoker.done()
    raw.raw_release.set()
    await reader
    await revoker

    assert revoke_committed_early is False
    if operation == "get":
        raw.pause_get = False
        with pytest.raises(PrivateWorkNotFound):
            await wrapper.aget_tuple(_config(record.thread_id))
    else:
        raw.pause_list = False
        with pytest.raises(PrivateWorkNotFound):
            _ = [item async for item in wrapper.alist(_config(record.thread_id))]


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


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_scoped_checkpointer_covers_real_async_postgres_saver_and_loop_bridges(
    seed: M4ThreadSeed,
    migrated_postgres_database_url: str,
) -> None:
    from app.private_work.checkpointer import (
        PRIVATE_SCOPE_MARKER,
        ProjectScopedCheckpointer,
    )
    from deerflow.runtime.checkpointer.async_provider import make_checkpointer

    record = await _create_thread(seed, "postgres-saver-thread")
    bad_record = await _create_thread(seed, "postgres-marker-thread")
    provider_config = SimpleNamespace(
        database=SimpleNamespace(
            checkpointer_url=migrated_postgres_database_url.replace(
                "postgresql+asyncpg://",
                "postgresql://",
            )
        )
    )
    async with make_checkpointer(provider_config) as raw:
        wrapper = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
        config = _config(record.thread_id)
        checkpoint = empty_checkpoint()
        written = await wrapper.aput(
            config,
            checkpoint,
            {"source": "input", "step": -1, "parents": {}},
            {},
        )
        await wrapper.aput_writes(
            written,
            [("messages", "async-postgres")],
            "async-postgres-task",
        )
        assert await wrapper.aget(written) is not None
        assert await wrapper.aget_tuple(written) is not None
        assert [item async for item in wrapper.alist(config)]

        assert await asyncio.to_thread(wrapper.get, written) is not None
        assert await asyncio.to_thread(wrapper.get_tuple, written) is not None
        assert await asyncio.to_thread(lambda: list(wrapper.list(config)))
        sync_written = await asyncio.to_thread(
            wrapper.put,
            written,
            copy.deepcopy(checkpoint),
            {"source": "loop", "step": 0, "parents": {}},
            {},
        )
        await asyncio.to_thread(
            wrapper.put_writes,
            sync_written,
            [("messages", "sync-postgres")],
            "sync-postgres-task",
        )

        async def call_sync_from_another_running_loop():
            return wrapper.get_tuple(sync_written)

        bridged = await asyncio.to_thread(lambda: asyncio.run(call_sync_from_another_running_loop()))
        assert bridged is not None

        await raw.aput(
            _config(bad_record.thread_id),
            empty_checkpoint(),
            {
                "source": "input",
                "step": -1,
                "parents": {},
                PRIVATE_SCOPE_MARKER: {
                    "project_id": str(seed.project_b_owner_a.project_id),
                    "owner_user_id": str(seed.owner_a.user_id),
                },
            },
            {},
        )
        with pytest.raises(PrivateWorkNotFound):
            await wrapper.aget_tuple(_config(bad_record.thread_id))

        await asyncio.to_thread(
            wrapper.delete_thread,
            bad_record.thread_id,
            expected_version=bad_record.version,
        )
        assert await raw.aget_tuple(_config(bad_record.thread_id)) is None

        await wrapper.adelete_thread(
            record.thread_id,
            expected_version=record.version,
        )
        assert await raw.aget_tuple(_config(record.thread_id)) is None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_scoped_checkpointer_owner_loop_sync_misuse_has_no_unawaited_coroutine_warning(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.checkpointer import ProjectScopedCheckpointer

    await _create_thread(seed, "owner-loop-sync-thread")
    wrapper = ProjectScopedCheckpointer(InMemorySaver(), seed.factory).for_context(seed.owner_a)

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        with pytest.raises(PrivateWorkUnavailable):
            wrapper.get_tuple(_config("owner-loop-sync-thread"))
        gc.collect()

    assert not [warning for warning in captured if issubclass(warning.category, RuntimeWarning) and "was never awaited" in str(warning.message)]


def _attribute_path(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def test_project_modules_cannot_import_raw_checkpointer() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    restricted = sorted(
        {
            *app_root.joinpath("private_work").rglob("*.py"),
            *app_root.joinpath("projects").rglob("*.py"),
        }
    )
    forbidden_deps = {"get_checkpointer", "get_run_context"}
    violations: list[str] = []

    for path in restricted:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.gateway.deps":
                        violations.append(f"{path.relative_to(app_root)}:{node.lineno} imports gateway deps module")
            elif isinstance(node, ast.ImportFrom) and node.module == "app.gateway.deps":
                for alias in node.names:
                    if alias.name in forbidden_deps:
                        violations.append(f"{path.relative_to(app_root)}:{node.lineno} imports {alias.name}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in forbidden_deps:
                    violations.append(f"{path.relative_to(app_root)}:{node.lineno} calls raw dependency")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value in forbidden_deps:
                violations.append(f"{path.relative_to(app_root)}:{node.lineno} resolves raw dependency dynamically")

    # Raw app-state access has one exact infrastructure allowlist: deps.py
    # constructs and serves the legacy saver. Every current/future project
    # module must go through get_project_checkpointer instead.
    raw_allowlist = {app_root / "gateway" / "deps.py"}
    for path in sorted(app_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            raw_access = False
            if isinstance(node, ast.Attribute):
                attr_path = _attribute_path(node)
                raw_access = node.attr == "_raw_checkpointer" or attr_path[-2:] == (
                    "state",
                    "checkpointer",
                )
            elif (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value in {"_raw_checkpointer", "checkpointer"}
            ):
                raw_access = _attribute_path(node.args[0])[-1:] == ("state",)
            if raw_access and path not in raw_allowlist:
                violations.append(f"{path.relative_to(app_root)}:{node.lineno} accesses raw app state")

    assert violations == []

    deps_tree = ast.parse((app_root / "gateway" / "deps.py").read_text(encoding="utf-8"))
    assert any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "get_project_checkpointer" for node in ast.walk(deps_tree))
