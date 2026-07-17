from __future__ import annotations

import uuid
from functools import wraps

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import (
    AuditAuthorityRejected,
    AuditCursorRejected,
    AuditMetadataRejected,
    AuditUnavailable,
    SystemAuditContext,
)
from app.gateway.deps import get_current_user_from_request, project_session
from app.reliability.cutover import ReliabilityCutoverGuard
from app.reliability.error_mapping import reliability_http_exception
from app.reliability.errors import (
    ReliabilityConflict,
    ReliabilityDatabaseUnavailable,
    ReliabilityError,
    ReliabilityInvalid,
    ReliabilityInvalidStreamCursor,
    ReliabilityNotFound,
)
from app.reliability.operations import (
    OperationsOverview,
    SystemOperationsRepository,
    resolve_current_system_audit_context,
)
from deerflow.persistence.jobs.sql import JobIdempotencyConflict, JobRequeueForbidden
from deerflow.trace_context import (
    generate_trace_id,
    get_current_trace_id,
    normalize_trace_id,
)


class AdminOperationsRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                request_id = get_current_trace_id() or normalize_trace_id(request.headers.get("x-trace-id")) or generate_trace_id()
                raise reliability_http_exception(ReliabilityInvalid(request_id)) from None

        return handler


router = APIRouter(
    prefix="/api/admin/operations",
    tags=["admin-operations"],
    route_class=AdminOperationsRoute,
)


class OperationsReadinessResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
    )
    status: str
    database: str
    schema_status: str = Field(alias="schema", serialization_alias="schema")


class OperationsCountsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    projects: int
    suspended_projects: int
    queued_jobs: int
    running_jobs: int
    dead_jobs: int


class AggregateUsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dimension: str
    used: int
    reserved: int


class OperationsOverviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    readiness: OperationsReadinessResponse
    counts: OperationsCountsResponse
    usage: list[AggregateUsageResponse]


async def authenticated_system_identity(
    request: Request,
    user=Depends(get_current_user_from_request),
) -> tuple[uuid.UUID, str]:
    try:
        user_id = uuid.UUID(str(user.id))
    except (AttributeError, TypeError, ValueError):
        raise reliability_http_exception(ReliabilityNotFound(get_current_trace_id() or generate_trace_id())) from None
    request_id = get_current_trace_id() or normalize_trace_id(request.headers.get("x-trace-id")) or generate_trace_id()
    return user_id, request_id


async def current_system_context(
    session: AsyncSession,
    identity: tuple[uuid.UUID, str],
) -> SystemAuditContext:
    context = await resolve_current_system_audit_context(
        session,
        identity[0],
        identity[1],
    )
    await ReliabilityCutoverGuard.for_session(
        session,
        request_id=identity[1],
    ).require_gateway_open()
    return context


def map_admin_operations_errors(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        identity = kwargs.get("identity")
        request_id = identity[1] if type(identity) is tuple and len(identity) == 2 else get_current_trace_id() or generate_trace_id()
        try:
            return await function(*args, **kwargs)
        except ReliabilityError as error:
            raise reliability_http_exception(error) from None
        except (AuditAuthorityRejected, JobRequeueForbidden):
            raise reliability_http_exception(ReliabilityNotFound(request_id)) from None
        except AuditCursorRejected:
            raise reliability_http_exception(ReliabilityInvalidStreamCursor(request_id)) from None
        except JobIdempotencyConflict:
            raise reliability_http_exception(ReliabilityConflict(request_id)) from None
        except (AuditMetadataRejected, TypeError, ValueError):
            raise reliability_http_exception(ReliabilityInvalid(request_id)) from None
        except (AuditUnavailable, DBAPIError):
            raise reliability_http_exception(ReliabilityDatabaseUnavailable(request_id)) from None

    return wrapped


def overview_response(value: OperationsOverview) -> OperationsOverviewResponse:
    return OperationsOverviewResponse(
        readiness=OperationsReadinessResponse(
            status="ready",
            database="ready",
            schema_status="ready",
        ),
        counts=OperationsCountsResponse(
            projects=value.counts.projects,
            suspended_projects=value.counts.suspended_projects,
            queued_jobs=value.counts.queued_jobs,
            running_jobs=value.counts.running_jobs,
            dead_jobs=value.counts.dead_jobs,
        ),
        usage=[
            AggregateUsageResponse(
                dimension=item.dimension,
                used=item.used,
                reserved=item.reserved,
            )
            for item in value.usage
        ],
    )


@router.get("", response_model=OperationsOverviewResponse)
@map_admin_operations_errors
async def get_operations_overview(
    identity: tuple[uuid.UUID, str] = Depends(authenticated_system_identity),
    session: AsyncSession = Depends(project_session),
) -> OperationsOverviewResponse:
    async with session.begin():
        await current_system_context(session, identity)
        return overview_response(await SystemOperationsRepository(session).overview())


__all__ = [
    "AdminOperationsRoute",
    "authenticated_system_identity",
    "current_system_context",
    "map_admin_operations_errors",
    "router",
]
