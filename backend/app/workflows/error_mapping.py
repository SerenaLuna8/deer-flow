from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from app.workflows.errors import (
    WORKFLOW_ERROR_STATUS,
    WorkflowDraftConflict,
    WorkflowDraftInvalid,
    WorkflowError,
    WorkflowForbidden,
    WorkflowInputInvalid,
    WorkflowNotFound,
    WorkflowOutputInvalid,
    WorkflowRunConflict,
    WorkflowRunNotResumable,
    WorkflowRunRetryForbidden,
    WorkflowSideEffectUnknown,
    WorkflowUnavailable,
    WorkflowVersionNotExecutable,
)
from deerflow.trace_context import generate_trace_id, get_current_trace_id

_WORKFLOW_ERROR_TYPES = (
    WorkflowNotFound,
    WorkflowForbidden,
    WorkflowDraftConflict,
    WorkflowDraftInvalid,
    WorkflowVersionNotExecutable,
    WorkflowRunConflict,
    WorkflowRunNotResumable,
    WorkflowRunRetryForbidden,
    WorkflowInputInvalid,
    WorkflowOutputInvalid,
    WorkflowSideEffectUnknown,
    WorkflowUnavailable,
)


def workflow_http_exception(error: WorkflowError) -> HTTPException:
    """Map an exact Workflow domain error without exposing its cause."""

    error_type = type(error)
    if error_type not in _WORKFLOW_ERROR_TYPES:
        raise TypeError("unsupported Workflow error")
    status_code = WORKFLOW_ERROR_STATUS[error_type.code]
    return HTTPException(
        status_code=status_code,
        detail={
            "code": error_type.code,
            "message": error_type.public_message,
            "request_id": error.request_id,
        },
        headers={"Retry-After": "1"} if status_code == 503 else None,
    )


class WorkflowRoute(APIRoute):
    """Keep request-validation failures inside the closed Workflow error shape."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                request_id = get_current_trace_id() or generate_trace_id()
                raise workflow_http_exception(WorkflowInputInvalid(request_id)) from None

        return handler


__all__ = ["WorkflowRoute", "workflow_http_exception"]
