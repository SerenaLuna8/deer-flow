from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response

from app.gateway.deps import private_work_context, require_project_private_open
from app.gateway.private_work_schemas import PrivateWorkRoute, StrictPrivateWorkRequest
from app.gateway.routers.channel_connections import (
    _PROVIDER_META,
    ChannelConnectionResponse,
    ChannelConnectionsResponse,
    ChannelConnectResponse,
    _connect_instruction,
    _connect_url,
    _ensure_runtime_channel_ready_if_available,
    _get_channel_connections_config,
    _get_channels_config,
    _provider_config,
    _provider_status,
)
from app.private_work.connection_service import ProjectConnectionService
from app.private_work.context import PrivateWorkContext
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import PrivateWorkError, PrivateWorkNotFound, PrivateWorkUnavailable
from deerflow.persistence.channel_connections import ChannelConnectionRepository
from deerflow.persistence.engine import get_session_factory

router = APIRouter(
    prefix="/api/projects/{project_id}/connections",
    tags=["project-connections"],
    route_class=PrivateWorkRoute,
    dependencies=[Depends(require_project_private_open)],
)


class ProjectConnectRequest(StrictPrivateWorkRequest):
    agent_asset_id: uuid.UUID
    agent_scope: Literal["project", "system"]
    redirect_after: str | None = None


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


async def _ready_provider(request: Request, provider: str, request_id: str):
    config = await _get_channel_connections_config(request)
    channels_config = await _get_channels_config(request)
    if not config.enabled:
        raise private_work_http_exception(PrivateWorkUnavailable(request_id))
    try:
        provider_config = _provider_config(config, provider)
    except Exception:
        raise private_work_http_exception(PrivateWorkNotFound(request_id)) from None
    if provider_config.enabled:
        await _ensure_runtime_channel_ready_if_available(provider, channels_config)
    status, unavailable_reason = _provider_status(config, channels_config, provider)
    if not status["enabled"] or not status["configured"] or unavailable_reason:
        raise private_work_http_exception(PrivateWorkUnavailable(request_id))
    return config


@router.get("", response_model=ChannelConnectionsResponse)
async def list_project_connections(
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ChannelConnectionsResponse:
    try:
        rows = await _service(request).list(context)
    except PrivateWorkError as exc:
        raise private_work_http_exception(exc) from None
    return ChannelConnectionsResponse(connections=[ChannelConnectionResponse(**row) for row in rows])


@router.post("/{provider}/connect", response_model=ChannelConnectResponse)
async def begin_project_connection(
    request: Request,
    provider: str,
    body: ProjectConnectRequest,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ChannelConnectResponse:
    config = await _ready_provider(request, provider, context.request_id)
    try:
        challenge = await _service(request).begin_connect(
            context,
            provider,
            body.agent_asset_id,
            body.agent_scope,
            body.redirect_after,
        )
    except PrivateWorkError as exc:
        raise private_work_http_exception(exc) from None
    now = datetime.now(UTC)
    expires_in = max(0, int((challenge.expires_at - now).total_seconds() + 0.999))
    return ChannelConnectResponse(
        provider=provider,
        mode=_PROVIDER_META[provider]["auth_mode"],
        url=_connect_url(config, provider, challenge.code),
        code=challenge.code,
        instruction=_connect_instruction(provider, challenge.code),
        expires_in=expires_in,
    )


@router.delete("/{connection_id}", status_code=204)
async def disconnect_project_connection(
    request: Request,
    connection_id: str,
    context: PrivateWorkContext = Depends(private_work_context),
) -> Response:
    try:
        await _service(request).disconnect(context, connection_id)
    except PrivateWorkError as exc:
        raise private_work_http_exception(exc) from None
    return Response(status_code=204)
