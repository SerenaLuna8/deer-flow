from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from app.channel_group_bindings.errors import (
    GroupBindingAgentUnavailable,
    GroupBindingConflict,
    GroupBindingForbidden,
    GroupBindingInvalid,
    GroupBindingNotFound,
    GroupBindingUnavailable,
    ProjectChannelGroupBindingError,
)
from app.channel_group_bindings.models import (
    CreateGroupBindingChallenge,
    UpdateGroupBinding,
)
from app.gateway.routers.project_asset_routes.common import project_asset_context
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from deerflow.persistence.engine import get_session_factory
from deerflow.trace_context import generate_trace_id, get_current_trace_id


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateGroupBindingChallengeRequest(_StrictRequest):
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    agent_asset_id: uuid.UUID
    agent_scope: Literal["project", "system"]


class UpdateGroupBindingRequest(_StrictRequest):
    expected_revision: StrictInt = Field(ge=1, le=9_223_372_036_854_775_807)
    enabled: StrictBool | None = None
    agent_asset_id: uuid.UUID | None = None
    agent_scope: Literal["project", "system"] | None = None

    @model_validator(mode="after")
    def validate_mutation(self) -> UpdateGroupBindingRequest:
        if self.enabled is None and self.agent_asset_id is None and self.agent_scope is None:
            raise ValueError("at least one group binding change is required")
        if (self.agent_asset_id is None) != (self.agent_scope is None):
            raise ValueError("agent_asset_id and agent_scope must be provided together")
        return self


class GroupBindingChallengeResponse(_StrictResponse):
    provider: str
    code: str
    command: str
    expires_at: datetime
    expires_in: int


class ProjectChannelGroupBindingResponse(_StrictResponse):
    id: uuid.UUID
    provider: str
    display_name: str
    status: Literal["active", "disabled"]
    agent_asset_id: uuid.UUID
    agent_scope: Literal["project", "system"]
    last_activity_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class ProjectChannelGroupBindingsResponse(_StrictResponse):
    bindings: list[ProjectChannelGroupBindingResponse]


class ProjectChannelGroupBindingRoute(APIRoute):
    """Collapse validation errors without reflecting provider identifiers."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                request_id = get_current_trace_id() or generate_trace_id()
                _raise_group_binding_error(GroupBindingInvalid(request_id))

        return handler


router = APIRouter(
    prefix="/api/projects/{project_id}/channel-group-bindings",
    tags=["project-channel-group-bindings"],
    route_class=ProjectChannelGroupBindingRoute,
)


def get_project_channel_group_binding_service(request: Request):
    existing = getattr(
        request.app.state,
        "project_channel_group_binding_service",
        None,
    )
    if existing is not None:
        return existing
    from app.channel_group_bindings.service import (
        ProjectChannelGroupBindingService,
    )

    return ProjectChannelGroupBindingService(get_session_factory())


def _raise_group_binding_error(exc: ProjectChannelGroupBindingError) -> None:
    status_codes = {
        GroupBindingAgentUnavailable: status.HTTP_409_CONFLICT,
        GroupBindingNotFound: status.HTTP_404_NOT_FOUND,
        GroupBindingForbidden: status.HTTP_403_FORBIDDEN,
        GroupBindingConflict: status.HTTP_409_CONFLICT,
        GroupBindingInvalid: status.HTTP_422_UNPROCESSABLE_CONTENT,
        GroupBindingUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    status_code = status_codes.get(type(exc))
    if status_code is None:
        raise exc
    detail: dict[str, object] = {
        "code": exc.code,
        "message": exc.message,
        "request_id": exc.request_id,
    }
    if isinstance(exc, GroupBindingInvalid) and exc.fields:
        detail["fields"] = list(exc.fields)
    raise HTTPException(status_code=status_code, detail=detail) from None


def _require_manage(context: ProjectContext) -> None:
    if Capability.PROJECT_CHANNELS_MANAGE not in context.capabilities:
        _raise_group_binding_error(GroupBindingForbidden(context.request_id))


def _binding_response(view: object) -> ProjectChannelGroupBindingResponse:
    return ProjectChannelGroupBindingResponse.model_validate(view, from_attributes=True)


@router.get("", response_model=ProjectChannelGroupBindingsResponse)
async def list_project_channel_group_bindings(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service=Depends(get_project_channel_group_binding_service),
) -> ProjectChannelGroupBindingsResponse:
    _require_manage(context)
    try:
        rows = await service.list(context)
        return ProjectChannelGroupBindingsResponse(bindings=[_binding_response(row) for row in rows])
    except ProjectChannelGroupBindingError as exc:
        _raise_group_binding_error(exc)


@router.post(
    "/challenge",
    response_model=GroupBindingChallengeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_channel_group_binding_challenge(
    body: CreateGroupBindingChallengeRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service=Depends(get_project_channel_group_binding_service),
) -> GroupBindingChallengeResponse:
    _require_manage(context)
    try:
        challenge = await service.create_challenge(
            context,
            CreateGroupBindingChallenge(
                provider=body.provider,
                agent_asset_id=body.agent_asset_id,
                agent_scope=body.agent_scope,
            ),
        )
        return GroupBindingChallengeResponse.model_validate(
            challenge,
            from_attributes=True,
        )
    except ProjectChannelGroupBindingError as exc:
        _raise_group_binding_error(exc)


@router.patch(
    "/{binding_id}",
    response_model=ProjectChannelGroupBindingResponse,
)
async def update_project_channel_group_binding(
    binding_id: uuid.UUID,
    body: UpdateGroupBindingRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service=Depends(get_project_channel_group_binding_service),
) -> ProjectChannelGroupBindingResponse:
    _require_manage(context)
    try:
        row = await service.update(
            context,
            binding_id,
            UpdateGroupBinding(
                expected_revision=body.expected_revision,
                enabled=body.enabled,
                agent_asset_id=body.agent_asset_id,
                agent_scope=body.agent_scope,
            ),
        )
        return _binding_response(row)
    except ProjectChannelGroupBindingError as exc:
        _raise_group_binding_error(exc)


@router.delete("/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_channel_group_binding(
    binding_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    expected_revision: Annotated[
        int,
        Query(ge=1, le=9_223_372_036_854_775_807),
    ],
    service=Depends(get_project_channel_group_binding_service),
) -> Response:
    _require_manage(context)
    try:
        await service.delete(
            context,
            binding_id,
            expected_revision=expected_revision,
        )
    except ProjectChannelGroupBindingError as exc:
        _raise_group_binding_error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
