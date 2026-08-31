"""MinIO-backed document file storage.

The ``minio`` SDK is synchronous; every network call here is pushed off the
event loop and joined before cancellation propagates. Callers only see
:class:`KnowledgeError` with ``KNOWLEDGE_STORAGE_UNAVAILABLE`` — provider
exception details never leak into user-facing messages, and log lines carry
only a safe failure classification: object keys are storage locators and raw
SDK exceptions may embed the endpoint, so neither may enter logs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path, PurePosixPath
from uuid import UUID

from minio import Minio
from minio.error import S3Error
from minio.helpers import MIN_PART_SIZE
from minio.versioningconfig import OFF

from ..asyncio_utils import run_sync_to_completion
from ..contracts import KNOWLEDGE_STORAGE_UNAVAILABLE, KnowledgeError, KnowledgeMinioSettings

logger = logging.getLogger(__name__)

_MISSING_OBJECT_CODES = frozenset({"NoSuchKey", "NoSuchObject"})
DOCUMENT_STORAGE_EXTENSIONS = frozenset({".pdf", ".docx", ".txt", ".md", ".csv", ".xlsx", ".html", ".htm", ".pptx", ".epub"})


def _failure_class(exc: Exception) -> str:
    """Log-safe failure detail: exception type plus S3 error code only."""

    if isinstance(exc, S3Error):
        return f"S3Error/{exc.code}"
    return type(exc).__name__


def document_storage_key(project_id: UUID, base_id: UUID, document_id: UUID, original_name: str) -> str:
    """Deterministic object key: ``projects/{p}/knowledge/{kb}/{doc}{ext}``.

    The extension is taken from ``original_name`` and lowercased; the rest of
    the user-controlled filename never reaches the object store.
    """

    extension = Path(original_name).suffix.lower()
    return f"projects/{project_id}/knowledge/{base_id}/{document_id}{extension}"


def is_document_storage_key(
    key: str,
    *,
    project_id: UUID,
    document_id: UUID,
) -> bool:
    """Whether ``key`` is the canonical exact object for this trusted scope."""

    parts = key.split("/")
    if len(parts) != 5 or parts[0] != "projects" or parts[2] != "knowledge":
        return False
    filename = PurePosixPath(parts[4])
    if filename.suffix not in DOCUMENT_STORAGE_EXTENSIONS:
        return False
    try:
        key_project_id = UUID(parts[1])
        base_id = UUID(parts[3])
        key_document_id = UUID(filename.stem)
    except ValueError:
        return False
    return str(key_project_id) == parts[1] and str(base_id) == parts[3] and str(key_document_id) == filename.stem and key_project_id == project_id and key_document_id == document_id


class MinioObjectStore:
    """Store, fetch, and delete document objects in one MinIO bucket."""

    def __init__(self, settings: KnowledgeMinioSettings) -> None:
        self._bucket = settings.bucket
        # The MinIO SDK materializes a one-part PUT as one ``bytes`` object.
        # One slot bounds this store instance to one such allocation at a time.
        self._upload_slot = asyncio.Semaphore(1)
        self._client = Minio(
            settings.endpoint,
            access_key=settings.access_key,
            secret_key=settings.secret_key.get_secret_value(),
            secure=settings.secure,
        )

    async def upload_from(self, key: str, source_path: Path, *, media_type: str | None = None) -> None:
        """Upload the staged file at ``source_path`` as object ``key``."""

        def _put() -> None:
            file_size = source_path.stat().st_size
            self._client.fput_object(
                self._bucket,
                key,
                str(source_path),
                content_type=media_type or "application/octet-stream",
                # The validated 50 MiB ceiling bounds the SDK's one-part
                # in-memory buffer. One PUT also means a crashed process cannot
                # leave an invisible incomplete multipart upload behind.
                part_size=max(file_size, MIN_PART_SIZE),
                num_parallel_uploads=1,
            )

        async with self._upload_slot:
            # Re-check after waiting for the process-memory slot: startup state
            # is not durable, and a versioned write cannot be physically
            # removed by the supported deletion path.
            await self.require_unversioned_bucket()
            try:
                await run_sync_to_completion(_put)
            except Exception as exc:
                logger.warning("knowledge object upload failed: %s", _failure_class(exc))
                raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储写入失败，请稍后重试") from exc

    async def download_to(self, key: str, target_path: Path) -> None:
        """Download object ``key`` to ``target_path`` (parent directory must exist)."""

        try:
            await run_sync_to_completion(self._client.fget_object, self._bucket, key, str(target_path))
        except S3Error as exc:
            if exc.code in _MISSING_OBJECT_CODES:
                logger.error("knowledge object missing in bucket")
                raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "文档文件在对象存储中缺失") from exc
            logger.warning("knowledge object download failed: %s", _failure_class(exc))
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储读取失败，请稍后重试") from exc
        except Exception as exc:
            logger.warning("knowledge object download failed: %s", _failure_class(exc))
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储读取失败，请稍后重试") from exc

    async def delete(self, key: str) -> None:
        """Delete object ``key``; deleting an absent object succeeds (S3 semantics)."""

        await self.require_unversioned_bucket()
        await self._delete_after_bucket_check(key)

    async def delete_many(self, keys: list[str]) -> None:
        """Delete an object batch after one fail-closed bucket-policy check."""

        await self.require_unversioned_bucket()
        for key in keys:
            await self._delete_after_bucket_check(key)

    async def _delete_after_bucket_check(self, key: str) -> None:
        """Remove one key after the caller verified versioning is off."""

        try:
            await run_sync_to_completion(self._client.remove_object, self._bucket, key)
        except S3Error as exc:
            if exc.code in _MISSING_OBJECT_CODES:
                return
            logger.warning("knowledge object delete failed: %s", _failure_class(exc))
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储删除失败，请稍后重试") from exc
        except Exception as exc:
            logger.warning("knowledge object delete failed: %s", _failure_class(exc))
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储删除失败，请稍后重试") from exc

    async def delete_project_objects(self, project_id: UUID) -> None:
        """Delete all objects under the database-issued Project prefix."""

        await self.require_unversioned_bucket()
        prefix = _project_storage_prefix(project_id)
        for key in await self._list_object_names(prefix):
            if key.startswith(prefix):
                await self._delete_after_bucket_check(key)

    async def _list_object_names(self, prefix: str) -> tuple[str, ...]:
        def _list() -> tuple[str, ...]:
            return tuple(
                entry.object_name
                for entry in self._client.list_objects(
                    self._bucket,
                    prefix=prefix,
                    recursive=True,
                )
                if isinstance(entry.object_name, str)
            )

        try:
            return await run_sync_to_completion(_list)
        except Exception as exc:
            logger.warning(
                "knowledge object listing failed: %s",
                _failure_class(exc),
            )
            raise KnowledgeError(
                KNOWLEDGE_STORAGE_UNAVAILABLE,
                "对象存储读取失败，请稍后重试",
            ) from exc

    async def check_bucket(self) -> bool:
        """True when the bucket is reachable and has versioning fully off."""

        try:
            await self.require_unversioned_bucket()
            return True
        except KnowledgeError:
            return False

    async def require_unversioned_bucket(self) -> None:
        """Fail closed unless deletion has physical, non-versioned semantics.

        On an enabled or suspended versioned bucket, ``remove_object`` writes
        a delete marker while retained versions remain invisible to ordinary
        prefix listing. Knowledge therefore supports only buckets whose SDK
        status is absent/``Off``; Object Lock is consequently excluded too.
        """

        try:
            exists = bool(
                await run_sync_to_completion(
                    self._client.bucket_exists,
                    self._bucket,
                )
            )
            if not exists:
                raise KnowledgeError(
                    KNOWLEDGE_STORAGE_UNAVAILABLE,
                    "对象存储 bucket 不可访问",
                )
            config = await run_sync_to_completion(
                self._client.get_bucket_versioning,
                self._bucket,
            )
            if getattr(config, "status", None) not in (None, OFF):
                raise KnowledgeError(
                    KNOWLEDGE_STORAGE_UNAVAILABLE,
                    "Knowledge bucket 必须关闭版本控制和 Object Lock",
                )
        except KnowledgeError:
            logger.warning("knowledge bucket is unavailable or has unsafe versioning")
            raise
        except Exception as exc:
            logger.warning("knowledge bucket check failed: %s", _failure_class(exc))
            raise KnowledgeError(
                KNOWLEDGE_STORAGE_UNAVAILABLE,
                "对象存储 bucket 不可访问",
            ) from exc


def _project_storage_prefix(project_id: UUID) -> str:
    return f"projects/{project_id}/knowledge/"
