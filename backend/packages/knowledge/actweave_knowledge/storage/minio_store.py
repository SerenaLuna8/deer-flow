"""MinIO-backed document file storage.

The ``minio`` SDK is synchronous; every network call here is pushed off the
event loop with :func:`asyncio.to_thread`. Callers only see
:class:`KnowledgeError` with ``KNOWLEDGE_STORAGE_UNAVAILABLE`` — provider
exception details never leak into user-facing messages, and log lines carry
only a safe failure classification: object keys are storage locators and raw
SDK exceptions may embed the endpoint, so neither may enter logs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import UUID

from minio import Minio
from minio.error import S3Error

from ..contracts import KNOWLEDGE_STORAGE_UNAVAILABLE, KnowledgeError, KnowledgeMinioSettings

logger = logging.getLogger(__name__)

_MISSING_OBJECT_CODES = frozenset({"NoSuchKey", "NoSuchObject"})


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


class MinioObjectStore:
    """Store, fetch, and delete document objects in one MinIO bucket."""

    def __init__(self, settings: KnowledgeMinioSettings) -> None:
        self._bucket = settings.bucket
        self._client = Minio(
            settings.endpoint,
            access_key=settings.access_key,
            secret_key=settings.secret_key.get_secret_value(),
            secure=settings.secure,
        )

    async def upload_from(self, key: str, source_path: Path, *, media_type: str | None = None) -> None:
        """Upload the staged file at ``source_path`` as object ``key``."""

        def _put() -> None:
            self._client.fput_object(
                self._bucket,
                key,
                str(source_path),
                content_type=media_type or "application/octet-stream",
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            logger.warning("knowledge object upload failed: %s", _failure_class(exc))
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储写入失败，请稍后重试") from exc

    async def download_to(self, key: str, target_path: Path) -> None:
        """Download object ``key`` to ``target_path`` (parent directory must exist)."""

        try:
            await asyncio.to_thread(self._client.fget_object, self._bucket, key, str(target_path))
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

        try:
            await asyncio.to_thread(self._client.remove_object, self._bucket, key)
        except S3Error as exc:
            if exc.code in _MISSING_OBJECT_CODES:
                return
            logger.warning("knowledge object delete failed: %s", _failure_class(exc))
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储删除失败，请稍后重试") from exc
        except Exception as exc:
            logger.warning("knowledge object delete failed: %s", _failure_class(exc))
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储删除失败，请稍后重试") from exc

    async def check_bucket(self) -> bool:
        """True when the configured bucket is reachable with the configured credentials."""

        try:
            return bool(await asyncio.to_thread(self._client.bucket_exists, self._bucket))
        except Exception as exc:
            logger.warning("knowledge bucket check failed: %s", _failure_class(exc))
            return False
