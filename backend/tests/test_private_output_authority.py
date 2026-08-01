from __future__ import annotations

import asyncio
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkInvalid, PrivateWorkUnavailable
from app.private_work.sandbox_files import (
    PrivateFileRunScope,
    PrivateRunFileAuthority,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.file_authority import AuthorityManifest
from deerflow.sandbox.sandbox import AuthorizationRevoked

MIB = 1024 * 1024


def _run_scope(boundary: object) -> PrivateFileRunScope:
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
        authorization_boundary=boundary,
    )


class _MemoryAtomicSandbox:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self._writes: dict[str, tuple[str, bytearray]] = {}
        self.aborted: list[str] = []
        self.removed: list[str] = []
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
        self.files.pop(path)
        self.removed.append(path)


async def _restored_authority(
    boundary: object,
) -> tuple[PrivateRunFileAuthority, _MemoryAtomicSandbox]:
    scope = _run_scope(boundary)
    sandbox = _MemoryAtomicSandbox()
    lease = SimpleNamespace(sandbox_id=f"sandbox-{uuid.uuid4()}")
    provider = SimpleNamespace(
        acquire_private_async=AsyncMock(return_value=lease),
        get=lambda sandbox_id: sandbox if sandbox_id == lease.sandbox_id else None,
        release_private_async=AsyncMock(),
    )
    authority = PrivateRunFileAuthority(
        scope,
        SimpleNamespace(restore=AsyncMock(return_value=AuthorityManifest(entries=(), run_id=scope.run_id))),
        SimpleNamespace(),
        provider=provider,
    )
    await authority.restore()
    return authority, sandbox


@pytest.mark.anyio
async def test_write_output_rechecks_boundary_and_publishes_bounded_chunks() -> None:
    class Boundary:
        def __init__(self) -> None:
            self.write_checks = 0

        async def before_sandbox_restore(self) -> None:
            return None

        async def before_sandbox_write(self) -> None:
            self.write_checks += 1

    boundary = Boundary()
    authority, sandbox = await _restored_authority(boundary)
    payload = b"a" * (MIB + 17)

    virtual_path = await authority.write_output(
        ".tool-results/result.txt",
        payload,
    )

    assert virtual_path.startswith("/mnt/user-data/outputs/.tool-results/result-")
    assert virtual_path.endswith(".txt")
    assert sandbox.files[virtual_path] == payload
    assert sandbox.max_append == MIB
    assert boundary.write_checks >= 4


@pytest.mark.anyio
async def test_write_internal_uses_workspace_not_presentable_outputs() -> None:
    class Boundary:
        async def before_sandbox_restore(self) -> None:
            return None

        async def before_sandbox_write(self) -> None:
            return None

    authority, sandbox = await _restored_authority(Boundary())

    virtual_path = await authority.write_internal(
        ".tool-results/result.json",
        b'{"ok":true}',
    )

    assert virtual_path.startswith("/mnt/user-data/workspace/.tool-results/result-")
    assert virtual_path.endswith(".json")
    assert sandbox.files[virtual_path] == b'{"ok":true}'


@pytest.mark.anyio
@pytest.mark.parametrize(
    "relative_path",
    [
        "",
        "/absolute.txt",
        "../escape.txt",
        "nested/../escape.txt",
        "nested//empty.txt",
        "nested\\windows.txt",
        ".deerflow-secret.txt",
        "nested/.deerflow-staging-file",
    ],
)
async def test_write_output_rejects_noncanonical_relative_paths(
    relative_path: str,
) -> None:
    class Boundary:
        async def before_sandbox_restore(self) -> None:
            return None

        async def before_sandbox_write(self) -> None:
            return None

    authority, sandbox = await _restored_authority(Boundary())

    with pytest.raises(PrivateWorkInvalid):
        await authority.write_output(relative_path, b"payload")

    assert sandbox.files == {}
    assert sandbox._writes == {}


@pytest.mark.anyio
async def test_write_output_aborts_atomic_file_when_authority_is_revoked() -> None:
    class Boundary:
        def __init__(self) -> None:
            self.write_checks = 0

        async def before_sandbox_restore(self) -> None:
            return None

        async def before_sandbox_write(self) -> None:
            self.write_checks += 1
            if self.write_checks == 2:
                raise AuthorizationRevoked

    authority, sandbox = await _restored_authority(Boundary())

    with pytest.raises(AuthorizationRevoked):
        await authority.write_output("capture.png", b"image")

    assert sandbox.files == {}
    assert sandbox._writes == {}
    assert len(sandbox.aborted) == 1


@pytest.mark.anyio
async def test_write_output_removes_published_file_when_final_authority_check_is_revoked() -> None:
    class Boundary:
        def __init__(self) -> None:
            self.write_checks = 0

        async def before_sandbox_restore(self) -> None:
            return None

        async def before_sandbox_write(self) -> None:
            self.write_checks += 1
            if self.write_checks == 4:
                raise AuthorizationRevoked

    authority, sandbox = await _restored_authority(Boundary())

    with pytest.raises(AuthorizationRevoked):
        await authority.write_output("capture.png", b"image")

    assert sandbox.files == {}
    assert sandbox._writes == {}
    assert len(sandbox.removed) == 1


@pytest.mark.anyio
async def test_write_output_removes_file_when_cancelled_during_atomic_publish() -> None:
    class Boundary:
        async def before_sandbox_restore(self) -> None:
            return None

        async def before_sandbox_write(self) -> None:
            return None

    class BlockingPublishSandbox(_MemoryAtomicSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.publish_started = threading.Event()
            self.allow_publish = threading.Event()

        def publish_atomic_file(self, handle: str) -> None:
            self.publish_started.set()
            assert self.allow_publish.wait(timeout=2)
            super().publish_atomic_file(handle)

    scope = _run_scope(Boundary())
    sandbox = BlockingPublishSandbox()
    lease = SimpleNamespace(sandbox_id=f"sandbox-{uuid.uuid4()}")
    authority = PrivateRunFileAuthority(
        scope,
        SimpleNamespace(restore=AsyncMock(return_value=AuthorityManifest(entries=(), run_id=scope.run_id))),
        SimpleNamespace(),
        provider=SimpleNamespace(
            acquire_private_async=AsyncMock(return_value=lease),
            get=lambda sandbox_id: sandbox if sandbox_id == lease.sandbox_id else None,
            release_private_async=AsyncMock(),
        ),
    )
    await authority.restore()

    write_task = asyncio.create_task(authority.write_output("capture.png", b"image"))
    assert await asyncio.to_thread(sandbox.publish_started.wait, 2)
    write_task.cancel()
    sandbox.allow_publish.set()

    with pytest.raises(asyncio.CancelledError):
        await write_task

    assert sandbox.files == {}
    assert sandbox._writes == {}
    assert len(sandbox.removed) == 1


@pytest.mark.anyio
async def test_write_output_retries_transient_post_publish_cleanup_failure() -> None:
    class Boundary:
        def __init__(self) -> None:
            self.write_checks = 0

        async def before_sandbox_restore(self) -> None:
            return None

        async def before_sandbox_write(self) -> None:
            self.write_checks += 1
            if self.write_checks == 4:
                raise AuthorizationRevoked

    class FlakyRemoveSandbox(_MemoryAtomicSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.remove_attempts = 0

        def remove_file(self, path: str) -> None:
            self.remove_attempts += 1
            if self.remove_attempts < 3:
                raise OSError("transient cleanup failure")
            super().remove_file(path)

    scope = _run_scope(Boundary())
    sandbox = FlakyRemoveSandbox()
    lease = SimpleNamespace(sandbox_id=f"sandbox-{uuid.uuid4()}")
    authority = PrivateRunFileAuthority(
        scope,
        SimpleNamespace(restore=AsyncMock(return_value=AuthorityManifest(entries=(), run_id=scope.run_id))),
        SimpleNamespace(),
        provider=SimpleNamespace(
            acquire_private_async=AsyncMock(return_value=lease),
            get=lambda sandbox_id: sandbox if sandbox_id == lease.sandbox_id else None,
            release_private_async=AsyncMock(),
        ),
    )
    await authority.restore()

    with pytest.raises(AuthorizationRevoked):
        await authority.write_output("capture.png", b"image")

    assert sandbox.remove_attempts == 3
    assert sandbox.files == {}


@pytest.mark.anyio
async def test_persistent_post_publish_cleanup_failure_poisons_authority() -> None:
    class Boundary:
        def __init__(self) -> None:
            self.write_checks = 0

        async def before_sandbox_restore(self) -> None:
            return None

        async def before_sandbox_write(self) -> None:
            self.write_checks += 1
            if self.write_checks == 4:
                raise AuthorizationRevoked

    class BrokenRemoveSandbox(_MemoryAtomicSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.remove_attempts = 0

        def remove_file(self, path: str) -> None:
            del path
            self.remove_attempts += 1
            raise OSError("persistent cleanup failure")

    scope = _run_scope(Boundary())
    sandbox = BrokenRemoveSandbox()
    lease = SimpleNamespace(sandbox_id=f"sandbox-{uuid.uuid4()}")
    provider = SimpleNamespace(
        acquire_private_async=AsyncMock(return_value=lease),
        get=lambda sandbox_id: sandbox if sandbox_id == lease.sandbox_id else None,
        release_private_async=AsyncMock(),
    )
    authority = PrivateRunFileAuthority(
        scope,
        SimpleNamespace(restore=AsyncMock(return_value=AuthorityManifest(entries=(), run_id=scope.run_id))),
        SimpleNamespace(),
        provider=provider,
    )
    await authority.restore()

    with pytest.raises(AuthorizationRevoked):
        await authority.write_output("capture.png", b"image")

    assert sandbox.remove_attempts == 3
    with pytest.raises(PrivateWorkUnavailable):
        await authority.write_output("second.png", b"image")
    with pytest.raises(PrivateWorkUnavailable):
        await authority.finalize()

    await authority.release()
    provider.release_private_async.assert_awaited_once_with(lease)


@pytest.mark.anyio
async def test_write_output_requires_a_restored_private_sandbox() -> None:
    scope = _run_scope(SimpleNamespace())
    authority = PrivateRunFileAuthority(
        scope,
        SimpleNamespace(),
        SimpleNamespace(),
    )

    with pytest.raises(Exception, match="unavailable"):
        await authority.write_output("result.txt", b"payload")
