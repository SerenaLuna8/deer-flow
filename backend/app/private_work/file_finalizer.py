from __future__ import annotations

import errno
import hashlib
import logging
import mimetypes
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Protocol

from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkRetryableUnavailable,
    PrivateWorkTooLarge,
    PrivateWorkUnavailable,
)
from app.private_work.file_paths import normalize_private_logical_path
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.sandbox_files import (
    AuthorityManifest,
    PrivateFileRunScope,
    _joined_to_thread,
)
from app.projects.capabilities import Capability
from app.upload_contracts import PRIVATE_UPLOAD_DEFAULTS
from deerflow.persistence.private_work.file_repository import (
    PRIVATE_FILE_CHUNK_SIZE,
    PrivateArtifactRecord,
    PrivateFileRepository,
)
from deerflow.persistence.private_work.model import (
    PrivateArtifactRow,
    PrivateFileChunkRow,
    PrivateFileRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.workspace_changes.diff import compare_snapshots
from deerflow.workspace_changes.scanner import (
    BINARY_EXTENSIONS,
    SAMPLE_BYTES,
    decode_workspace_text_bytes,
    is_sensitive_workspace_path,
    workspace_bytes_look_binary,
)
from deerflow.workspace_changes.types import (
    FileSnapshot,
    WorkspaceChangeLimits,
    WorkspaceChangeResult,
    WorkspaceSnapshot,
)

logger = logging.getLogger(__name__)

_SCAN_ROOTS = (
    ("/mnt/user-data/workspace", "workspace", "workspace"),
    ("/mnt/user-data/outputs", "outputs", "output"),
)
_PRESENTED_OUTPUT_PREFIX = "/mnt/user-data/outputs/"
_WORKSPACE_RUNTIME_ROOT_NAMES = (".venv",)
_DELEGATED_OUTPUT_RUNTIME_PREFIX = "/mnt/user-data/workspace/.deerflow/subagents/"
_DEFAULT_PRIVATE_FINALIZATION_MAX_SCANNED_FILES = 2_000
_DEFAULT_PRIVATE_FINALIZATION_MAX_SCAN_ENTRIES = 10_000


def _canonical_ready_authority(rows: Any) -> tuple[tuple[object, ...], ...]:
    """Compare ready-file facts without depending on PostgreSQL collation."""

    facts: list[tuple[object, ...]] = []
    for row in rows:
        file_id = getattr(row, "file_id", None)
        if file_id is None:
            file_id = row.id
        facts.append(
            (
                file_id,
                row.logical_path,
                row.kind,
                row.media_type,
                row.size,
                row.sha256,
                row.version,
            )
        )
    return tuple(sorted(facts, key=lambda item: (item[1], str(item[0]))))


@dataclass(frozen=True, slots=True)
class _AfterFile:
    logical_path: str
    virtual_path: str
    kind: str
    size: int
    media_type: str


@dataclass(frozen=True, slots=True)
class _StagedFile:
    id: uuid.UUID
    after: _AfterFile
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    files: tuple[Any, ...]
    artifacts: tuple[PrivateArtifactRecord, ...]
    deleted_file_ids: tuple[uuid.UUID, ...]
    workspace_changes: WorkspaceChangeResult | None
    produced_output_paths: tuple[str, ...]


@dataclass(slots=True)
class _FinalizationCommitState:
    result: FinalizationResult | None = None
    body_complete: bool = False
    phase: str = "commit_authority"


class _RetryableFinalizationCommit(Exception):
    """One transaction attempt rolled back before a durable commit receipt."""

    def __init__(self, *, phase: str, failure_type: str) -> None:
        self.phase = phase
        self.failure_type = failure_type
        super().__init__(phase)


_TRANSIENT_POSTGRES_STATES = frozenset({"57P01", "57P02", "57P03"})


def _database_sqlstate(error: BaseException) -> str | None:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if isinstance(value, str) and value:
                return value
        for nested in (
            getattr(current, "orig", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return None


def _is_transient_database_error(error: BaseException) -> bool:
    if isinstance(error, (IntegrityError, ProgrammingError)):
        return False
    if isinstance(error, SQLAlchemyTimeoutError):
        return True
    if isinstance(error, DBAPIError) and error.connection_invalidated:
        return True
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    sqlstate = _database_sqlstate(error)
    return sqlstate is not None and (sqlstate.startswith("08") or sqlstate in _TRANSIENT_POSTGRES_STATES)


@dataclass(frozen=True, slots=True)
class PrivateFileFinalizationLimits:
    """Bound one finalization pass independently from upload batching and diff display."""

    max_scanned_files: int = _DEFAULT_PRIVATE_FINALIZATION_MAX_SCANNED_FILES
    max_scan_entries: int = _DEFAULT_PRIVATE_FINALIZATION_MAX_SCAN_ENTRIES
    max_changed_file_size: int = PRIVATE_UPLOAD_DEFAULTS.max_file_size
    max_changed_total_size: int = PRIVATE_UPLOAD_DEFAULTS.max_total_size

    def __post_init__(self) -> None:
        if self.max_scanned_files < 1 or self.max_scan_entries < self.max_scanned_files or self.max_changed_file_size < 1 or self.max_changed_total_size < 1:
            raise ValueError("private file finalization limits must be positive")


class PrivateFileFinalizationQuotaPort(Protocol):
    async def reserve_file(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        file_id: uuid.UUID,
        size: int,
    ) -> None: ...

    async def release_file(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        file_id: uuid.UUID,
        size: int,
        request_id: str,
    ) -> None: ...


class PrivateFileFinalizationAuditPort(Protocol):
    async def run_files_finalized(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        request_id: str,
        created_count: int,
        modified_count: int,
        deleted_count: int,
        artifact_count: int,
        committed_bytes: int,
    ) -> None: ...


class _NoopPrivateFileFinalizationQuota:
    async def reserve_file(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        file_id: uuid.UUID,
        size: int,
    ) -> None:
        del session, context, file_id, size

    async def release_file(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        file_id: uuid.UUID,
        size: int,
        request_id: str,
    ) -> None:
        del session, scope, file_id, size, request_id


class PrivateFileFinalizer:
    """Commit verified workspace/output changes before a private Run terminates."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        limits: PrivateFileFinalizationLimits | None = None,
        quota: PrivateFileFinalizationQuotaPort | None = None,
        audit: PrivateFileFinalizationAuditPort | None = None,
        output_delivery_port: object | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits or PrivateFileFinalizationLimits()
        self._quota = quota or _NoopPrivateFileFinalizationQuota()
        self._audit = audit
        self._output_delivery_port = output_delivery_port
        self._revalidator = PrivateWorkRevalidator()

    @staticmethod
    async def _authorize_mutation(
        run_scope: PrivateFileRunScope,
        session: AsyncSession,
    ) -> None:
        boundary = run_scope.authorization_boundary
        checker = getattr(
            boundary,
            "before_file_finalization_in_session",
            None,
        )
        if callable(checker):
            await checker(session)

    async def _set_run_finalization(
        self,
        run_scope: PrivateFileRunScope,
        status: str,
    ) -> None:
        if status not in {"finalizing", "failed"}:
            raise ValueError("unsupported private file finalization transition")
        async with self._session_factory() as session, session.begin():
            await self._revalidator.require(
                session,
                run_scope.context,
                Capability.PRIVATE_WORK_CREATE,
                lock_mode="update",
            )
            await self._authorize_mutation(run_scope, session)
            result = await session.execute(
                update(RunRow)
                .where(
                    RunRow.run_id == run_scope.run_id,
                    RunRow.project_id == run_scope.context.project_id,
                    RunRow.owner_user_id == str(run_scope.context.user_id),
                    RunRow.thread_id == run_scope.thread_id,
                    RunRow.status.in_(("pending", "running")),
                    RunRow.finalization_status.in_(
                        ("pending", "finalizing", "failed"),
                    ),
                )
                .values(finalization_status=status, updated_at=datetime.now(UTC))
            )
            if result.rowcount == 1:
                return
            if status == "failed":
                committed = await session.scalar(
                    select(RunRow.run_id)
                    .where(
                        RunRow.run_id == run_scope.run_id,
                        RunRow.project_id == run_scope.context.project_id,
                        RunRow.owner_user_id == str(run_scope.context.user_id),
                        RunRow.thread_id == run_scope.thread_id,
                        RunRow.status.in_(("pending", "running")),
                        RunRow.finalization_status == "complete",
                    )
                    .with_for_update(of=RunRow)
                )
                if committed is not None:
                    return
            raise PrivateWorkUnavailable(run_scope.context.request_id)

    async def mark_failed(self, run_scope: PrivateFileRunScope) -> None:
        try:
            await self._set_run_finalization(run_scope, "failed")
        except PrivateWorkError:
            raise
        except Exception:
            raise PrivateWorkUnavailable(run_scope.context.request_id) from None

    def _scan(self, run_scope: PrivateFileRunScope, sandbox: Any) -> tuple[_AfterFile, ...]:
        files: list[_AfterFile] = []
        logical_paths: set[str] = set()
        count = 0
        scanned_entries = 0
        for virtual_root, logical_root, kind in _SCAN_ROOTS:
            prefix = virtual_root.rstrip("/") + "/"
            entries = None
            try:
                entries = sandbox.list_secure_files(
                    virtual_root,
                    max_entries=self._limits.max_scan_entries,
                    excluded_root_names=(_WORKSPACE_RUNTIME_ROOT_NAMES if logical_root == "workspace" else ()),
                )
                for entry in entries:
                    if logical_root == "workspace" and entry.path.startswith(
                        _DELEGATED_OUTPUT_RUNTIME_PREFIX,
                    ):
                        continue
                    scanned_entries += 1
                    if scanned_entries > self._limits.max_scan_entries:
                        raise PrivateWorkTooLarge(run_scope.context.request_id)
                    if entry.file_type == "directory":
                        continue
                    if entry.file_type != "regular" or not entry.path.startswith(prefix):
                        raise PrivateWorkInvalid(run_scope.context.request_id)
                    relative = entry.path[len(prefix) :]
                    relative_path = PurePosixPath(relative)
                    if not relative or relative_path.is_absolute() or ".." in relative_path.parts or any(part.startswith(".deerflow") for part in relative_path.parts):
                        raise PrivateWorkInvalid(run_scope.context.request_id)
                    logical_path = normalize_private_logical_path(
                        f"{logical_root}/{relative_path.as_posix()}",
                        request_id=run_scope.context.request_id,
                    )
                    if logical_path in logical_paths:
                        raise PrivateWorkInvalid(run_scope.context.request_id)
                    logical_paths.add(logical_path)
                    count += 1
                    if count > self._limits.max_scanned_files:
                        raise PrivateWorkTooLarge(run_scope.context.request_id)
                    media_type = mimetypes.guess_type(relative_path.name)[0] or "application/octet-stream"
                    files.append(
                        _AfterFile(
                            logical_path=logical_path,
                            virtual_path=entry.path,
                            kind=kind,
                            size=entry.size,
                            media_type=media_type,
                        )
                    )
            except OSError as exc:
                if exc.errno == errno.EFBIG:
                    raise PrivateWorkTooLarge(run_scope.context.request_id) from None
                raise
            finally:
                close_entries = getattr(entries, "close", None)
                if callable(close_entries):
                    close_entries()
        return tuple(sorted(files, key=lambda item: item.logical_path))

    def _validate_changed_files(
        self,
        run_scope: PrivateFileRunScope,
        changed_files: tuple[_AfterFile, ...],
    ) -> None:
        total = 0
        for changed in changed_files:
            total += changed.size
            if changed.size > self._limits.max_changed_file_size or total > self._limits.max_changed_total_size:
                raise PrivateWorkTooLarge(run_scope.context.request_id)

    async def _hash_sandbox_file(
        self,
        run_scope: PrivateFileRunScope,
        sandbox: Any,
        after: _AfterFile,
    ) -> str:
        handle = await _joined_to_thread(
            sandbox.open_regular_file,
            after.virtual_path,
            cancel_cleanup=sandbox.close_regular_file,
        )
        whole = hashlib.sha256()
        total = 0
        try:
            while True:
                content = await _joined_to_thread(
                    sandbox.read_regular_file,
                    handle,
                    PRIVATE_FILE_CHUNK_SIZE,
                )
                if not content:
                    break
                if not isinstance(content, bytes) or len(content) > PRIVATE_FILE_CHUNK_SIZE:
                    raise PrivateWorkInvalid(run_scope.context.request_id)
                total += len(content)
                if total > after.size:
                    raise PrivateWorkInvalid(run_scope.context.request_id)
                whole.update(content)
        finally:
            await _joined_to_thread(sandbox.close_regular_file, handle)
        if total != after.size:
            raise PrivateWorkInvalid(run_scope.context.request_id)
        return whole.hexdigest()

    async def _stage_file(
        self,
        run_scope: PrivateFileRunScope,
        sandbox: Any,
        after: _AfterFile,
        *,
        file_id: uuid.UUID,
    ) -> _StagedFile:
        temporary_logical_path = f"{after.logical_path.rsplit('/', 1)[0]}/.deerflow-staging-{file_id.hex}"
        async with self._session_factory() as session, session.begin():
            await self._revalidator.require(
                session,
                run_scope.context,
                Capability.PRIVATE_WORK_CREATE,
                lock_mode="update",
            )
            await self._authorize_mutation(run_scope, session)
            await PrivateFileRepository(session).stage(
                scope=run_scope.resource_scope,
                thread_id=run_scope.thread_id,
                kind=after.kind,
                logical_path=temporary_logical_path,
                media_type=after.media_type,
                created_by_run_id=run_scope.run_id,
                file_id=file_id,
            )

        handle = await _joined_to_thread(
            sandbox.open_regular_file,
            after.virtual_path,
            cancel_cleanup=sandbox.close_regular_file,
        )
        whole = hashlib.sha256()
        total = 0
        index = 0
        try:
            while True:
                content = await _joined_to_thread(
                    sandbox.read_regular_file,
                    handle,
                    PRIVATE_FILE_CHUNK_SIZE,
                )
                if not content:
                    break
                whole.update(content)
                total += len(content)
                if total > after.size or total > self._limits.max_changed_file_size:
                    raise PrivateWorkInvalid(run_scope.context.request_id)
                async with self._session_factory() as session, session.begin():
                    await self._revalidator.require(
                        session,
                        run_scope.context,
                        Capability.PRIVATE_WORK_CREATE,
                        lock_mode="update",
                    )
                    await self._authorize_mutation(
                        run_scope,
                        session,
                    )
                    await PrivateFileRepository(session).append_chunk(
                        scope=run_scope.resource_scope,
                        thread_id=run_scope.thread_id,
                        file_id=file_id,
                        chunk_index=index,
                        content=content,
                        size=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                    )
                index += 1
        finally:
            await _joined_to_thread(sandbox.close_regular_file, handle)
        if total != after.size:
            raise PrivateWorkInvalid(run_scope.context.request_id)
        return _StagedFile(file_id, after, total, whole.hexdigest())

    async def _cleanup_staging(
        self,
        run_scope: PrivateFileRunScope,
        file_ids: tuple[uuid.UUID, ...],
    ) -> None:
        if not file_ids:
            return
        async with self._session_factory() as session, session.begin():
            # Exact compensation must remain possible after this attempt loses
            # its lease.  It is constrained to random IDs staged by this Run.
            await session.execute(
                delete(PrivateFileRow).where(
                    PrivateFileRow.project_id == run_scope.context.project_id,
                    PrivateFileRow.owner_user_id == str(run_scope.context.user_id),
                    PrivateFileRow.thread_id == run_scope.thread_id,
                    PrivateFileRow.created_by_run_id == run_scope.run_id,
                    PrivateFileRow.id.in_(file_ids),
                    PrivateFileRow.status == "staging",
                )
            )

    @staticmethod
    def _presented_logical_paths(
        run_scope: PrivateFileRunScope,
        after_files: tuple[_AfterFile, ...],
        presented_paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        if type(presented_paths) is not tuple:
            raise PrivateWorkInvalid(run_scope.context.request_id)
        after_by_path = {item.logical_path: item for item in after_files}
        logical_paths: list[str] = []
        for raw_path in presented_paths:
            if type(raw_path) is not str or not raw_path.startswith(_PRESENTED_OUTPUT_PREFIX):
                raise PrivateWorkInvalid(run_scope.context.request_id)
            relative = raw_path[len(_PRESENTED_OUTPUT_PREFIX) :]
            logical_path = normalize_private_logical_path(
                f"outputs/{relative}",
                request_id=run_scope.context.request_id,
            )
            after = after_by_path.get(logical_path)
            if after is None or after.kind != "output":
                raise PrivateWorkInvalid(run_scope.context.request_id)
            logical_paths.append(logical_path)
        return tuple(sorted(set(logical_paths)))

    def _authoritative_file_snapshot(
        self,
        run_scope: PrivateFileRunScope,
        row: PrivateFileRow,
        *,
        limits: WorkspaceChangeLimits,
        chunk_rows: tuple[PrivateFileChunkRow, ...],
    ) -> FileSnapshot:
        virtual_path = f"/mnt/user-data/{row.logical_path}"
        root = row.logical_path.split("/", 1)[0]
        sensitive = is_sensitive_workspace_path(virtual_path)
        binary = PurePosixPath(row.logical_path).suffix.lower() in BINARY_EXTENSIONS
        unavailable_reason = None
        text: str | None = None

        if sensitive:
            unavailable_reason = "sensitive"
        elif binary:
            unavailable_reason = "binary"
        elif row.size > limits.max_file_bytes_for_diff:
            unavailable_reason = "large"
        else:
            content_parts: list[bytes] = []
            whole = hashlib.sha256()
            total = 0
            for expected_index, chunk in enumerate(chunk_rows):
                content = bytes(chunk.content)
                if chunk.chunk_index != expected_index or chunk.size != len(content) or not 0 < len(content) <= PRIVATE_FILE_CHUNK_SIZE or hashlib.sha256(content).hexdigest() != chunk.sha256:
                    raise PrivateWorkUnavailable(run_scope.context.request_id)
                total += len(content)
                whole.update(content)
                content_parts.append(content)
            content = b"".join(content_parts)
            if total != row.size or whole.hexdigest() != row.sha256:
                raise PrivateWorkUnavailable(run_scope.context.request_id)
            sample = content[:SAMPLE_BYTES]
            if workspace_bytes_look_binary(sample):
                binary = True
                unavailable_reason = "binary"
            else:
                text = decode_workspace_text_bytes(content)
                if text is None:
                    binary = True
                    unavailable_reason = "binary"

        return FileSnapshot(
            path=virtual_path,
            root=root,
            size=row.size,
            mtime_ns=0,
            sha256=row.sha256,
            binary=binary,
            sensitive=sensitive,
            text=text,
            content_unavailable_reason=unavailable_reason,
        )

    async def _authoritative_file_snapshots(
        self,
        session: AsyncSession,
        run_scope: PrivateFileRunScope,
        rows: tuple[PrivateFileRow, ...],
        *,
        limits: WorkspaceChangeLimits,
    ) -> dict[uuid.UUID, FileSnapshot]:
        """Verify all needed DB chunks in one bounded, streaming transaction query."""

        unique_rows = {row.id: row for row in rows}
        content_rows = {
            row.id: row
            for row in unique_rows.values()
            if not is_sensitive_workspace_path(
                f"/mnt/user-data/{row.logical_path}",
            )
            and PurePosixPath(row.logical_path).suffix.lower() not in BINARY_EXTENSIONS
            and row.size <= limits.max_file_bytes_for_diff
        }
        snapshots = {
            file_id: self._authoritative_file_snapshot(
                run_scope,
                row,
                limits=limits,
                chunk_rows=(),
            )
            for file_id, row in unique_rows.items()
            if file_id not in content_rows
        }
        pending_content_ids = set(content_rows)

        if content_rows:
            stream = await session.stream_scalars(
                select(PrivateFileChunkRow)
                .where(
                    PrivateFileChunkRow.file_id.in_(tuple(content_rows)),
                )
                .order_by(
                    PrivateFileChunkRow.file_id,
                    PrivateFileChunkRow.chunk_index,
                )
                .execution_options(yield_per=128)
            )
            active_file_id: uuid.UUID | None = None
            active_chunks: list[PrivateFileChunkRow] = []

            def finish_active_file() -> None:
                if active_file_id is None:
                    return
                row = content_rows.get(active_file_id)
                if row is None or active_file_id not in pending_content_ids:
                    raise PrivateWorkUnavailable(run_scope.context.request_id)
                snapshots[active_file_id] = self._authoritative_file_snapshot(
                    run_scope,
                    row,
                    limits=limits,
                    chunk_rows=tuple(active_chunks),
                )
                pending_content_ids.remove(active_file_id)

            try:
                async for chunk in stream:
                    if chunk.file_id not in content_rows:
                        raise PrivateWorkUnavailable(run_scope.context.request_id)
                    if active_file_id is None:
                        active_file_id = chunk.file_id
                    elif chunk.file_id != active_file_id:
                        finish_active_file()
                        active_file_id = chunk.file_id
                        active_chunks = []
                    active_chunks.append(chunk)
                finish_active_file()
            finally:
                await stream.close()

        for file_id in pending_content_ids:
            snapshots[file_id] = self._authoritative_file_snapshot(
                run_scope,
                content_rows[file_id],
                limits=limits,
                chunk_rows=(),
            )

        return {file_id: snapshots[file_id] for file_id in unique_rows}

    async def _workspace_change_result(
        self,
        session: AsyncSession,
        run_scope: PrivateFileRunScope,
        *,
        old_by_path: dict[str, PrivateFileRow],
        promoted: list[PrivateFileRow],
        deleted_paths: list[str],
    ) -> WorkspaceChangeResult | None:
        after_by_path = {row.logical_path: row for row in promoted}
        touched_paths = set(after_by_path) | set(deleted_paths)
        if not touched_paths:
            return None
        limits = WorkspaceChangeLimits()
        all_rows = tuple(
            {
                row.id: row
                for row in (
                    *(old_by_path[path] for path in touched_paths if path in old_by_path),
                    *(after_by_path[path] for path in touched_paths if path in after_by_path),
                )
            }.values()
        )
        snapshots = await self._authoritative_file_snapshots(
            session,
            run_scope,
            all_rows,
            limits=limits,
        )
        before_files: dict[str, FileSnapshot] = {}
        after_files: dict[str, FileSnapshot] = {}
        for logical_path in sorted(touched_paths):
            old = old_by_path.get(logical_path)
            if old is not None:
                before_snapshot = snapshots[old.id]
                before_files[before_snapshot.path] = before_snapshot
            new = after_by_path.get(logical_path)
            if new is not None:
                after_snapshot = snapshots[new.id]
                after_files[after_snapshot.path] = after_snapshot
        result = compare_snapshots(
            WorkspaceSnapshot(files=before_files),
            WorkspaceSnapshot(files=after_files),
            limits=limits,
        )
        return result if result.has_changes() else None

    async def _commit(
        self,
        run_scope: PrivateFileRunScope,
        before_manifest: AuthorityManifest,
        after_files: tuple[_AfterFile, ...],
        staged: tuple[_StagedFile, ...],
        presented_logical_paths: tuple[str, ...],
    ) -> FinalizationResult:
        for attempt in range(2):
            try:
                return await self._commit_attempt(
                    run_scope,
                    before_manifest,
                    after_files,
                    staged,
                    presented_logical_paths,
                )
            except _RetryableFinalizationCommit as error:
                if attempt == 0:
                    logger.warning(
                        "Private file finalization transaction failed phase=%s outcome=retrying failure_type=%s",
                        error.phase,
                        error.failure_type,
                    )
                    continue
                logger.error(
                    "Private file finalization transaction failed phase=%s outcome=failed failure_type=%s",
                    error.phase,
                    error.failure_type,
                )
                raise PrivateWorkUnavailable(
                    run_scope.context.request_id,
                ) from None
        raise AssertionError("private file finalization retry loop exhausted")

    async def _read_finalization_receipt(
        self,
        run_scope: PrivateFileRunScope,
    ) -> str | None:
        async with self._session_factory() as session, session.begin():
            await self._revalidator.require(
                session,
                run_scope.context,
                Capability.PRIVATE_WORK_CREATE,
                lock_mode="update",
            )
            thread = (
                await session.execute(
                    select(ThreadMetaRow.thread_id)
                    .where(
                        ThreadMetaRow.project_id == run_scope.context.project_id,
                        ThreadMetaRow.owner_user_id == str(run_scope.context.user_id),
                        ThreadMetaRow.thread_id == run_scope.thread_id,
                        ThreadMetaRow.deleted_at.is_(None),
                        ThreadMetaRow.frozen_at.is_(None),
                    )
                    .with_for_update(of=ThreadMetaRow)
                )
            ).scalar_one_or_none()
            if thread is None:
                raise PrivateWorkUnavailable(run_scope.context.request_id)
            # Preserve the mutation lock order while observing the atomic Run
            # receipt: project/member -> Thread -> Job -> Run/Attempt.
            await self._authorize_mutation(run_scope, session)
            return await session.scalar(
                select(RunRow.finalization_status)
                .where(
                    RunRow.run_id == run_scope.run_id,
                    RunRow.project_id == run_scope.context.project_id,
                    RunRow.owner_user_id == str(run_scope.context.user_id),
                    RunRow.thread_id == run_scope.thread_id,
                    RunRow.status.in_(("pending", "running")),
                )
                .with_for_update(of=RunRow)
            )

    @asynccontextmanager
    async def _commit_transaction(
        self,
        run_scope: PrivateFileRunScope,
        state: _FinalizationCommitState,
    ) -> AsyncIterator[AsyncSession]:
        try:
            async with self._session_factory() as session, session.begin():
                yield session
        except PrivateWorkRetryableUnavailable as error:
            logger.warning(
                "Private file finalization transaction failed phase=%s outcome=retrying reason_code=%s failure_type=%s",
                state.phase,
                error.reason_code,
                type(error).__name__,
            )
            raise _RetryableFinalizationCommit(
                phase=state.phase,
                failure_type=type(error).__name__,
            ) from None
        except PrivateWorkUnavailable as error:
            logger.error(
                "Private file finalization transaction failed phase=%s outcome=failed reason_code=private_work_unavailable failure_type=%s",
                state.phase,
                type(error).__name__,
            )
            raise
        except (PrivateWorkError, AuthorizationRevoked):
            raise
        except Exception as error:
            failure_type = type(error).__name__
            if state.body_complete and state.result is not None:
                receipt: str | None = None
                for receipt_attempt in range(2):
                    try:
                        receipt = await self._read_finalization_receipt(run_scope)
                        break
                    except Exception as receipt_error:
                        if isinstance(
                            receipt_error,
                            (PrivateWorkError, AuthorizationRevoked),
                        ) and not isinstance(receipt_error, PrivateWorkUnavailable):
                            raise
                        if receipt_attempt == 0:
                            logger.warning(
                                "Private file finalization transaction failed phase=commit_reconcile outcome=retrying failure_type=%s reconcile_failure_type=%s",
                                failure_type,
                                type(receipt_error).__name__,
                            )
                            continue
                        logger.error(
                            "Private file finalization transaction failed phase=commit_reconcile outcome=unknown failure_type=%s reconcile_failure_type=%s",
                            failure_type,
                            type(receipt_error).__name__,
                        )
                        raise PrivateWorkUnavailable(
                            run_scope.context.request_id,
                        ) from None
                if receipt == "complete":
                    logger.warning(
                        "Private file finalization transaction failed phase=commit_ack outcome=recovered failure_type=%s",
                        failure_type,
                    )
                    return
                if receipt != "finalizing":
                    logger.error(
                        "Private file finalization transaction failed phase=commit_reconcile outcome=invalid_receipt failure_type=%s",
                        failure_type,
                    )
                    raise PrivateWorkUnavailable(
                        run_scope.context.request_id,
                    ) from None
            if _is_transient_database_error(error):
                raise _RetryableFinalizationCommit(
                    phase=state.phase,
                    failure_type=failure_type,
                ) from None
            raise

    async def _commit_attempt(
        self,
        run_scope: PrivateFileRunScope,
        before_manifest: AuthorityManifest,
        after_files: tuple[_AfterFile, ...],
        staged: tuple[_StagedFile, ...],
        presented_logical_paths: tuple[str, ...],
    ) -> FinalizationResult:
        now = datetime.now(UTC)
        before = before_manifest.by_logical_path()
        after_paths = {item.logical_path for item in after_files}
        changed_by_path = {item.after.logical_path: item for item in staged}
        deleted_paths = sorted(path for path, entry in before.items() if entry.kind in {"workspace", "output"} and path not in after_paths)
        touched_paths = sorted(set(changed_by_path) | set(deleted_paths))
        state = _FinalizationCommitState()
        async with self._commit_transaction(run_scope, state) as session:
            await self._revalidator.require(
                session,
                run_scope.context,
                Capability.PRIVATE_WORK_CREATE,
                lock_mode="update",
            )
            thread = (
                await session.execute(
                    select(ThreadMetaRow.thread_id)
                    .where(
                        ThreadMetaRow.project_id == run_scope.context.project_id,
                        ThreadMetaRow.owner_user_id == str(run_scope.context.user_id),
                        ThreadMetaRow.thread_id == run_scope.thread_id,
                        ThreadMetaRow.deleted_at.is_(None),
                        ThreadMetaRow.frozen_at.is_(None),
                    )
                    .with_for_update(of=ThreadMetaRow)
                )
            ).scalar_one_or_none()
            if thread is None:
                raise PrivateWorkUnavailable(run_scope.context.request_id)
            # Match cancellation: project/member -> Thread -> Job -> Run.
            await self._authorize_mutation(run_scope, session)
            run = (
                await session.execute(
                    select(RunRow)
                    .where(
                        RunRow.run_id == run_scope.run_id,
                        RunRow.project_id == run_scope.context.project_id,
                        RunRow.owner_user_id == str(run_scope.context.user_id),
                        RunRow.thread_id == run_scope.thread_id,
                        RunRow.status.in_(("pending", "running")),
                        RunRow.finalization_status == "finalizing",
                    )
                    .with_for_update(of=RunRow)
                )
            ).scalar_one_or_none()
            if run is None:
                raise PrivateWorkUnavailable(run_scope.context.request_id)

            current_rows = (
                (
                    await session.execute(
                        select(PrivateFileRow)
                        .where(
                            PrivateFileRow.project_id == run_scope.context.project_id,
                            PrivateFileRow.owner_user_id == str(run_scope.context.user_id),
                            PrivateFileRow.thread_id == run_scope.thread_id,
                            PrivateFileRow.status == "ready",
                        )
                        .order_by(PrivateFileRow.logical_path, PrivateFileRow.id)
                        .with_for_update(of=PrivateFileRow)
                    )
                )
                .scalars()
                .all()
            )
            expected_authority = _canonical_ready_authority(
                before_manifest.entries,
            )
            current_authority = _canonical_ready_authority(current_rows)
            if len({entry.logical_path for entry in before_manifest.entries}) != len(before_manifest.entries) or current_authority != expected_authority:
                raise PrivateWorkUnavailable(run_scope.context.request_id)

            state.phase = "commit_quota"
            current_by_path = {row.logical_path: row for row in current_rows}
            old_rows = [current_by_path[path] for path in touched_paths if path in current_by_path]
            old_by_path = {row.logical_path: row for row in old_rows}
            for row in old_rows:
                await self._quota.release_file(
                    session,
                    run_scope.resource_scope,
                    file_id=row.id,
                    size=row.size,
                    request_id=run_scope.context.request_id,
                )
            for item in staged:
                await self._quota.reserve_file(
                    session,
                    run_scope.context,
                    file_id=item.id,
                    size=item.size,
                )
            state.phase = "commit_files"
            deleted_ids: list[uuid.UUID] = []
            for row in old_rows:
                row.status = "deleted"
                row.deleted_at = now
                row.updated_at = now
                deleted_ids.append(row.id)

            promoted: list[PrivateFileRow] = []
            for logical_path in sorted(changed_by_path):
                item = changed_by_path[logical_path]
                row = (
                    await session.execute(
                        select(PrivateFileRow)
                        .where(
                            PrivateFileRow.id == item.id,
                            PrivateFileRow.project_id == run_scope.context.project_id,
                            PrivateFileRow.owner_user_id == str(run_scope.context.user_id),
                            PrivateFileRow.thread_id == run_scope.thread_id,
                            PrivateFileRow.status == "staging",
                        )
                        .with_for_update(of=PrivateFileRow)
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise PrivateWorkUnavailable(run_scope.context.request_id)
                old = old_by_path.get(logical_path)
                row.logical_path = logical_path
                row.size = item.size
                row.sha256 = item.sha256
                row.status = "ready"
                row.version = 1 if old is None else old.version + 1
                row.source_file_id = old.id if old is not None and row.kind == "workspace" else None
                row.updated_at = now
                promoted.append(row)

            after_ready: dict[str, PrivateFileRow] = {row.logical_path: row for row in promoted}
            unchanged_paths = sorted(after_paths - set(changed_by_path))
            after_ready.update({path: current_by_path[path] for path in unchanged_paths if path in current_by_path})

            state.phase = "commit_artifacts"
            existing_artifact_rows = (
                (
                    await session.execute(
                        select(PrivateArtifactRow)
                        .where(
                            PrivateArtifactRow.project_id == run_scope.context.project_id,
                            PrivateArtifactRow.owner_user_id == str(run_scope.context.user_id),
                            PrivateArtifactRow.thread_id == run_scope.thread_id,
                            PrivateArtifactRow.run_id == run_scope.run_id,
                            PrivateArtifactRow.deleted_at.is_(None),
                        )
                        .order_by(
                            PrivateArtifactRow.created_at,
                            PrivateArtifactRow.id,
                        )
                        .with_for_update(of=PrivateArtifactRow)
                    )
                )
                .scalars()
                .all()
            )
            existing_artifacts: dict[tuple[str, uuid.UUID], PrivateArtifactRow] = {}
            for existing in existing_artifact_rows:
                metadata = existing.artifact_metadata
                logical_path = metadata.get("logical_path") if isinstance(metadata, dict) else None
                if isinstance(logical_path, str):
                    existing_artifacts.setdefault(
                        (logical_path, existing.file_id),
                        existing,
                    )

            artifacts: list[PrivateArtifactRow] = []
            created_artifact_count = 0
            for logical_path in presented_logical_paths:
                file_row = after_ready.get(logical_path)
                if file_row is None:
                    raise PrivateWorkUnavailable(run_scope.context.request_id)
                artifact = existing_artifacts.get(
                    (logical_path, file_row.id),
                )
                if artifact is None:
                    artifact = PrivateArtifactRow(
                        id=uuid.uuid4(),
                        project_id=run_scope.context.project_id,
                        owner_user_id=str(run_scope.context.user_id),
                        thread_id=run_scope.thread_id,
                        run_id=run_scope.run_id,
                        file_id=file_row.id,
                        display_name=PurePosixPath(logical_path).name,
                        media_type=file_row.media_type,
                        artifact_metadata={"logical_path": logical_path},
                        created_at=now,
                    )
                    session.add(artifact)
                    existing_artifacts[(logical_path, file_row.id)] = artifact
                    created_artifact_count += 1
                artifacts.append(artifact)

                # The obligation FK must only be updated after the Artifact is
                # a real row in this same transaction. This gives the helper an
                # exact DB object to revalidate and avoids relying on eventual
                # unit-of-work ordering for a pending Artifact.
                await session.flush()

                delivery = getattr(
                    self._output_delivery_port,
                    "deliver_output_obligation_in_session",
                    None,
                )
                if callable(delivery):
                    await delivery(
                        session,
                        artifact_id=artifact.id,
                        logical_path=logical_path,
                    )

            run.finalization_status = "complete"
            run.updated_at = now
            state.phase = "commit_audit"
            if self._audit is not None:
                try:
                    job_id = uuid.UUID(str(run.job_id))
                except (AttributeError, TypeError, ValueError):
                    raise PrivateWorkUnavailable(run_scope.context.request_id) from None
                if type(run.origin_trace_id) is not str or not run.origin_trace_id:
                    raise PrivateWorkUnavailable(run_scope.context.request_id)
                await self._audit.run_files_finalized(
                    session,
                    run_scope.resource_scope,
                    run_id=run.run_id,
                    job_id=job_id,
                    request_id=run.origin_trace_id,
                    created_count=sum(path not in before for path in changed_by_path),
                    modified_count=sum(path in before for path in changed_by_path),
                    deleted_count=len(deleted_paths),
                    artifact_count=created_artifact_count,
                    committed_bytes=sum(item.size for item in staged),
                )
            await session.flush()
            state.phase = "commit_diff"
            workspace_changes = await self._workspace_change_result(
                session,
                run_scope,
                old_by_path=old_by_path,
                promoted=promoted,
                deleted_paths=deleted_paths,
            )
            file_records = tuple(PrivateFileRepository._file_record(row) for row in promoted)
            artifact_records = tuple(
                PrivateArtifactRecord(
                    id=row.id,
                    project_id=row.project_id,
                    owner_user_id=row.owner_user_id,
                    thread_id=row.thread_id,
                    run_id=row.run_id,
                    file_id=row.file_id,
                    display_name=row.display_name,
                    media_type=row.media_type,
                    metadata=dict(row.artifact_metadata),
                    created_at=row.created_at,
                    deleted_at=row.deleted_at,
                )
                for row in artifacts
            )
            state.result = FinalizationResult(
                files=file_records,
                artifacts=artifact_records,
                deleted_file_ids=tuple(deleted_ids),
                workspace_changes=workspace_changes,
                produced_output_paths=tuple(sorted(path for path in changed_by_path if path.startswith("outputs/"))),
            )
            state.body_complete = True
            state.phase = "commit_ack"
        if state.result is None:
            raise PrivateWorkUnavailable(run_scope.context.request_id)
        return state.result

    async def finalize(
        self,
        run_scope: PrivateFileRunScope,
        before_manifest: AuthorityManifest,
        sandbox: Any,
        presented_paths: tuple[str, ...] = (),
    ) -> FinalizationResult:
        staged: list[_StagedFile] = []
        staging_ids: list[uuid.UUID] = []
        committed = False
        phase = "begin"
        try:
            await self._set_run_finalization(run_scope, "finalizing")
            phase = "authorization"
            boundary = run_scope.authorization_boundary
            checker = getattr(boundary, "before_file_finalization", None)
            if callable(checker):
                await checker()
            phase = "scan"
            after_files = await _joined_to_thread(self._scan, run_scope, sandbox)
            before = before_manifest.by_logical_path()
            changed_files: list[_AfterFile] = []
            phase = "hash"
            for after in after_files:
                old = before.get(after.logical_path)
                if old is not None and old.size == after.size:
                    if (
                        await self._hash_sandbox_file(
                            run_scope,
                            sandbox,
                            after,
                        )
                        == old.sha256
                    ):
                        continue
                changed_files.append(after)
            self._validate_changed_files(run_scope, tuple(changed_files))
            phase = "stage"
            for after in changed_files:
                file_id = uuid.uuid4()
                staging_ids.append(file_id)
                staged.append(
                    await self._stage_file(
                        run_scope,
                        sandbox,
                        after,
                        file_id=file_id,
                    )
                )
            phase = "verify"
            verified_after_files = await _joined_to_thread(
                self._scan,
                run_scope,
                sandbox,
            )
            if verified_after_files != after_files:
                raise PrivateWorkInvalid(run_scope.context.request_id)
            staged_hashes = {item.after.logical_path: item.sha256 for item in staged}
            for after in verified_after_files:
                expected_hash = staged_hashes.get(after.logical_path)
                if expected_hash is None:
                    old = before.get(after.logical_path)
                    if old is None:
                        raise PrivateWorkInvalid(run_scope.context.request_id)
                    expected_hash = old.sha256
                if (
                    await self._hash_sandbox_file(
                        run_scope,
                        sandbox,
                        after,
                    )
                    != expected_hash
                ):
                    raise PrivateWorkInvalid(run_scope.context.request_id)
            presented_logical_paths = self._presented_logical_paths(
                run_scope,
                verified_after_files,
                presented_paths,
            )
            phase = "commit"
            result = await self._commit(
                run_scope,
                before_manifest,
                verified_after_files,
                tuple(staged),
                presented_logical_paths,
            )
            committed = True
            return result
        except PrivateWorkUnavailable as error:
            logger.error(
                "Private file finalization failed phase=%s reason_code=%s failure_type=%s",
                phase,
                getattr(error, "reason_code", "private_work_unavailable"),
                type(error).__name__,
            )
            raise
        except (PrivateWorkError, AuthorizationRevoked):
            raise
        except Exception as error:
            logger.error(
                "Private file finalization failed phase=%s failure_type=%s",
                phase,
                type(error).__name__,
            )
            raise PrivateWorkUnavailable(run_scope.context.request_id) from None
        finally:
            if not committed:
                try:
                    await self._cleanup_staging(
                        run_scope,
                        tuple(staging_ids),
                    )
                finally:
                    try:
                        await self.mark_failed(run_scope)
                    except PrivateWorkError:
                        # A stale task cannot mutate Run state.  The durable
                        # job terminal hook converges finalization status.
                        pass
