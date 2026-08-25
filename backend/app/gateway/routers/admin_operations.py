from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from functools import wraps
from inspect import isawaitable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import (
    AuditAuthorityRejected,
    AuditCursorRejected,
    AuditMetadataRejected,
    AuditUnavailable,
    SystemAuditContext,
)
from app.final_schema import FinalSchemaProbe, FinalSchemaRequired, FinalSchemaUnavailable
from app.gateway.deps import get_current_user_from_request, project_session
from app.reliability.error_mapping import reliability_http_exception
from app.reliability.errors import (
    ReliabilityConflict,
    ReliabilityDatabaseUnavailable,
    ReliabilityError,
    ReliabilityInvalid,
    ReliabilityInvalidStreamCursor,
    ReliabilityNotFound,
)
from app.reliability.models import ReliabilityReadiness
from app.reliability.operations import (
    ChannelProviderHealth,
    OperationsOverview,
    SystemOperationsRepository,
    resolve_current_system_audit_context,
    safe_channel_provider_health,
)
from app.reliability.process_readiness import read_process_readiness
from app.reliability.readiness import ReliabilityReadinessService
from deerflow.config import get_app_config
from deerflow.persistence.engine import get_session_factory
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
                await _require_validation_system_admin(request, request_id)
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
    status: Literal["ready", "degraded", "closed"]
    database: str
    schema_status: str = Field(alias="schema", serialization_alias="schema")
    worker_fleet: str
    scheduler: str
    stream: str
    quota: str
    audit: str
    role: str
    worker_count: int
    worker_capacity: int
    worker_oldest_heartbeat_age_seconds: int | None
    private_run_worker_fleet: str
    private_run_worker_count: int
    private_run_worker_capacity: int
    scheduler_ownership: str
    schema_state: Literal["ready", "unavailable"]
    run_skill_writer_mode: Literal["v4_reference", "legacy_v3"]
    run_skill_writer_artifact_version: str
    run_skill_legacy_policy_digest: str
    run_skill_writer_ready: bool


class OperationsCountsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    projects: int
    suspended_projects: int
    queued_jobs: int
    running_jobs: int
    dead_jobs: int
    ready_jobs: int
    oldest_ready_job_age_seconds: int | None
    stale_leases: int
    waiting_for_worker_runs: int
    waiting_for_terminalization_runs: int


class AggregateUsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dimension: str
    used: int
    reserved: int


class ChannelProviderHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: str
    status: Literal["ready", "degraded", "unavailable"]
    checked_at: datetime
    code: Literal["CHANNEL_READY", "CHANNEL_STOPPED", "CHANNEL_DISABLED"]


class OperationsOverviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    readiness: OperationsReadinessResponse
    data_status: Literal["available", "unavailable"]
    counts: OperationsCountsResponse | None
    usage: list[AggregateUsageResponse] | None
    channel_providers: list[ChannelProviderHealthResponse]

    @model_validator(mode="after")
    def validate_aggregate_availability(self) -> OperationsOverviewResponse:
        closed = self.readiness.status == "closed"
        if closed and (self.data_status != "unavailable" or self.counts is not None or self.usage is not None):
            raise ValueError("closed readiness must not represent aggregate data as available")
        if not closed and (self.data_status != "available" or self.counts is None or self.usage is None):
            raise ValueError("open readiness requires available aggregate data")
        return self


def _channel_provider_response(
    item: ChannelProviderHealth,
) -> ChannelProviderHealthResponse:
    return ChannelProviderHealthResponse(
        provider=item.provider,
        status=item.status,
        checked_at=item.checked_at,
        code=item.code,
    )


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
    try:
        await FinalSchemaProbe().require_ready(session)
    except (FinalSchemaRequired, FinalSchemaUnavailable):
        raise ReliabilityDatabaseUnavailable(identity[1]) from None
    return context


async def _validation_user(request: Request):
    resolver = request.app.dependency_overrides.get(
        get_current_user_from_request,
        get_current_user_from_request,
    )
    result = resolver(request)
    return await result if isawaitable(result) else result


async def _require_validation_system_admin(request: Request, request_id: str) -> None:
    try:
        user = await _validation_user(request)
        user_id = uuid.UUID(str(user.id))
    except HTTPException:
        raise
    except (AttributeError, TypeError, ValueError):
        raise reliability_http_exception(ReliabilityNotFound(request_id)) from None

    session_factory = getattr(request.app.state, "admin_operations_session_factory", None)
    try:
        factory = session_factory or get_session_factory()
        async with factory() as session, session.begin():
            await resolve_current_system_audit_context(session, user_id, request_id)
    except AuditAuthorityRejected:
        raise reliability_http_exception(ReliabilityNotFound(request_id)) from None
    except (DBAPIError, RuntimeError):
        raise reliability_http_exception(ReliabilityDatabaseUnavailable(request_id)) from None


async def current_reliability_readiness(
    request: Request,
    session: AsyncSession,
    identity: tuple[uuid.UUID, str],
    *,
    worker_fresh_for_seconds: int | None = None,
) -> ReliabilityReadiness:
    service = getattr(request.app.state, "reliability_readiness_service", None)
    if service is None:
        selected_worker_freshness = worker_fresh_for_seconds if worker_fresh_for_seconds is not None else await current_worker_fresh_for_seconds()
        try:
            from app.system_runtime_settings import (
                AutomationsPolicyValue,
                RuntimePolicySection,
            )
            from app.system_runtime_settings.materializer import (
                SystemRuntimePolicyMaterializer,
            )

            policy = await SystemRuntimePolicyMaterializer.materialize_current_in_session(
                session,
                RuntimePolicySection.AUTOMATIONS,
            )
            if type(policy) is not AutomationsPolicyValue:
                raise TypeError
            scheduler_enabled = policy.enabled
        except asyncio.CancelledError:
            raise
        except Exception:
            scheduler_enabled = False
        process = await read_process_readiness(
            session,
            role="gateway",
            scheduler_enabled=scheduler_enabled,
            worker_fresh_for_seconds=selected_worker_freshness,
        )
        service = ReliabilityReadinessService(
            FinalSchemaProbe(),
            session,
            identity[1],
            stream=lambda: _gateway_components_status(
                request,
                "private_run_event_store",
                "private_stream_bridge",
            ),
            quota=lambda: _gateway_components_status(
                request,
                "project_quota_service",
                "project_quota_enforcer",
            ),
            audit=lambda: _gateway_components_status(
                request,
                "project_audit_service",
                "operational_audit_sink",
            ),
            process=process,
        )
    result = service.read()
    if not isawaitable(result):
        raise TypeError("reliability readiness service must be async")
    return await result


async def current_worker_fresh_for_seconds() -> int:
    try:
        config = await asyncio.to_thread(get_app_config)
        return config.worker.heartbeat_seconds * 3
    except FileNotFoundError:
        # Small embedded/test apps may intentionally omit a config file.
        return 60


def _gateway_components_status(
    request: Request,
    *state_names: str,
) -> Literal["ready", "unavailable"]:
    """Project initialized Gateway adapters to one content-free health enum."""

    return "ready" if all(getattr(request.app.state, name, None) is not None for name in state_names) else "unavailable"


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


def overview_response(
    value: OperationsOverview | None,
    readiness: ReliabilityReadiness,
    channel_providers: tuple[ChannelProviderHealth, ...] = (),
) -> OperationsOverviewResponse:
    if readiness.status == "closed":
        return OperationsOverviewResponse(
            readiness=OperationsReadinessResponse(
                status=readiness.status,
                database=readiness.database,
                schema_status=readiness.schema,
                worker_fleet=readiness.worker_fleet,
                scheduler=readiness.scheduler,
                stream=readiness.stream,
                quota=readiness.quota,
                audit=readiness.audit,
                role=readiness.role,
                worker_count=readiness.worker_count,
                worker_capacity=readiness.worker_capacity,
                worker_oldest_heartbeat_age_seconds=readiness.worker_oldest_heartbeat_age_seconds,
                private_run_worker_fleet=readiness.private_run_worker_fleet,
                private_run_worker_count=readiness.private_run_worker_count,
                private_run_worker_capacity=readiness.private_run_worker_capacity,
                scheduler_ownership=readiness.scheduler_ownership,
                schema_state=readiness.schema_state,
                run_skill_writer_mode=readiness.run_skill_writer_mode,
                run_skill_writer_artifact_version=(readiness.run_skill_writer_artifact_version),
                run_skill_legacy_policy_digest=(readiness.run_skill_legacy_policy_digest),
                run_skill_writer_ready=readiness.run_skill_writer_ready,
            ),
            data_status="unavailable",
            counts=None,
            usage=None,
            channel_providers=[_channel_provider_response(item) for item in channel_providers],
        )
    if value is None:
        raise ValueError("open readiness requires operations overview")
    return OperationsOverviewResponse(
        readiness=OperationsReadinessResponse(
            status=readiness.status,
            database=readiness.database,
            schema_status=readiness.schema,
            worker_fleet=readiness.worker_fleet,
            scheduler=readiness.scheduler,
            stream=readiness.stream,
            quota=readiness.quota,
            audit=readiness.audit,
            role=readiness.role,
            worker_count=readiness.worker_count,
            worker_capacity=readiness.worker_capacity,
            worker_oldest_heartbeat_age_seconds=readiness.worker_oldest_heartbeat_age_seconds,
            private_run_worker_fleet=readiness.private_run_worker_fleet,
            private_run_worker_count=readiness.private_run_worker_count,
            private_run_worker_capacity=readiness.private_run_worker_capacity,
            scheduler_ownership=readiness.scheduler_ownership,
            schema_state=readiness.schema_state,
            run_skill_writer_mode=readiness.run_skill_writer_mode,
            run_skill_writer_artifact_version=(readiness.run_skill_writer_artifact_version),
            run_skill_legacy_policy_digest=(readiness.run_skill_legacy_policy_digest),
            run_skill_writer_ready=readiness.run_skill_writer_ready,
        ),
        data_status="available",
        counts=OperationsCountsResponse(
            projects=value.counts.projects,
            suspended_projects=value.counts.suspended_projects,
            queued_jobs=value.counts.queued_jobs,
            running_jobs=value.counts.running_jobs,
            dead_jobs=value.counts.dead_jobs,
            ready_jobs=value.counts.ready_jobs,
            oldest_ready_job_age_seconds=(value.counts.oldest_ready_job_age_seconds),
            stale_leases=value.counts.stale_leases,
            waiting_for_worker_runs=value.counts.waiting_for_worker_runs,
            waiting_for_terminalization_runs=(value.counts.waiting_for_terminalization_runs),
        ),
        usage=[
            AggregateUsageResponse(
                dimension=item.dimension,
                used=item.used,
                reserved=item.reserved,
            )
            for item in value.usage
        ],
        channel_providers=[_channel_provider_response(item) for item in channel_providers],
    )


async def current_channel_provider_health() -> tuple[ChannelProviderHealth, ...]:
    """Read channel status once under a short bound and expose safe enums only."""

    try:
        from app.channels.service import get_channel_service

        service = get_channel_service()
        if service is None:
            return ()
        raw = await asyncio.wait_for(
            asyncio.to_thread(service.get_status),
            timeout=1.0,
        )
    except Exception:
        return ()
    return safe_channel_provider_health(raw)


@router.get("", response_model=OperationsOverviewResponse)
@map_admin_operations_errors
async def get_operations_overview(
    request: Request,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_system_identity),
    session: AsyncSession = Depends(project_session),
) -> OperationsOverviewResponse:
    async with session.begin():
        await resolve_current_system_audit_context(session, identity[0], identity[1])
        worker_fresh_for_seconds = await current_worker_fresh_for_seconds()
        readiness = await current_reliability_readiness(
            request,
            session,
            identity,
            worker_fresh_for_seconds=worker_fresh_for_seconds,
        )
        channel_providers = await current_channel_provider_health()
        if readiness.status == "closed":
            return overview_response(None, readiness, channel_providers)
        return overview_response(
            await SystemOperationsRepository(session).overview(
                worker_fresh_for_seconds=worker_fresh_for_seconds,
            ),
            readiness,
            channel_providers,
        )


__all__ = [
    "AdminOperationsRoute",
    "authenticated_system_identity",
    "current_reliability_readiness",
    "current_worker_fresh_for_seconds",
    "current_system_context",
    "map_admin_operations_errors",
    "router",
]
