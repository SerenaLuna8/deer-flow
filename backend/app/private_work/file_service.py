from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import AsyncIterable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import PrivateWorkContext, require_issued_private_work_context
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkTooLarge,
    PrivateWorkUnavailable,
)
from app.private_work.file_paths import (
    normalize_private_logical_path,
    validate_private_media_type,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability
from app.upload_contracts import PRIVATE_UPLOAD_DEFAULTS
from deerflow.persistence.private_work.file_repository import (
    PRIVATE_FILE_CHUNK_SIZE,
    PrivateFileChunkRecord,
    PrivateFileConflict,
    PrivateFileIntegrityError,
    PrivateFileRecord,
    PrivateFileRepository,
)

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

PRIVATE_DEFAULT_MAX_FILES = PRIVATE_UPLOAD_DEFAULTS.max_files
PRIVATE_DEFAULT_MAX_FILE_SIZE = PRIVATE_UPLOAD_DEFAULTS.max_file_size
PRIVATE_DEFAULT_MAX_TOTAL_SIZE = PRIVATE_UPLOAD_DEFAULTS.max_total_size


@dataclass(frozen=True, slots=True)
class PrivateFileLimits:
    max_files: int = PRIVATE_DEFAULT_MAX_FILES
    max_file_size: int = PRIVATE_DEFAULT_MAX_FILE_SIZE
    max_total_size: int = PRIVATE_DEFAULT_MAX_TOTAL_SIZE

    def __post_init__(self) -> None:
        if self.max_files < 1 or self.max_file_size < 1 or self.max_total_size < 1:
            raise ValueError("private file limits must be positive")


@dataclass(frozen=True, slots=True)
class PrivateUpload:
    logical_path: str
    media_type: str
    chunks: AsyncIterable[bytes]
    kind: str = "upload"
    created_by_run_id: str | None = None
    source_file_id: uuid.UUID | None = None


class PrivateFileService:
    """Application transaction boundary for PostgreSQL-authoritative files."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        conversion_temp_root: Path | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._revalidator = PrivateWorkRevalidator()
        self._conversion_temp_root = conversion_temp_root

    @staticmethod
    def _media_type(value: str, request_id: str) -> str:
        return validate_private_media_type(value, request_id=request_id)

    async def stage_upload(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        logical_path: str,
        media_type: str,
    ) -> PrivateFileRecord:
        context = require_issued_private_work_context(context)
        logical_path = normalize_private_logical_path(logical_path, request_id=context.request_id)
        media_type = self._media_type(media_type, context.request_id)
        file_id = uuid.uuid4()
        try:
            try:
                async with self._session_factory() as session, session.begin():
                    await self._revalidator.require(session, context, Capability.PRIVATE_WORK_CREATE, lock=True)
                    return await PrivateFileRepository(session).stage(
                        scope=context.resource_scope,
                        thread_id=thread_id,
                        kind="upload",
                        logical_path=logical_path,
                        media_type=media_type,
                        file_id=file_id,
                    )
            except PrivateFileConflict:
                raise PrivateWorkConflict(context.request_id) from None
            except PrivateWorkError:
                raise
            except DBAPIError:
                raise PrivateWorkUnavailable(context.request_id) from None
        except BaseException:
            await self._cleanup_after_failure(context, thread_id, (file_id,))
            raise

    async def append_chunk(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        file_id: uuid.UUID,
        chunk_index: int,
        content: bytes,
        *,
        size: int,
        sha256: str,
    ) -> PrivateFileChunkRecord:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(session, context, Capability.PRIVATE_WORK_CREATE, lock=True)
                return await PrivateFileRepository(session).append_chunk(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    file_id=file_id,
                    chunk_index=chunk_index,
                    content=content,
                    size=size,
                    sha256=sha256,
                )
        except PrivateFileConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateFileIntegrityError:
            raise PrivateWorkInvalid(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def finalize_upload(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        file_id: uuid.UUID,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> PrivateFileRecord:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(session, context, Capability.PRIVATE_WORK_CREATE, lock=True)
                return await PrivateFileRepository(session).finalize(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    file_id=file_id,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                )
        except PrivateFileConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateFileIntegrityError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def abort_upload(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        file_id: uuid.UUID,
    ) -> bool:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(session, context, Capability.PRIVATE_WORK_CREATE, lock=True)
                return await PrivateFileRepository(session).abort(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    file_id=file_id,
                )
        except PrivateWorkError:
            raise
        except (DBAPIError, PrivateFileConflict):
            raise PrivateWorkUnavailable(context.request_id) from None

    async def upload(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        logical_path: str,
        media_type: str,
        chunks: AsyncIterable[bytes],
        limits: PrivateFileLimits | None = None,
        kind: str = "upload",
        created_by_run_id: str | None = None,
        source_file_id: uuid.UUID | None = None,
    ) -> PrivateFileRecord:
        results = await self.upload_many(
            context,
            thread_id=thread_id,
            uploads=(
                PrivateUpload(
                    logical_path=logical_path,
                    media_type=media_type,
                    chunks=chunks,
                    kind=kind,
                    created_by_run_id=created_by_run_id,
                    source_file_id=source_file_id,
                ),
            ),
            limits=limits or PrivateFileLimits(),
        )
        return results[0]

    async def upload_many(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        uploads: Sequence[PrivateUpload],
        limits: PrivateFileLimits | None = None,
    ) -> tuple[PrivateFileRecord, ...]:
        context = require_issued_private_work_context(context)
        limits = limits or PrivateFileLimits()
        if not uploads:
            raise PrivateWorkInvalid(context.request_id)
        if len(uploads) > limits.max_files:
            raise PrivateWorkTooLarge(context.request_id)
        prepared = tuple(
            PrivateUpload(
                logical_path=normalize_private_logical_path(upload.logical_path, request_id=context.request_id),
                media_type=self._media_type(upload.media_type, context.request_id),
                chunks=upload.chunks,
                kind=upload.kind,
                created_by_run_id=upload.created_by_run_id,
                source_file_id=upload.source_file_id,
            )
            for upload in uploads
        )
        if len({upload.logical_path for upload in prepared}) != len(prepared):
            raise PrivateWorkConflict(context.request_id)
        if any(upload.source_file_id is not None and upload.kind != "workspace" for upload in prepared):
            raise PrivateWorkConflict(context.request_id)

        staging_ids = tuple(uuid.uuid4() for _upload in prepared)
        try:
            try:
                staged = await self._stage_many(context, thread_id, prepared, staging_ids)
                totals: list[tuple[int, str]] = []
                request_total = 0
                for upload, file_record in zip(prepared, staged, strict=True):
                    file_size, whole_sha256, request_total = await self._persist_stream_short_transactions(
                        context,
                        thread_id,
                        file_record.id,
                        upload.chunks,
                        limits,
                        request_total,
                    )
                    totals.append((file_size, whole_sha256))
                return await self._finalize_many_at_commit_point(
                    context,
                    thread_id,
                    staged,
                    totals,
                )
            except PrivateFileConflict:
                raise PrivateWorkConflict(context.request_id) from None
            except PrivateFileIntegrityError:
                raise PrivateWorkUnavailable(context.request_id) from None
            except PrivateWorkError:
                raise
            except DBAPIError:
                raise PrivateWorkUnavailable(context.request_id) from None
            except Exception:
                raise PrivateWorkUnavailable(context.request_id) from None
        except BaseException:
            await self._cleanup_after_failure(context, thread_id, staging_ids)
            raise

    async def _stage_many(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        uploads: Sequence[PrivateUpload],
        file_ids: Sequence[uuid.UUID],
    ) -> tuple[PrivateFileRecord, ...]:
        async with self._session_factory() as session, session.begin():
            await self._revalidator.require(session, context, Capability.PRIVATE_WORK_CREATE, lock=True)
            repository = PrivateFileRepository(session)
            return tuple(
                [
                    await repository.stage(
                        scope=context.resource_scope,
                        thread_id=thread_id,
                        kind=upload.kind,
                        logical_path=upload.logical_path,
                        media_type=upload.media_type,
                        created_by_run_id=upload.created_by_run_id,
                        source_file_id=upload.source_file_id,
                        file_id=file_id,
                    )
                    for upload, file_id in zip(uploads, file_ids, strict=True)
                ]
            )

    async def _persist_stream_short_transactions(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        file_id: uuid.UUID,
        chunks: AsyncIterable[bytes],
        limits: PrivateFileLimits,
        request_total: int,
    ) -> tuple[int, str, int]:
        carry = bytearray()
        whole = hashlib.sha256()
        file_size = 0
        chunk_index = 0

        async def persist(content: bytes) -> None:
            nonlocal chunk_index
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    lock=True,
                )
                await PrivateFileRepository(session).append_chunk(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    file_id=file_id,
                    chunk_index=chunk_index,
                    content=content,
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            chunk_index += 1

        async for incoming in chunks:
            if not isinstance(incoming, bytes):
                raise PrivateWorkInvalid(context.request_id)
            if not incoming:
                continue
            file_size += len(incoming)
            request_total += len(incoming)
            if file_size > limits.max_file_size or request_total > limits.max_total_size:
                raise PrivateWorkTooLarge(context.request_id)
            whole.update(incoming)
            offset = 0
            if carry:
                take = min(PRIVATE_FILE_CHUNK_SIZE - len(carry), len(incoming))
                carry.extend(incoming[:take])
                offset = take
                if len(carry) == PRIVATE_FILE_CHUNK_SIZE:
                    await persist(bytes(carry))
                    carry.clear()
            while len(incoming) - offset >= PRIVATE_FILE_CHUNK_SIZE:
                end = offset + PRIVATE_FILE_CHUNK_SIZE
                await persist(incoming[offset:end])
                offset = end
            if offset < len(incoming):
                carry.extend(incoming[offset:])
        if carry:
            await persist(bytes(carry))
        return file_size, whole.hexdigest(), request_total

    async def _finalize_many(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        staged: Sequence[PrivateFileRecord],
        totals: Sequence[tuple[int, str]],
    ) -> tuple[PrivateFileRecord, ...]:
        async with self._session_factory() as session, session.begin():
            await self._revalidator.require(session, context, Capability.PRIVATE_WORK_CREATE, lock=True)
            repository = PrivateFileRepository(session)
            return tuple(
                [
                    await repository.finalize(
                        scope=context.resource_scope,
                        thread_id=thread_id,
                        file_id=file_record.id,
                        expected_size=file_size,
                        expected_sha256=whole_sha256,
                    )
                    for file_record, (file_size, whole_sha256) in zip(staged, totals, strict=True)
                ]
            )

    async def _finalize_many_at_commit_point(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        staged: Sequence[PrivateFileRecord],
        totals: Sequence[tuple[int, str]],
    ) -> tuple[PrivateFileRecord, ...]:
        """Finish the atomic ready commit even if request cancellation races it.

        Cancellation before this point cleans staging rows. Once finalization starts,
        its all-files transaction is the commit point and cancellation is deferred so
        the caller observes the authoritative committed result instead of an unknown
        staging/ready outcome.
        """

        finalizer = asyncio.create_task(self._finalize_many(context, thread_id, staged, totals))
        cancellation_pending = False
        while True:
            try:
                result = await asyncio.shield(finalizer)
                break
            except asyncio.CancelledError:
                if finalizer.cancelled():
                    raise
                cancellation_pending = True
        if cancellation_pending:
            logger.info("Private file cancellation deferred across finalize commit point")
        return result

    async def _cleanup_after_failure(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        file_ids: tuple[uuid.UUID, ...],
    ) -> None:
        cleanup_task = asyncio.create_task(self._purge_staging_with_retries(context, thread_id, file_ids))
        while True:
            try:
                await asyncio.shield(cleanup_task)
                return
            except asyncio.CancelledError:
                if not cleanup_task.done():
                    continue
                try:
                    cleanup_task.result()
                except Exception:
                    logger.error("Private file staging cleanup failed")
                return

    async def _purge_staging_with_retries(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        file_ids: tuple[uuid.UUID, ...],
    ) -> None:
        for attempt in range(3):
            try:
                async with self._session_factory() as session, session.begin():
                    await PrivateFileRepository(session).purge_staging(
                        scope=context.resource_scope,
                        thread_id=thread_id,
                        file_ids=file_ids,
                    )
                return
            except DBAPIError:
                if attempt == 2:
                    logger.error("Private file staging cleanup exhausted retries")
                    return
                await asyncio.sleep(0)

    async def get_ready(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        file_id: uuid.UUID,
    ) -> PrivateFileRecord | None:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(session, context, Capability.PRIVATE_WORK_READ_OWN)
                return await PrivateFileRepository(session).get_ready(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    file_id=file_id,
                )
        except PrivateWorkError:
            raise
        except (DBAPIError, PrivateFileConflict):
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_ready(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        after: tuple[str, int, uuid.UUID] | None = None,
        limit: int = 100,
    ) -> tuple[PrivateFileRecord, ...]:
        """List all ready upload/workspace/output files in the private Thread."""

        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                if not await PrivateThreadRepository(session).check_access(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                ):
                    raise PrivateWorkNotFound(context.request_id)
                return await PrivateFileRepository(session).list_ready(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    after=after,
                    limit=limit,
                )
        except PrivateFileConflict:
            raise PrivateWorkInvalid(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def delete_ready(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        file_id: uuid.UUID,
    ) -> PrivateFileRecord:
        """Soft-delete a ready file while retaining authoritative chunk bytes."""

        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                    lock=True,
                )
                return await PrivateFileRepository(session).mark_deleted(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    file_id=file_id,
                )
        except PrivateFileConflict:
            raise PrivateWorkNotFound(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def convert_upload(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        source_file_id: uuid.UUID,
        logical_path: str,
        media_type: str,
        converter: Callable[[Path], Path | None],
    ) -> PrivateFileRecord:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                )
                source = await PrivateFileRepository(session).get_ready(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    file_id=source_file_id,
                )
        except PrivateWorkError:
            raise
        except (DBAPIError, PrivateFileConflict):
            raise PrivateWorkUnavailable(context.request_id) from None
        if source is None:
            raise PrivateWorkNotFound(context.request_id)
        logical_path = normalize_private_logical_path(logical_path, request_id=context.request_id)
        media_type = self._media_type(media_type, context.request_id)
        temp_dir: Path | None = None
        output_fd: int | None = None
        try:
            temp_dir = await self._run_sync_to_completion(
                self._create_conversion_dir,
                cleanup_on_cancel=self._remove_conversion_dir,
            )
            source_path = await self._run_sync_to_completion(
                self._create_conversion_source,
                temp_dir,
                source.logical_path,
            )
            from app.private_work.file_streaming import PrivateFileStreamer

            stream = await PrivateFileStreamer(self._session_factory).stream_file(
                context,
                thread_id=thread_id,
                file_id=source.id,
            )
            handle = await self._run_sync_to_completion(
                source_path.open,
                "wb",
                cleanup_on_cancel=self._close_file_object,
            )
            try:
                async for chunk in stream.body:
                    await self._run_sync_to_completion(handle.write, chunk)
                await self._run_sync_to_completion(handle.flush)
                await self._run_sync_to_completion(os.fsync, handle.fileno())
            finally:
                await self._run_sync_to_completion(handle.close)

            converted_path = await self._run_converter_to_completion(converter, source_path)
            if inspect.isawaitable(converted_path):
                if inspect.iscoroutine(converted_path):
                    converted_path.close()
                raise PrivateWorkUnavailable(context.request_id)
            if converted_path is None:
                raise PrivateWorkUnavailable(context.request_id)
            converted_path = Path(converted_path)
            output_fd = await self._run_sync_to_completion(
                self._open_controlled_conversion_output,
                temp_dir,
                converted_path,
                cleanup_on_cancel=os.close,
            )
            return await self.upload(
                context,
                thread_id=thread_id,
                logical_path=logical_path,
                media_type=media_type,
                chunks=self._read_output_chunks(output_fd),
                kind="workspace",
                source_file_id=source.id,
            )
        except PrivateWorkError:
            raise
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None
        finally:
            try:
                if output_fd is not None:
                    try:
                        await self._run_sync_to_completion(os.close, output_fd)
                    except OSError:
                        logger.warning("Private conversion output close failed")
            finally:
                if temp_dir is not None:
                    try:
                        await self._run_sync_to_completion(shutil.rmtree, temp_dir)
                    except Exception:
                        logger.warning("Private conversion temporary cleanup failed")

    def _create_conversion_dir(self) -> Path:
        if self._conversion_temp_root is not None:
            self._conversion_temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix="deerflow-private-convert-", dir=self._conversion_temp_root))
        try:
            path.chmod(0o700)
        except BaseException:
            try:
                shutil.rmtree(path)
            except Exception:
                logger.warning("Private conversion temporary cleanup failed")
            raise
        return path

    @staticmethod
    def _create_conversion_source(temp_dir: Path, logical_path: str) -> Path:
        suffix = PurePosixPath(logical_path).suffix.lower()
        if not suffix or len(suffix) > 16 or not suffix[1:].isalnum() or not suffix.isascii():
            suffix = ".bin"
        descriptor, raw_path = tempfile.mkstemp(prefix="source-", suffix=suffix, dir=temp_dir)
        os.close(descriptor)
        path = Path(raw_path)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return path

    @staticmethod
    def _open_controlled_conversion_output(temp_dir: Path, output: Path) -> int:
        """Open a regular, single-link output through no-follow directory FDs."""

        boundary = temp_dir.absolute()
        candidate = output.absolute()
        try:
            relative = candidate.relative_to(boundary)
        except ValueError:
            raise OSError("conversion output outside boundary") from None
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise OSError("invalid conversion output")

        if os.open not in os.supports_dir_fd:
            raise OSError("secure directory-relative open is unavailable")
        try:
            nofollow = os.O_NOFOLLOW
            cloexec = os.O_CLOEXEC
            directory = os.O_DIRECTORY
            nonblock = os.O_NONBLOCK
        except AttributeError:
            raise OSError("secure conversion open flags are unavailable") from None
        directory_flags = os.O_RDONLY | directory | nofollow | cloexec
        directory_fd = os.open(boundary, directory_flags)
        try:
            for part in relative.parts[:-1]:
                next_fd = os.open(
                    part,
                    directory_flags,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
            output_fd = os.open(
                relative.parts[-1],
                os.O_RDONLY | nofollow | cloexec | nonblock,
                dir_fd=directory_fd,
            )
            try:
                metadata = os.fstat(output_fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise OSError("unsafe conversion output")
            except BaseException:
                os.close(output_fd)
                raise
            return output_fd
        finally:
            os.close(directory_fd)

    async def _run_converter_to_completion(
        self,
        converter: Callable[[Path], Path | None],
        source_path: Path,
    ) -> Path | None:
        return await self._run_sync_to_completion(converter, source_path)

    @staticmethod
    async def _run_sync_to_completion(
        operation: Callable[..., _T],
        *args: object,
        cleanup_on_cancel: Callable[[_T], Any] | None = None,
    ) -> _T:
        worker = asyncio.create_task(asyncio.to_thread(operation, *args))
        pending_cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError as exc:
                if worker.cancelled():
                    raise
                if pending_cancellation is None:
                    pending_cancellation = exc
            except Exception:
                if pending_cancellation is None:
                    raise
                logger.error("Private file worker failed while cancellation was pending")
                raise pending_cancellation
        if pending_cancellation is not None:
            if cleanup_on_cancel is not None:
                cleanup = asyncio.create_task(asyncio.to_thread(cleanup_on_cancel, result))
                while True:
                    try:
                        await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        logger.error("Private file cancellation cleanup failed")
                        break
            raise pending_cancellation
        return result

    async def _read_output_chunks(self, output_fd: int) -> AsyncIterable[bytes]:
        while chunk := await self._run_sync_to_completion(
            os.read,
            output_fd,
            PRIVATE_FILE_CHUNK_SIZE,
        ):
            yield chunk

    @staticmethod
    def _remove_conversion_dir(path: Path) -> None:
        shutil.rmtree(path)

    @staticmethod
    def _close_file_object(handle: Any) -> None:
        handle.close()
