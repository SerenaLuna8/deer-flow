from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import PrivateWorkContext, require_issued_private_work_context
from app.private_work.errors import (
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkUnavailable,
)
from app.private_work.file_paths import normalize_private_logical_path
from app.private_work.revalidation import PrivateWorkRevalidator
from app.projects.capabilities import Capability
from deerflow.file_authority import AuthorityManifest, AuthorityManifestEntry
from deerflow.persistence.private_work.file_repository import (
    PRIVATE_FILE_CHUNK_SIZE,
    PrivateFileRecord,
    PrivateFileRepository,
)
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.sandbox.sandbox import PRIVATE_FILE_IO_CHUNK_SIZE
from deerflow.sandbox.sandbox_provider import (
    PrivateSandboxLease,
    RunScopedReadOnlyMount,
    SandboxProvider,
    get_sandbox_provider,
)

_VIRTUAL_ROOTS = {
    "upload": "/mnt/user-data/uploads",
    "workspace": "/mnt/user-data/workspace",
    "output": "/mnt/user-data/outputs",
}
_LOGICAL_ROOTS = {
    "upload": "uploads",
    "workspace": "workspace",
    "output": "outputs",
}
_PRIVATE_FILE_REMOVE_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class PrivateFileRunScope:
    context: PrivateWorkContext
    thread_id: str
    run_id: str
    authorization_boundary: object | None = None

    def __post_init__(self) -> None:
        require_issued_private_work_context(self.context)
        if not self.thread_id or not self.run_id:
            raise ValueError("Private file run scope requires thread and run")

    @property
    def resource_scope(self) -> PrivateResourceScope:
        return self.context.resource_scope


def private_projection_root(
    base: Path,
    scope: PrivateResourceScope,
    thread_id: str,
) -> Path:
    if type(scope) is not PrivateResourceScope:
        raise ValueError("Invalid private projection scope")
    if not thread_id or PurePosixPath(thread_id).name != thread_id:
        raise ValueError("Invalid private projection thread")
    return base / "projects" / scope.project_id / "users" / scope.owner_user_id / "threads" / thread_id / "user-data"


async def _joined_to_thread(function, /, *args, cancel_cleanup=None):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        if cancel_cleanup is not None:
            cleanup_task = asyncio.create_task(asyncio.to_thread(cancel_cleanup, result))
            while True:
                try:
                    await asyncio.shield(cleanup_task)
                    break
                except asyncio.CancelledError:
                    if cleanup_task.cancelled():
                        break
                except Exception:
                    break
        raise asyncio.CancelledError
    return result


class PrivateSandboxFileProjection:
    """Restore PostgreSQL-ready authority into one temporary sandbox lease."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._revalidator = PrivateWorkRevalidator()

    @staticmethod
    def _virtual_path(file: PrivateFileRecord, request_id: str) -> str:
        logical = normalize_private_logical_path(file.logical_path, request_id=request_id)
        expected_root = _LOGICAL_ROOTS.get(file.kind)
        virtual_root = _VIRTUAL_ROOTS.get(file.kind)
        if expected_root is None or virtual_root is None:
            raise PrivateWorkInvalid(request_id)
        path = PurePosixPath(logical)
        if not path.parts or path.parts[0] != expected_root or len(path.parts) < 2:
            raise PrivateWorkInvalid(request_id)
        return f"{virtual_root}/{PurePosixPath(*path.parts[1:]).as_posix()}"

    async def _ready_files(self, run_scope: PrivateFileRunScope) -> tuple[PrivateFileRecord, ...]:
        rows: list[PrivateFileRecord] = []
        after = None
        while True:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    run_scope.context,
                    Capability.PRIVATE_WORK_CREATE,
                )
                page = await PrivateFileRepository(session).list_ready(
                    scope=run_scope.resource_scope,
                    thread_id=run_scope.thread_id,
                    after=after,
                    limit=100,
                )
            rows.extend(page)
            if len(page) < 100:
                return tuple(rows)
            tail = page[-1]
            after = (tail.logical_path, tail.version, tail.id)

    async def _chunk_page(
        self,
        run_scope: PrivateFileRunScope,
        file_id: uuid.UUID,
        after_index: int,
    ):
        async with self._session_factory() as session, session.begin():
            await self._revalidator.require(
                session,
                run_scope.context,
                Capability.PRIVATE_WORK_CREATE,
            )
            return await PrivateFileRepository(session).fetch_chunk_page(
                scope=run_scope.resource_scope,
                thread_id=run_scope.thread_id,
                file_id=file_id,
                after_index=after_index,
                limit=8,
            )

    async def restore(
        self,
        run_scope: PrivateFileRunScope,
        sandbox: Any,
    ) -> AuthorityManifest:
        run_scope = PrivateFileRunScope(
            require_issued_private_work_context(run_scope.context),
            run_scope.thread_id,
            run_scope.run_id,
            run_scope.authorization_boundary,
        )
        boundary = run_scope.authorization_boundary
        check = getattr(boundary, "before_sandbox_restore", None)
        if not callable(check):
            check = getattr(boundary, "before_sandbox_write", None)
        if callable(check):
            await check()
        published: list[str] = []
        handle: str | None = None
        failed = True
        try:
            files = await self._ready_files(run_scope)
            entries: list[AuthorityManifestEntry] = []
            for file in files:
                virtual_path = self._virtual_path(file, run_scope.context.request_id)
                if callable(check):
                    await check()
                handle = await _joined_to_thread(
                    sandbox.begin_atomic_file,
                    virtual_path,
                    cancel_cleanup=sandbox.abort_atomic_file,
                )
                whole = hashlib.sha256()
                total = 0
                expected_index = 0
                after_index = -1
                while True:
                    if callable(check):
                        await check()
                    page = await self._chunk_page(run_scope, file.id, after_index)
                    if not page:
                        break
                    for chunk in page:
                        content = chunk.content
                        if chunk.chunk_index != expected_index or chunk.size != len(content) or not 0 < chunk.size <= PRIVATE_FILE_CHUNK_SIZE or hashlib.sha256(content).hexdigest() != chunk.sha256:
                            raise PrivateWorkUnavailable(run_scope.context.request_id)
                        if callable(check):
                            await check()
                        await _joined_to_thread(
                            sandbox.append_atomic_file,
                            handle,
                            content,
                        )
                        whole.update(content)
                        total += len(content)
                        expected_index += 1
                        after_index = chunk.chunk_index
                    if len(page) < 8:
                        break
                if total != file.size or whole.hexdigest() != file.sha256:
                    raise PrivateWorkUnavailable(run_scope.context.request_id)
                if callable(check):
                    await check()
                await _joined_to_thread(sandbox.publish_atomic_file, handle)
                handle = None
                published.append(virtual_path)
                if callable(check):
                    await check()
                entries.append(
                    AuthorityManifestEntry(
                        file_id=file.id,
                        logical_path=file.logical_path,
                        kind=file.kind,
                        media_type=file.media_type,
                        size=file.size,
                        sha256=file.sha256,
                        version=file.version,
                    )
                )
            manifest = AuthorityManifest(entries=tuple(entries), run_id=run_scope.run_id)
            failed = False
            return manifest
        except PrivateWorkError:
            raise
        except (DBAPIError, OSError, ValueError):
            raise PrivateWorkUnavailable(run_scope.context.request_id) from None
        finally:
            if handle is not None:
                try:
                    await _joined_to_thread(sandbox.abort_atomic_file, handle)
                except Exception:
                    pass
            if failed:
                for path in reversed(published):
                    try:
                        await _joined_to_thread(sandbox.remove_file, path)
                    except Exception:
                        pass


class PrivateRunFileAuthority:
    """Opaque harness hook that owns one private sandbox lease end-to-end."""

    def __init__(
        self,
        run_scope: PrivateFileRunScope,
        projection: PrivateSandboxFileProjection,
        finalizer: Any,
        *,
        mounts: tuple[RunScopedReadOnlyMount, ...] = (),
        provider: SandboxProvider | None = None,
    ) -> None:
        self._run_scope = run_scope
        self._projection = projection
        self._finalizer = finalizer
        self._mounts = mounts
        self._provider = provider
        self._lease: PrivateSandboxLease | None = None
        self._sandbox: Any | None = None
        self._manifest: AuthorityManifest | None = None
        self._presented_paths: list[str] = []
        self._current_upload_ids: list[str] = []
        self._cleanup_failed = False
        self._release_lock = asyncio.Lock()

    @property
    def sandbox_id(self) -> str | None:
        return None if self._lease is None else self._lease.sandbox_id

    @property
    def manifest(self) -> AuthorityManifest | None:
        return self._manifest

    async def restore(self) -> AuthorityManifest:
        if self._manifest is not None:
            return self._manifest
        boundary = self._run_scope.authorization_boundary
        check = getattr(boundary, "before_sandbox_restore", None)
        if not callable(check):
            check = getattr(boundary, "before_sandbox_write", None)
        if callable(check):
            # Sandbox acquisition can create a container/Pod, so authority is
            # checked before asking the provider to allocate anything.
            await check()
        provider = self._provider or get_sandbox_provider()
        self._provider = provider
        lease = await provider.acquire_private_async(
            self._run_scope.thread_id,
            scope=self._run_scope.resource_scope,
            user_id=self._run_scope.resource_scope.owner_user_id,
            run_id=self._run_scope.run_id,
            mounts=self._mounts,
        )
        sandbox = provider.get(lease.sandbox_id)
        if sandbox is None:
            await provider.release_private_async(lease)
            raise PrivateWorkUnavailable(self._run_scope.context.request_id)
        self._lease = lease
        self._sandbox = sandbox
        self._manifest = await self._projection.restore(self._run_scope, sandbox)
        return self._manifest

    def record_presented_paths(self, presented_paths: tuple[str, ...]) -> None:
        if type(presented_paths) is not tuple or any(type(path) is not str for path in presented_paths):
            raise ValueError("Invalid private presented paths")
        self._presented_paths = list(dict.fromkeys((*self._presented_paths, *presented_paths)))

    def record_current_upload_ids(self, file_ids: tuple[str, ...]) -> None:
        """Remember authorized current-Run uploads for lead and subagents."""

        if type(file_ids) is not tuple or any(type(file_id) is not str or not file_id for file_id in file_ids):
            raise ValueError("Invalid current private upload ids")
        visible_ids = {str(entry.file_id) for entry in (self._manifest.entries if self._manifest else ()) if entry.kind == "upload"}
        if any(file_id not in visible_ids for file_id in file_ids):
            raise ValueError("Current private uploads are outside authority")
        self._current_upload_ids = list(dict.fromkeys((*self._current_upload_ids, *file_ids)))

    def current_upload_ids(self) -> tuple[str, ...]:
        return tuple(self._current_upload_ids)

    async def write_output(
        self,
        relative_path: str,
        content: bytes,
    ) -> str:
        """Atomically write one uniquely named presentable Run output."""

        return await self._write_private_file(
            root_kind="outputs",
            relative_path=relative_path,
            content=content,
        )

    async def write_internal(
        self,
        relative_path: str,
        content: bytes,
    ) -> str:
        """Write internal Run data below the durable workspace root.

        These files remain readable by the model and are finalized into
        PostgreSQL, but the Worker does not treat them as unpresented
        user-facing artifacts.
        """

        return await self._write_private_file(
            root_kind="workspace",
            relative_path=relative_path,
            content=content,
        )

    async def _write_private_file(
        self,
        *,
        root_kind: str,
        relative_path: str,
        content: bytes,
    ) -> str:
        """Own validation, fencing, bounded writes, and cancellation cleanup."""

        if self._cleanup_failed or self._manifest is None or self._sandbox is None:
            raise PrivateWorkUnavailable(self._run_scope.context.request_id)
        if root_kind not in {"outputs", "workspace"}:
            raise PrivateWorkInvalid(self._run_scope.context.request_id)
        if type(relative_path) is not str or type(content) is not bytes or not content:
            raise PrivateWorkInvalid(self._run_scope.context.request_id)

        logical_path = normalize_private_logical_path(
            f"{root_kind}/{relative_path}",
            request_id=self._run_scope.context.request_id,
        )
        path = PurePosixPath(logical_path)
        if len(path.parts) < 2 or path.parts[0] != root_kind or any(part.startswith(".deerflow") for part in path.parts[1:]):
            raise PrivateWorkInvalid(self._run_scope.context.request_id)

        unique_name = f"{path.stem}-{uuid.uuid4().hex[:12]}{path.suffix}"
        unique_relative = PurePosixPath(*path.parts[1:-1], unique_name)
        virtual_path = f"/mnt/user-data/{root_kind}/{unique_relative.as_posix()}"

        boundary = self._run_scope.authorization_boundary
        check = getattr(boundary, "before_sandbox_write", None)
        if not callable(check):
            raise PrivateWorkUnavailable(self._run_scope.context.request_id)

        sandbox = self._sandbox
        handle: str | None = None
        published_path: str | None = None
        try:
            await check()
            handle = await _joined_to_thread(
                sandbox.begin_atomic_file,
                virtual_path,
                cancel_cleanup=sandbox.abort_atomic_file,
            )
            for offset in range(
                0,
                len(content),
                PRIVATE_FILE_IO_CHUNK_SIZE,
            ):
                await check()
                await _joined_to_thread(
                    sandbox.append_atomic_file,
                    handle,
                    content[offset : offset + PRIVATE_FILE_IO_CHUNK_SIZE],
                )
            await check()
            await _joined_to_thread(
                sandbox.publish_atomic_file,
                handle,
                cancel_cleanup=lambda _result: self._remove_published_file(
                    sandbox,
                    virtual_path,
                ),
            )
            handle = None
            published_path = virtual_path
            await check()
            published_path = None
            return virtual_path
        finally:
            if handle is not None:
                try:
                    await _joined_to_thread(
                        sandbox.abort_atomic_file,
                        handle,
                    )
                except Exception:
                    pass
            if published_path is not None:
                try:
                    await _joined_to_thread(
                        self._remove_published_file,
                        sandbox,
                        published_path,
                    )
                except Exception:
                    pass

    def _remove_published_file(self, sandbox: Any, path: str) -> None:
        last_error: Exception | None = None
        for _attempt in range(_PRIVATE_FILE_REMOVE_MAX_ATTEMPTS):
            try:
                sandbox.remove_file(path)
                return
            except Exception as exc:
                last_error = exc
        self._cleanup_failed = True
        assert last_error is not None
        raise last_error

    async def finalize(self):
        if self._cleanup_failed or self._manifest is None or self._sandbox is None:
            raise PrivateWorkUnavailable(self._run_scope.context.request_id)
        return await self._finalizer.finalize(
            self._run_scope,
            self._manifest,
            self._sandbox,
            presented_paths=tuple(self._presented_paths),
        )

    async def mark_failed(self) -> None:
        await self._finalizer.mark_failed(self._run_scope)

    def _clear_released_state(self) -> None:
        self._lease = None
        self._sandbox = None
        self._manifest = None
        self._presented_paths = []
        self._current_upload_ids = []
        self._cleanup_failed = False

    async def release(self) -> None:
        async with self._release_lock:
            lease = self._lease
            if lease is None:
                return
            provider = self._provider
            if provider is None:
                raise PrivateWorkUnavailable(self._run_scope.context.request_id)
            current = asyncio.current_task()
            cancellation_count = current.cancelling() if current is not None else 0
            try:
                await provider.release_private_async(lease)
            except asyncio.CancelledError:
                # SandboxProvider.release_private_async() joins its blocking
                # destroy before propagating caller cancellation. Forget the
                # completed lease so a cleanup retry cannot target an already
                # destroyed private sandbox. An internally-raised
                # CancelledError does not increase this task's cancellation
                # count and keeps the state available for an explicit retry.
                if current is not None and current.cancelling() > cancellation_count:
                    self._clear_released_state()
                raise
            self._clear_released_state()

    def thread_data_paths(self) -> dict[str, str]:
        if self._manifest is None:
            raise RuntimeError("Private file authority has not been restored")
        return {
            "workspace_path": "/mnt/user-data/workspace",
            "uploads_path": "/mnt/user-data/uploads",
            "outputs_path": "/mnt/user-data/outputs",
        }

    def visible_uploads(self) -> tuple[dict[str, object], ...]:
        manifest = self._manifest
        if manifest is None:
            return ()
        result: list[dict[str, object]] = []
        for entry in manifest.entries:
            if entry.kind != "upload":
                continue
            path = PurePosixPath(entry.logical_path)
            result.append(
                {
                    "file_id": str(entry.file_id),
                    "version": entry.version,
                    "filename": path.name,
                    "size": entry.size,
                    "path": f"/mnt/user-data/uploads/{PurePosixPath(*path.parts[1:]).as_posix()}",
                    "extension": path.suffix,
                    "media_type": entry.media_type,
                }
            )
        return tuple(result)
