"""Internal object storage: the MinIO implementation of document file storage."""

from .minio_store import MinioObjectStore, document_storage_key

__all__ = [
    "MinioObjectStore",
    "document_storage_key",
]
