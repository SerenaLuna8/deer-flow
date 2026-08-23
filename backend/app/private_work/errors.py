from __future__ import annotations

from typing import ClassVar

PRIVATE_WORK_ERROR_STATUS = {
    "PRIVATE_WORK_NOT_FOUND": 404,
    "PRIVATE_WORK_FORBIDDEN": 403,
    "PRIVATE_WORK_CONFLICT": 409,
    "PRIVATE_WORK_ASSET_STALE": 409,
    "PRIVATE_WORK_AGENT_ARCHIVED": 409,
    "RUN_MODEL_SELECTION_LOCKED": 409,
    "RUN_MODEL_UNAVAILABLE": 409,
    "RUN_EXECUTION_PROFILE_UNSUPPORTED": 409,
    "RUN_WORKLOAD_PROFILE_UNSUPPORTED": 409,
    "MEMORY_DREAM_MODEL_UNAVAILABLE": 409,
    "DEFAULT_AGENT_UNAVAILABLE": 409,
    "PRIVATE_WORK_TOO_LARGE": 413,
    "PROJECT_STORAGE_QUOTA_EXCEEDED": 429,
    "PROJECT_RUN_QUOTA_EXCEEDED": 429,
    "PROJECT_MCP_QUOTA_EXCEEDED": 429,
    "PRIVATE_WORK_INVALID": 422,
    "PRIVATE_WORK_UNAVAILABLE": 503,
    "EXECUTION_APPROVAL_NOT_FOUND": 404,
    "EXECUTION_APPROVAL_FORBIDDEN": 403,
    "EXECUTION_APPROVAL_CONFLICT": 409,
    "EXECUTION_APPROVAL_EXPIRED": 409,
    "EXECUTION_APPROVAL_POLICY_DISABLED": 409,
    "EXECUTION_APPROVAL_INVALID": 422,
}


class PrivateWorkError(Exception):
    code: ClassVar[str]
    public_message: ClassVar[str]

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(self.public_message)


class PrivateWorkNotFound(PrivateWorkError):
    code = "PRIVATE_WORK_NOT_FOUND"
    public_message = "Private work was not found."


class PrivateWorkForbidden(PrivateWorkError):
    code = "PRIVATE_WORK_FORBIDDEN"
    public_message = "Private work action is forbidden."


class PrivateWorkConflict(PrivateWorkError):
    code = "PRIVATE_WORK_CONFLICT"
    public_message = "Private work conflict."


class PrivateWorkThreadBusy(PrivateWorkConflict):
    """An incomplete Run currently owns the Thread mutation seam."""


class PrivateWorkCompactionDisabled(PrivateWorkConflict):
    """The authoritative runtime policy disables context compaction."""


class PrivateWorkAssetStale(PrivateWorkError):
    code = "PRIVATE_WORK_ASSET_STALE"
    public_message = "Private work asset is stale."


class PrivateWorkAgentArchived(PrivateWorkError):
    code = "PRIVATE_WORK_AGENT_ARCHIVED"
    public_message = "The Agent was archived."


class PrivateWorkRunModelSelectionLocked(PrivateWorkError):
    code = "RUN_MODEL_SELECTION_LOCKED"
    public_message = "The selected Agent model is locked."


class PrivateWorkRunModelUnavailable(PrivateWorkError):
    code = "RUN_MODEL_UNAVAILABLE"
    public_message = "The selected model is unavailable."


class PrivateWorkRunExecutionProfileUnsupported(PrivateWorkError):
    code = "RUN_EXECUTION_PROFILE_UNSUPPORTED"
    public_message = "The selected model does not support the Run execution profile."


class PrivateWorkRunWorkloadProfileUnsupported(PrivateWorkError):
    code = "RUN_WORKLOAD_PROFILE_UNSUPPORTED"
    public_message = "The Run workload profile is unavailable for the admitted runtime policy."


class PrivateWorkDreamModelUnavailable(PrivateWorkError):
    code = "MEMORY_DREAM_MODEL_UNAVAILABLE"
    public_message = "The configured Dream model is unavailable."


class PrivateWorkDefaultAgentUnavailable(PrivateWorkError):
    code = "DEFAULT_AGENT_UNAVAILABLE"
    public_message = "The project default Agent is unavailable."


class PrivateWorkTooLarge(PrivateWorkError):
    code = "PRIVATE_WORK_TOO_LARGE"
    public_message = "Private work payload is too large."


class PrivateWorkStorageQuotaExceeded(PrivateWorkError):
    code = "PROJECT_STORAGE_QUOTA_EXCEEDED"
    public_message = "Project storage quota was exceeded."


class PrivateWorkRunQuotaExceeded(PrivateWorkError):
    code = "PROJECT_RUN_QUOTA_EXCEEDED"
    public_message = "Project concurrent Run quota was exceeded."


class PrivateWorkMcpQuotaExceeded(PrivateWorkError):
    code = "PROJECT_MCP_QUOTA_EXCEEDED"
    public_message = "Project MCP call quota was exceeded."


class PrivateWorkInvalid(PrivateWorkError):
    code = "PRIVATE_WORK_INVALID"
    public_message = "Private work request is invalid."


class PrivateWorkUnavailable(PrivateWorkError):
    code = "PRIVATE_WORK_UNAVAILABLE"
    public_message = "Private work is unavailable."


class ExecutionApprovalNotFound(PrivateWorkError):
    code = "EXECUTION_APPROVAL_NOT_FOUND"
    public_message = "Execution approval was not found."


class ExecutionApprovalForbidden(PrivateWorkError):
    code = "EXECUTION_APPROVAL_FORBIDDEN"
    public_message = "Host execution approval is forbidden."


class ExecutionApprovalConflict(PrivateWorkError):
    code = "EXECUTION_APPROVAL_CONFLICT"
    public_message = "Execution approval state changed."


class ExecutionApprovalExpired(PrivateWorkError):
    code = "EXECUTION_APPROVAL_EXPIRED"
    public_message = "Execution approval expired."


class ExecutionApprovalPolicyDisabled(PrivateWorkError):
    code = "EXECUTION_APPROVAL_POLICY_DISABLED"
    public_message = "Host execution approval is disabled."


class ExecutionApprovalInvalid(PrivateWorkError):
    code = "EXECUTION_APPROVAL_INVALID"
    public_message = "Execution approval request is invalid."
