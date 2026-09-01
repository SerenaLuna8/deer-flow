"""Strict public projections; MinIO secrets exist only on the write boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class KnowledgeSettingsFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, from_attributes=True)

    enabled: bool
    etl_type: Literal["dify", "unstructured_local"] = "dify"
    extraction_cache_enabled: bool = True
    worker_concurrency: int = Field(ge=1, le=16)
    task_timeout_seconds: int = Field(ge=30, le=7200)
    upload_max_bytes: int = Field(ge=1, le=52428800)
    max_knowledge_bases_per_project: int = Field(ge=1)
    max_documents_per_knowledge_base: int = Field(ge=1)
    max_segments_per_document: int = Field(ge=1, le=5000)
    minio_endpoint: str | None = Field(max_length=512)
    minio_bucket: str | None = Field(max_length=255)
    minio_access_key: str | None = Field(max_length=512, repr=False)
    minio_secure: bool
    summary_model_name: str | None
    query_cache_enabled: bool
    query_cache_max_entries: int = Field(ge=16, le=65536)
    query_cache_ttl_seconds: int = Field(ge=5, le=86400)


class SummaryModelInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model_name: str
    display_name: str


class AdminKnowledgeSettingsResponse(KnowledgeSettingsFields):
    revision: int = Field(ge=1)
    updated_at: datetime
    secret_key_configured: bool
    summary_model: SummaryModelInfo | None
    request_id: str


class AdminKnowledgeSettingsUpdateRequest(KnowledgeSettingsFields):
    expected_revision: int = Field(ge=1)
    minio_secret_key: SecretStr | None = Field(default=None, repr=False, exclude=True, min_length=1, max_length=65536)

    @field_validator("summary_model_name")
    @classmethod
    def valid_model_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(UUID(value))
        except ValueError:
            raise ValueError("Select an active System Model") from None

    @field_validator("minio_endpoint", "minio_bucket", "minio_access_key")
    @classmethod
    def nonempty_storage_field(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Storage fields must be nonempty or null")
        return value

    @field_validator("minio_secret_key")
    @classmethod
    def nonempty_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("Secret must not be empty")
        return value
