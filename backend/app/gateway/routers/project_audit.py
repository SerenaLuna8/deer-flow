from __future__ import annotations

import uuid
from functools import wraps
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import (
    AuditAuthorityRejected,
    AuditCursorRejected,
    AuditMetadataRejected,
    AuditPage,
    AuditRecord,
    AuditUnavailable,
)
from app.gateway.deps import get_project_audit_service, project_session
from app.gateway.routers.project_usage import ProjectGovernanceRoute
from app.gateway.routers.projects import authenticated_project_identity
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context_in_transaction
from app.projects.errors import ProjectDatabaseUnavailable, ProjectForbidden, ProjectNotFound
from app.reliability.cutover import ReliabilityCutoverGuard
from app.reliability.error_mapping import reliability_http_exception
from app.reliability.errors import (
    ReliabilityDatabaseUnavailable,
    ReliabilityError,
    ReliabilityInvalid,
    ReliabilityInvalidStreamCursor,
    ReliabilityNotFound,
)

router = APIRouter(
    prefix="/api/projects/{project_id}/audit",
    tags=["project-audit"],
    route_class=ProjectGovernanceRoute,
)


class ProjectAuditItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: uuid.UUID
    occurred_at: str
    actor: str
    action: str
    target_kind: str
    outcome: str
    public_error_code: str | None
    metadata: dict[str, object]


class ProjectAuditPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[ProjectAuditItemResponse]
    next_cursor: str | None


def _actor(record: AuditRecord) -> str:
    if record.actor_process is not None:
        return record.actor_process.value
    if record.actor_platform_role is not None:
        return record.actor_platform_role.value
    return "user"


def _response(page: AuditPage) -> ProjectAuditPageResponse:
    return ProjectAuditPageResponse(
        items=[
            ProjectAuditItemResponse(
                id=item.id,
                occurred_at=item.occurred_at.isoformat(),
                actor=_actor(item),
                action=item.action.value,
                target_kind=item.target_kind.value,
                outcome=item.outcome.value,
                public_error_code=item.public_error_code,
                metadata=item.metadata,
            )
            for item in page.items
        ],
        next_cursor=page.next_cursor,
    )


def _map_audit_errors(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        identity = kwargs.get("identity")
        request_id = identity[1] if type(identity) is tuple and len(identity) == 2 else "project-governance"
        try:
            return await function(*args, **kwargs)
        except ReliabilityError as error:
            raise reliability_http_exception(error) from None
        except (ProjectNotFound, ProjectForbidden, AuditAuthorityRejected):
            raise reliability_http_exception(ReliabilityNotFound(request_id)) from None
        except AuditCursorRejected:
            raise reliability_http_exception(ReliabilityInvalidStreamCursor(request_id)) from None
        except AuditMetadataRejected:
            raise reliability_http_exception(ReliabilityInvalid(request_id)) from None
        except (AuditUnavailable, ProjectDatabaseUnavailable, DBAPIError):
            raise reliability_http_exception(ReliabilityDatabaseUnavailable(request_id)) from None

    return wrapped


@router.get("", response_model=ProjectAuditPageResponse)
@_map_audit_errors
async def list_project_audit(
    project_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
    audit=Depends(get_project_audit_service),
) -> ProjectAuditPageResponse:
    async with session.begin():
        context = await resolve_project_context_in_transaction(
            session,
            identity[0],
            project_id,
            identity[1],
            lock=True,
        )
        if Capability.PROJECT_AUDIT_READ not in context.capabilities:
            raise ProjectForbidden(Capability.PROJECT_AUDIT_READ)
        await ReliabilityCutoverGuard.for_session(
            session,
            request_id=identity[1],
        ).require_gateway_open()
        return _response(
            await audit.list_project(
                session,
                context,
                limit=limit,
                cursor=cursor,
            )
        )


__all__ = ["router"]
