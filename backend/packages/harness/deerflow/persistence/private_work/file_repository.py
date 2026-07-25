from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.private_work.model import (
    PrivateArtifactRow,
    PrivateFileChunkRow,
    PrivateFileRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.private_scope import PrivateResourceScope

PRIVATE_FILE_CHUNK_SIZE = 1024 * 1024
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class PrivateFileConflict(Exception):
    """A scoped lifecycle invariant was not satisfied."""


class PrivateFileIntegrityError(Exception):
    """Persisted file bytes do not match their authoritative metadata."""


@dataclass(frozen=True, slots=True)
class PrivateFileRecord:
    id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    thread_id: str
    kind: str
    logical_path: str
    media_type: str
    size: int
    sha256: str
    status: str
    version: int
    created_by_run_id: str | None
    source_file_id: uuid.UUID | None
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PrivateFileChunkRecord:
    file_id: uuid.UUID
    chunk_index: int
    content: bytes
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PrivateArtifactRecord:
    id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    thread_id: str
    run_id: str
    file_id: uuid.UUID
    display_name: str
    media_type: str
    metadata: dict
    created_at: datetime
    deleted_at: datetime | None


class PrivateFileRepository:
    """Session-bound PostgreSQL file authority with scope on every statement."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _coordinates(scope: PrivateResourceScope) -> tuple[uuid.UUID, str]:
        if type(scope) is not PrivateResourceScope:
            raise PrivateFileConflict
        try:
            return uuid.UUID(scope.project_id), str(uuid.UUID(scope.owner_user_id))
        except (TypeError, ValueError):
            raise PrivateFileConflict from None

    @classmethod
    def _file_scope(cls, scope: PrivateResourceScope, thread_id: str):
        project_id, owner_user_id = cls._coordinates(scope)
        return (
            PrivateFileRow.project_id == project_id,
            PrivateFileRow.owner_user_id == owner_user_id,
            PrivateFileRow.thread_id == thread_id,
        )

    @staticmethod
    def _active_thread_join():
        return (
            (ThreadMetaRow.thread_id == PrivateFileRow.thread_id)
            & (ThreadMetaRow.project_id == PrivateFileRow.project_id)
            & (ThreadMetaRow.owner_user_id == PrivateFileRow.owner_user_id)
            & ThreadMetaRow.deleted_at.is_(None)
            & ThreadMetaRow.frozen_at.is_(None)
        )

    @staticmethod
    def _file_record(row: PrivateFileRow) -> PrivateFileRecord:
        return PrivateFileRecord(
            id=row.id,
            project_id=row.project_id,
            owner_user_id=row.owner_user_id,
            thread_id=row.thread_id,
            kind=row.kind,
            logical_path=row.logical_path,
            media_type=row.media_type,
            size=row.size,
            sha256=row.sha256,
            status=row.status,
            version=row.version,
            created_by_run_id=row.created_by_run_id,
            source_file_id=row.source_file_id,
            deleted_at=row.deleted_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _chunk_record(row: PrivateFileChunkRow) -> PrivateFileChunkRecord:
        return PrivateFileChunkRecord(
            file_id=row.file_id,
            chunk_index=row.chunk_index,
            content=bytes(row.content),
            size=row.size,
            sha256=row.sha256,
        )

    async def _lock_thread(self, scope: PrivateResourceScope, thread_id: str) -> None:
        project_id, owner_user_id = self._coordinates(scope)
        found = (
            await self.session.execute(
                select(ThreadMetaRow.thread_id)
                .where(
                    ThreadMetaRow.thread_id == thread_id,
                    ThreadMetaRow.project_id == project_id,
                    ThreadMetaRow.owner_user_id == owner_user_id,
                    ThreadMetaRow.deleted_at.is_(None),
                    ThreadMetaRow.frozen_at.is_(None),
                )
                .with_for_update(of=ThreadMetaRow)
            )
        ).scalar_one_or_none()
        if found is None:
            raise PrivateFileConflict

    async def stage(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        kind: str,
        logical_path: str,
        media_type: str,
        created_by_run_id: str | None = None,
        source_file_id: uuid.UUID | None = None,
        file_id: uuid.UUID | None = None,
    ) -> PrivateFileRecord:
        project_id, owner_user_id = self._coordinates(scope)
        if file_id is not None and type(file_id) is not uuid.UUID:
            raise PrivateFileConflict
        await self._lock_thread(scope, thread_id)
        if source_file_id is not None:
            if kind != "workspace":
                raise PrivateFileConflict
            source = (
                await self.session.execute(
                    select(PrivateFileRow.id).where(
                        PrivateFileRow.id == source_file_id,
                        *self._file_scope(scope, thread_id),
                        PrivateFileRow.status == "ready",
                    )
                )
            ).scalar_one_or_none()
            if source is None:
                raise PrivateFileConflict
        version = (
            await self.session.scalar(
                select(func.coalesce(func.max(PrivateFileRow.version), 0)).where(
                    *self._file_scope(scope, thread_id),
                    PrivateFileRow.logical_path == logical_path,
                )
            )
        ) + 1
        now = datetime.now(UTC)
        row = PrivateFileRow(
            id=file_id or uuid.uuid4(),
            project_id=project_id,
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            kind=kind,
            logical_path=logical_path,
            media_type=media_type or "application/octet-stream",
            size=0,
            sha256=_EMPTY_SHA256,
            status="staging",
            version=version,
            created_by_run_id=created_by_run_id,
            source_file_id=source_file_id,
            created_at=now,
            updated_at=now,
        )
        try:
            self.session.add(row)
            await self.session.flush()
        except IntegrityError:
            raise PrivateFileConflict from None
        return self._file_record(row)

    async def get(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        file_id: uuid.UUID,
        lock: bool = False,
    ) -> PrivateFileRecord | None:
        statement = select(PrivateFileRow).where(
            PrivateFileRow.id == file_id,
            *self._file_scope(scope, thread_id),
        )
        if lock:
            statement = statement.with_for_update(of=PrivateFileRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else self._file_record(row)

    async def get_ready(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        file_id: uuid.UUID,
    ) -> PrivateFileRecord | None:
        row = (
            await self.session.execute(
                select(PrivateFileRow)
                .join(ThreadMetaRow, self._active_thread_join())
                .where(
                    PrivateFileRow.id == file_id,
                    *self._file_scope(scope, thread_id),
                    PrivateFileRow.status == "ready",
                )
            )
        ).scalar_one_or_none()
        return None if row is None else self._file_record(row)

    async def list_ready(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        after: tuple[str, int, uuid.UUID] | None = None,
        limit: int = 100,
    ) -> tuple[PrivateFileRecord, ...]:
        """List every ready file kind in stable logical-path/version/id order."""

        if not 1 <= limit <= 100:
            raise PrivateFileConflict
        statement = (
            select(PrivateFileRow)
            .join(ThreadMetaRow, self._active_thread_join())
            .where(
                *self._file_scope(scope, thread_id),
                PrivateFileRow.status == "ready",
            )
        )
        if after is not None:
            try:
                logical_path, version, file_id = after
                if not isinstance(logical_path, str) or not isinstance(version, int):
                    raise ValueError
                file_id = uuid.UUID(str(file_id))
            except (TypeError, ValueError):
                raise PrivateFileConflict from None
            statement = statement.where(
                or_(
                    PrivateFileRow.logical_path > logical_path,
                    and_(
                        PrivateFileRow.logical_path == logical_path,
                        PrivateFileRow.version > version,
                    ),
                    and_(
                        PrivateFileRow.logical_path == logical_path,
                        PrivateFileRow.version == version,
                        PrivateFileRow.id > file_id,
                    ),
                )
            )
        rows = (
            await self.session.execute(
                statement.order_by(
                    PrivateFileRow.logical_path,
                    PrivateFileRow.version,
                    PrivateFileRow.id,
                ).limit(limit)
            )
        ).scalars()
        return tuple(self._file_record(row) for row in rows)

    async def append_chunk(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        file_id: uuid.UUID,
        chunk_index: int,
        content: bytes,
        size: int,
        sha256: str,
    ) -> PrivateFileChunkRecord:
        if not isinstance(content, bytes) or size != len(content) or not 0 < size <= PRIVATE_FILE_CHUNK_SIZE or hashlib.sha256(content).hexdigest() != sha256:
            raise PrivateFileIntegrityError
        await self._lock_thread(scope, thread_id)
        file_row = (
            await self.session.execute(
                select(PrivateFileRow)
                .where(
                    PrivateFileRow.id == file_id,
                    *self._file_scope(scope, thread_id),
                )
                .with_for_update(of=PrivateFileRow)
            )
        ).scalar_one_or_none()
        if file_row is None or file_row.status != "staging":
            raise PrivateFileConflict
        next_index = (await self.session.scalar(select(func.coalesce(func.max(PrivateFileChunkRow.chunk_index), -1)).where(PrivateFileChunkRow.file_id == file_id))) + 1
        if chunk_index != next_index:
            raise PrivateFileIntegrityError
        row = PrivateFileChunkRow(
            file_id=file_id,
            chunk_index=chunk_index,
            content=content,
            size=size,
            sha256=sha256,
        )
        try:
            self.session.add(row)
            await self.session.flush()
        except IntegrityError:
            raise PrivateFileConflict from None
        return self._chunk_record(row)

    async def finalize(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        file_id: uuid.UUID,
        expected_size: int,
        expected_sha256: str,
    ) -> PrivateFileRecord:
        await self._lock_thread(scope, thread_id)
        file_row = (
            await self.session.execute(
                select(PrivateFileRow)
                .where(
                    PrivateFileRow.id == file_id,
                    *self._file_scope(scope, thread_id),
                )
                .with_for_update(of=PrivateFileRow)
            )
        ).scalar_one_or_none()
        if file_row is None or file_row.status != "staging":
            raise PrivateFileConflict

        whole = hashlib.sha256()
        total = 0
        expected_index = 0
        result = await self.session.stream(select(PrivateFileChunkRow).where(PrivateFileChunkRow.file_id == file_id).order_by(PrivateFileChunkRow.chunk_index).execution_options(yield_per=1))
        async for chunk in result.scalars():
            content = bytes(chunk.content)
            if chunk.chunk_index != expected_index or chunk.size != len(content) or not 0 < chunk.size <= PRIVATE_FILE_CHUNK_SIZE or hashlib.sha256(content).hexdigest() != chunk.sha256:
                raise PrivateFileIntegrityError
            whole.update(content)
            total += len(content)
            expected_index += 1
        if total != expected_size or whole.hexdigest() != expected_sha256:
            raise PrivateFileIntegrityError
        file_row.size = total
        file_row.sha256 = whole.hexdigest()
        file_row.status = "ready"
        file_row.updated_at = datetime.now(UTC)
        await self.session.flush()
        # The PostgreSQL BEFORE UPDATE trigger owns the durable timestamp and
        # uses the transaction timestamp. Refresh it before constructing the
        # response so an immediate GET cannot appear to move time backwards.
        await self.session.refresh(file_row, attribute_names=("updated_at",))
        return self._file_record(file_row)

    async def abort(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        file_id: uuid.UUID,
    ) -> bool:
        try:
            await self._lock_thread(scope, thread_id)
        except PrivateFileConflict:
            return False
        result = await self.session.execute(
            delete(PrivateFileRow).where(
                PrivateFileRow.id == file_id,
                *self._file_scope(scope, thread_id),
                PrivateFileRow.status == "staging",
            )
        )
        return result.rowcount != 0

    async def purge_staging(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        file_ids: Sequence[uuid.UUID],
    ) -> int:
        """Trusted cleanup for exact staged IDs, including after revocation."""

        if not file_ids:
            return 0
        result = await self.session.execute(
            delete(PrivateFileRow).where(
                PrivateFileRow.id.in_(tuple(file_ids)),
                *self._file_scope(scope, thread_id),
                PrivateFileRow.status == "staging",
            )
        )
        return int(result.rowcount or 0)

    async def delete_ready(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        file_id: uuid.UUID,
    ) -> PrivateFileRecord:
        """Physically delete one exact ready file and its dependent private bytes."""

        project_id, owner_user_id = self._coordinates(scope)
        await self._lock_thread(scope, thread_id)
        row = (
            await self.session.execute(
                select(PrivateFileRow)
                .where(
                    PrivateFileRow.id == file_id,
                    *self._file_scope(scope, thread_id),
                )
                .with_for_update(of=PrivateFileRow)
            )
        ).scalar_one_or_none()
        if row is None or row.status != "ready":
            raise PrivateFileConflict

        current = self._file_record(row)
        await self.session.execute(
            delete(PrivateArtifactRow).where(
                PrivateArtifactRow.project_id == project_id,
                PrivateArtifactRow.owner_user_id == owner_user_id,
                PrivateArtifactRow.thread_id == thread_id,
                PrivateArtifactRow.file_id == file_id,
            )
        )
        await self.session.execute(
            update(PrivateFileRow)
            .where(
                PrivateFileRow.project_id == project_id,
                PrivateFileRow.owner_user_id == owner_user_id,
                PrivateFileRow.source_file_id == file_id,
            )
            .values(source_file_id=None)
        )
        await self.session.execute(
            delete(PrivateFileChunkRow).where(
                PrivateFileChunkRow.file_id == file_id,
            )
        )
        result = await self.session.execute(
            delete(PrivateFileRow).where(
                PrivateFileRow.id == file_id,
                *self._file_scope(scope, thread_id),
                PrivateFileRow.status == "ready",
            )
        )
        if result.rowcount != 1:
            raise PrivateFileConflict
        now = datetime.now(UTC)
        return replace(
            current,
            status="deleted",
            deleted_at=now,
            updated_at=now,
        )

    async def fetch_chunk_page(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        file_id: uuid.UUID,
        after_index: int,
        limit: int,
    ) -> tuple[PrivateFileChunkRecord, ...]:
        if limit < 1:
            raise PrivateFileConflict
        rows = (
            await self.session.execute(
                select(PrivateFileChunkRow)
                .join(PrivateFileRow, PrivateFileRow.id == PrivateFileChunkRow.file_id)
                .join(ThreadMetaRow, self._active_thread_join())
                .where(
                    PrivateFileChunkRow.file_id == file_id,
                    PrivateFileChunkRow.chunk_index > after_index,
                    *self._file_scope(scope, thread_id),
                    PrivateFileRow.status == "ready",
                )
                .order_by(PrivateFileChunkRow.chunk_index)
                .limit(limit)
            )
        ).scalars()
        return tuple(self._chunk_record(row) for row in rows)

    async def get_ready_artifact(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        artifact_id: uuid.UUID,
    ) -> tuple[PrivateArtifactRecord, PrivateFileRecord] | None:
        project_id, owner_user_id = self._coordinates(scope)
        row = (
            await self.session.execute(
                select(PrivateArtifactRow, PrivateFileRow)
                .join(
                    PrivateFileRow,
                    (PrivateFileRow.id == PrivateArtifactRow.file_id)
                    & (PrivateFileRow.project_id == PrivateArtifactRow.project_id)
                    & (PrivateFileRow.owner_user_id == PrivateArtifactRow.owner_user_id)
                    & (PrivateFileRow.thread_id == PrivateArtifactRow.thread_id),
                )
                .join(ThreadMetaRow, self._active_thread_join())
                .where(
                    PrivateArtifactRow.id == artifact_id,
                    PrivateArtifactRow.project_id == project_id,
                    PrivateArtifactRow.owner_user_id == owner_user_id,
                    PrivateArtifactRow.thread_id == thread_id,
                    PrivateArtifactRow.deleted_at.is_(None),
                    PrivateFileRow.status == "ready",
                )
            )
        ).one_or_none()
        if row is None:
            return None
        artifact, file_row = row
        return (
            PrivateArtifactRecord(
                id=artifact.id,
                project_id=artifact.project_id,
                owner_user_id=artifact.owner_user_id,
                thread_id=artifact.thread_id,
                run_id=artifact.run_id,
                file_id=artifact.file_id,
                display_name=artifact.display_name,
                media_type=artifact.media_type,
                metadata=dict(artifact.artifact_metadata or {}),
                created_at=artifact.created_at,
                deleted_at=artifact.deleted_at,
            ),
            self._file_record(file_row),
        )
