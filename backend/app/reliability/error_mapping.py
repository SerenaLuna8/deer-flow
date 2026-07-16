"""Stable HTTP mapping for M6 reliability errors."""

from typing import Any

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from app.reliability.errors import (
    RELIABILITY_ERROR_STATUS,
    ReliabilityConflict,
    ReliabilityCutover,
    ReliabilityDatabaseUnavailable,
    ReliabilityError,
    ReliabilityForbidden,
    ReliabilityInvalid,
    ReliabilityInvalidStreamCursor,
    ReliabilityMigrationRequired,
    ReliabilityNotFound,
    ReliabilityQuotaExceeded,
    ReliabilityWorkerUnavailable,
)

_RELIABILITY_ERROR_TYPES = (
    ReliabilityInvalidStreamCursor,
    ReliabilityNotFound,
    ReliabilityForbidden,
    ReliabilityConflict,
    ReliabilityInvalid,
    ReliabilityMigrationRequired,
    ReliabilityQuotaExceeded,
    ReliabilityCutover,
    ReliabilityWorkerUnavailable,
    ReliabilityDatabaseUnavailable,
)


class ReliabilityHTTPException(HTTPException):
    """HTTP exception carrying the exact public top-level response body."""

    def __init__(
        self,
        *,
        status_code: int,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=body, headers=headers)
        self.body = body


async def reliability_http_exception_handler(
    _request: Request,
    error: ReliabilityHTTPException,
) -> JSONResponse:
    """Serialize the stable contract without FastAPI's default detail wrapper."""

    return JSONResponse(
        status_code=error.status_code,
        content=error.body,
        headers=error.headers,
    )


def reliability_http_exception(error: ReliabilityError) -> ReliabilityHTTPException:
    """Map only explicitly approved public errors without rendering internals."""

    error_type = type(error)
    if error_type not in _RELIABILITY_ERROR_TYPES:
        raise TypeError("unsupported reliability error")
    status_code = RELIABILITY_ERROR_STATUS[error_type.code]
    return ReliabilityHTTPException(
        status_code=status_code,
        body={
            "code": error_type.code,
            "message": error_type.public_message,
            "request_id": error.request_id,
        },
        headers={"Retry-After": "1"} if status_code in {429, 503} else None,
    )


__all__ = [
    "ReliabilityHTTPException",
    "reliability_http_exception",
    "reliability_http_exception_handler",
]
