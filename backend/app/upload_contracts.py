"""Pure upload schemas and configurable limit resolution shared by gateway flows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


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
