from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.deps import (
    get_operational_audit_sink,
    get_project_quota_enforcer,
    project_session,
)
from app.gateway.public_project_roles import PublicProjectRole
from app.gateway.routers.project_governance import (
    GOVERNANCE_DOMAIN_ERRORS,
    GovernanceRoute,
    raise_governance_error,
)
from app.gateway.routers.projects import authenticated_project_identity
from app.projects.context import resolve_project_context
from app.projects.membership_models import MembershipView
from app.projects.membership_repository import MembershipRepository
from app.projects.membership_service import MembershipService
from app.projects.models import ProjectRole

router = APIRouter(
    prefix="/api/projects",
    tags=["project-members"],
    route_class=GovernanceRoute,
)


class MembershipMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: PublicProjectRole
    version: int = Field(ge=1)


class MembershipVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class MembershipResponse(BaseModel):
    membership_id: uuid.UUID
    user_id: uuid.UUID
    account_email: str
    role: PublicProjectRole
    status: str
    version: int
    joined_at: datetime


def _response(view: MembershipView) -> MembershipResponse:
    return MembershipResponse(**vars(view))


async def _context(
    project_id: uuid.UUID,
    identity: tuple[uuid.UUID, str],
    session: AsyncSession,
):
    user_id, request_id = identity
    return await resolve_project_context(session, user_id, project_id, request_id)


@router.get("/{project_id}/members", response_model=list[MembershipResponse])
async def list_members(
    project_id: uuid.UUID,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
):
    try:
        context = await _context(project_id, identity, session)
        views = await MembershipService(MembershipRepository(session)).list_members(context)
        return [_response(view) for view in views]
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, identity[1])


@router.patch("/{project_id}/members/{membership_id}", response_model=MembershipResponse)
async def patch_member(
    project_id: uuid.UUID,
    membership_id: uuid.UUID,
    body: MembershipMutationRequest,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
    audit=Depends(get_operational_audit_sink),
):
    try:
        context = await _context(project_id, identity, session)
        view = await MembershipService(
            MembershipRepository(session),
            audit=audit,
        ).change_role(
            context,
            membership_id,
            ProjectRole(body.role),
            body.version,
        )
        return _response(view)
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, identity[1])


@router.delete("/{project_id}/members/{membership_id}", response_model=MembershipResponse)
async def remove_member(
    project_id: uuid.UUID,
    membership_id: uuid.UUID,
    body: MembershipVersionRequest,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
    quota=Depends(get_project_quota_enforcer),
    audit=Depends(get_operational_audit_sink),
):
    try:
        context = await _context(project_id, identity, session)
        view = await MembershipService(
            MembershipRepository(session),
            quota=quota,
            audit=audit,
        ).remove(
            context,
            membership_id,
            body.version,
        )
        return _response(view)
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, identity[1])


@router.post("/{project_id}/leave", response_model=MembershipResponse)
async def leave_project(
    project_id: uuid.UUID,
    body: MembershipVersionRequest,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
    quota=Depends(get_project_quota_enforcer),
    audit=Depends(get_operational_audit_sink),
):
    try:
        context = await _context(project_id, identity, session)
        view = await MembershipService(
            MembershipRepository(session),
            quota=quota,
            audit=audit,
        ).leave(
            context,
            body.version,
        )
        return _response(view)
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, identity[1])
