from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from app.projects.errors import (
    ProjectDatabaseUnavailable,
    ProjectDeletionStateConflict,
    ProjectForbidden,
    ProjectLastAdmin,
    ProjectMemberQuotaExceeded,
    ProjectMembershipVersionConflict,
    ProjectNotFound,
    ProjectQuotaStateConflict,
    ProjectValidationFailed,
)
from app.projects.invitation_models import (
    ProjectInvitationConflict,
    ProjectInvitationInvalid,
)
from app.quotas.models import QuotaUnavailable
from deerflow.trace_context import generate_trace_id, get_current_trace_id

GOVERNANCE_DOMAIN_ERRORS = (
    ProjectNotFound,
    ProjectForbidden,
    ProjectLastAdmin,
    ProjectMemberQuotaExceeded,
    ProjectQuotaStateConflict,
    ProjectMembershipVersionConflict,
    ProjectInvitationConflict,
    ProjectInvitationInvalid,
    ProjectDeletionStateConflict,
    ProjectValidationFailed,
    ProjectDatabaseUnavailable,
    QuotaUnavailable,
)


class GovernanceRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                request_id = get_current_trace_id() or generate_trace_id()
                raise HTTPException(
                    422,
                    detail=governance_error_detail(
                        "PROJECT_VALIDATION_FAILED",
                        "Project validation failed",
                        request_id,
                    ),
                ) from None

        return handler


def governance_error(exc: Exception, request_id: str) -> tuple[int, dict[str, str]]:
    mapping: tuple[tuple[type[Exception], int, str, str], ...] = (
        (
            ProjectNotFound,
            404,
            "PROJECT_OR_MEMBER_NOT_FOUND",
            "Project or member not found",
        ),
        (
            ProjectForbidden,
            403,
            "PROJECT_MEMBERSHIP_FORBIDDEN",
            "Project membership does not allow this operation",
        ),
        (ProjectLastAdmin, 409, "PROJECT_LAST_ADMIN", "Project must keep an active admin"),
        (
            ProjectMemberQuotaExceeded,
            429,
            "PROJECT_MEMBER_QUOTA_EXCEEDED",
            "Project member quota was exceeded",
        ),
        (
            ProjectMembershipVersionConflict,
            409,
            "PROJECT_MEMBERSHIP_VERSION_CONFLICT",
            "Project membership version conflict",
        ),
        (
            ProjectQuotaStateConflict,
            409,
            "PROJECT_QUOTA_STATE_CONFLICT",
            "Project quota state conflict",
        ),
        (
            ProjectInvitationConflict,
            409,
            "PROJECT_INVITATION_CONFLICT",
            "Project invitation conflict",
        ),
        (
            ProjectInvitationInvalid,
            409,
            "PROJECT_INVITATION_INVALID",
            "Project invitation is invalid",
        ),
        (
            ProjectDeletionStateConflict,
            409,
            "PROJECT_DELETION_STATE_CONFLICT",
            "Project deletion state conflict",
        ),
        (
            ProjectValidationFailed,
            422,
            "PROJECT_VALIDATION_FAILED",
            "Project validation failed",
        ),
        (
            ProjectDatabaseUnavailable,
            503,
            "DATABASE_UNAVAILABLE",
            "Project storage unavailable",
        ),
        (
            QuotaUnavailable,
            503,
            "QUOTA_POLICY_UNAVAILABLE",
            "Quota policy unavailable",
        ),
    )
    for error_type, status_code, code, message in mapping:
        if isinstance(exc, error_type):
            return status_code, governance_error_detail(code, message, request_id)
    raise exc


def governance_error_detail(code: str, message: str, request_id: str) -> dict[str, str]:
    return {"code": code, "message": message, "request_id": request_id}


def raise_governance_error(exc: Exception, request_id: str) -> None:
    status_code, detail = governance_error(exc, request_id)
    raise HTTPException(
        status_code,
        detail=detail,
        headers={"Retry-After": "1"} if status_code == 429 else None,
    ) from None
