from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Mapping
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
RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG = "__run_current_upload_snapshot"
_CURRENT_UPLOAD_SNAPSHOT_KEYS = frozenset(
    {
        "file_id",
        "logical_path",
        "media_type",
        "size",
        "sha256",
        "version",
    }
)


class CurrentUploadSnapshotInvalid(ValueError):
    """The server-owned current-upload snapshot is absent or malformed."""


class CurrentUploadSnapshotStale(RuntimeError):
    """The restored file authority no longer matches the admitted snapshot."""


@dataclass(frozen=True, slots=True)
class CurrentUploadSnapshotEntry:
    """Secret-free exact metadata frozen for one current-message upload."""

    file_id: str
    logical_path: str
    media_type: str
    size: int
    sha256: str
    version: int

    def __post_init__(self) -> None:
        if _canonical_private_file_id(self.file_id) is None:
            raise CurrentUploadSnapshotInvalid
        if type(self.logical_path) is not str:
            raise CurrentUploadSnapshotInvalid
        path = PurePosixPath(self.logical_path)
        if path.is_absolute() or len(path.parts) < 2 or path.parts[0] != "uploads" or path.as_posix() != self.logical_path or any(part in {"", ".", ".."} for part in path.parts):
            raise CurrentUploadSnapshotInvalid
        if type(self.media_type) is not str or not self.media_type or len(self.media_type) > 255:
            raise CurrentUploadSnapshotInvalid
        if type(self.size) is not int or self.size < 0:
            raise CurrentUploadSnapshotInvalid
        if type(self.sha256) is not str or len(self.sha256) != 64 or any(character not in "0123456789abcdef" for character in self.sha256):
            raise CurrentUploadSnapshotInvalid
        if type(self.version) is not int or self.version < 1:
            raise CurrentUploadSnapshotInvalid

    def as_dict(self) -> dict[str, object]:
        return {
            "file_id": self.file_id,
            "logical_path": self.logical_path,
            "media_type": self.media_type,
            "size": self.size,
            "sha256": self.sha256,
            "version": self.version,
        }


def _canonical_private_file_id(value: object) -> str | None:
    if type(value) is not str:
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return value if str(parsed) == value else None


def current_upload_candidates_from_run_kwargs(
    run_kwargs: object,
) -> tuple[str, ...]:
    """Recover current-message file references from immutable Run input.

    The returned IDs are client-selected candidates, never file authority.
    Gateway admission intersects them with locked, ready, thread-scoped upload
    rows and freezes exact metadata. The Worker later accepts only that frozen
    subset. Keeping image bytes and data URLs out of this payload also prevents
    them from entering LangGraph state/checkpoints.
    """

    if not isinstance(run_kwargs, Mapping):
        return ()
    graph_input = run_kwargs.get("input")
    if not isinstance(graph_input, Mapping):
        return ()
    messages = graph_input.get("messages")
    if not isinstance(messages, list):
        return ()

    for raw_message in reversed(messages):
        if not isinstance(raw_message, Mapping):
            continue
        message_type = raw_message.get("type")
        message_role = raw_message.get("role")
        if message_type != "human" and message_role != "user":
            continue
        additional_kwargs = raw_message.get("additional_kwargs")
        if not isinstance(additional_kwargs, Mapping):
            return ()
        files = additional_kwargs.get("files")
        if not isinstance(files, list):
            return ()
        candidates: list[str] = []
        for raw_file in files:
            if not isinstance(raw_file, Mapping):
                continue
            file_id = _canonical_private_file_id(raw_file.get("file_id"))
            if file_id is not None and file_id not in candidates:
                candidates.append(file_id)
        return tuple(candidates)
    return ()


def persisted_current_upload_snapshot(
    entries: tuple[CurrentUploadSnapshotEntry, ...],
) -> list[dict[str, object]]:
    if type(entries) is not tuple or any(type(entry) is not CurrentUploadSnapshotEntry for entry in entries):
        raise CurrentUploadSnapshotInvalid
    file_ids = tuple(entry.file_id for entry in entries)
    if len(file_ids) != len(set(file_ids)):
        raise CurrentUploadSnapshotInvalid
    return [entry.as_dict() for entry in entries]


def required_current_upload_snapshot_from_run_kwargs(
    run_kwargs: object,
) -> tuple[CurrentUploadSnapshotEntry, ...]:
    """Return the exact server snapshot, rejecting legacy attachment Runs.

    Runs without current-message file references remain compatible. A Run that
    does reference files must carry the server-owned snapshot so a Worker never
    falls back to re-authorizing mutable client selections after admission.
    """

    if not isinstance(run_kwargs, Mapping):
        raise CurrentUploadSnapshotInvalid
    candidates = current_upload_candidates_from_run_kwargs(run_kwargs)
    if RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG not in run_kwargs:
        if candidates:
            raise CurrentUploadSnapshotInvalid
        return ()
    raw_entries = run_kwargs.get(RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG)
    if not isinstance(raw_entries, list):
        raise CurrentUploadSnapshotInvalid
    entries: list[CurrentUploadSnapshotEntry] = []
    try:
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, Mapping) or set(raw_entry) != _CURRENT_UPLOAD_SNAPSHOT_KEYS:
                raise CurrentUploadSnapshotInvalid
            entries.append(
                CurrentUploadSnapshotEntry(
                    file_id=raw_entry.get("file_id"),
                    logical_path=raw_entry.get("logical_path"),
                    media_type=raw_entry.get("media_type"),
                    size=raw_entry.get("size"),
                    sha256=raw_entry.get("sha256"),
                    version=raw_entry.get("version"),
                )
            )
    except (TypeError, ValueError):
        raise CurrentUploadSnapshotInvalid from None
    snapshot = tuple(entries)
    snapshot_ids = tuple(entry.file_id for entry in snapshot)
    if len(snapshot_ids) != len(set(snapshot_ids)):
        raise CurrentUploadSnapshotInvalid
    snapshot_set = set(snapshot_ids)
    if snapshot_ids != tuple(file_id for file_id in candidates if file_id in snapshot_set):
        raise CurrentUploadSnapshotInvalid
    return snapshot


async def admit_current_upload_snapshot(
    session: AsyncSession,
    *,
    scope: PrivateResourceScope,
    thread_id: str,
    run_kwargs: object,
) -> tuple[CurrentUploadSnapshotEntry, ...]:
    """Lock and freeze the authorized subset of current-message file claims."""

    repository = PrivateFileRepository(session)
    entries: list[CurrentUploadSnapshotEntry] = []
    for raw_file_id in current_upload_candidates_from_run_kwargs(run_kwargs):
        record = await repository.get(
            scope=scope,
            thread_id=thread_id,
            file_id=uuid.UUID(raw_file_id),
            lock=True,
        )
        if record is None or record.status != "ready" or record.kind != "upload":
            continue
        entries.append(
            CurrentUploadSnapshotEntry(
                file_id=str(record.id),
                logical_path=record.logical_path,
                media_type=record.media_type,
                size=record.size,
                sha256=record.sha256,
                version=record.version,
            )
        )
    return tuple(entries)


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
        current_upload_snapshot: tuple[CurrentUploadSnapshotEntry, ...] = (),
        output_delivery_port: object | None = None,
    ) -> None:
        if type(current_upload_snapshot) is not tuple or any(type(entry) is not CurrentUploadSnapshotEntry for entry in current_upload_snapshot):
            raise ValueError("Invalid current private upload snapshot")
        snapshot_ids = tuple(entry.file_id for entry in current_upload_snapshot)
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise ValueError("Invalid current private upload snapshot")
        self._run_scope = run_scope
        self._projection = projection
        self._finalizer = finalizer
        self._mounts = mounts
        self._provider = provider
        self._current_upload_snapshot = current_upload_snapshot
        self._current_upload_snapshot_ids = frozenset(snapshot_ids)
        self._output_delivery_port = output_delivery_port
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

    @staticmethod
    def _validated_presented_paths(paths: object) -> tuple[str, ...]:
        if type(paths) is not tuple or any(type(path) is not str for path in paths):
            raise ValueError("Invalid private presented paths")
        normalized: list[str] = []
        root = PurePosixPath("/mnt/user-data/outputs")
        for path in paths:
            parsed = PurePosixPath(path)
            if not parsed.is_absolute() or parsed.as_posix() != path or ".." in parsed.parts:
                raise ValueError("Invalid private presented paths")
            try:
                relative = parsed.relative_to(root)
            except ValueError:
                raise ValueError("Invalid private presented paths") from None
            if not relative.parts:
                raise ValueError("Invalid private presented paths")
            normalized.append(path)
        return tuple(dict.fromkeys(normalized))

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
        upload_entries = tuple(entry for entry in self._manifest.entries if entry.kind == "upload")
        by_id = {str(entry.file_id): entry for entry in upload_entries}
        if len(by_id) != len(upload_entries):
            raise CurrentUploadSnapshotStale
        for expected in self._current_upload_snapshot:
            restored = by_id.get(expected.file_id)
            if restored is None or (
                restored.logical_path,
                restored.media_type,
                restored.size,
                restored.sha256,
                restored.version,
            ) != (
                expected.logical_path,
                expected.media_type,
                expected.size,
                expected.sha256,
                expected.version,
            ):
                raise CurrentUploadSnapshotStale
        self._current_upload_ids = [entry.file_id for entry in self._current_upload_snapshot]
        intent_restorer = getattr(
            self._output_delivery_port,
            "restore_output_delivery_intent_paths",
            None,
        )
        if callable(intent_restorer):
            restored_paths = self._validated_presented_paths(
                await intent_restorer(),
            )
            self._presented_paths = list(
                dict.fromkeys((*self._presented_paths, *restored_paths)),
            )
        return self._manifest

    async def record_presented_paths(
        self,
        presented_paths: tuple[str, ...],
        *,
        tool_call_id: str,
    ) -> None:
        presented_paths = self._validated_presented_paths(presented_paths)
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise ValueError("Invalid private presentation tool call")
        recorder = getattr(
            self._output_delivery_port,
            "record_output_delivery_intent",
            None,
        )
        if callable(recorder):
            await recorder(
                presented_paths,
                tool_call_id=tool_call_id,
            )
        self._presented_paths = list(dict.fromkeys((*self._presented_paths, *presented_paths)))

    async def output_delivery_status(self) -> str:
        status_reader = getattr(
            self._output_delivery_port,
            "output_delivery_status",
            None,
        )
        if not callable(status_reader):
            return "not_required"
        status = await status_reader()
        if status not in {
            "not_required",
            "assigned",
            "intent_recorded",
            "delivered",
            "cancelled",
            "blocked_unknown",
            "failed",
        }:
            raise PrivateWorkUnavailable(self._run_scope.context.request_id)
        return status

    def record_current_upload_ids(self, file_ids: tuple[str, ...]) -> None:
        """Remember authorized current-Run uploads for lead requests."""

        if type(file_ids) is not tuple or any(type(file_id) is not str or not file_id for file_id in file_ids):
            raise ValueError("Invalid current private upload ids")
        visible_ids = {str(entry.file_id) for entry in (self._manifest.entries if self._manifest else ()) if entry.kind == "upload"}
        if any(file_id not in visible_ids or file_id not in self._current_upload_snapshot_ids for file_id in file_ids):
            raise CurrentUploadSnapshotStale
        self._current_upload_ids = list(dict.fromkeys((*self._current_upload_ids, *file_ids)))

    def current_upload_ids(self) -> tuple[str, ...]:
        return tuple(self._current_upload_ids)

    def current_uploads(self) -> tuple[AuthorityManifestEntry, ...]:
        """Return only server-restored entries selected for this Run message."""

        manifest = self._manifest
        if manifest is None:
            raise RuntimeError("Private file authority has not been restored")
        upload_entries = tuple(entry for entry in manifest.entries if entry.kind == "upload")
        by_id = {str(entry.file_id): entry for entry in upload_entries}
        if len(by_id) != len(upload_entries):
            raise RuntimeError("Private file authority is unavailable")
        try:
            return tuple(by_id[file_id] for file_id in self._current_upload_ids)
        except KeyError:
            raise RuntimeError("Private file authority is unavailable") from None

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
