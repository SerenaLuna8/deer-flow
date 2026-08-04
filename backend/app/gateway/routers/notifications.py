from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.deps import (
    get_operational_audit_sink,
    get_project_quota_enforcer,
    project_session,
)
from app.gateway.public_project_roles import PublicInvitationRole
from app.gateway.routers.project_governance import (
    GOVERNANCE_DOMAIN_ERRORS,
    GovernanceRoute,
    raise_governance_error,
)
from app.gateway.routers.project_invitations import (
    RedeemedInvitationResponse,
    authenticated_invitation_identity,
)
from app.notifications.models import InvitationNotificationView, NotificationPage
from app.projects.invitation_repository import InvitationRepository
from app.projects.invitation_service import InvitationService

router = APIRouter(tags=["notifications"], route_class=GovernanceRoute)


class NotificationProjectResponse(BaseModel):
    id: uuid.UUID
    slug: str
    display_name: str


class NotificationActorResponse(BaseModel):
    email: str


class NotificationResponse(BaseModel):
    id: uuid.UUID
    kind: Literal["project_invitation"] = "project_invitation"
    project: NotificationProjectResponse
    actor: NotificationActorResponse
    role: PublicInvitationRole
    status: Literal["pending", "redeemed", "revoked", "expired"]
    is_read: bool
    created_at: datetime
    expires_at: datetime
    version: int


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    next_cursor: str | None = None
    unread_count: int


class NotificationAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class NotificationsMarkedResponse(BaseModel):
    marked_count: int = Field(ge=0)


def _notification_response(view: InvitationNotificationView) -> NotificationResponse:
    return NotificationResponse(
        id=view.id,
        project=NotificationProjectResponse(
            id=view.project_id,
            slug=view.project_slug,
            display_name=view.project_display_name,
        ),
        actor=NotificationActorResponse(email=view.inviter_email),
        role=view.role,
        status=view.status,
        is_read=view.is_read,
        created_at=view.created_at,
        expires_at=view.expires_at,
        version=view.version,
    )


def _page_response(page: NotificationPage) -> NotificationListResponse:
    return NotificationListResponse(
        items=[_notification_response(item) for item in page.items],
        next_cursor=page.next_cursor,
        unread_count=page.unread_count,
    )


@router.get("/api/notifications", response_model=NotificationListResponse)
async def list_notifications(
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
    identity: tuple[uuid.UUID, str, str] = Depends(authenticated_invitation_identity),
    session: AsyncSession = Depends(project_session),
):
    user_id, _, request_id = identity
    try:
        page = await InvitationService(InvitationRepository(session)).list_notifications(
            user_id,
            datetime.now(UTC),
            cursor=cursor,
            limit=limit,
        )
        return _page_response(page)
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, request_id)


@router.post(
    "/api/notifications/read-all",
    response_model=NotificationsMarkedResponse,
)
async def mark_all_notifications_read(
    identity: tuple[uuid.UUID, str, str] = Depends(authenticated_invitation_identity),
    session: AsyncSession = Depends(project_session),
):
    user_id, _, request_id = identity
    try:
        marked_count = await InvitationService(InvitationRepository(session)).mark_all_notifications_read(
            user_id,
            datetime.now(UTC),
        )
        return NotificationsMarkedResponse(marked_count=marked_count)
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, request_id)


@router.post(
    "/api/notifications/{notification_id}/read",
    response_model=NotificationResponse,
)
async def mark_notification_read(
    notification_id: uuid.UUID,
    identity: tuple[uuid.UUID, str, str] = Depends(authenticated_invitation_identity),
    session: AsyncSession = Depends(project_session),
):
    user_id, _, request_id = identity
    try:
        notification = await InvitationService(InvitationRepository(session)).mark_notification_read(
            user_id,
            notification_id,
            datetime.now(UTC),
        )
        return _notification_response(notification)
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, request_id)


@router.post(
    "/api/notifications/{notification_id}/accept",
    response_model=RedeemedInvitationResponse,
)
async def accept_notification(
    notification_id: uuid.UUID,
    body: NotificationAcceptRequest,
    identity: tuple[uuid.UUID, str, str] = Depends(authenticated_invitation_identity),
    session: AsyncSession = Depends(project_session),
    quota=Depends(get_project_quota_enforcer),
    audit=Depends(get_operational_audit_sink),
):
    user_id, _, request_id = identity
    try:
        redeemed = await InvitationService(
            InvitationRepository(session),
            quota=quota,
            audit=audit,
        ).accept_notification(
            user_id,
            notification_id,
            expected_version=body.version,
            now=datetime.now(UTC),
            request_id=request_id,
        )
        return RedeemedInvitationResponse(**vars(redeemed))
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, request_id)
