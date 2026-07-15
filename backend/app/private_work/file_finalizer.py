from __future__ import annotations

import hashlib
import mimetypes
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.errors import (
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkTooLarge,
    PrivateWorkUnavailable,
)
from app.private_work.file_paths import normalize_private_logical_path
from app.private_work.file_service import PrivateFileLimits
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.sandbox_files import (
    AuthorityManifest,
    PrivateFileRunScope,
    _joined_to_thread,
)
from app.projects.capabilities import Capability
from deerflow.persistence.private_work.file_repository import (
    PRIVATE_FILE_CHUNK_SIZE,
    PrivateArtifactRecord,
    PrivateFileRepository,
)
from deerflow.persistence.private_work.model import (
    PrivateArtifactRow,
    PrivateFileRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow

_SCAN_ROOTS = (
    ("/mnt/user-data/workspace", "workspace", "workspace"),
    ("/mnt/user-data/outputs", "outputs", "output"),
)
_PRESENTED_OUTPUT_PREFIX = "/mnt/user-data/outputs/"


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
    workspace_changes: dict[str, list[str]] | None


class PrivateFileFinalizer:
    """Commit verified workspace/output changes before a private Run terminates."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        limits: PrivateFileLimits | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits or PrivateFileLimits()
        self._revalidator = PrivateWorkRevalidator()

    async def _set_run_finalization(
        self,
        run_scope: PrivateFileRunScope,
        status: str,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(RunRow)
                .where(
                    RunRow.run_id == run_scope.run_id,
                    RunRow.project_id == run_scope.context.project_id,
                    RunRow.owner_user_id == str(run_scope.context.user_id),
                    RunRow.thread_id == run_scope.thread_id,
                    RunRow.status.in_(("pending", "running")),
                )
                .values(finalization_status=status, updated_at=datetime.now(UTC))
            )
            if result.rowcount != 1:
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
        count = 0
        total = 0
        for virtual_root, logical_root, kind in _SCAN_ROOTS:
            entries = sandbox.list_secure_files(virtual_root)
            prefix = virtual_root.rstrip("/") + "/"
            for entry in entries:
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
                count += 1
                total += entry.size
                if count > self._limits.max_files or entry.size > self._limits.max_file_size or total > self._limits.max_total_size:
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
        return tuple(sorted(files, key=lambda item: item.logical_path))

    async def _hash_sandbox_file(
        self,
        run_scope: PrivateFileRunScope,
        sandbox: Any,
        after: _AfterFile,
    ) -> str:
        handle = await _joined_to_thread(
            sandbox.open_regular_file,
            after.virtual_path,
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
                if total > after.size or total > self._limits.max_file_size:
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
                lock=True,
            )
            await PrivateFileRepository(session).stage(
                scope=run_scope.resource_scope,
                thread_id=run_scope.thread_id,
                kind=after.kind,
                logical_path=temporary_logical_path,
                media_type=after.media_type,
                created_by_run_id=run_scope.run_id,
                file_id=file_id,
            )

        handle = await _joined_to_thread(sandbox.open_regular_file, after.virtual_path)
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
                if total > after.size or total > self._limits.max_file_size:
                    raise PrivateWorkInvalid(run_scope.context.request_id)
                async with self._session_factory() as session, session.begin():
                    await self._revalidator.require(
                        session,
                        run_scope.context,
                        Capability.PRIVATE_WORK_CREATE,
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
            await session.execute(
                delete(PrivateFileRow).where(
                    PrivateFileRow.project_id == run_scope.context.project_id,
                    PrivateFileRow.owner_user_id == str(run_scope.context.user_id),
                    PrivateFileRow.thread_id == run_scope.thread_id,
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

    async def _commit(
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
        async with self._session_factory() as session, session.begin():
            await self._revalidator.require(
                session,
                run_scope.context,
                Capability.PRIVATE_WORK_CREATE,
                lock=True,
            )
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

            old_rows = (
                (
                    await session.execute(
                        select(PrivateFileRow)
                        .where(
                            PrivateFileRow.project_id == run_scope.context.project_id,
                            PrivateFileRow.owner_user_id == str(run_scope.context.user_id),
                            PrivateFileRow.thread_id == run_scope.thread_id,
                            PrivateFileRow.logical_path.in_(touched_paths),
                            PrivateFileRow.status == "ready",
                        )
                        .order_by(PrivateFileRow.logical_path, PrivateFileRow.id)
                        .with_for_update(of=PrivateFileRow)
                    )
                )
                .scalars()
                .all()
            )
            old_by_path = {row.logical_path: row for row in old_rows}
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
            if unchanged_paths:
                rows = (
                    await session.execute(
                        select(PrivateFileRow).where(
                            PrivateFileRow.project_id == run_scope.context.project_id,
                            PrivateFileRow.owner_user_id == str(run_scope.context.user_id),
                            PrivateFileRow.thread_id == run_scope.thread_id,
                            PrivateFileRow.logical_path.in_(unchanged_paths),
                            PrivateFileRow.status == "ready",
                        )
                    )
                ).scalars()
                after_ready.update({row.logical_path: row for row in rows})

            artifacts: list[PrivateArtifactRow] = []
            for logical_path in presented_logical_paths:
                file_row = after_ready.get(logical_path)
                if file_row is None:
                    raise PrivateWorkUnavailable(run_scope.context.request_id)
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
                artifacts.append(artifact)

            run.finalization_status = "complete"
            run.updated_at = now
            await session.flush()
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
            return FinalizationResult(
                files=file_records,
                artifacts=artifact_records,
                deleted_file_ids=tuple(deleted_ids),
                workspace_changes=(
                    {
                        "created": sorted(path for path in changed_by_path if path not in before),
                        "modified": sorted(path for path in changed_by_path if path in before),
                        "deleted": deleted_paths,
                    }
                    if changed_by_path or deleted_paths
                    else None
                ),
            )

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
        try:
            await self._set_run_finalization(run_scope, "finalizing")
            boundary = run_scope.authorization_boundary
            checker = getattr(boundary, "before_file_finalization", None)
            if callable(checker):
                await checker()
            after_files = await _joined_to_thread(self._scan, run_scope, sandbox)
            before = before_manifest.by_logical_path()
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
            result = await self._commit(
                run_scope,
                before_manifest,
                verified_after_files,
                tuple(staged),
                presented_logical_paths,
            )
            committed = True
            return result
        except PrivateWorkError:
            raise
        except Exception:
            raise PrivateWorkUnavailable(run_scope.context.request_id) from None
        finally:
            if not committed:
                try:
                    await self._cleanup_staging(
                        run_scope,
                        tuple(staging_ids),
                    )
                finally:
                    await self.mark_failed(run_scope)
