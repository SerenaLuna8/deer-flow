from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Protocol

from email_validator import EmailNotValidError, validate_email
from sqlalchemy.ext.asyncio import AsyncSession

from app.notifications.models import (
    InvitationNotificationView,
    NotificationPage,
    decode_notification_cursor,
)
from app.notifications.repository import NotificationRepository
from app.private_work.context import PrivateWorkContext
from app.private_work.retention import PrivateWorkRetentionService
from app.private_work.retention_jobs import RetentionJobAdmission
from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectValidationFailed
from app.projects.invitation_models import (
    CreatedInvitation,
    InvitationClaim,
    InvitationView,
    ProjectInvitationConflict,
    ProjectInvitationInvalid,
    RedeemedInvitation,
)
from app.projects.invitation_repository import (
    InvitationMutationAuditPort,
    InvitationRepository,
)
from app.projects.models import ProjectRole


class InvitationQuotaPort(Protocol):
    async def reserve_member(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        membership_id: uuid.UUID,
        activation_generation: int,
    ) -> None: ...


class InvitationAuditPort(InvitationMutationAuditPort, Protocol):
    async def invitation_redeemed_and_member_joined(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        invitation_id: uuid.UUID,
        membership_id: uuid.UUID,
        role: ProjectRole,
        request_id: str,
    ) -> None: ...


class _NoopInvitationQuota:
    async def reserve_member(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        membership_id: uuid.UUID,
        activation_generation: int,
    ) -> None:
        del session, context, membership_id, activation_generation


def hash_invitation_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    if not isinstance(email, str):
        raise ProjectValidationFailed("invalid_invitation_email")
    normalized = email.strip().lower()
    if not normalized or len(normalized) > 320:
        raise ProjectValidationFailed("invalid_invitation_email")
    try:
        validate_email(normalized, check_deliverability=False)
    except EmailNotValidError:
        raise ProjectValidationFailed("invalid_invitation_email") from None
    return normalized


class InvitationService:
    def __init__(
        self,
        repository: InvitationRepository,
        *,
        retention: object = PrivateWorkRetentionService,
        retention_jobs: object = RetentionJobAdmission,
        quota: InvitationQuotaPort | None = None,
        audit: InvitationAuditPort | None = None,
        notifications: NotificationRepository | None = None,
    ):
        self.repository = repository
        self._retention = retention
        self._retention_jobs = retention_jobs
        self._quota = quota or _NoopInvitationQuota()
        self._audit = audit
        self._notifications = notifications or NotificationRepository(repository.session)

    async def create(
        self,
        context: ProjectContext,
        email: str,
        role: ProjectRole,
        now: datetime,
    ) -> CreatedInvitation:
        context.require(Capability.PROJECT_MEMBERS_MANAGE)
        try:
            invitation_role = ProjectRole(role)
        except ValueError:
            raise ProjectValidationFailed("invalid_invitation_role") from None
        if invitation_role not in {
            ProjectRole.EDITOR,
            ProjectRole.RUNNER,
            ProjectRole.VIEWER,
        }:
            raise ProjectValidationFailed("invalid_invitation_role")
        invited_email = normalize_email(email)
        token = secrets.token_urlsafe(32)
        kwargs = {
            "invited_email": invited_email,
            "role": invitation_role,
            "token_hash": hash_invitation_token(token),
            "now": now,
            "expires_at": now + timedelta(days=7),
        }
        if self._audit is not None:
            kwargs["audit"] = self._audit
        invitation = await self.repository.create(context, **kwargs)
        return CreatedInvitation(invitation=invitation, token=token)

    async def list_for_project(
        self,
        context: ProjectContext,
    ) -> tuple[InvitationView, ...]:
        context.require(Capability.PROJECT_MEMBERS_MANAGE)
        return await self.repository.list_for_project(context)

    async def list_mine(
        self,
        user_email: str,
        now: datetime,
    ) -> tuple[InvitationView, ...]:
        invited_email = normalize_email(user_email)
        return await self.repository.list_mine(invited_email, now)

    async def list_notifications(
        self,
        user_id: uuid.UUID,
        now: datetime,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> NotificationPage:
        if limit < 1 or limit > 100:
            raise ProjectValidationFailed("invalid_notification_limit")
        decoded_cursor = None
        if cursor is not None:
            try:
                decoded_cursor = decode_notification_cursor(cursor)
            except ValueError:
                raise ProjectValidationFailed("invalid_notification_cursor") from None
        return await self._notifications.list_for_recipient(
            user_id,
            now,
            cursor=decoded_cursor,
            limit=limit,
        )

    async def mark_notification_read(
        self,
        user_id: uuid.UUID,
        notification_id: uuid.UUID,
        now: datetime,
    ) -> InvitationNotificationView:
        return await self._notifications.mark_read(
            user_id,
            notification_id,
            now,
        )

    async def mark_all_notifications_read(
        self,
        user_id: uuid.UUID,
        now: datetime,
    ) -> int:
        return await self._notifications.mark_all_read(user_id, now)

    async def revoke(
        self,
        context: ProjectContext,
        invitation_id: uuid.UUID,
        expected_version: int,
        now: datetime,
    ) -> InvitationView:
        context.require(Capability.PROJECT_MEMBERS_MANAGE)
        kwargs = {"expected_version": expected_version, "now": now}
        if self._audit is not None:
            kwargs["audit"] = self._audit
        return await self.repository.revoke(context, invitation_id, **kwargs)

    async def claim(self, token: str, now: datetime) -> InvitationClaim:
        if not isinstance(token, str) or not token or len(token) > 512:
            raise ProjectValidationFailed("invalid_invitation_token")
        token_hash = hash_invitation_token(token)
        invitation = await self.repository.get_by_token_hash(token_hash)
        self._require_redeemable(invitation, now)
        return InvitationClaim(invitation_id=invitation.id, token_hash=token_hash)

    async def redeem(
        self,
        user_id: uuid.UUID,
        user_email: str,
        claim: InvitationClaim,
        now: datetime,
        *,
        request_id: str = "invitation-redeem",
    ) -> RedeemedInvitation:
        try:
            normalized_email = normalize_email(user_email)
        except ProjectValidationFailed:
            raise ProjectInvitationInvalid() from None
        async with self.repository.transaction():
            project_id = await self.repository.locate_invitation_project(
                claim.invitation_id,
                claim.token_hash,
            )
            project = await self.repository.lock_project(project_id)
            invitation = await self.repository.lock_invitation(
                project_id,
                claim.invitation_id,
                claim.token_hash,
            )
            self._require_redeemable(invitation, now)
            if invitation.invited_email != normalized_email:
                raise ProjectInvitationInvalid()
            return await self._redeem_locked(
                project,
                invitation,
                user_id=user_id,
                now=now,
                request_id=request_id,
            )

    async def accept_notification(
        self,
        user_id: uuid.UUID,
        notification_id: uuid.UUID,
        *,
        expected_version: int,
        now: datetime,
        request_id: str = "notification-accept",
    ) -> RedeemedInvitation:
        async with self.repository.transaction():
            project_id = await self._notifications.locate_project_for_accept(
                user_id,
                notification_id,
            )
            project = await self.repository.lock_project(project_id)
            invitation = await self._notifications.lock_invitation_for_accept(
                user_id,
                notification_id,
                project_id,
            )
            self._require_redeemable(invitation, now)
            if invitation.version != expected_version:
                raise ProjectInvitationConflict()
            return await self._redeem_locked(
                project,
                invitation,
                user_id=user_id,
                now=now,
                request_id=request_id,
            )

    async def _redeem_locked(
        self,
        project,
        invitation,
        *,
        user_id: uuid.UUID,
        now: datetime,
        request_id: str,
    ) -> RedeemedInvitation:
        result = await self.repository.redeem_locked(
            project,
            invitation,
            user_id=user_id,
            now=now,
        )
        membership = await self.repository.lock_membership(
            project.id,
            user_id,
        )
        issued_context = PrivateWorkContext.from_project(
            ProjectContext(
                user_id=user_id,
                project_id=project.id,
                membership_id=membership.id,
                role=result.role,
                capabilities=capabilities_for(result.role),
                membership_version=membership.version,
                request_id=request_id,
            )
        )
        await self._quota.reserve_member(
            self.repository.session,
            issued_context,
            membership_id=membership.id,
            activation_generation=membership.activation_generation,
        )
        await self._retention.restore_owner(
            self.repository.session,
            project_id=project.id,
            owner_user_id=str(user_id),
            now=now,
        )
        await self._retention_jobs.cancel_former_owner(
            self.repository.session,
            project_id=project.id,
            owner_user_id=str(user_id),
            now=now,
        )
        await self._notifications.mark_invitation_acted(
            user_id,
            result.invitation_id,
            now,
        )
        if self._audit is not None:
            await self._audit.invitation_redeemed_and_member_joined(
                self.repository.session,
                user_id=user_id,
                project_id=project.id,
                invitation_id=result.invitation_id,
                membership_id=result.membership_id,
                role=result.role,
                request_id=request_id,
            )
        return result

    @staticmethod
    def _require_redeemable(invitation, now: datetime) -> None:
        if invitation is None or invitation.status != "pending" or invitation.expires_at <= now:
            raise ProjectInvitationInvalid()
