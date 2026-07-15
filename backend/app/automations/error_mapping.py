from __future__ import annotations

from fastapi import HTTPException

from app.automations.errors import (
    AUTOMATION_ERROR_STATUS,
    AutomationActiveRun,
    AutomationConcurrencyLimit,
    AutomationCutover,
    AutomationError,
    AutomationForbidden,
    AutomationInvalid,
    AutomationNotFound,
    AutomationUnavailable,
    AutomationVersionConflict,
)

_AUTOMATION_ERROR_TYPES = (
    AutomationNotFound,
    AutomationForbidden,
    AutomationInvalid,
    AutomationVersionConflict,
    AutomationActiveRun,
    AutomationCutover,
    AutomationConcurrencyLimit,
    AutomationUnavailable,
)


def automation_http_exception(error: AutomationError) -> HTTPException:
    """Map an approved public automation error without rendering internals."""

    error_type = type(error)
    if error_type not in _AUTOMATION_ERROR_TYPES:
        raise TypeError("unsupported automation error")
    return HTTPException(
        status_code=AUTOMATION_ERROR_STATUS[error_type.code],
        detail={
            "code": error_type.code,
            "message": error_type.public_message,
            "request_id": error.request_id,
        },
    )


__all__ = ["automation_http_exception"]
