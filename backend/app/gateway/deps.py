"""Centralized accessors for singleton objects stored on ``app.state``.

**Getters** (used by routers): raise 503 when a required dependency is
missing, except ``get_store`` which returns ``None``.

``AppConfig`` is intentionally *not* cached on ``app.state``. Routers resolve
it through :func:`deerflow.config.app_config.get_app_config`,
which performs mtime-based hot reload, so edits to ``config.yaml`` take
effect on the next request without a process restart. The engines created in
:func:`gateway_platform_runtime` (persistence, checkpointer, and store) accept
a ``startup_config`` snapshot — they are
restart-required by design and stay bound to that snapshot to keep the live
process consistent with itself.

Initialization is handled directly in ``app.py`` via :class:`AsyncExitStack`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, TypeVar, cast

from fastapi import Depends, FastAPI, HTTPException, Request
from langgraph.types import Checkpointer
from sqlalchemy.ext.asyncio import AsyncSession

from app.automations.error_mapping import automation_http_exception
from app.automations.errors import AutomationNotFound, AutomationUnavailable
from app.private_work.context import PrivateWorkContext
from app.private_work.cutover import PrivateWorkCutoverGuard
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import (
    PrivateWorkError,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.projects.context import resolve_project_context
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound
from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.persistence.feedback import FeedbackRepository
from deerflow.runtime.events.store.base import RunEventStore
from deerflow.trace_context import generate_trace_id, get_current_trace_id

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.gateway.auth.local_provider import LocalAuthProvider
    from app.gateway.auth.repositories.sql import SQLUserRepository
    from deerflow.persistence.thread_meta.base import ThreadMetaStore


T = TypeVar("T")


def get_config() -> AppConfig:
    """Return the freshest ``AppConfig`` for the current request.

    Routes through :func:`deerflow.config.app_config.get_app_config`, which
    honours runtime ``ContextVar`` overrides and reloads ``config.yaml`` from
    disk when its mtime changes. ``AppConfig`` is not cached on ``app.state``
    at all — the only startup-time snapshot lives as a local
    ``startup_config`` variable inside ``lifespan()`` and is passed
    explicitly into :func:`gateway_platform_runtime` for the engines that are
    restart-required by design. Routing every request through
    :func:`get_app_config` closes the bytedance/deer-flow issue #3107 BUG-001
    split-brain where the worker / lead-agent thread saw a stale startup
    snapshot.

    Hot-reload boundary: fields backed by startup-time singletons
    (engines, sandbox provider, IM channels, logging handler) require a
    process restart to change at runtime. The authoritative list lives in
    :mod:`deerflow.config.reload_boundary` and is mirrored by the
    standardised ``"startup-only:"`` prefix on the matching
    ``Field(description=...)`` in :class:`AppConfig` — IDE hover on those
    fields will surface the boundary inline. See
    ``backend/CLAUDE.md`` "Config Hot-Reload Boundary" for the operator
    summary.

    Any failure to materialise the config (missing file, permission denied,
    YAML parse error, validation error) is reported as 503 — semantically
    "the gateway cannot serve requests without a usable configuration" — and
    logged with the original exception so operators have something to debug.
    """
    try:
        return get_app_config()
    except Exception as exc:  # noqa: BLE001 - request boundary: log and degrade gracefully
        logger.exception("Failed to load AppConfig at request time")
        raise HTTPException(status_code=503, detail="Configuration not available") from exc


async def project_session() -> AsyncIterator[AsyncSession]:
    """Yield the request-scoped project session or fail closed before routing."""
    from deerflow.persistence.engine import get_session_factory

    try:
        factory = get_session_factory()
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATABASE_UNAVAILABLE",
                "message": "Project storage unavailable",
                "request_id": get_current_trace_id() or generate_trace_id(),
            },
        ) from None
    async with factory() as session:
        yield session


@asynccontextmanager
async def gateway_platform_runtime(
    app: FastAPI,
    startup_config: AppConfig,
) -> AsyncGenerator[None, None]:
    """Bootstrap the project-scoped Gateway platform services.

    ``startup_config`` is the ``AppConfig`` snapshot taken once during
    ``lifespan()`` for one-shot infrastructure bootstrap. The engines and
    stores constructed here (persistence engine, checkpointer, and store) are
    restart-required by design — they hold live
    connections, file handles, or singleton providers — so they bind to this
    snapshot and survive across `config.yaml` edits. Request-time consumers
    must still go through :func:`get_config` for any field that should be
    hot-reloadable. See ``backend/CLAUDE.md`` "Config Hot-Reload Boundary".

    Usage in ``app.py``::

        async with gateway_platform_runtime(app, startup_config):
            yield
    """
    from deerflow.persistence.engine import (
        close_engine,
        get_session_factory,
        init_engine_from_config,
    )
    from deerflow.runtime import make_store
    from deerflow.runtime.checkpointer.async_provider import make_checkpointer

    async with AsyncExitStack() as stack:
        config = startup_config

        # Initialize and probe PostgreSQL before opening checkpointer/store pools.
        await init_engine_from_config(config.database)
        stack.push_async_callback(close_engine)

        app.state._raw_checkpointer = await stack.enter_async_context(make_checkpointer(config))
        app.state.store = await stack.enter_async_context(make_store(config))

        # Initialize repositories — one get_session_factory() call for all.
        sf = get_session_factory()
        from app.quotas.integration import ProjectQuotaEnforcer
        from app.quotas.service import QuotaService
        from app.reliability.owner_refs import AuditHmacKeyring
        from deerflow.config.quota_config import QuotaConfig

        audit_keyring = AuditHmacKeyring.from_environment()
        from app.audit.service import AuditService, _bind_gateway_audit_process
        from app.audit.sinks import OperationalAuditSink

        audit_service = AuditService(sf, audit_keyring)
        operational_audit_sink = OperationalAuditSink(
            audit_service,
            process_context=_bind_gateway_audit_process(audit_service),
        )
        from app.shared_assets.audit import (
            DurableSharedAssetGovernanceEventSink,
        )

        app.state.project_audit_service = audit_service
        app.state.operational_audit_sink = operational_audit_sink
        app.state.shared_asset_audit_sink = DurableSharedAssetGovernanceEventSink(audit_service)
        quota_config = getattr(config, "quotas", None) or QuotaConfig()
        quota_service = QuotaService(
            sf,
            quota_config,
            source_ref_hasher=audit_keyring,
        )
        project_quota_enforcer = ProjectQuotaEnforcer(quota_service)
        app.state.project_quota_service = quota_service
        app.state.project_quota_enforcer = project_quota_enforcer
        from app.private_work.checkpointer import ProjectScopedCheckpointer

        app.state.private_work_cutover_guard = PrivateWorkCutoverGuard(sf)
        app.state.project_scoped_checkpointer = ProjectScopedCheckpointer(
            app.state._raw_checkpointer,
            sf,
            quota=project_quota_enforcer,
        )
        from app.private_work.connection_service import ProjectConnectionService
        from app.private_work.file_service import PrivateFileService
        from app.private_work.file_streaming import PrivateFileStreamer
        from app.private_work.memory_service import PrivateMemoryService
        from app.private_work.run_admission import PrivateRunAdmissionService
        from app.private_work.run_service import PrivateRunService
        from app.private_work.thread_service import PrivateThreadService
        from deerflow.persistence.channel_connections import ChannelConnectionRepository

        app.state.private_file_service = PrivateFileService(
            sf,
            quota=project_quota_enforcer,
        )
        app.state.private_thread_service = PrivateThreadService(
            sf,
            app.state.project_scoped_checkpointer,
            branch_copy_hook=app.state.private_file_service,
        )
        app.state.private_run_admission_service = PrivateRunAdmissionService(
            sf,
            quota=project_quota_enforcer,
            audit=operational_audit_sink,
        )
        app.state.private_run_service = PrivateRunService(
            sf,
            quota=project_quota_enforcer,
            audit=operational_audit_sink,
        )
        app.state.private_file_streamer = PrivateFileStreamer(sf)
        app.state.project_memory_service = PrivateMemoryService(sf)
        app.state.channel_connection_repo = ChannelConnectionRepository(sf)
        app.state.project_connection_service = ProjectConnectionService(
            sf,
            repository=app.state.channel_connection_repo,
        )
        from deerflow.persistence.feedback import FeedbackRepository
        from deerflow.persistence.run import RunRepository

        app.state.run_store = RunRepository(sf)
        app.state.feedback_repo = FeedbackRepository(sf)

        from app.automations.dispatcher import AutomationDispatcher
        from app.automations.occurrences import AutomationOccurrenceService
        from app.automations.readiness import AutomationReadinessService
        from app.automations.service import ProjectAutomationService
        from deerflow.config.scheduler_config import SchedulerConfig

        scheduler_config = getattr(config, "scheduler", None)
        effective_scheduler_config = scheduler_config or SchedulerConfig()
        app.state.automation_service = ProjectAutomationService(
            sf,
            min_once_delay_seconds=effective_scheduler_config.min_once_delay_seconds,
            audit=operational_audit_sink,
        )
        # Gateway reports configured-but-external scheduling as stopped. M6
        # runtime health aggregation replaces this local projection in Task 13.
        app.state.automation_readiness_service = AutomationReadinessService()
        app.state.automation_occurrence_service = AutomationOccurrenceService(
            sf,
            max_concurrent_runs=effective_scheduler_config.max_concurrent_runs,
        )
        app.state.automation_dispatcher = AutomationDispatcher(
            sf,
            max_concurrent_runs=effective_scheduler_config.max_concurrent_runs,
            quota=project_quota_enforcer,
            audit=operational_audit_sink,
        )
        app.state.automation_scheduler_enabled = effective_scheduler_config.enabled

        from deerflow.runtime.events.store.db import DbRunEventStore

        app.state.private_run_event_store = DbRunEventStore(sf)
        from deerflow.runtime.events.stream import PostgresStreamBridge

        app.state.private_stream_bridge = PostgresStreamBridge(sf)
        yield


# ---------------------------------------------------------------------------
# Getters – called by routers per-request
# ---------------------------------------------------------------------------


def _require(attr: str, label: str) -> Callable[[Request], T]:
    """Create a FastAPI dependency that returns ``app.state.<attr>`` or 503."""

    def dep(request: Request) -> T:
        val = getattr(request.app.state, attr, None)
        if val is None:
            raise HTTPException(status_code=503, detail=f"{label} not available")
        return cast(T, val)

    dep.__name__ = dep.__qualname__ = f"get_{attr}"
    return dep


get_run_manager: Callable[[Request], object] = _require("run_manager", "Run manager")
get_private_run_event_store: Callable[[Request], RunEventStore] = _require(
    "private_run_event_store",
    "Private run event store",
)
get_feedback_repo: Callable[[Request], FeedbackRepository] = _require("feedback_repo", "Feedback")


def get_private_work_cutover_guard(request: Request) -> PrivateWorkCutoverGuard:
    value = getattr(request.app.state, "private_work_cutover_guard", None)
    if not isinstance(value, PrivateWorkCutoverGuard) and not hasattr(
        value,
        "require_legacy_open",
    ):
        raise HTTPException(status_code=503, detail="Private work cutover guard not available")
    return cast(PrivateWorkCutoverGuard, value)


def _automation_state_dependency(
    request: Request,
    attr: str,
):
    value = getattr(request.app.state, attr, None)
    if value is None:
        raise automation_http_exception(AutomationUnavailable(get_current_trace_id() or generate_trace_id()))
    return value


def _require_automation_audit_sink(request: Request):
    try:
        return get_operational_audit_sink(request)
    except HTTPException as error:
        if error.status_code != 503:
            raise
        raise automation_http_exception(AutomationUnavailable(get_current_trace_id() or generate_trace_id())) from None


def get_automation_service(request: Request):
    _require_automation_audit_sink(request)
    return _automation_state_dependency(request, "automation_service")


def get_automation_occurrence_service(request: Request):
    return _automation_state_dependency(request, "automation_occurrence_service")


def get_automation_dispatcher(request: Request):
    _require_automation_audit_sink(request)
    return _automation_state_dependency(request, "automation_dispatcher")


def get_automation_readiness_service(request: Request):
    return _automation_state_dependency(request, "automation_readiness_service")


def get_automation_scheduler_enabled(request: Request) -> bool:
    value = getattr(request.app.state, "automation_scheduler_enabled", None)
    if type(value) is not bool:
        raise automation_http_exception(AutomationUnavailable(get_current_trace_id() or generate_trace_id()))
    return value


def get_project_quota_enforcer(request: Request):
    value = getattr(request.app.state, "project_quota_enforcer", None)
    if value is None:
        raise HTTPException(status_code=503, detail="Project quota service not available")
    return value


def get_project_quota_service(request: Request):
    value = getattr(request.app.state, "project_quota_service", None)
    if value is None:
        raise HTTPException(status_code=503, detail="Project quota service not available")
    return value


def get_project_audit_service(request: Request):
    value = getattr(request.app.state, "project_audit_service", None)
    if value is None:
        raise HTTPException(status_code=503, detail="Project audit service not available")
    return value


def get_operational_audit_sink(request: Request):
    """Return the transaction-bound governance audit sink or fail closed."""

    value = getattr(request.app.state, "operational_audit_sink", None)
    if value is None:
        raise HTTPException(
            status_code=503,
            detail="Operational audit service not available",
        )
    return value


def get_checkpointer(request: Request) -> Checkpointer:
    """Return the legacy raw saver without exposing it to project modules."""

    raw = getattr(request.app.state, "_raw_checkpointer", None)
    if raw is None:
        # Compatibility for isolated legacy router tests and external FastAPI
        # embeddings that predate the private app-state name. Production
        # lifespan only installs ``_raw_checkpointer``.
        raw = getattr(request.app.state, "checkpointer", None)
    if raw is None:
        raise HTTPException(status_code=503, detail="Checkpointer not available")
    return cast(Checkpointer, raw)


def get_store(request: Request):
    """Return the global store (may be ``None`` if not configured)."""
    return getattr(request.app.state, "store", None)


def get_thread_store(request: Request) -> ThreadMetaStore:
    """Return the thread metadata store (SQL or memory-backed)."""
    val = getattr(request.app.state, "thread_store", None)
    if val is None:
        raise HTTPException(status_code=503, detail="Thread metadata store not available")
    return val


def get_project_checkpointer(request: Request, context: PrivateWorkContext):
    """Return a trusted-context view; project code cannot obtain the raw saver."""

    project_scoped_checkpointer = getattr(
        request.app.state,
        "project_scoped_checkpointer",
        None,
    )
    if project_scoped_checkpointer is None:
        raise HTTPException(status_code=503, detail="Project checkpointer not available")
    return project_scoped_checkpointer.for_context(context)


# ---------------------------------------------------------------------------
# Auth helpers (used by authz.py and auth middleware)
# ---------------------------------------------------------------------------

# Cached singletons to avoid repeated instantiation per request
_cached_local_provider: LocalAuthProvider | None = None
_cached_repo: SQLUserRepository | None = None


def get_local_provider() -> LocalAuthProvider:
    """Get or create the cached LocalAuthProvider singleton.

    Must be called after ``init_engine_from_config()`` — the shared
    session factory is required to construct the user repository.
    """
    global _cached_local_provider, _cached_repo
    if _cached_repo is None:
        from app.gateway.auth.repositories.sql import SQLUserRepository
        from deerflow.persistence.engine import get_session_factory

        sf = get_session_factory()
        _cached_repo = SQLUserRepository(sf)
    if _cached_local_provider is None:
        from app.gateway.auth.local_provider import LocalAuthProvider

        _cached_local_provider = LocalAuthProvider(repository=_cached_repo)
    return _cached_local_provider


async def get_current_user_from_request(request: Request):
    """Get the current authenticated user from the request cookie.

    Raises HTTPException 401 if not authenticated.
    """
    state = getattr(request, "state", None)
    state_user = getattr(state, "user", None)
    from app.gateway.auth_disabled import AUTH_SOURCE_AUTH_DISABLED, AUTH_SOURCE_INTERNAL, AUTH_SOURCE_SESSION

    if state_user is not None and getattr(state, "auth_source", None) in {
        AUTH_SOURCE_SESSION,
        AUTH_SOURCE_AUTH_DISABLED,
        AUTH_SOURCE_INTERNAL,
    }:
        return state_user

    from app.gateway.auth import decode_token
    from app.gateway.auth.errors import AuthErrorCode, AuthErrorResponse, TokenError, token_error_to_code

    access_token = request.cookies.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=AuthErrorCode.NOT_AUTHENTICATED, message="Not authenticated").model_dump(),
        )

    payload = decode_token(access_token)
    if isinstance(payload, TokenError):
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=token_error_to_code(payload), message=f"Token error: {payload.value}").model_dump(),
        )

    provider = get_local_provider()
    user = await provider.get_user(payload.sub)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=AuthErrorCode.USER_NOT_FOUND, message="User not found").model_dump(),
        )

    # Token version mismatch → password was changed, token is stale
    if user.token_version != payload.ver:
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=AuthErrorCode.TOKEN_INVALID, message="Token revoked (password changed)").model_dump(),
        )

    return user


async def private_work_context(
    project_id: uuid.UUID,
    user=Depends(get_current_user_from_request),
    session: AsyncSession = Depends(project_session),
) -> PrivateWorkContext:
    """Resolve the only HTTP-issued project-private authority context."""

    request_id = get_current_trace_id() or generate_trace_id()
    try:
        user_id = uuid.UUID(str(user.id))
    except (AttributeError, TypeError, ValueError):
        raise private_work_http_exception(PrivateWorkNotFound(request_id)) from None
    try:
        project = await resolve_project_context(
            session,
            user_id,
            project_id,
            request_id,
        )
        return PrivateWorkContext.from_project(project)
    except ProjectNotFound:
        raise private_work_http_exception(PrivateWorkNotFound(request_id)) from None
    except ProjectDatabaseUnavailable:
        raise private_work_http_exception(PrivateWorkUnavailable(request_id)) from None
    except PrivateWorkError as exc:
        raise private_work_http_exception(exc) from None


async def project_input_polish_context(
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateWorkContext:
    """Issue only current project authority allowed to call input polish."""

    from app.projects.capabilities import Capability

    for capability in (
        Capability.PRIVATE_WORK_CREATE,
        Capability.SHARED_ASSETS_EXECUTE,
    ):
        if capability not in context.capabilities:
            raise private_work_http_exception(PrivateWorkForbidden(context.request_id))
    return context


async def automation_context(
    project_id: uuid.UUID,
    user=Depends(get_current_user_from_request),
    session: AsyncSession = Depends(project_session),
) -> PrivateWorkContext:
    """Resolve project authority while exposing only Automation errors."""

    request_id = get_current_trace_id() or generate_trace_id()
    try:
        user_id = uuid.UUID(str(user.id))
    except (AttributeError, TypeError, ValueError):
        raise automation_http_exception(AutomationNotFound(request_id)) from None
    try:
        project = await resolve_project_context(
            session,
            user_id,
            project_id,
            request_id,
        )
        return PrivateWorkContext.from_project(project)
    except ProjectNotFound:
        raise automation_http_exception(AutomationNotFound(request_id)) from None
    except ProjectDatabaseUnavailable:
        raise automation_http_exception(AutomationUnavailable(request_id)) from None


async def require_project_automation_open(
    context: PrivateWorkContext = Depends(automation_context),
    session: AsyncSession = Depends(project_session),
) -> None:
    from app.final_schema import FinalSchemaProbe, FinalSchemaRequired, FinalSchemaUnavailable

    try:
        await FinalSchemaProbe().require_ready(session)
    except (FinalSchemaRequired, FinalSchemaUnavailable):
        raise automation_http_exception(AutomationUnavailable(context.request_id)) from None


async def require_legacy_private_open(
    request: Request,
) -> None:
    """Stop legacy routes after the global auth middleware has run."""

    try:
        await get_private_work_cutover_guard(request).require_legacy_open()
    except PrivateWorkError as exc:
        raise private_work_http_exception(exc) from None


async def require_project_private_open(
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> None:
    """Open project private routes only on the final cutover schema."""

    # Readiness is the operator-visible probe for an incomplete or unavailable
    # marker. It is read-only and must remain callable to explain why the
    # project data routes are closed.
    if request.url.path.endswith("/private-work/readiness"):
        return

    try:
        await get_private_work_cutover_guard(request).require_project_open()
    except PrivateWorkError as exc:
        raise private_work_http_exception(exc) from None


async def require_admin_user(request: Request, *, detail: str) -> None:
    """Require the authenticated caller to be an admin user.

    ``AuthMiddleware`` normally stamps ``request.state.user`` before the request
    reaches a router. Falling back to the strict dependency keeps the route safe
    in tests or alternative ASGI compositions that mount a router without the
    global middleware. ``detail`` is the route-specific 403 message.

    Centralising this here means a future change to the admin definition (e.g.
    allowing an internal system role, adding audit logging, or switching to a
    permission-based check) lands in one place instead of drifting across the
    per-router copies that previously existed in ``mcp``, ``channel_connections``
    and ``channels``.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        user = await get_current_user_from_request(request)

    if getattr(user, "system_role", None) != "system_admin":
        raise HTTPException(status_code=403, detail=detail)


async def get_optional_user_from_request(request: Request):
    """Get optional authenticated user from request.

    Returns None if not authenticated.
    """
    try:
        return await get_current_user_from_request(request)
    except HTTPException:
        return None


async def get_current_user(request: Request) -> str | None:
    """Extract user_id from request cookie, or None if not authenticated.

    Thin adapter that returns the string id for callers that only need
    identification (e.g., ``feedback.py``). Full-user callers should use
    ``get_current_user_from_request`` or ``get_optional_user_from_request``.
    """
    user = await get_optional_user_from_request(request)
    return str(user.id) if user else None
