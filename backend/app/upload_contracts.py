"""Project-private upload limits shared by Gateway flows."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UploadLimitDefaults:
    max_files: int
    max_file_size: int
    max_total_size: int


PRIVATE_UPLOAD_DEFAULTS = UploadLimitDefaults(
    max_files=10,
    max_file_size=100 * 1024 * 1024,
    max_total_size=100 * 1024 * 1024,
)
