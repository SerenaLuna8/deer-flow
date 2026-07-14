from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta

from email_validator import EmailNotValidError, validate_email

from app.private_work.retention import PrivateWorkRetentionService
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.errors import ProjectValidationFailed
from app.projects.invitation_models import (
    CreatedInvitation,
    InvitationClaim,
    InvitationView,
    ProjectInvitationInvalid,
    RedeemedInvitation,
)
from app.projects.invitation_repository import InvitationRepository
from app.projects.models import ProjectRole


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
    ):
        self.repository = repository
        self._retention = retention

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
        if invitation_role is ProjectRole.ADMIN:
            raise ProjectValidationFailed("invalid_invitation_role")
        invited_email = normalize_email(email)
        token = secrets.token_urlsafe(32)
        invitation = await self.repository.create(
            context,
            invited_email=invited_email,
            role=invitation_role,
            token_hash=hash_invitation_token(token),
            now=now,
            expires_at=now + timedelta(days=7),
        )
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

    async def revoke(
        self,
        context: ProjectContext,
        invitation_id: uuid.UUID,
        expected_version: int,
        now: datetime,
    ) -> InvitationView:
        context.require(Capability.PROJECT_MEMBERS_MANAGE)
        return await self.repository.revoke(
            context,
            invitation_id,
            expected_version=expected_version,
            now=now,
        )

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
            result = await self.repository.redeem_locked(
                project,
                invitation,
                user_id=user_id,
                now=now,
            )
            await self._retention.restore_owner(
                self.repository.session,
                project_id=project.id,
                owner_user_id=str(user_id),
                now=now,
            )
            return result

    @staticmethod
    def _require_redeemable(invitation, now: datetime) -> None:
        if invitation is None or invitation.status != "pending" or invitation.expires_at <= now:
            raise ProjectInvitationInvalid()
