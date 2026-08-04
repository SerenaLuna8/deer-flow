from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from app.gateway.channel_schemas import (
    ProjectChannelInstanceConfigureRequest,
    ProjectChannelInstanceResponse,
    ProjectChannelInstancesResponse,
)
from app.gateway.routers.project_assets import project_asset_context
from app.project_channels.errors import (
    ChannelInstanceConflict,
    ChannelInstanceForbidden,
    ChannelInstanceIdentityConflict,
    ChannelInstanceNotFound,
    ChannelInstanceStorageUnavailable,
    ChannelInstanceValidationFailed,
    ProjectChannelError,
)
from app.project_channels.models import ConfigureProjectChannelInstance
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.errors import ProjectForbidden
from deerflow.persistence.engine import get_session_factory
from deerflow.trace_context import generate_trace_id, get_current_trace_id


class ProjectChannelInstanceRoute(APIRoute):
    """Return a stable validation envelope without echoing secret input."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                request_id = get_current_trace_id() or generate_trace_id()
                _raise_channel_error(
                    ChannelInstanceValidationFailed(
                        request_id,
                        "Channel configuration is invalid.",
                    )
                )

        return handler


router = APIRouter(
    prefix="/api/projects/{project_id}/channel-instances",
    tags=["project-channel-instances"],
    route_class=ProjectChannelInstanceRoute,
)


def get_project_channel_instance_service(request: Request):
    from app.project_channels.service import ProjectChannelInstanceService

    return ProjectChannelInstanceService(
        get_session_factory(),
        runtime_coordinator=getattr(
            request.app.state,
            "project_channel_runtime_coordinator",
            None,
        ),
        audit=getattr(request.app.state, "operational_audit_sink", None),
    )


def _raise_channel_error(exc: ProjectChannelError) -> None:
    status_codes = {
        ChannelInstanceNotFound: status.HTTP_404_NOT_FOUND,
        ChannelInstanceForbidden: status.HTTP_403_FORBIDDEN,
        ChannelInstanceConflict: status.HTTP_409_CONFLICT,
        ChannelInstanceIdentityConflict: status.HTTP_409_CONFLICT,
        ChannelInstanceValidationFailed: status.HTTP_422_UNPROCESSABLE_CONTENT,
        ChannelInstanceStorageUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    status_code = status_codes.get(type(exc))
    if status_code is None:
        raise exc
    detail: dict[str, object] = {
        "code": exc.code,
        "message": exc.message,
        "request_id": exc.request_id,
    }
    if isinstance(exc, ChannelInstanceValidationFailed) and exc.fields:
        detail["fields"] = list(exc.fields)
    raise HTTPException(status_code=status_code, detail=detail) from None


def _require_manage(context: ProjectContext) -> None:
    try:
        context.require(Capability.PROJECT_CHANNELS_MANAGE)
    except ProjectForbidden:
        _raise_channel_error(ChannelInstanceForbidden(context.request_id))


def _response(view: object) -> ProjectChannelInstanceResponse:
    return ProjectChannelInstanceResponse.model_validate(view, from_attributes=True)


@router.get("", response_model=ProjectChannelInstancesResponse)
async def list_project_channel_instances(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service=Depends(get_project_channel_instance_service),
) -> ProjectChannelInstancesResponse:
    try:
        rows = await service.list(context)
        return ProjectChannelInstancesResponse(instances=[_response(row) for row in rows])
    except ProjectChannelError as exc:
        _raise_channel_error(exc)


@router.put("/{provider}", response_model=ProjectChannelInstanceResponse)
async def configure_project_channel_instance(
    provider: str,
    body: ProjectChannelInstanceConfigureRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service=Depends(get_project_channel_instance_service),
) -> ProjectChannelInstanceResponse:
    _require_manage(context)
    command = ConfigureProjectChannelInstance(
        display_name=body.display_name,
        public_config=dict(body.public_config),
        credentials=dict(body.credentials),
        enabled=body.enabled,
    )
    try:
        return _response(await service.configure(context, provider, command))
    except ProjectChannelError as exc:
        _raise_channel_error(exc)


async def _set_enabled(
    provider: str,
    enabled: bool,
    context: ProjectContext,
    service,
) -> ProjectChannelInstanceResponse:
    _require_manage(context)
    try:
        return _response(await service.set_enabled(context, provider, enabled))
    except ProjectChannelError as exc:
        _raise_channel_error(exc)


@router.post("/{provider}/enable", response_model=ProjectChannelInstanceResponse)
async def enable_project_channel_instance(
    provider: str,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service=Depends(get_project_channel_instance_service),
) -> ProjectChannelInstanceResponse:
    return await _set_enabled(provider, True, context, service)


@router.post("/{provider}/disable", response_model=ProjectChannelInstanceResponse)
async def disable_project_channel_instance(
    provider: str,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service=Depends(get_project_channel_instance_service),
) -> ProjectChannelInstanceResponse:
    return await _set_enabled(provider, False, context, service)


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_channel_instance(
    provider: str,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service=Depends(get_project_channel_instance_service),
) -> Response:
    _require_manage(context)
    try:
        await service.delete(context, provider)
    except ProjectChannelError as exc:
        _raise_channel_error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
