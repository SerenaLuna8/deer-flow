from __future__ import annotations

import hashlib
import logging
import unicodedata
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import quote

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import StreamingResponse

from app.private_work.context import PrivateWorkContext, require_issued_private_work_context
from app.private_work.errors import (
    PrivateWorkError,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.file_paths import is_safe_private_media_type
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability
from deerflow.persistence.private_work.file_repository import (
    PRIVATE_FILE_CHUNK_SIZE,
    PrivateArtifactRecord,
    PrivateFileChunkRecord,
    PrivateFileConflict,
    PrivateFileRecord,
    PrivateFileRepository,
)

logger = logging.getLogger(__name__)

ACTIVE_CONTENT_MIME_TYPES = frozenset(
    {
        "application/ecmascript",
        "application/javascript",
        "application/pdf",
        "application/xhtml+xml",
        "application/xml",
        "image/svg+xml",
        "text/ecmascript",
        "text/html",
        "text/javascript",
        "text/xml",
    }
)


def _safe_display_name(value: str) -> str:
    basename = value.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(character for character in basename if not unicodedata.category(character).startswith("C"))
    return cleaned[:256] or "download"


def safe_download_headers(
    display_name: str,
    *,
    media_type: str,
    download: bool = False,
) -> dict[str, str]:
    """Build injection-safe download headers without rendering logical paths."""

    normalized_media_type = media_type.split(";", 1)[0].strip().lower()
    disposition = "attachment" if download or normalized_media_type in ACTIVE_CONTENT_MIME_TYPES else "inline"
    encoded = quote(_safe_display_name(display_name), safe="")
    return {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded}",
        "X-Content-Type-Options": "nosniff",
    }


@dataclass(frozen=True, slots=True)
class PrivateFileStream:
    file: PrivateFileRecord
    body: AsyncIterator[bytes]
    display_name: str
    media_type: str
    headers: dict[str, str]
    artifact: PrivateArtifactRecord | None = None


def private_streaming_response(stream: PrivateFileStream) -> StreamingResponse:
    """Create the future project-router response without consuming its body."""

    return StreamingResponse(
        stream.body,
        media_type=stream.media_type,
        headers=stream.headers,
    )


class PrivateFileStreamer:
    """Scoped, keyset-paged stream construction with no idle DB transaction."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        chunk_page_size: int = 4,
    ) -> None:
        if not 1 <= chunk_page_size <= 64:
            raise ValueError("chunk_page_size must be between 1 and 64")
        self._session_factory = session_factory
        self._chunk_page_size = chunk_page_size
        self._revalidator = PrivateWorkRevalidator()

    async def stream_file(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        file_id: uuid.UUID,
        download: bool = False,
    ) -> PrivateFileStream:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(session, context, Capability.PRIVATE_WORK_READ_OWN)
                file_record = await PrivateFileRepository(session).get_ready(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    file_id=file_id,
                )
            if file_record is None:
                raise PrivateWorkNotFound(context.request_id)
            if not is_safe_private_media_type(file_record.media_type):
                raise PrivateWorkUnavailable(context.request_id)
            first_page = await self._fetch_page(context, thread_id, file_record.id, -1)
            self._preflight_first_page(context, file_record, first_page)
            display_name = file_record.logical_path.rsplit("/", 1)[-1]
            return PrivateFileStream(
                file=file_record,
                body=self._body(context, thread_id, file_record, first_page),
                display_name=display_name,
                media_type=file_record.media_type,
                headers=safe_download_headers(
                    display_name,
                    media_type=file_record.media_type,
                    download=download,
                ),
            )
        except PrivateWorkError:
            raise
        except (DBAPIError, PrivateFileConflict):
            raise PrivateWorkUnavailable(context.request_id) from None

    async def stream_artifact(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        artifact_id: uuid.UUID,
        download: bool = False,
    ) -> PrivateFileStream:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(session, context, Capability.PRIVATE_WORK_READ_OWN)
                resolved = await PrivateFileRepository(session).get_ready_artifact(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    artifact_id=artifact_id,
                )
            if resolved is None:
                raise PrivateWorkNotFound(context.request_id)
            artifact, file_record = resolved
            if not is_safe_private_media_type(file_record.media_type) or not is_safe_private_media_type(artifact.media_type):
                raise PrivateWorkUnavailable(context.request_id)
            first_page = await self._fetch_page(context, thread_id, file_record.id, -1)
            self._preflight_first_page(context, file_record, first_page)
            return PrivateFileStream(
                file=file_record,
                body=self._body(context, thread_id, file_record, first_page),
                display_name=_safe_display_name(artifact.display_name),
                media_type=file_record.media_type,
                headers=safe_download_headers(
                    artifact.display_name,
                    media_type=file_record.media_type,
                    download=download or artifact.media_type.split(";", 1)[0].strip().lower() in ACTIVE_CONTENT_MIME_TYPES,
                ),
                artifact=artifact,
            )
        except PrivateWorkError:
            raise
        except (DBAPIError, PrivateFileConflict):
            raise PrivateWorkUnavailable(context.request_id) from None

    async def _fetch_page(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        file_id: uuid.UUID,
        after_index: int,
    ) -> tuple[PrivateFileChunkRecord, ...]:
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(session, context, Capability.PRIVATE_WORK_READ_OWN)
                if not await PrivateThreadRepository(session).check_access(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                ):
                    raise PrivateWorkNotFound(context.request_id)
                return await PrivateFileRepository(session).fetch_chunk_page(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    file_id=file_id,
                    after_index=after_index,
                    limit=self._chunk_page_size,
                )
        except PrivateWorkError:
            raise
        except (DBAPIError, PrivateFileConflict):
            raise PrivateWorkUnavailable(context.request_id) from None

    @staticmethod
    def _valid_chunk(chunk: PrivateFileChunkRecord, expected_index: int) -> bool:
        return chunk.chunk_index == expected_index and chunk.size == len(chunk.content) and 0 < chunk.size <= PRIVATE_FILE_CHUNK_SIZE and hashlib.sha256(chunk.content).hexdigest() == chunk.sha256

    def _preflight_first_page(
        self,
        context: PrivateWorkContext,
        file_record: PrivateFileRecord,
        first_page: tuple[PrivateFileChunkRecord, ...],
    ) -> None:
        empty_hash = hashlib.sha256(b"").hexdigest()
        if file_record.size == 0:
            if first_page or file_record.sha256 != empty_hash:
                self._raise_integrity(context, file_record.id)
            return
        if not first_page:
            self._raise_integrity(context, file_record.id)
        for expected_index, chunk in enumerate(first_page):
            if not self._valid_chunk(chunk, expected_index):
                self._raise_integrity(context, file_record.id)

    async def _body(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        file_record: PrivateFileRecord,
        first_page: tuple[PrivateFileChunkRecord, ...],
    ) -> AsyncIterator[bytes]:
        page = first_page
        expected_index = 0
        total = 0
        whole = hashlib.sha256()
        while page:
            for chunk in page:
                if not self._valid_chunk(chunk, expected_index):
                    self._raise_integrity(context, file_record.id)
                whole.update(chunk.content)
                total += len(chunk.content)
                expected_index += 1
                yield chunk.content
            if len(page) < self._chunk_page_size:
                break
            try:
                page = await self._fetch_page(
                    context,
                    thread_id,
                    file_record.id,
                    expected_index - 1,
                )
            except PrivateWorkNotFound:
                # Headers may already be committed after the first yielded page.
                # Abort the body with the stable unavailable contract.
                raise PrivateWorkUnavailable(context.request_id) from None
        if total != file_record.size or whole.hexdigest() != file_record.sha256:
            self._raise_integrity(context, file_record.id)

    @staticmethod
    def _raise_integrity(context: PrivateWorkContext, file_id: uuid.UUID) -> None:
        logger.warning(
            "Private file stream integrity check failed request_id=%s file_id=%s",
            context.request_id,
            file_id,
        )
        raise PrivateWorkUnavailable(context.request_id)
