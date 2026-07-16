"""Stable, non-sensitive M6 reliability errors."""

from __future__ import annotations

from typing import ClassVar

RELIABILITY_ERROR_STATUS = {
    "INVALID_STREAM_CURSOR": 400,
    "RELIABILITY_NOT_FOUND": 404,
    "RELIABILITY_FORBIDDEN": 403,
    "RELIABILITY_CONFLICT": 409,
    "RELIABILITY_INVALID": 422,
    "RELIABILITY_MIGRATION_REQUIRED": 409,
    "RELIABILITY_QUOTA_EXCEEDED": 429,
    "RELIABILITY_CUTOVER": 503,
    "WORKER_UNAVAILABLE": 503,
    "DATABASE_UNAVAILABLE": 503,
}


class ReliabilityError(Exception):
    code: ClassVar[str] = "DATABASE_UNAVAILABLE"
    public_message: ClassVar[str] = "Reliability services are temporarily unavailable."

    def __init__(self, request_id: str) -> None:
        super().__init__(self.public_message)
        self.request_id = request_id


class ReliabilityNotFound(ReliabilityError):
    code = "RELIABILITY_NOT_FOUND"
    public_message = "Reliability resource was not found."


class ReliabilityInvalidStreamCursor(ReliabilityError):
    code = "INVALID_STREAM_CURSOR"
    public_message = "Stream cursor is invalid."


class ReliabilityForbidden(ReliabilityError):
    code = "RELIABILITY_FORBIDDEN"
    public_message = "Reliability action is forbidden."


class ReliabilityConflict(ReliabilityError):
    code = "RELIABILITY_CONFLICT"
    public_message = "Reliability resource conflict."


class ReliabilityInvalid(ReliabilityError):
    code = "RELIABILITY_INVALID"
    public_message = "Reliability request is invalid."


class ReliabilityMigrationRequired(ReliabilityError):
    code = "RELIABILITY_MIGRATION_REQUIRED"
    public_message = "Reliability migration is required before this action."


class ReliabilityQuotaExceeded(ReliabilityError):
    code = "RELIABILITY_QUOTA_EXCEEDED"
    public_message = "Reliability quota was exceeded."


class ReliabilityCutover(ReliabilityError):
    code = "RELIABILITY_CUTOVER"
    public_message = "Reliability cutover is not complete."


class ReliabilityWorkerUnavailable(ReliabilityError):
    code = "WORKER_UNAVAILABLE"
    public_message = "No Worker is currently available."


class ReliabilityDatabaseUnavailable(ReliabilityError):
    code = "DATABASE_UNAVAILABLE"
    public_message = "Reliability database is temporarily unavailable."


__all__ = [
    "RELIABILITY_ERROR_STATUS",
    "ReliabilityConflict",
    "ReliabilityCutover",
    "ReliabilityDatabaseUnavailable",
    "ReliabilityError",
    "ReliabilityForbidden",
    "ReliabilityInvalid",
    "ReliabilityInvalidStreamCursor",
    "ReliabilityMigrationRequired",
    "ReliabilityNotFound",
    "ReliabilityQuotaExceeded",
    "ReliabilityWorkerUnavailable",
]
