from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.errors import PrivateWorkForbidden, PrivateWorkUnavailable
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.sandbox.exceptions import SandboxRuntimeError

MIB = 1024 * 1024


def _local_workspace_sandbox(root: Path):
    from deerflow.sandbox.local.local_sandbox import LocalSandbox, PathMapping

    root.mkdir(parents=True)
    return LocalSandbox(
        "local-private-test",
        [PathMapping("/mnt/user-data/workspace", str(root))],
    )


async def _chunks(payload: bytes) -> AsyncIterator[bytes]:
    for offset in range(0, len(payload), 131_071):
        await asyncio.sleep(0)
        yield payload[offset : offset + 131_071]


def _private_file_run_scope():
    from app.private_work.context import PrivateWorkContext
    from app.private_work.sandbox_files import PrivateFileRunScope
    from app.projects.capabilities import capabilities_for
    from app.projects.context import ProjectContext
    from app.projects.models import ProjectRole

    context = PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id=f"request-{uuid.uuid4()}",
        )
    )
    return PrivateFileRunScope(
        context,
        thread_id=f"thread-{uuid.uuid4()}",
        run_id=f"run-{uuid.uuid4()}",
    )


class MemoryAtomicSandbox:
    """Contract fake: it enforces bounded writes but never substitutes for provider tests."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self._writes: dict[str, tuple[str, bytearray]] = {}
        self.aborted: list[str] = []
        self.max_append = 0

    def begin_atomic_file(self, path: str) -> str:
        handle = uuid.uuid4().hex
        self._writes[handle] = (path, bytearray())
        return handle

    def append_atomic_file(self, handle: str, content: bytes) -> None:
        assert 0 < len(content) <= MIB
        self.max_append = max(self.max_append, len(content))
        self._writes[handle][1].extend(content)

    def publish_atomic_file(self, handle: str) -> None:
        path, content = self._writes.pop(handle)
        self.files[path] = bytes(content)

    def abort_atomic_file(self, handle: str) -> None:
        if handle in self._writes:
            self._writes.pop(handle)
            self.aborted.append(handle)

    def remove_file(self, path: str) -> None:
        self.files.pop(path, None)


class RevokingPublishBoundary:
    def __init__(self) -> None:
        self.revoked = False

    async def before_sandbox_write(self) -> None:
        from deerflow.sandbox.sandbox import AuthorizationRevoked

        if self.revoked:
            raise AuthorizationRevoked


class RevokingPublishSandbox(MemoryAtomicSandbox):
    def __init__(self, boundary: RevokingPublishBoundary) -> None:
        super().__init__()
        self._boundary = boundary

    def publish_atomic_file(self, handle: str) -> None:
        super().publish_atomic_file(handle)
        self._boundary.revoked = True


class BlockingBeginSandbox(MemoryAtomicSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.begin_started = threading.Event()
        self.allow_begin = threading.Event()

    def begin_atomic_file(self, path: str) -> str:
        handle = super().begin_atomic_file(path)
        self.begin_started.set()
        if not self.allow_begin.wait(2):
            raise TimeoutError("test did not release atomic begin")
        return handle


@pytest_asyncio.fixture()
async def projection_seed(migrated_postgres_database_url: str):
    from app.private_work.file_service import PrivateFileService

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"projection-{uuid.uuid4()}"
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
    payload = b"a" * MIB + b"tail"
    ready = await PrivateFileService(seed.factory).upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/report.bin",
        media_type="application/octet-stream",
        chunks=_chunks(payload),
    )
    try:
        yield seed, thread_id, ready, payload
    finally:
        await seed.engine.dispose()


def test_private_projection_root_contains_project_owner_and_thread(tmp_path: Path) -> None:
    from app.private_work.sandbox_files import private_projection_root

    scope = PrivateResourceScope(
        project_id=str(uuid.uuid4()),
        owner_user_id=str(uuid.uuid4()),
        membership_version=1,
    )
    result = private_projection_root(tmp_path, scope, "thread-safe")

    assert result.relative_to(tmp_path).as_posix() == (f"projects/{scope.project_id}/users/{scope.owner_user_id}/threads/thread-safe/user-data")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_restore_streams_ready_authority_in_bounded_chunks(projection_seed) -> None:
    from app.private_work.sandbox_files import (
        PrivateFileRunScope,
        PrivateSandboxFileProjection,
    )

    seed, thread_id, ready, payload = projection_seed
    sandbox = MemoryAtomicSandbox()
    projection = PrivateSandboxFileProjection(seed.factory)
    run_scope = PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=f"run-{uuid.uuid4()}")

    manifest = await projection.restore(run_scope, sandbox)

    assert sandbox.files["/mnt/user-data/uploads/report.bin"] == payload
    assert sandbox.max_append == MIB
    assert [entry.file_id for entry in manifest.entries] == [ready.id]
    assert manifest.entries[0].sha256 == hashlib.sha256(payload).hexdigest()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_restore_revalidates_membership_for_each_chunk_page(projection_seed) -> None:
    from unittest.mock import AsyncMock

    from app.private_work.sandbox_files import (
        PrivateFileRunScope,
        PrivateSandboxFileProjection,
    )

    seed, thread_id, _ready, _payload = projection_seed
    projection = PrivateSandboxFileProjection(seed.factory)
    projection._revalidator.require = AsyncMock(wraps=projection._revalidator.require)

    await projection.restore(
        PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=f"run-{uuid.uuid4()}"),
        MemoryAtomicSandbox(),
    )

    assert projection._revalidator.require.await_count == 2


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_restore_revoked_after_publish_removes_published_file(projection_seed) -> None:
    from app.private_work.sandbox_files import (
        PrivateFileRunScope,
        PrivateSandboxFileProjection,
    )
    from deerflow.sandbox.sandbox import AuthorizationRevoked

    seed, thread_id, _ready, _payload = projection_seed
    boundary = RevokingPublishBoundary()
    sandbox = RevokingPublishSandbox(boundary)

    with pytest.raises(AuthorizationRevoked):
        await PrivateSandboxFileProjection(seed.factory).restore(
            PrivateFileRunScope(
                seed.owner_a,
                thread_id=thread_id,
                run_id=f"run-{uuid.uuid4()}",
                authorization_boundary=boundary,
            ),
            sandbox,
        )

    assert sandbox.files == {}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_restore_cancelled_during_atomic_begin_aborts_returned_handle(
    projection_seed,
) -> None:
    from app.private_work.sandbox_files import (
        PrivateFileRunScope,
        PrivateSandboxFileProjection,
    )

    seed, thread_id, _ready, _payload = projection_seed
    sandbox = BlockingBeginSandbox()
    task = asyncio.create_task(
        PrivateSandboxFileProjection(seed.factory).restore(
            PrivateFileRunScope(
                seed.owner_a,
                thread_id=thread_id,
                run_id=f"run-{uuid.uuid4()}",
            ),
            sandbox,
        )
    )
    assert await asyncio.to_thread(sandbox.begin_started.wait, 2)
    task.cancel()
    sandbox.allow_begin.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sandbox._writes == {}
    assert len(sandbox.aborted) == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_private_authority_finalizer_consumes_only_registered_presented_paths(
    projection_seed,
) -> None:
    from unittest.mock import AsyncMock

    from app.private_work.sandbox_files import (
        AuthorityManifest,
        PrivateFileRunScope,
        PrivateRunFileAuthority,
    )

    seed, thread_id, _ready, _payload = projection_seed
    run_scope = PrivateFileRunScope(
        seed.owner_a,
        thread_id=thread_id,
        run_id=f"run-{uuid.uuid4()}",
    )
    manifest = AuthorityManifest(entries=())
    sandbox = object()
    lease = SimpleNamespace(sandbox_id="private-sandbox")
    provider = SimpleNamespace(
        acquire_private_async=AsyncMock(return_value=lease),
        get=lambda sandbox_id: sandbox if sandbox_id == lease.sandbox_id else None,
        release_private_async=AsyncMock(),
    )
    projection = SimpleNamespace(restore=AsyncMock(return_value=manifest))
    finalizer = SimpleNamespace(finalize=AsyncMock(return_value=SimpleNamespace()))
    authority = PrivateRunFileAuthority(
        run_scope,
        projection,
        finalizer,
        provider=provider,
    )

    await authority.restore()
    authority.record_presented_paths(
        (
            "/mnt/user-data/outputs/report.md",
            "/mnt/user-data/outputs/report.md",
        )
    )
    await authority.finalize()

    finalizer.finalize.assert_awaited_once_with(
        run_scope,
        manifest,
        sandbox,
        presented_paths=("/mnt/user-data/outputs/report.md",),
    )


@pytest.mark.anyio
async def test_private_authority_failed_release_preserves_state_for_retry() -> None:
    from unittest.mock import AsyncMock

    from app.private_work.sandbox_files import PrivateRunFileAuthority
    from deerflow.file_authority import AuthorityManifest

    run_scope = _private_file_run_scope()
    manifest = AuthorityManifest(entries=(), run_id=run_scope.run_id)
    sandbox = object()
    lease = SimpleNamespace(sandbox_id="private-release-retry")
    provider = SimpleNamespace(
        acquire_private_async=AsyncMock(return_value=lease),
        get=lambda sandbox_id: sandbox if sandbox_id == lease.sandbox_id else None,
        release_private_async=AsyncMock(side_effect=[RuntimeError("transient destroy failure"), None]),
    )
    projection = SimpleNamespace(restore=AsyncMock(return_value=manifest))
    finalizer = SimpleNamespace(finalize=AsyncMock(return_value=SimpleNamespace()))
    authority = PrivateRunFileAuthority(
        run_scope,
        projection,
        finalizer,
        provider=provider,
    )
    await authority.restore()
    authority.record_presented_paths(("/mnt/user-data/outputs/report.md",))

    with pytest.raises(RuntimeError, match="transient destroy failure"):
        await authority.release()

    assert authority.sandbox_id == lease.sandbox_id
    assert authority.manifest is manifest
    await authority.finalize()
    finalizer.finalize.assert_awaited_once_with(
        run_scope,
        manifest,
        sandbox,
        presented_paths=("/mnt/user-data/outputs/report.md",),
    )

    await authority.release()

    assert authority.sandbox_id is None
    assert authority.manifest is None
    assert provider.release_private_async.await_args_list == [
        ((lease,),),
        ((lease,),),
    ]


@pytest.mark.anyio
async def test_private_authority_concurrent_release_destroys_lease_once() -> None:
    from unittest.mock import AsyncMock

    from app.private_work.sandbox_files import PrivateRunFileAuthority
    from deerflow.file_authority import AuthorityManifest

    run_scope = _private_file_run_scope()
    manifest = AuthorityManifest(entries=(), run_id=run_scope.run_id)
    sandbox = object()
    lease = SimpleNamespace(sandbox_id="private-release-concurrent")
    destroy_started = asyncio.Event()
    allow_destroy = asyncio.Event()

    async def destroy(_lease) -> None:
        destroy_started.set()
        await allow_destroy.wait()

    provider = SimpleNamespace(
        acquire_private_async=AsyncMock(return_value=lease),
        get=lambda sandbox_id: sandbox if sandbox_id == lease.sandbox_id else None,
        release_private_async=AsyncMock(side_effect=destroy),
    )
    authority = PrivateRunFileAuthority(
        run_scope,
        SimpleNamespace(restore=AsyncMock(return_value=manifest)),
        SimpleNamespace(finalize=AsyncMock()),
        provider=provider,
    )
    await authority.restore()

    first = asyncio.create_task(authority.release())
    await destroy_started.wait()
    second = asyncio.create_task(authority.release())
    await asyncio.sleep(0)
    assert provider.release_private_async.await_count == 1

    allow_destroy.set()
    await asyncio.gather(first, second)

    provider.release_private_async.assert_awaited_once_with(lease)
    assert authority.sandbox_id is None
    assert authority.manifest is None


@pytest.mark.anyio
async def test_worker_ignores_current_values_and_historical_checkpoint_artifacts() -> None:
    from unittest.mock import AsyncMock

    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.worker import RunContext, run_agent

    checkpoint = SimpleNamespace(
        config={"configurable": {"thread_id": "thread-trusted-registry"}},
        checkpoint={
            "channel_values": {
                "artifacts": ["/mnt/user-data/outputs/historical.txt"],
            }
        },
        metadata={},
        pending_writes=[],
    )
    checkpointer = SimpleNamespace(aget_tuple=AsyncMock(return_value=checkpoint))
    authority = SimpleNamespace(
        restore=AsyncMock(),
        finalize=AsyncMock(),
        mark_failed=AsyncMock(),
        release=AsyncMock(),
    )
    manager = RunManager()
    record = await manager.create("thread-trusted-registry")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield {
                "messages": [],
                "artifacts": ["/mnt/user-data/outputs/forged-current.txt"],
            }

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(checkpointer=checkpointer, file_authority=authority),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={"artifacts": ["/mnt/user-data/outputs/request.txt"]},
        config={},
    )

    authority.finalize.assert_awaited_once_with()


@pytest.mark.anyio
async def test_worker_private_recorder_uses_committed_result_without_host_scan(
    monkeypatch,
) -> None:
    from unittest.mock import AsyncMock

    from deerflow.runtime.events.store.memory import MemoryRunEventStore
    from deerflow.runtime.runs import worker as worker_module
    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.worker import RunContext, run_agent
    from deerflow.workspace_changes.api import get_workspace_changes_response
    from deerflow.workspace_changes.types import (
        WORKSPACE_CHANGES_EVENT_TYPE,
        WORKSPACE_CHANGES_METADATA_KEY,
    )

    capture = AsyncMock(side_effect=AssertionError("private run scanned host workspace"))
    legacy_record = AsyncMock(side_effect=AssertionError("private run used legacy workspace recorder"))
    monkeypatch.setattr(worker_module, "capture_workspace_snapshot", capture)
    monkeypatch.setattr(worker_module, "record_workspace_changes", legacy_record)
    workspace_changes = {
        "created": ["outputs/report.txt"],
        "modified": ["workspace/draft.txt"],
        "deleted": ["workspace/old.txt"],
    }
    authority = SimpleNamespace(
        restore=AsyncMock(),
        finalize=AsyncMock(return_value=SimpleNamespace(workspace_changes=workspace_changes)),
        mark_failed=AsyncMock(),
        release=AsyncMock(),
    )
    event_store = MemoryRunEventStore()
    manager = RunManager()
    record = await manager.create("thread-private-recorder")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield {"messages": []}

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            event_store=event_store,
            file_authority=authority,
        ),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={},
        config={},
    )

    capture.assert_not_awaited()
    legacy_record.assert_not_awaited()
    events = await event_store.list_events(record.thread_id, record.run_id)
    workspace_event = next(event for event in events if event["event_type"] == WORKSPACE_CHANGES_EVENT_TYPE)
    payload = workspace_event["metadata"][WORKSPACE_CHANGES_METADATA_KEY]
    assert payload["version"] == 1
    assert payload["summary"] == {
        "created": 1,
        "modified": 1,
        "deleted": 1,
        "additions": 0,
        "deletions": 0,
        "truncated": False,
    }
    assert [(item["path"], item["root"], item["status"]) for item in payload["files"]] == [
        ("/mnt/user-data/outputs/report.txt", "outputs", "created"),
        ("/mnt/user-data/workspace/draft.txt", "workspace", "modified"),
        ("/mnt/user-data/workspace/old.txt", "workspace", "deleted"),
    ]
    assert payload["limits"]["max_files"] == 200

    response = await get_workspace_changes_response(
        event_store,
        record.thread_id,
        record.run_id,
    )
    assert response == {"available": True, **payload}


@pytest.mark.anyio
async def test_worker_private_recorder_does_not_publish_empty_available_event() -> None:
    from unittest.mock import AsyncMock

    from deerflow.runtime.events.store.memory import MemoryRunEventStore
    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.worker import RunContext, run_agent
    from deerflow.workspace_changes.api import get_workspace_changes_response

    authority = SimpleNamespace(
        restore=AsyncMock(),
        finalize=AsyncMock(
            return_value=SimpleNamespace(
                workspace_changes={"created": [], "modified": [], "deleted": []},
            )
        ),
        mark_failed=AsyncMock(),
        release=AsyncMock(),
    )
    event_store = MemoryRunEventStore()
    manager = RunManager()
    record = await manager.create("thread-private-no-changes")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield {"messages": []}

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            event_store=event_store,
            file_authority=authority,
        ),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={},
        config={},
    )

    response = await get_workspace_changes_response(
        event_store,
        record.thread_id,
        record.run_id,
    )
    assert response["available"] is False


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_restore_removes_every_published_file_after_tampered_chunk(projection_seed) -> None:
    from app.private_work.file_service import PrivateFileService
    from app.private_work.sandbox_files import (
        PrivateFileRunScope,
        PrivateSandboxFileProjection,
    )

    seed, thread_id, ready, _payload = projection_seed
    await PrivateFileService(seed.factory).upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/first.txt",
        media_type="text/plain",
        chunks=_chunks(b"first"),
    )
    async with seed.engine.begin() as connection:
        await connection.execute(
            text("UPDATE file_chunks SET content=:bad WHERE file_id=:file_id AND chunk_index=0"),
            {"bad": b"x" * MIB, "file_id": ready.id},
        )

    sandbox = MemoryAtomicSandbox()
    projection = PrivateSandboxFileProjection(seed.factory)
    run_scope = PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=f"run-{uuid.uuid4()}")

    with pytest.raises(PrivateWorkUnavailable):
        await projection.restore(run_scope, sandbox)

    assert sandbox.files == {}
    assert sandbox._writes == {}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_viewer_cannot_restore_private_sandbox(projection_seed) -> None:
    from app.private_work.sandbox_files import (
        PrivateFileRunScope,
        PrivateSandboxFileProjection,
    )

    seed, thread_id, _ready, _payload = projection_seed
    with pytest.raises(PrivateWorkForbidden):
        await PrivateSandboxFileProjection(seed.factory).restore(
            PrivateFileRunScope(seed.viewer, thread_id=thread_id, run_id=f"run-{uuid.uuid4()}"),
            MemoryAtomicSandbox(),
        )


def test_local_provider_private_scope_uses_exact_project_root(tmp_path: Path, monkeypatch) -> None:
    from deerflow.config.app_config import AppConfig, set_app_config
    from deerflow.sandbox.local import LocalSandboxProvider

    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    provider = LocalSandboxProvider()
    scope = PrivateResourceScope(str(uuid.uuid4()), str(uuid.uuid4()), 1)

    lease = provider.acquire_private(
        "thread-1",
        scope=scope,
        user_id=scope.owner_user_id,
        run_id="run-1",
    )
    sandbox = provider.get(lease.sandbox_id)
    assert sandbox is not None
    sandbox.update_file("/mnt/user-data/workspace/probe.bin", b"ok")

    expected = tmp_path / "projects" / scope.project_id / "users" / scope.owner_user_id / "threads" / "thread-1" / "user-data" / "workspace" / "probe.bin"
    assert expected.read_bytes() == b"ok"
    assert not (tmp_path / "users" / scope.owner_user_id / "threads" / "thread-1").exists()


@pytest.mark.anyio
async def test_local_private_next_run_clears_uncommitted_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
    from deerflow.sandbox.local import LocalSandboxProvider

    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    provider = LocalSandboxProvider()
    scope = PrivateResourceScope(str(uuid.uuid4()), str(uuid.uuid4()), 1)
    try:
        first = await provider.acquire_private_async(
            "thread-stale",
            scope=scope,
            user_id=scope.owner_user_id,
            run_id="run-rollback",
        )
        first_sandbox = provider.get(first.sandbox_id)
        assert first_sandbox is not None
        first_sandbox.update_file(
            "/mnt/user-data/outputs/uncommitted.txt",
            b"must not survive",
        )
        await provider.release_private_async(first)

        second = await provider.acquire_private_async(
            "thread-stale",
            scope=scope,
            user_id=scope.owner_user_id,
            run_id="run-next",
        )
        second_sandbox = provider.get(second.sandbox_id)
        assert second_sandbox is not None
        assert (
            tuple(
                second_sandbox.list_secure_files(
                    "/mnt/user-data/outputs",
                    max_entries=1,
                )
            )
            == ()
        )
        await provider.release_private_async(second)
    finally:
        provider.reset()
        reset_app_config()


def test_local_private_rejects_second_active_run_for_same_scope_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
    from deerflow.sandbox.local import LocalSandboxProvider

    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    provider = LocalSandboxProvider()
    scope = PrivateResourceScope(str(uuid.uuid4()), str(uuid.uuid4()), 1)
    try:
        first = provider.acquire_private(
            "thread-active",
            scope=scope,
            user_id=scope.owner_user_id,
            run_id="run-first",
        )
        with pytest.raises(SandboxRuntimeError):
            provider.acquire_private(
                "thread-active",
                scope=scope,
                user_id=scope.owner_user_id,
                run_id="run-second",
            )
        provider.release(first.sandbox_id)
    finally:
        provider.reset()
        reset_app_config()


@pytest.mark.parametrize("replacement", ["mapping_root", "ancestor_symlink"])
def test_local_private_lease_rejects_mapping_identity_replacement(
    replacement: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
    from deerflow.sandbox.local import LocalSandboxProvider

    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    provider = LocalSandboxProvider()
    scope = PrivateResourceScope(str(uuid.uuid4()), str(uuid.uuid4()), 1)
    lease = provider.acquire_private(
        "thread-root-swap",
        scope=scope,
        user_id=scope.owner_user_id,
        run_id=f"run-{replacement}",
    )
    sandbox = provider.get(lease.sandbox_id)
    assert sandbox is not None
    workspace = tmp_path / lease.relative_root / "user-data" / "workspace"
    outside = tmp_path / "outside" / "threads" / "thread-root-swap" / "user-data" / "workspace"
    outside.mkdir(parents=True)
    (outside / "payload.bin").write_bytes(b"outside")
    try:
        if replacement == "mapping_root":
            workspace.rename(workspace.with_name("workspace-original"))
            workspace.mkdir()
            (workspace / "payload.bin").write_bytes(b"replacement")
        else:
            owner_root = tmp_path / "projects" / scope.project_id / "users" / scope.owner_user_id
            owner_root.rename(owner_root.with_name(f"{scope.owner_user_id}-original"))
            os.symlink(tmp_path / "outside", owner_root)

        with pytest.raises(OSError):
            sandbox.open_regular_file("/mnt/user-data/workspace/payload.bin")
        with pytest.raises(OSError):
            sandbox.begin_atomic_file("/mnt/user-data/workspace/new.bin")
        assert (outside / "payload.bin").read_bytes() == b"outside"
        assert not (outside / "new.bin").exists()

        provider.release(lease.sandbox_id)
        with pytest.raises(OSError):
            tuple(sandbox.list_secure_files("/mnt/user-data/workspace", max_entries=1))
    finally:
        provider.reset()
        reset_app_config()


@pytest.mark.parametrize("attack_kind", ["symlink", "hardlink", "fifo"])
def test_local_private_reset_fails_closed_on_link_or_special_file(
    attack_kind: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
    from deerflow.sandbox.local import LocalSandboxProvider

    if attack_kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO requires POSIX")
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    provider = LocalSandboxProvider()
    scope = PrivateResourceScope(str(uuid.uuid4()), str(uuid.uuid4()), 1)
    output_root = tmp_path / "projects" / scope.project_id / "users" / scope.owner_user_id / "threads" / "thread-hostile" / "user-data" / "outputs"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    hostile = output_root / "hostile"
    try:
        first = provider.acquire_private(
            "thread-hostile",
            scope=scope,
            user_id=scope.owner_user_id,
            run_id="run-first",
        )
        provider.release(first.sandbox_id)
        if attack_kind == "symlink":
            os.symlink(outside, hostile)
        elif attack_kind == "hardlink":
            os.link(outside, hostile)
        else:
            os.mkfifo(hostile)

        with pytest.raises(OSError):
            provider.acquire_private(
                "thread-hostile",
                scope=scope,
                user_id=scope.owner_user_id,
                run_id="run-rejected",
            )
        assert outside.read_bytes() == b"outside"
        hostile.unlink()

        recovered = provider.acquire_private(
            "thread-hostile",
            scope=scope,
            user_id=scope.owner_user_id,
            run_id="run-recovered",
        )
        provider.release(recovered.sandbox_id)
    finally:
        provider.reset()
        reset_app_config()


@pytest.mark.anyio
async def test_local_private_cancelled_acquire_joins_and_releases_lease(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
    from deerflow.sandbox.local import LocalSandboxProvider

    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    provider = LocalSandboxProvider()
    scope = PrivateResourceScope(str(uuid.uuid4()), str(uuid.uuid4()), 1)
    original_acquire = provider.acquire_private
    started = threading.Event()
    proceed = threading.Event()

    def slow_acquire(*args, **kwargs):
        started.set()
        assert proceed.wait(2)
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(provider, "acquire_private", slow_acquire)
    try:
        task = asyncio.create_task(
            provider.acquire_private_async(
                "thread-cancelled",
                scope=scope,
                user_id=scope.owner_user_id,
                run_id="run-cancelled",
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        monkeypatch.setattr(provider, "acquire_private", original_acquire)
        recovered = await provider.acquire_private_async(
            "thread-cancelled",
            scope=scope,
            user_id=scope.owner_user_id,
            run_id="run-recovered",
        )
        await provider.release_private_async(recovered)
    finally:
        proceed.set()
        provider.reset()
        reset_app_config()


def test_local_atomic_file_rejects_symlink_and_never_publishes_partial(tmp_path: Path, monkeypatch) -> None:
    from deerflow.config.app_config import AppConfig, set_app_config
    from deerflow.sandbox.local import LocalSandboxProvider

    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    provider = LocalSandboxProvider()
    scope = PrivateResourceScope(str(uuid.uuid4()), str(uuid.uuid4()), 1)
    lease = provider.acquire_private(
        "thread-2",
        scope=scope,
        user_id=scope.owner_user_id,
        run_id="run-2",
    )
    sandbox = provider.get(lease.sandbox_id)
    assert sandbox is not None

    user_data = tmp_path / "projects" / scope.project_id / "users" / scope.owner_user_id / "threads" / "thread-2" / "user-data"
    outside = tmp_path / "outside"
    outside.mkdir()
    (user_data / "workspace").mkdir(parents=True, exist_ok=True)
    os.symlink(outside, user_data / "workspace" / "escape")

    with pytest.raises(OSError):
        sandbox.begin_atomic_file("/mnt/user-data/workspace/escape/payload.bin")
    assert list(outside.iterdir()) == []

    handle = sandbox.begin_atomic_file("/mnt/user-data/workspace/good.bin")
    sandbox.append_atomic_file(handle, b"prefix")
    sandbox.abort_atomic_file(handle)
    assert not (user_data / "workspace" / "good.bin").exists()


def test_local_regular_reader_rejects_hardlinks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sandbox = _local_workspace_sandbox(workspace)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    os.link(outside, workspace / "linked.bin")

    handle = None
    try:
        handle = sandbox.open_regular_file("/mnt/user-data/workspace/linked.bin")
    except OSError:
        pass
    else:
        pytest.fail("private regular reader accepted a hardlinked inode")
    finally:
        if handle is not None:
            sandbox.close_regular_file(handle)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_local_regular_reader_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sandbox = _local_workspace_sandbox(workspace)
    fifo = workspace / "pipe"
    os.mkfifo(fifo)
    errors: list[BaseException] = []
    handles: list[str] = []

    def open_fifo() -> None:
        try:
            handles.append(sandbox.open_regular_file("/mnt/user-data/workspace/pipe"))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=open_fifo)
    worker.start()
    worker.join(0.2)
    blocked = worker.is_alive()
    if blocked:
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        worker.join(1)
    for handle in handles:
        sandbox.close_regular_file(handle)

    assert not blocked, "private FIFO validation blocked waiting for a writer"
    assert errors and isinstance(errors[0], OSError)


def test_local_regular_reader_rejects_ancestor_swap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    sandbox = _local_workspace_sandbox(workspace)
    parent = workspace / "parent"
    parent.mkdir()
    (parent / "payload.bin").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.bin").write_bytes(b"outside")
    moved = workspace / "moved"
    real_open = os.open
    swapped = False

    def swap_before_final_open(path, flags, *args, **kwargs):
        nonlocal swapped
        raw_path = os.fspath(path)
        if not swapped and (raw_path == str(parent / "payload.bin") or (raw_path == "payload.bin" and kwargs.get("dir_fd") is not None)):
            swapped = True
            parent.rename(moved)
            os.symlink(outside, parent)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_final_open)
    handle = None
    try:
        handle = sandbox.open_regular_file("/mnt/user-data/workspace/parent/payload.bin")
    except OSError:
        pass
    else:
        pytest.fail("private regular reader accepted an ancestor path swap")
    finally:
        if handle is not None:
            sandbox.close_regular_file(handle)


def test_local_atomic_writer_rejects_ancestor_swap_before_publish(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sandbox = _local_workspace_sandbox(workspace)
    parent = workspace / "parent"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    handle = sandbox.begin_atomic_file("/mnt/user-data/workspace/parent/payload.bin")
    sandbox.append_atomic_file(handle, b"private")
    temp_name = next(parent.iterdir()).name
    moved = workspace / "moved"
    parent.rename(moved)
    os.replace(moved / temp_name, outside / temp_name)
    os.symlink(outside, parent)

    with pytest.raises(OSError):
        sandbox.publish_atomic_file(handle)
    assert not (outside / "payload.bin").exists()


def test_local_atomic_writer_removes_published_target_when_parent_fsync_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    sandbox = _local_workspace_sandbox(workspace)
    real_fsync = os.fsync
    calls = 0

    def fail_parent_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("parent fsync failed")
        real_fsync(fd)

    handle = sandbox.begin_atomic_file("/mnt/user-data/workspace/payload.bin")
    sandbox.append_atomic_file(handle, b"private")
    monkeypatch.setattr(os, "fsync", fail_parent_fsync)

    with pytest.raises(OSError, match="parent fsync failed"):
        sandbox.publish_atomic_file(handle)

    assert not (workspace / "payload.bin").exists()


def test_local_atomic_writer_rolls_back_post_publish_close_error_and_retains_handle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    sandbox = _local_workspace_sandbox(workspace)
    handle = sandbox.begin_atomic_file("/mnt/user-data/workspace/payload.bin")
    sandbox.append_atomic_file(handle, b"private")
    writer_fd = sandbox._private_atomic_writers[handle]._fd
    real_close = os.close
    failed = False

    def fail_writer_close(fd: int) -> None:
        nonlocal failed
        if fd == writer_fd and not failed:
            failed = True
            raise OSError("writer close failed")
        real_close(fd)

    monkeypatch.setattr(os, "close", fail_writer_close)
    with pytest.raises(OSError, match="writer close failed"):
        sandbox.publish_atomic_file(handle)

    assert not (workspace / "payload.bin").exists()
    assert handle in sandbox._private_atomic_writers
    sandbox.abort_atomic_file(handle)
    assert sandbox._private_atomic_writers == {}


def test_local_atomic_writer_rolls_back_when_root_fd_close_fails_after_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    sandbox = _local_workspace_sandbox(workspace)
    handle = sandbox.begin_atomic_file("/mnt/user-data/workspace/payload.bin")
    sandbox.append_atomic_file(handle, b"private")
    root_fd = sandbox._private_atomic_writers[handle]._root_fd
    real_close = os.close
    failed = False

    def fail_root_close(fd: int) -> None:
        nonlocal failed
        if fd == root_fd and not failed:
            failed = True
            raise OSError("root close failed")
        real_close(fd)

    monkeypatch.setattr(os, "close", fail_root_close)
    with pytest.raises(OSError, match="root close failed"):
        sandbox.publish_atomic_file(handle)

    assert not (workspace / "payload.bin").exists()
    sandbox.abort_atomic_file(handle)


def test_local_atomic_writer_rolls_back_when_replace_publishes_then_raises(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    sandbox = _local_workspace_sandbox(workspace)
    handle = sandbox.begin_atomic_file("/mnt/user-data/workspace/payload.bin")
    sandbox.append_atomic_file(handle, b"private")
    real_replace = os.replace

    def publish_then_fail(*args, **kwargs) -> None:
        real_replace(*args, **kwargs)
        raise OSError("replace completion uncertain")

    monkeypatch.setattr(os, "replace", publish_then_fail)
    with pytest.raises(OSError, match="replace completion uncertain"):
        sandbox.publish_atomic_file(handle)

    assert not (workspace / "payload.bin").exists()
    sandbox.abort_atomic_file(handle)


def test_local_secure_scan_enforces_primitive_entry_limit_lazily(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sandbox = _local_workspace_sandbox(workspace)
    for index in range(4):
        (workspace / f"{index}.txt").write_text(str(index))

    entries = sandbox.list_secure_files(
        "/mnt/user-data/workspace",
        max_entries=2,
    )

    assert not isinstance(entries, tuple)
    iterator = iter(entries)
    first_two = {next(iterator).path, next(iterator).path}
    assert len(first_two) == 2
    with pytest.raises(OSError):
        next(iterator)


def test_local_secure_scan_closes_mapping_root_when_subdirectory_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    sandbox = _local_workspace_sandbox(workspace)
    fd_events: list[tuple[str, int]] = []
    real_open_root = sandbox._open_private_mapping_root
    real_close = os.close

    def record_open_root(mapping):
        fd = real_open_root(mapping)
        fd_events.append(("open-root", fd))
        return fd

    def record_close(fd: int) -> None:
        fd_events.append(("close", fd))
        real_close(fd)

    monkeypatch.setattr(sandbox, "_open_private_mapping_root", record_open_root)
    monkeypatch.setattr(os, "close", record_close)

    assert (
        tuple(
            sandbox.list_secure_files(
                "/mnt/user-data/workspace/missing",
                max_entries=1,
            )
        )
        == ()
    )
    root_events = [(index, fd) for index, (event, fd) in enumerate(fd_events) if event == "open-root"]
    assert root_events
    assert all(any(event == "close" and closed_fd == fd for event, closed_fd in fd_events[index + 1 :]) for index, fd in root_events)


@pytest.mark.parametrize(
    "sandbox_path",
    [
        "deerflow.community.aio_sandbox.aio_sandbox:AioSandbox",
        "deerflow.community.e2b_sandbox.e2b_sandbox:E2BSandbox",
        "deerflow.community.boxlite.box:BoxliteBox",
    ],
)
def test_remote_atomic_writer_implements_private_boundary(sandbox_path: str) -> None:
    """Every remote adapter owns the bounded writer boundary."""

    from deerflow.reflection import resolve_class
    from deerflow.sandbox.sandbox import Sandbox

    sandbox_class = resolve_class(sandbox_path, Sandbox)
    assert sandbox_class.open_atomic_writer is not Sandbox.open_atomic_writer


@pytest.mark.parametrize(
    "provider_path",
    [
        "deerflow.community.aio_sandbox:AioSandboxProvider",
        "deerflow.community.e2b_sandbox:E2BSandboxProvider",
        "deerflow.community.boxlite:BoxliteProvider",
    ],
)
def test_remote_provider_private_authority_uses_fresh_private_hook(
    provider_path: str,
) -> None:
    from deerflow.reflection import resolve_class
    from deerflow.sandbox.sandbox_provider import SandboxProvider

    provider_class = resolve_class(provider_path, SandboxProvider)
    assert provider_class._supports_isolated_private_file_authority is True
    assert provider_class._acquire_private_fresh is not SandboxProvider._acquire_private_fresh


@pytest.mark.parametrize(
    "sandbox_path",
    [
        "deerflow.community.aio_sandbox.aio_sandbox:AioSandbox",
        "deerflow.community.e2b_sandbox.e2b_sandbox:E2BSandbox",
        "deerflow.community.boxlite.box:BoxliteBox",
    ],
)
@pytest.mark.parametrize(
    ("method_name", "argument"),
    [
        ("list_secure_files", "/mnt/user-data/workspace"),
        ("open_regular_reader", "/mnt/user-data/workspace/input.bin"),
        ("open_atomic_writer", "/mnt/user-data/workspace/output.bin"),
        ("remove_path", "/mnt/user-data/workspace/output.bin"),
    ],
)
def test_remote_sandbox_implements_private_secure_io_contract(
    sandbox_path: str,
    method_name: str,
    argument: str,
) -> None:
    from deerflow.reflection import resolve_class
    from deerflow.sandbox.sandbox import Sandbox

    sandbox_class = resolve_class(sandbox_path, Sandbox)
    del argument
    assert getattr(sandbox_class, method_name) is not getattr(Sandbox, method_name)


@pytest.mark.anyio
async def test_worker_private_authority_owns_restore_finalize_release_order() -> None:
    from unittest.mock import AsyncMock

    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.worker import RunContext, run_agent

    order: list[str] = []

    async def release() -> None:
        current = await manager.get(record.run_id)
        assert current is not None
        assert current.finalizing is True
        order.append("release")

    authority = SimpleNamespace(
        restore=AsyncMock(side_effect=lambda: order.append("restore")),
        finalize=AsyncMock(side_effect=lambda: order.append("finalize")),
        mark_failed=AsyncMock(side_effect=lambda: order.append("failed")),
        release=AsyncMock(side_effect=release),
    )
    manager = RunManager()
    record = await manager.create("thread-lease")
    bridge = SimpleNamespace(publish=AsyncMock(), publish_end=AsyncMock(), cleanup=AsyncMock())

    class Agent:
        async def astream(self, *_args, **_kwargs):
            order.append("graph")
            yield {
                "messages": [],
                "artifacts": ["/mnt/user-data/outputs/report.txt"],
            }

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(checkpointer=None, file_authority=authority),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={},
        config={},
    )

    assert order == ["restore", "graph", "finalize", "release"]
    authority.finalize.assert_awaited_once_with()


@pytest.mark.anyio
@pytest.mark.parametrize("failure_stage", ["restore", "agent"])
async def test_worker_private_error_keeps_finalizing_until_authority_release(
    failure_stage: str,
) -> None:
    from unittest.mock import AsyncMock

    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.worker import RunContext, run_agent

    manager = RunManager()
    record = await manager.create(f"thread-private-error-barrier-{failure_stage}")
    release_observed_finalizing: list[bool] = []

    async def restore() -> None:
        if failure_stage == "restore":
            raise RuntimeError("restore failed")

    async def release() -> None:
        current = await manager.get(record.run_id)
        assert current is not None
        release_observed_finalizing.append(current.finalizing)

    authority = SimpleNamespace(
        restore=AsyncMock(side_effect=restore),
        finalize=AsyncMock(),
        mark_failed=AsyncMock(),
        release=AsyncMock(side_effect=release),
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            raise RuntimeError("agent failed")
            yield

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(checkpointer=None, file_authority=authority),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={},
        config={},
    )

    assert release_observed_finalizing == [True]
    persisted = await manager.get(record.run_id)
    assert persisted is not None
    assert persisted.finalizing is False


@pytest.mark.anyio
async def test_worker_repeated_cancel_waits_for_private_runtime_and_authority_cleanup() -> None:
    from unittest.mock import AsyncMock

    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.worker import RunContext, run_agent

    manager = RunManager()
    record = await manager.create("thread-private-repeated-cancel")
    graph_started = asyncio.Event()
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    cleanup_order: list[str] = []

    async def close_runtime() -> None:
        current = await manager.get(record.run_id)
        assert current is not None
        assert current.finalizing is True
        cleanup_order.append("runtime")

    async def release_authority() -> None:
        current = await manager.get(record.run_id)
        assert current is not None
        assert current.finalizing is True
        cleanup_order.append("release-start")
        release_started.set()
        await allow_release.wait()
        cleanup_order.append("release-complete")

    authority = SimpleNamespace(
        restore=AsyncMock(),
        finalize=AsyncMock(),
        mark_failed=AsyncMock(),
        release=AsyncMock(side_effect=release_authority),
    )
    private_runtime = SimpleNamespace(
        skill_root=Path("/private/skills"),
        aclose=AsyncMock(side_effect=close_runtime),
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            graph_started.set()
            await asyncio.Event().wait()
            yield

    worker = asyncio.create_task(
        run_agent(
            bridge,
            manager,
            record,
            ctx=RunContext(
                checkpointer=None,
                file_authority=authority,
                private_agent_runtime=private_runtime,
            ),
            agent_factory=lambda *, config, private_runtime: Agent(),
            graph_input={},
            config={},
        )
    )
    await graph_started.wait()
    worker.cancel()
    await release_started.wait()
    worker.cancel()
    await asyncio.sleep(0)
    assert not worker.done()

    allow_release.set()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert cleanup_order == ["runtime", "release-start", "release-complete"]
    persisted = await manager.get(record.run_id)
    assert persisted is not None
    assert persisted.finalizing is False


@pytest.mark.anyio
async def test_worker_retries_private_runtime_and_authority_cleanup_without_losing_success() -> None:
    from unittest.mock import AsyncMock

    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.schemas import RunStatus
    from deerflow.runtime.runs.worker import RunContext, run_agent

    runtime_attempts = 0
    release_attempts = 0

    async def close_runtime() -> None:
        nonlocal runtime_attempts
        runtime_attempts += 1
        if runtime_attempts == 1:
            raise RuntimeError("transient runtime cleanup")

    async def release_authority() -> None:
        nonlocal release_attempts
        release_attempts += 1
        if release_attempts == 1:
            raise RuntimeError("transient authority cleanup")

    authority = SimpleNamespace(
        restore=AsyncMock(),
        finalize=AsyncMock(),
        mark_failed=AsyncMock(),
        release=AsyncMock(side_effect=release_authority),
    )
    private_runtime = SimpleNamespace(
        skill_root=Path("/private/skills"),
        aclose=AsyncMock(side_effect=close_runtime),
    )
    manager = RunManager()
    record = await manager.create("thread-private-cleanup-retry")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield {"messages": []}

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            file_authority=authority,
            private_agent_runtime=private_runtime,
        ),
        agent_factory=lambda *, config, private_runtime: Agent(),
        graph_input={},
        config={},
    )

    persisted = await manager.get(record.run_id)
    assert persisted is not None
    assert persisted.status is RunStatus.success
    assert persisted.finalizing is False
    assert private_runtime.aclose.await_count == 2
    assert authority.release.await_count == 2


@pytest.mark.anyio
async def test_worker_exhausted_private_cleanup_records_error_and_keeps_barrier(
    caplog,
) -> None:
    from unittest.mock import AsyncMock

    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.schemas import RunStatus
    from deerflow.runtime.runs.worker import RunContext, run_agent

    authority = SimpleNamespace(
        restore=AsyncMock(),
        finalize=AsyncMock(),
        mark_failed=AsyncMock(),
        release=AsyncMock(side_effect=RuntimeError("persistent destroy failure")),
    )
    manager = RunManager()
    record = await manager.create("thread-private-cleanup-exhausted")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield {"messages": []}

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(checkpointer=None, file_authority=authority),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={},
        config={},
    )

    persisted = await manager.get(record.run_id)
    assert persisted is not None
    assert authority.release.await_count == 3
    assert persisted.status is RunStatus.error
    assert persisted.error == "Private run cleanup failed"
    assert persisted.finalizing is True
    assert "Private file authority cleanup failed" in caplog.text


@pytest.mark.anyio
async def test_worker_private_file_authority_owns_skill_mount_validation_and_release(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.worker import RunContext, run_agent

    manager = RunManager()
    record = await manager.create("thread-authority-mount")
    record.scope = PrivateResourceScope(str(uuid.uuid4()), str(uuid.uuid4()), 1)
    authority = SimpleNamespace(
        restore=AsyncMock(),
        finalize=AsyncMock(),
        mark_failed=AsyncMock(),
        release=AsyncMock(),
    )
    runtime = SimpleNamespace(
        skill_root=tmp_path,
        aclose=AsyncMock(),
    )
    provider = SimpleNamespace(
        validate_run_scoped_mounts=Mock(),
        release_run_scoped_mounts_async=AsyncMock(),
    )
    provider_factory = Mock(return_value=provider)
    monkeypatch.setattr(
        "deerflow.runtime.runs.worker.get_sandbox_provider",
        provider_factory,
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield {"messages": []}

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            file_authority=authority,
            private_agent_runtime=runtime,
            app_config=SimpleNamespace(skills=SimpleNamespace(container_path="/mnt/skills")),
        ),
        agent_factory=lambda *, config, private_runtime: Agent(),
        graph_input={},
        config={},
    )

    provider_factory.assert_not_called()
    provider.validate_run_scoped_mounts.assert_not_called()
    provider.release_run_scoped_mounts_async.assert_not_awaited()
    authority.release.assert_awaited_once_with()
    runtime.aclose.assert_awaited_once_with()


@pytest.mark.anyio
async def test_worker_finalizer_failure_prevents_success_and_records_failed() -> None:
    from unittest.mock import AsyncMock

    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.schemas import RunStatus
    from deerflow.runtime.runs.worker import RunContext, run_agent

    authority = SimpleNamespace(
        restore=AsyncMock(),
        finalize=AsyncMock(side_effect=RuntimeError("commit failed")),
        mark_failed=AsyncMock(),
        release=AsyncMock(),
    )
    manager = RunManager()
    record = await manager.create("thread-finalization")
    bridge = SimpleNamespace(publish=AsyncMock(), publish_end=AsyncMock(), cleanup=AsyncMock())

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield {"messages": []}

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(checkpointer=None, file_authority=authority),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={},
        config={},
    )

    persisted = await manager.get(record.run_id)
    assert persisted is not None
    assert persisted.status is RunStatus.error
    authority.mark_failed.assert_awaited_once()
    authority.release.assert_awaited_once()


@pytest.mark.anyio
async def test_worker_cancel_during_private_finalize_joins_then_rethrows() -> None:
    from unittest.mock import AsyncMock

    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.schemas import RunStatus
    from deerflow.runtime.runs.worker import RunContext, run_agent

    finalize_started = asyncio.Event()
    allow_commit = asyncio.Event()
    order: list[str] = []

    async def finalize() -> None:
        order.append("finalize-start")
        finalize_started.set()
        await allow_commit.wait()
        order.append("finalize-commit")

    async def release() -> None:
        order.append("release")

    authority = SimpleNamespace(
        restore=AsyncMock(),
        finalize=AsyncMock(side_effect=finalize),
        mark_failed=AsyncMock(),
        release=AsyncMock(side_effect=release),
    )
    manager = RunManager()
    record = await manager.create("thread-cancel-finalization")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield {"messages": []}

    worker = asyncio.create_task(
        run_agent(
            bridge,
            manager,
            record,
            ctx=RunContext(checkpointer=None, file_authority=authority),
            agent_factory=lambda **_kwargs: Agent(),
            graph_input={},
            config={},
        )
    )
    await finalize_started.wait()
    worker.cancel()
    await asyncio.sleep(0)
    allow_commit.set()

    with pytest.raises(asyncio.CancelledError):
        await worker

    persisted = await manager.get(record.run_id)
    assert persisted is not None
    assert persisted.status is RunStatus.interrupted
    assert order == ["finalize-start", "finalize-commit", "release"]
    authority.mark_failed.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("terminal_action", ["rollback", "authorization_revoked"])
async def test_worker_private_noncommit_marks_durable_finalization_failed_before_terminal_status(
    terminal_action: str,
) -> None:
    from unittest.mock import AsyncMock

    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.schemas import RunStatus
    from deerflow.runtime.runs.worker import RunContext, run_agent
    from deerflow.sandbox.sandbox import AuthorizationRevoked

    manager = RunManager()
    record = await manager.create(f"thread-{terminal_action}")
    observed_statuses: list[RunStatus] = []

    async def mark_failed() -> None:
        current = await manager.get(record.run_id)
        assert current is not None
        observed_statuses.append(current.status)

    authority = SimpleNamespace(
        restore=AsyncMock(),
        finalize=AsyncMock(),
        mark_failed=AsyncMock(side_effect=mark_failed),
        release=AsyncMock(),
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            if terminal_action == "authorization_revoked":
                raise AuthorizationRevoked
            record.abort_action = "rollback"
            record.abort_event.set()
            if False:
                yield None

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(checkpointer=None, file_authority=authority),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={},
        config={},
    )

    authority.finalize.assert_not_awaited()
    authority.mark_failed.assert_awaited_once_with()
    assert observed_statuses == [RunStatus.running]
    persisted = await manager.get(record.run_id)
    assert persisted is not None
    assert persisted.status in {RunStatus.error, RunStatus.interrupted}


@pytest.mark.anyio
@pytest.mark.parametrize("terminal_action", ["exception", "authorization_revoked"])
async def test_worker_private_marker_failure_preserves_original_terminal_error_and_release(
    terminal_action: str,
) -> None:
    from unittest.mock import AsyncMock

    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.schemas import RunStatus
    from deerflow.runtime.runs.worker import RunContext, run_agent
    from deerflow.sandbox.sandbox import (
        AUTHORIZATION_REVOKED_REASON,
        AuthorizationRevoked,
    )

    manager = RunManager()
    record = await manager.create(f"thread-marker-failure-{terminal_action}")
    authority = SimpleNamespace(
        restore=AsyncMock(),
        finalize=AsyncMock(),
        mark_failed=AsyncMock(side_effect=RuntimeError("marker unavailable")),
        release=AsyncMock(),
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            if terminal_action == "authorization_revoked":
                raise AuthorizationRevoked
            raise RuntimeError("original agent failure")
            yield

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(checkpointer=None, file_authority=authority),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={},
        config={},
    )

    persisted = await manager.get(record.run_id)
    assert persisted is not None
    expected_error = AUTHORIZATION_REVOKED_REASON if terminal_action == "authorization_revoked" else "original agent failure"
    expected_status = RunStatus.interrupted if terminal_action == "authorization_revoked" else RunStatus.error
    assert persisted.status is expected_status
    assert persisted.error == expected_error
    authority.mark_failed.assert_awaited_once_with()
    authority.release.assert_awaited_once_with()
    bridge.publish.assert_any_await(
        record.run_id,
        "error",
        {
            "message": expected_error,
            "name": (AUTHORIZATION_REVOKED_REASON if terminal_action == "authorization_revoked" else "RuntimeError"),
        },
    )
