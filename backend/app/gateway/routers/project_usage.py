from __future__ import annotations

import uuid
from datetime import datetime
from functools import wraps

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditError
from app.final_schema import FinalSchemaProbe, FinalSchemaRequired, FinalSchemaUnavailable
from app.gateway.deps import (
    get_operational_audit_sink,
    get_project_quota_service,
    project_session,
)
from app.gateway.routers.projects import authenticated_project_identity
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context_in_transaction
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound
from app.projects.token_usage import (
    ProjectTokenUsageSeries,
    read_project_token_usage_24h,
)
from app.quotas.models import (
    EffectiveQuotaLimits,
    ProjectQuotaLimits,
    ProjectQuotaPolicy,
    ProjectQuotaUsage,
    QuotaConflict,
    QuotaForbidden,
    QuotaPolicyInvalid,
)
from app.reliability.error_mapping import reliability_http_exception
from app.reliability.errors import (
    ReliabilityConflict,
    ReliabilityDatabaseUnavailable,
    ReliabilityError,
    ReliabilityInvalid,
    ReliabilityNotFound,
)
from deerflow.trace_context import generate_trace_id, get_current_trace_id


class ProjectGovernanceRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                request_id = get_current_trace_id() or generate_trace_id()
                raise reliability_http_exception(ReliabilityInvalid(request_id)) from None

        return handler


router = APIRouter(
    prefix="/api/projects/{project_id}/usage",
    tags=["project-usage"],
    route_class=ProjectGovernanceRoute,
)


class QuotaLimitsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    member_limit: int | None
    storage_bytes_limit: int | None
    concurrent_run_limit: int | None
    mcp_calls_daily_limit: int | None


class EffectiveQuotaLimitsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    member_limit: int
    storage_bytes_limit: int
    concurrent_run_limit: int
    mcp_calls_daily_limit: int


class QuotaPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int
    configured: QuotaLimitsResponse
    effective: EffectiveQuotaLimitsResponse


class QuotaDimensionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dimension: str
    bucket: str
    used: int
    reserved: int
    limit: int
    warning_threshold_reached: bool


class ProjectUsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy: QuotaPolicyResponse
    dimensions: list[QuotaDimensionResponse]


class TokenUsageTotalsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class TokenUsagePointResponse(TokenUsageTotalsResponse):
    bucket_start: datetime


class ProjectTokenUsageSeriesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    window_start: datetime
    window_end: datetime
    bucket_minutes: int = Field(ge=1)
    totals: TokenUsageTotalsResponse
    points: list[TokenUsagePointResponse]


class QuotaLimitsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    member_limit: int | None = None
    storage_bytes_limit: int | None = None
    concurrent_run_limit: int | None = None
    mcp_calls_daily_limit: int | None = None


class QuotaPolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=0)
    limits: QuotaLimitsRequest


def _limits_response(value: ProjectQuotaLimits) -> QuotaLimitsResponse:
    return QuotaLimitsResponse(
        member_limit=value.member_limit,
        storage_bytes_limit=value.storage_bytes_limit,
        concurrent_run_limit=value.concurrent_run_limit,
        mcp_calls_daily_limit=value.mcp_calls_daily_limit,
    )


def _effective_response(value: EffectiveQuotaLimits) -> EffectiveQuotaLimitsResponse:
    return EffectiveQuotaLimitsResponse(
        member_limit=value.member_limit,
        storage_bytes_limit=value.storage_bytes_limit,
        concurrent_run_limit=value.concurrent_run_limit,
        mcp_calls_daily_limit=value.mcp_calls_daily_limit,
    )


def _policy_response(value: ProjectQuotaPolicy) -> QuotaPolicyResponse:
    return QuotaPolicyResponse(
        version=value.version,
        configured=_limits_response(value.configured),
        effective=_effective_response(value.effective),
    )


def _usage_response(value: ProjectQuotaUsage) -> ProjectUsageResponse:
    return ProjectUsageResponse(
        policy=_policy_response(value.policy),
        dimensions=[
            QuotaDimensionResponse(
                dimension=item.dimension,
                bucket=item.bucket,
                used=item.used,
                reserved=item.reserved,
                limit=item.limit,
                warning_threshold_reached=item.warning_threshold_reached,
            )
            for item in value.dimensions
        ],
    )


def _token_usage_series_response(
    value: ProjectTokenUsageSeries,
) -> ProjectTokenUsageSeriesResponse:
    return ProjectTokenUsageSeriesResponse(
        window_start=value.window_start,
        window_end=value.window_end,
        bucket_minutes=value.bucket_minutes,
        totals=TokenUsageTotalsResponse(
            input_tokens=value.input_tokens,
            output_tokens=value.output_tokens,
            total_tokens=value.total_tokens,
        ),
        points=[
            TokenUsagePointResponse(
                bucket_start=point.bucket_start,
                input_tokens=point.input_tokens,
                output_tokens=point.output_tokens,
                total_tokens=point.total_tokens,
            )
            for point in value.points
        ],
    )


def _map_project_governance_errors(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        identity = kwargs.get("identity")
        request_id = identity[1] if type(identity) is tuple and len(identity) == 2 else "project-governance"
        try:
            return await function(*args, **kwargs)
        except ReliabilityError as error:
            raise reliability_http_exception(error) from None
        except (ProjectNotFound, QuotaForbidden):
            raise reliability_http_exception(ReliabilityNotFound(request_id)) from None
        except QuotaConflict:
            raise reliability_http_exception(ReliabilityConflict(request_id)) from None
        except QuotaPolicyInvalid:
            raise reliability_http_exception(ReliabilityInvalid(request_id)) from None
        except (AuditError, ProjectDatabaseUnavailable, DBAPIError):
            raise reliability_http_exception(ReliabilityDatabaseUnavailable(request_id)) from None

    return wrapped


async def _project_context(
    session: AsyncSession,
    project_id: uuid.UUID,
    identity: tuple[uuid.UUID, str],
    *,
    lock: bool,
):
    context = await resolve_project_context_in_transaction(
        session,
        identity[0],
        project_id,
        identity[1],
        lock=lock,
    )
    if Capability.PROJECT_USAGE_READ not in context.capabilities:
        raise QuotaForbidden("project usage authority is required")
    try:
        await FinalSchemaProbe().require_ready(session)
    except (FinalSchemaRequired, FinalSchemaUnavailable):
        raise ReliabilityDatabaseUnavailable(identity[1]) from None
    return context


@router.get("", response_model=ProjectUsageResponse)
@_map_project_governance_errors
async def get_project_usage(
    project_id: uuid.UUID,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
    quotas=Depends(get_project_quota_service),
) -> ProjectUsageResponse:
    async with session.begin():
        context = await _project_context(session, project_id, identity, lock=False)
        return _usage_response(await quotas.read_usage(session, context))


@router.get(
    "/token-series",
    response_model=ProjectTokenUsageSeriesResponse,
)
@_map_project_governance_errors
async def get_project_token_usage_series(
    project_id: uuid.UUID,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
) -> ProjectTokenUsageSeriesResponse:
    async with session.begin():
        context = await _project_context(
            session,
            project_id,
            identity,
            lock=True,
        )
        return _token_usage_series_response(
            await read_project_token_usage_24h(
                session,
                context.project_id,
            )
        )


@router.patch("/limits", response_model=QuotaPolicyResponse)
@_map_project_governance_errors
async def update_project_quota_limits(
    project_id: uuid.UUID,
    body: QuotaPolicyUpdateRequest,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
    quotas=Depends(get_project_quota_service),
    audit=Depends(get_operational_audit_sink),
) -> QuotaPolicyResponse:
    async with session.begin():
        project = await _project_context(session, project_id, identity, lock=True)
        context = PrivateWorkContext.from_project(project)
        policy = await quotas.set_limits(
            session,
            context,
            ProjectQuotaLimits(**body.limits.model_dump()),
            expected_version=body.expected_version,
        )
        await audit.quota_policy_updated(session, context, policy)
        return _policy_response(policy)


__all__ = ["ProjectGovernanceRoute", "router"]
