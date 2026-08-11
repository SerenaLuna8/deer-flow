"""Stable public Workflow domain errors.

The Gateway may translate these errors into the declared HTTP status without
exposing persistence exceptions.  Worker admission/settlement may use the same
``job_terminal_code``; neither surface serializes ``str(cause)``.
"""

from __future__ import annotations

from typing import ClassVar

WORKFLOW_ERROR_STATUS = {
    "WORKFLOW_NOT_FOUND": 404,
    "WORKFLOW_FORBIDDEN": 403,
    "WORKFLOW_DRAFT_CONFLICT": 409,
    "WORKFLOW_DRAFT_INVALID": 422,
    "WORKFLOW_VERSION_NOT_EXECUTABLE": 409,
    "WORKFLOW_RUN_CONFLICT": 409,
    "WORKFLOW_RUN_NOT_RESUMABLE": 409,
    "WORKFLOW_RUN_RETRY_FORBIDDEN": 409,
    "WORKFLOW_INPUT_INVALID": 422,
    "WORKFLOW_OUTPUT_INVALID": 422,
    "SIDE_EFFECT_STATE_UNKNOWN": 409,
    "WORKFLOW_UNAVAILABLE": 503,
}


class WorkflowError(Exception):
    code: ClassVar[str] = "WORKFLOW_UNAVAILABLE"
    public_message: ClassVar[str] = "Workflow is temporarily unavailable."
    http_status: ClassVar[int] = 503
    job_terminal_code: ClassVar[str] = "WORKFLOW_UNAVAILABLE"

    def __init__(self, request_id: str) -> None:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("Workflow errors require a request ID")
        self.request_id = request_id
        super().__init__(self.public_message)


class WorkflowNotFound(WorkflowError):
    code = "WORKFLOW_NOT_FOUND"
    public_message = "Workflow was not found."
    http_status = 404
    job_terminal_code = code


class WorkflowForbidden(WorkflowError):
    code = "WORKFLOW_FORBIDDEN"
    public_message = "Workflow action is forbidden."
    http_status = 403
    job_terminal_code = code


class WorkflowDraftConflict(WorkflowError):
    code = "WORKFLOW_DRAFT_CONFLICT"
    public_message = "Workflow draft conflict."
    http_status = 409
    job_terminal_code = code


class WorkflowDraftInvalid(WorkflowError):
    code = "WORKFLOW_DRAFT_INVALID"
    public_message = "Workflow draft is invalid."
    http_status = 422
    job_terminal_code = code


class WorkflowVersionNotExecutable(WorkflowError):
    code = "WORKFLOW_VERSION_NOT_EXECUTABLE"
    public_message = "Workflow version is not executable."
    http_status = 409
    job_terminal_code = code


class WorkflowRunConflict(WorkflowError):
    code = "WORKFLOW_RUN_CONFLICT"
    public_message = "Workflow Run conflict."
    http_status = 409
    job_terminal_code = code


class WorkflowRunNotResumable(WorkflowError):
    code = "WORKFLOW_RUN_NOT_RESUMABLE"
    public_message = "Workflow Run is not resumable."
    http_status = 409
    job_terminal_code = code


class WorkflowRunRetryForbidden(WorkflowError):
    code = "WORKFLOW_RUN_RETRY_FORBIDDEN"
    public_message = "Workflow Run cannot be retried."
    http_status = 409
    job_terminal_code = code


class WorkflowInputInvalid(WorkflowError):
    code = "WORKFLOW_INPUT_INVALID"
    public_message = "Workflow input is invalid."
    http_status = 422
    job_terminal_code = code


class WorkflowOutputInvalid(WorkflowError):
    code = "WORKFLOW_OUTPUT_INVALID"
    public_message = "Workflow output is invalid."
    http_status = 422
    job_terminal_code = code


class WorkflowSideEffectUnknown(WorkflowError):
    code = "SIDE_EFFECT_STATE_UNKNOWN"
    public_message = "Workflow side-effect state is unknown."
    http_status = 409
    job_terminal_code = code


class WorkflowUnavailable(WorkflowError):
    code = "WORKFLOW_UNAVAILABLE"
    public_message = "Workflow is temporarily unavailable."
    http_status = 503
    job_terminal_code = code


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

assert {error_type.code for error_type in _WORKFLOW_ERROR_TYPES} == set(WORKFLOW_ERROR_STATUS)
assert all(WORKFLOW_ERROR_STATUS[error_type.code] == error_type.http_status and error_type.job_terminal_code == error_type.code for error_type in _WORKFLOW_ERROR_TYPES)


__all__ = [
    "WORKFLOW_ERROR_STATUS",
    "WorkflowDraftConflict",
    "WorkflowDraftInvalid",
    "WorkflowError",
    "WorkflowForbidden",
    "WorkflowInputInvalid",
    "WorkflowNotFound",
    "WorkflowOutputInvalid",
    "WorkflowRunConflict",
    "WorkflowRunNotResumable",
    "WorkflowRunRetryForbidden",
    "WorkflowSideEffectUnknown",
    "WorkflowUnavailable",
    "WorkflowVersionNotExecutable",
]
