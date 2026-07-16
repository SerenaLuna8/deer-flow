from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.deps import (
    get_current_user_from_request,
    get_project_quota_enforcer,
    project_session,
)
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context
from app.projects.errors import ProjectDatabaseUnavailable, ProjectForbidden, ProjectMemberQuotaExceeded, ProjectNotFound, ProjectSlugConflict, ProjectValidationFailed
from app.projects.models import CreateProject, ProjectChanges, ProjectPage, ProjectRole, ProjectView
from app.projects.repository import ProjectRepository
from app.projects.service import ProjectService
from deerflow.trace_context import generate_trace_id, get_current_trace_id


class ProjectRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(422, detail={"code": "PROJECT_VALIDATION_FAILED", "message": "Project validation failed"}) from None

        return handler


router = APIRouter(prefix="/api/projects", tags=["projects"], route_class=ProjectRoute)
DOMAIN_ERRORS = (ProjectNotFound, ProjectForbidden, ProjectSlugConflict, ProjectMemberQuotaExceeded, ProjectValidationFailed, ProjectDatabaseUnavailable)


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    icon: str = Field(default="folder", min_length=1, max_length=32)


class PatchProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    icon: str | None = Field(default=None, min_length=1, max_length=32)


class PinProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pinned: StrictBool


class ProjectResponse(BaseModel):
    id: uuid.UUID
    slug: str
    display_name: str
    description: str
    icon: str
    role: ProjectRole
    capabilities: list[Capability]
    is_pinned: bool
    last_entered_at: datetime | None
    member_count: int
    agent_count: int
    skill_count: int
    mcp_count: int
    status: str
    is_suspended: bool
    membership_version: int
    request_id: str
    deletion_effective_at: datetime | None = None


class ProjectPageResponse(BaseModel):
    items: list[ProjectResponse]
    next_cursor: str | None


def _response(view: ProjectView) -> ProjectResponse:
    data = vars(view).copy()
    data["capabilities"] = [capability for capability in Capability if capability in view.capabilities]
    return ProjectResponse(**data)


def _raise_domain(exc: Exception) -> None:
    mapping = {
        ProjectNotFound: (404, "PROJECT_NOT_FOUND"),
        ProjectForbidden: (403, "PROJECT_FORBIDDEN"),
        ProjectSlugConflict: (409, "PROJECT_SLUG_CONFLICT"),
        ProjectMemberQuotaExceeded: (429, "PROJECT_MEMBER_QUOTA_EXCEEDED"),
        ProjectValidationFailed: (422, "PROJECT_VALIDATION_FAILED"),
        ProjectDatabaseUnavailable: (503, "DATABASE_UNAVAILABLE"),
    }
    for error_type, (status_code, code) in mapping.items():
        if isinstance(exc, error_type):
            raise HTTPException(
                status_code,
                detail={"code": code, "message": str(exc)},
                headers={"Retry-After": "1"} if status_code == 429 else None,
            ) from None
    raise exc


async def authenticated_project_identity(user=Depends(get_current_user_from_request)) -> tuple[uuid.UUID, str]:
    return uuid.UUID(str(user.id)), get_current_trace_id() or generate_trace_id()


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(body: CreateProjectRequest, identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity), session: AsyncSession = Depends(project_session), quota=Depends(get_project_quota_enforcer)):
    user_id, request_id = identity
    service = ProjectService(ProjectRepository(session, quota=quota))
    try:
        context = await service.create(user_id, CreateProject(**body.model_dump()), request_id)
        return _response(await service.get(context))
    except DOMAIN_ERRORS as exc:
        _raise_domain(exc)


@router.get("", response_model=ProjectPageResponse)
async def list_projects(
    query: str | None = None,
    pinned: bool | None = None,
    cursor: str | None = None,
    limit: int = Query(20),
    include_recoverable: bool = False,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
):
    user_id, request_id = identity
    try:
        page: ProjectPage = await ProjectService(ProjectRepository(session)).list(
            user_id,
            query=query,
            pinned=pinned,
            cursor=cursor,
            limit=limit,
            include_recoverable=include_recoverable,
            request_id=request_id,
        )
        return ProjectPageResponse(items=[_response(item) for item in page.items], next_cursor=page.next_cursor)
    except DOMAIN_ERRORS as exc:
        _raise_domain(exc)


async def _context_service(project_id: uuid.UUID, identity: tuple[uuid.UUID, str], session: AsyncSession):
    user_id, request_id = identity
    context = await resolve_project_context(session, user_id, project_id, request_id)
    return context, ProjectService(ProjectRepository(session))


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: uuid.UUID, identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity), session: AsyncSession = Depends(project_session)):
    try:
        context, service = await _context_service(project_id, identity, session)
        return _response(await service.get(context))
    except DOMAIN_ERRORS as exc:
        _raise_domain(exc)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def patch_project(project_id: uuid.UUID, body: PatchProjectRequest, identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity), session: AsyncSession = Depends(project_session)):
    try:
        context, service = await _context_service(project_id, identity, session)
        return _response(await service.update(context, ProjectChanges(**body.model_dump())))
    except DOMAIN_ERRORS as exc:
        _raise_domain(exc)


@router.post("/{project_id}/enter", response_model=ProjectResponse)
async def enter_project(project_id: uuid.UUID, identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity), session: AsyncSession = Depends(project_session)):
    try:
        context, service = await _context_service(project_id, identity, session)
        return _response(await service.enter(context))
    except DOMAIN_ERRORS as exc:
        _raise_domain(exc)


@router.put("/{project_id}/pin", response_model=ProjectResponse)
async def pin_project(project_id: uuid.UUID, body: PinProjectRequest, identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity), session: AsyncSession = Depends(project_session)):
    try:
        context, service = await _context_service(project_id, identity, session)
        return _response(await service.pin(context, body.pinned))
    except DOMAIN_ERRORS as exc:
        _raise_domain(exc)
