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

    async def finalize(self):
        if self._manifest is None or self._sandbox is None:
            raise PrivateWorkUnavailable(self._run_scope.context.request_id)
        return await self._finalizer.finalize(
            self._run_scope,
            self._manifest,
            self._sandbox,
            presented_paths=tuple(self._presented_paths),
        )

    async def mark_failed(self) -> None:
        await self._finalizer.mark_failed(self._run_scope)

    async def release(self) -> None:
        async with self._release_lock:
            lease = self._lease
            if lease is None:
                return
            provider = self._provider
            if provider is None:
                raise PrivateWorkUnavailable(self._run_scope.context.request_id)
            await provider.release_private_async(lease)
            self._lease = None
            self._sandbox = None
            self._manifest = None
            self._presented_paths = []

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
                    "filename": path.name,
                    "size": entry.size,
                    "path": f"/mnt/user-data/uploads/{PurePosixPath(*path.parts[1:]).as_posix()}",
                    "extension": path.suffix,
                    "media_type": entry.media_type,
                }
            )
        return tuple(result)
