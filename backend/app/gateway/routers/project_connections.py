from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.exc import DBAPIError

from app.gateway.channel_schemas import (
    PROJECT_CONNECTION_PROVIDER_META,
    PROJECT_CONNECTION_RUNTIME_REQUIREMENTS,
    ProjectConnectionProviderResponse,
    ProjectConnectionProvidersResponse,
    ProjectConnectionResponse,
    ProjectConnectionsResponse,
    ProjectConnectRequest,
    ProjectConnectResponse,
    project_connect_instruction,
)
from app.gateway.deps import private_work_context, require_project_private_open
from app.gateway.private_work_schemas import PrivateWorkRoute
from app.private_work.connection_service import ProjectConnectionService
from app.private_work.context import PrivateWorkContext
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import (
    PrivateWorkError,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.projects.capabilities import Capability
from deerflow.config.app_config import get_app_config
from deerflow.config.channel_connections_config import ChannelConnectionsConfig
from deerflow.persistence.channel_connections import ChannelConnectionRepository
from deerflow.persistence.channel_connections.project_instance_repository import (
    ProjectChannelInstanceRepository,
)
from deerflow.persistence.engine import get_session_factory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ProjectProviderRuntime:
    instance_id: uuid.UUID
    provider: str
    public_config: dict[str, object]
    enabled: bool
    configured: bool
    running: bool
    observed_status: str


router = APIRouter(
    prefix="/api/projects/{project_id}/connections",
    tags=["project-connections"],
    route_class=PrivateWorkRoute,
    dependencies=[Depends(require_project_private_open)],
)


def _require_project_channel_management(context: PrivateWorkContext) -> None:
    if Capability.PROJECT_CHANNELS_MANAGE not in context.capabilities:
        raise private_work_http_exception(
            PrivateWorkForbidden(context.request_id),
        )


def _service(request: Request) -> ProjectConnectionService:
    service = getattr(request.app.state, "project_connection_service", None)
    if isinstance(service, ProjectConnectionService):
        return service
    session_factory = get_session_factory()
    repository = getattr(request.app.state, "channel_connection_repo", None)
    if not isinstance(repository, ChannelConnectionRepository):
        repository = ChannelConnectionRepository(session_factory)
        request.app.state.channel_connection_repo = repository
    service = ProjectConnectionService(session_factory, repository=repository)
    request.app.state.project_connection_service = service
    return service


async def _provider_config(request: Request) -> tuple[ChannelConnectionsConfig, dict[str, object]]:
    configured = getattr(request.app.state, "channel_connections_config", None)
    app_config = None
    if not isinstance(configured, ChannelConnectionsConfig):
        app_config = await asyncio.to_thread(get_app_config)
        configured = app_config.channel_connections
    request.app.state.channel_connections_config = configured

    channels = getattr(request.app.state, "channels_config", None)
    if not isinstance(channels, dict):
        app_config = app_config or await asyncio.to_thread(get_app_config)
        extra = app_config.model_extra or {}
        raw_channels = extra.get("channels")
        channels = dict(raw_channels) if isinstance(raw_channels, dict) else {}
        request.app.state.channels_config = channels
    return configured, channels


def _runtime_configured(provider: str, channels: dict[str, object]) -> bool:
    runtime = channels.get(provider)
    if not isinstance(runtime, dict) or runtime.get("enabled") is not True:
        return False
    return all(isinstance(runtime.get(key), str) and bool(runtime[key].strip()) for key in PROJECT_CONNECTION_RUNTIME_REQUIREMENTS[provider])


def _runtime_running(provider: str) -> bool | None:
    try:
        from app.channels.service import get_channel_service

        service = get_channel_service()
        if service is None:
            return None
        status = service.get_status()
    except Exception:
        logger.debug("Unable to read project channel provider health", exc_info=True)
        return None
    if status.get("service_running") is not True:
        return False
    provider_status = status.get("channels", {}).get(provider)
    return bool(provider_status.get("running")) if isinstance(provider_status, dict) else None


def _instance_runtime_running(instance_id: uuid.UUID) -> bool | None:
    try:
        from app.channels.service import get_channel_service

        service = get_channel_service()
        if service is None:
            return None
        state = service.get_channel_instance_status(str(instance_id))
        return bool(state.get("running")) if isinstance(state, dict) else None
    except Exception:
        return None


def _connection_status_for_runtime(
    connections: Iterable[Mapping[str, object]],
    *,
    provider: str,
    channel_instance_id: uuid.UUID | None,
) -> str:
    """Return status only for the exact runtime that owns the connection.

    A deleted/replaced project instance and the legacy deployment-configured
    provider can all leave rows with the same provider name.  Provider alone
    is therefore not an outbound authority coordinate.
    """

    expected_instance_id = str(channel_instance_id) if channel_instance_id is not None else None
    for row in connections:
        row_instance_id = row.get("channel_instance_id")
        normalized_row_instance_id = str(row_instance_id) if row_instance_id is not None else None
        if row.get("provider") == provider and normalized_row_instance_id == expected_instance_id and row.get("status") == "connected":
            return "connected"
    return "not_connected"


async def _project_provider_runtimes(
    context: PrivateWorkContext,
) -> dict[str, _ProjectProviderRuntime]:
    try:
        factory = get_session_factory()
        repository = ProjectChannelInstanceRepository()
        async with factory() as session, session.begin():
            rows = await repository.list_project_instances(session, context.project_id)
            result: dict[str, _ProjectProviderRuntime] = {}
            for row in rows:
                binding = await repository.get_credential_binding(
                    session,
                    row.id,
                    project_id=context.project_id,
                )
                local_running = _instance_runtime_running(row.id)
                result[row.provider] = _ProjectProviderRuntime(
                    instance_id=row.id,
                    provider=row.provider,
                    public_config=dict(row.public_config),
                    enabled=row.desired_status == "enabled",
                    configured=binding is not None,
                    running=(row.observed_status == "running" or local_running is True),
                    observed_status=row.observed_status,
                )
            return result
    except (DBAPIError, RuntimeError):
        raise PrivateWorkUnavailable(context.request_id) from None


async def _ensure_runtime_ready(provider: str, channels: dict[str, object]) -> bool | None:
    runtime = channels.get(provider)
    if not isinstance(runtime, dict) or runtime.get("enabled") is not True:
        return None
    try:
        from app.channels.service import get_channel_service

        service = get_channel_service()
        if service is None:
            return None
        ensure_ready = getattr(service, "ensure_channel_ready", None)
        if ensure_ready is None:
            return None
        return await ensure_ready(provider, runtime)
    except Exception:
        logger.exception("Failed to reconcile project channel provider health")
        return False


def _provider_state(
    config: ChannelConnectionsConfig,
    channels: dict[str, object],
    provider: str,
) -> tuple[bool, bool, str | None]:
    declared = config.provider_status(provider)
    enabled = bool(declared["enabled"])
    configured = bool(declared["configured"]) and _runtime_configured(provider, channels)
    reason = None
    if enabled and not configured:
        reason = f"{PROJECT_CONNECTION_PROVIDER_META[provider]['display_name']} provider is not configured."
    elif enabled and _runtime_running(provider) is False:
        reason = f"{PROJECT_CONNECTION_PROVIDER_META[provider]['display_name']} provider is unavailable."
    return enabled, configured, reason


async def _ready_provider(
    request: Request,
    provider: str,
    context: PrivateWorkContext,
) -> tuple[ChannelConnectionsConfig, uuid.UUID | None, dict[str, object]]:
    config, channels = await _provider_config(request)
    if provider not in PROJECT_CONNECTION_PROVIDER_META:
        raise private_work_http_exception(PrivateWorkNotFound(context.request_id))
    project_runtime = (await _project_provider_runtimes(context)).get(provider)
    if project_runtime is not None:
        if not project_runtime.enabled or not project_runtime.configured:
            raise private_work_http_exception(PrivateWorkUnavailable(context.request_id))
        coordinator = getattr(
            request.app.state,
            "project_channel_runtime_coordinator",
            None,
        )
        if coordinator is None:
            raise private_work_http_exception(PrivateWorkUnavailable(context.request_id))
        try:
            ready = await coordinator.reconcile(project_runtime.instance_id)
        except Exception:
            raise private_work_http_exception(PrivateWorkUnavailable(context.request_id)) from None
        if ready is False:
            raise private_work_http_exception(PrivateWorkUnavailable(context.request_id))
        refreshed = (await _project_provider_runtimes(context)).get(provider)
        if refreshed is None or not refreshed.running:
            raise private_work_http_exception(PrivateWorkUnavailable(context.request_id))
        return config, refreshed.instance_id, refreshed.public_config
    if not config.enabled:
        raise private_work_http_exception(PrivateWorkNotFound(context.request_id))
    enabled, configured, reason = _provider_state(config, channels, provider)
    if enabled:
        await _ensure_runtime_ready(provider, channels)
        enabled, configured, reason = _provider_state(config, channels, provider)
    if not enabled or not configured or reason is not None:
        raise private_work_http_exception(PrivateWorkUnavailable(context.request_id))
    return config, None, {}


def _connect_url(
    config: ChannelConnectionsConfig,
    provider: str,
    code: str,
    public_config: dict[str, object],
) -> str | None:
    if provider != "telegram":
        return None
    bot_username = str(public_config.get("bot_username") or getattr(config.telegram, "bot_username", "") or "").strip().lstrip("@")
    if not bot_username:
        return None
    return f"https://t.me/{bot_username}?start={code}"


@router.get("/providers", response_model=ProjectConnectionProvidersResponse)
async def list_project_connection_providers(
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectConnectionProvidersResponse:
    _require_project_channel_management(context)
    config, channels = await _provider_config(request)
    try:
        connections = await _service(request).list(context)
        project_runtimes = await _project_provider_runtimes(context)
    except PrivateWorkError as exc:
        raise private_work_http_exception(exc) from None
    if not config.enabled and not project_runtimes:
        return ProjectConnectionProvidersResponse(enabled=False, providers=[])
    providers: list[ProjectConnectionProviderResponse] = []
    for provider, metadata in PROJECT_CONNECTION_PROVIDER_META.items():
        project_runtime = project_runtimes.get(provider)
        if project_runtime is not None:
            enabled = project_runtime.enabled
            configured = project_runtime.configured
            reason = None
            if enabled and not configured:
                reason = f"{metadata['display_name']} provider is not configured."
            elif (
                enabled
                and getattr(
                    request.app.state,
                    "project_channel_runtime_coordinator",
                    None,
                )
                is None
            ):
                reason = f"{metadata['display_name']} provider is unavailable."
            elif enabled and not project_runtime.running:
                reason = f"{metadata['display_name']} provider could not connect. Check the project channel configuration."
        else:
            if not config.enabled:
                continue
            enabled, configured, reason = _provider_state(config, channels, provider)
        if not enabled:
            continue
        providers.append(
            ProjectConnectionProviderResponse(
                provider=provider,
                display_name=metadata["display_name"],
                enabled=enabled,
                configured=configured,
                connectable=configured and reason is None,
                unavailable_reason=reason,
                auth_mode=metadata["auth_mode"],
                connection_status=_connection_status_for_runtime(
                    connections,
                    provider=provider,
                    channel_instance_id=(project_runtime.instance_id if project_runtime is not None else None),
                ),
            )
        )
    return ProjectConnectionProvidersResponse(
        enabled=config.enabled or bool(project_runtimes),
        providers=providers,
    )


@router.get("", response_model=ProjectConnectionsResponse)
async def list_project_connections(
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectConnectionsResponse:
    _require_project_channel_management(context)
    try:
        rows = await _service(request).list(context)
    except PrivateWorkError as exc:
        raise private_work_http_exception(exc) from None
    return ProjectConnectionsResponse(
        connections=[
            ProjectConnectionResponse(
                id=str(row.get("id", "")),
                provider=str(row.get("provider", "")),
                status=str(row.get("status", "")),
                external_account_id=row.get("external_account_id"),
                external_account_name=row.get("external_account_name"),
                workspace_id=row.get("workspace_id"),
                workspace_name=row.get("workspace_name"),
                scopes=list(row.get("scopes") or []),
                metadata=dict(row.get("metadata") or {}),
            )
            for row in rows
        ]
    )


@router.post("/{provider}/connect", response_model=ProjectConnectResponse)
async def begin_project_connection(
    request: Request,
    provider: str,
    body: ProjectConnectRequest,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectConnectResponse:
    _require_project_channel_management(context)
    config, channel_instance_id, public_config = await _ready_provider(
        request,
        provider,
        context,
    )
    try:
        if channel_instance_id is None:
            challenge = await _service(request).begin_legacy_connect(
                context,
                provider,
                body.agent_asset_id,
                body.agent_scope,
                body.redirect_after,
            )
        else:
            challenge = await _service(request).begin_connect(
                context,
                provider,
                body.agent_asset_id,
                body.agent_scope,
                body.redirect_after,
                channel_instance_id=str(channel_instance_id),
            )
    except PrivateWorkError as exc:
        raise private_work_http_exception(exc) from None
    now = datetime.now(UTC)
    expires_in = max(0, int((challenge.expires_at - now).total_seconds() + 0.999))
    return ProjectConnectResponse(
        provider=provider,
        mode=PROJECT_CONNECTION_PROVIDER_META[provider]["auth_mode"],
        url=_connect_url(config, provider, challenge.code, public_config),
        code=challenge.code,
        instruction=project_connect_instruction(provider, challenge.code),
        expires_in=expires_in,
    )


@router.delete("/{connection_id}", status_code=204)
async def disconnect_project_connection(
    request: Request,
    connection_id: str,
    context: PrivateWorkContext = Depends(private_work_context),
) -> Response:
    _require_project_channel_management(context)
    try:
        await _service(request).disconnect(context, connection_id)
    except PrivateWorkError as exc:
        raise private_work_http_exception(exc) from None
    return Response(status_code=204)
