"""Pure upload schemas and configurable limit resolution shared by gateway flows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class UploadedFileInfo(BaseModel):
    """Legacy host-backed upload metadata; private PostgreSQL files use another schema."""

    filename: str
    size: int
    path: str
    virtual_path: str
    artifact_url: str
    extension: str | None = None
    modified: float | None = None
    original_filename: str | None = None
    markdown_file: str | None = None
    markdown_path: str | None = None
    markdown_virtual_path: str | None = None
    markdown_artifact_url: str | None = None


class UploadResponse(BaseModel):
    """Legacy host-backed upload response."""

    success: bool
    files: list[UploadedFileInfo]
    message: str
    skipped_files: list[str] = Field(default_factory=list)


class UploadListResponse(BaseModel):
    """Legacy host-backed upload listing response."""

    files: list[UploadedFileInfo]
    count: int


class UploadLimits(BaseModel):
    """Application-level upload limits exposed to clients."""

    max_files: int
    max_file_size: int
    max_total_size: int


@dataclass(frozen=True, slots=True)
class UploadLimitDefaults:
    max_files: int
    max_file_size: int
    max_total_size: int


LEGACY_UPLOAD_DEFAULTS = UploadLimitDefaults(
    max_files=10,
    max_file_size=50 * 1024 * 1024,
    max_total_size=100 * 1024 * 1024,
)
PRIVATE_UPLOAD_DEFAULTS = UploadLimitDefaults(
    max_files=10,
    max_file_size=100 * 1024 * 1024,
    max_total_size=100 * 1024 * 1024,
)


def get_uploads_config_value(app_config: Any, key: str, default: object) -> object:
    """Read an uploads setting from mapping- or attribute-shaped configuration."""

    uploads_config = getattr(app_config, "uploads", None)
    if isinstance(uploads_config, dict):
        return uploads_config.get(key, default)
    return getattr(uploads_config, key, default)


def _resolve_positive_limit(
    app_config: Any,
    key: str,
    default: int,
    *,
    legacy_key: str | None = None,
) -> int:
    try:
        value = get_uploads_config_value(app_config, key, None)
        if value is None and legacy_key is not None:
            value = get_uploads_config_value(app_config, legacy_key, None)
        if value is None:
            value = default
        limit = int(value)
        if limit <= 0:
            raise ValueError
        return limit
    except Exception:
        logger.warning("Invalid uploads.%s value; falling back to %d", key, default)
        return default


def resolve_upload_limits(
    app_config: Any,
    *,
    defaults: UploadLimitDefaults,
) -> UploadLimits:
    """Resolve positive limits while preserving caller-specific defaults."""

    return UploadLimits(
        max_files=_resolve_positive_limit(
            app_config,
            "max_files",
            defaults.max_files,
            legacy_key="max_file_count",
        ),
        max_file_size=_resolve_positive_limit(
            app_config,
            "max_file_size",
            defaults.max_file_size,
            legacy_key="max_single_file_size",
        ),
        max_total_size=_resolve_positive_limit(
            app_config,
            "max_total_size",
            defaults.max_total_size,
        ),
    )
