from __future__ import annotations

from fastapi import HTTPException

from app.private_work.errors import (
    PRIVATE_WORK_ERROR_STATUS,
    ExecutionApprovalConflict,
    ExecutionApprovalExpired,
    ExecutionApprovalForbidden,
    ExecutionApprovalInvalid,
    ExecutionApprovalNotFound,
    ExecutionApprovalPolicyDisabled,
    PrivateWorkAssetStale,
    PrivateWorkConflict,
    PrivateWorkDefaultAgentUnavailable,
    PrivateWorkError,
    PrivateWorkForbidden,
    PrivateWorkInvalid,
    PrivateWorkMcpQuotaExceeded,
    PrivateWorkNotFound,
    PrivateWorkRunExecutionProfileUnsupported,
    PrivateWorkRunModelSelectionLocked,
    PrivateWorkRunModelUnavailable,
    PrivateWorkRunQuotaExceeded,
    PrivateWorkStorageQuotaExceeded,
    PrivateWorkTooLarge,
    PrivateWorkUnavailable,
)

_PRIVATE_WORK_ERROR_TYPES = (
    ExecutionApprovalNotFound,
    ExecutionApprovalForbidden,
    ExecutionApprovalConflict,
    ExecutionApprovalExpired,
    ExecutionApprovalPolicyDisabled,
    ExecutionApprovalInvalid,
    PrivateWorkNotFound,
    PrivateWorkForbidden,
    PrivateWorkConflict,
    PrivateWorkDefaultAgentUnavailable,
    PrivateWorkAssetStale,
    PrivateWorkRunModelSelectionLocked,
    PrivateWorkRunModelUnavailable,
    PrivateWorkRunExecutionProfileUnsupported,
    PrivateWorkTooLarge,
    PrivateWorkStorageQuotaExceeded,
    PrivateWorkRunQuotaExceeded,
    PrivateWorkMcpQuotaExceeded,
    PrivateWorkInvalid,
    PrivateWorkUnavailable,
)


def private_work_http_exception(error: PrivateWorkError) -> HTTPException:
    """Map a known private-work error without rendering internal exception data."""

    error_type = type(error)
    if error_type not in _PRIVATE_WORK_ERROR_TYPES:
        raise TypeError("unsupported private work error")
    status_code = PRIVATE_WORK_ERROR_STATUS[error_type.code]
    return HTTPException(
        status_code=status_code,
        detail={
            "code": error_type.code,
            "message": error_type.public_message,
            "request_id": error.request_id,
        },
        headers={"Retry-After": "1"} if status_code in {429, 503} else None,
    )
