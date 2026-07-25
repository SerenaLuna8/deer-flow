from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Protocol

from sqlalchemy import exists, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.projects.context import ProjectContext
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound
from app.projects.invitation_models import (
    InvitationView,
    ProjectInvitationConflict,
    ProjectInvitationInvalid,
    RedeemedInvitation,
)
from app.projects.models import ProjectRole
from deerflow.persistence.notifications import UserNotificationRow
from deerflow.persistence.projects.invitation_model import ProjectInvitationRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.user.model import UserRow


class InvitationMutationAuditPort(Protocol):
    async def invitation_created(
        self,
        session: AsyncSession,
        context: ProjectContext,
        invitation_id: uuid.UUID,
        role: ProjectRole,
    ) -> None: ...

    async def invitation_revoked(
        self,
        session: AsyncSession,
        context: ProjectContext,
        invitation_id: uuid.UUID,
    ) -> None: ...


class InvitationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        try:
            async with self.session.begin():
                yield
        except IntegrityError:
            raise ProjectInvitationConflict() from None
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    def _actor_scope(self, context: ProjectContext):
        actor = aliased(ProjectMembershipRow)
        return exists(
            select(1).where(
                actor.id == context.membership_id,
                actor.project_id == context.project_id,
                actor.user_id == str(context.user_id),
                actor.status == "active",
                actor.version == context.membership_version,
            )
        )

    async def _require_actor(self, context: ProjectContext) -> None:
        if not (await self.session.execute(select(self._actor_scope(context)))).scalar_one():
            raise ProjectNotFound()

    async def create(
        self,
        context: ProjectContext,
        *,
        invited_email: str,
        role: ProjectRole,
        token_hash: str,
        now: datetime,
        expires_at: datetime,
        audit: InvitationMutationAuditPort | None = None,
    ) -> InvitationView:
        async with self.transaction():
            await self.lock_project(context.project_id, not_found=ProjectNotFound)
            await self._require_actor(context)
            pending = (
                await self.session.execute(
                    select(ProjectInvitationRow)
                    .where(
                        ProjectInvitationRow.project_id == context.project_id,
                        ProjectInvitationRow.invited_email == invited_email,
                        ProjectInvitationRow.status == "pending",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if pending is not None:
                if pending.expires_at > now:
                    raise ProjectInvitationConflict()
                pending.status = "expired"
                pending.version += 1
                await self.session.flush()

            invitation = ProjectInvitationRow(
                project_id=context.project_id,
                invited_email=invited_email,
                role=role.value,
                token_hash=token_hash,
                status="pending",
                expires_at=expires_at,
                version=1,
                created_by_user_id=str(context.user_id),
                created_at=now,
            )
            self.session.add(invitation)
            await self.session.flush()
            recipient_user_id = (await self.session.execute(select(UserRow.id).where(UserRow.email == invited_email))).scalar_one_or_none()
            if recipient_user_id is not None:
                self.session.add(
                    UserNotificationRow(
                        recipient_user_id=recipient_user_id,
                        kind="project_invitation",
                        project_invitation_id=invitation.id,
                        created_at=now,
                    )
                )
                await self.session.flush()
            view = self._view(invitation)
            if audit is not None:
                await audit.invitation_created(
                    self.session,
                    context,
                    invitation.id,
                    role,
                )
            return view

    async def get(self, invitation_id: uuid.UUID) -> ProjectInvitationRow | None:
        async with self.transaction():
            return (await self.session.execute(select(ProjectInvitationRow).where(ProjectInvitationRow.id == invitation_id))).scalar_one_or_none()

    async def get_by_token_hash(self, token_hash: str) -> ProjectInvitationRow | None:
        async with self.transaction():
            return (await self.session.execute(select(ProjectInvitationRow).where(ProjectInvitationRow.token_hash == token_hash))).scalar_one_or_none()

    async def list_for_project(
        self,
        context: ProjectContext,
    ) -> tuple[InvitationView, ...]:
        try:
            async with self.session.begin():
                await self._require_actor(context)
                project_exists = exists(
                    select(1).where(
                        ProjectRow.id == context.project_id,
                        ProjectRow.status == "active",
                        ProjectRow.is_suspended.is_(False),
                    )
                )
                rows = (
                    await self.session.execute(
                        select(ProjectInvitationRow)
                        .where(
                            ProjectInvitationRow.project_id == context.project_id,
                            project_exists,
                        )
                        .order_by(
                            ProjectInvitationRow.created_at.desc(),
                            ProjectInvitationRow.id.desc(),
                        )
                    )
                ).scalars()
                return tuple(self._view(row) for row in rows)
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    async def list_mine(
        self,
        invited_email: str,
        now: datetime,
    ) -> tuple[InvitationView, ...]:
        try:
            async with self.session.begin():
                rows = (
                    await self.session.execute(
                        select(ProjectInvitationRow)
                        .join(ProjectRow, ProjectRow.id == ProjectInvitationRow.project_id)
                        .where(
                            ProjectInvitationRow.invited_email == invited_email,
                            ProjectInvitationRow.status == "pending",
                            ProjectInvitationRow.expires_at > now,
                            ProjectRow.status == "active",
                            ProjectRow.is_suspended.is_(False),
                        )
                        .order_by(
                            ProjectInvitationRow.created_at.desc(),
                            ProjectInvitationRow.id.desc(),
                        )
                    )
                ).scalars()
                return tuple(self._view(row) for row in rows)
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    async def revoke(
        self,
        context: ProjectContext,
        invitation_id: uuid.UUID,
        *,
        expected_version: int,
        now: datetime,
        audit: InvitationMutationAuditPort | None = None,
    ) -> InvitationView:
        async with self.transaction():
            await self.lock_project(context.project_id, not_found=ProjectNotFound)
            await self._require_actor(context)
            invitation = (
                await self.session.execute(
                    select(ProjectInvitationRow)
                    .where(
                        ProjectInvitationRow.id == invitation_id,
                        ProjectInvitationRow.project_id == context.project_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if invitation is None:
                raise ProjectNotFound()
            if invitation.status != "pending" or invitation.expires_at <= now:
                raise ProjectInvitationInvalid()
            if invitation.version != expected_version:
                raise ProjectInvitationConflict()
            invitation.status = "revoked"
            invitation.revoked_at = now
            invitation.version += 1
            await self.session.flush()
            view = self._view(invitation)
            if audit is not None:
                await audit.invitation_revoked(
                    self.session,
                    context,
                    invitation.id,
                )
            return view

    async def locate_invitation_project(self, invitation_id: uuid.UUID, token_hash: str) -> uuid.UUID:
        project_id = (
            await self.session.execute(
                select(ProjectInvitationRow.project_id).where(
                    ProjectInvitationRow.id == invitation_id,
                    ProjectInvitationRow.token_hash == token_hash,
                )
            )
        ).scalar_one_or_none()
        if project_id is None:
            raise ProjectInvitationInvalid()
        return project_id

    async def lock_project(
        self,
        project_id: uuid.UUID,
        *,
        not_found: type[Exception] = ProjectInvitationInvalid,
    ) -> ProjectRow:
        project = (
            await self.session.execute(
                select(ProjectRow)
                .where(
                    ProjectRow.id == project_id,
                    ProjectRow.status == "active",
                    ProjectRow.is_suspended.is_(False),
                )
                .with_for_update(of=ProjectRow)
            )
        ).scalar_one_or_none()
        if project is None:
            raise not_found()
        return project

    async def lock_invitation(
        self,
        project_id: uuid.UUID,
        invitation_id: uuid.UUID,
        token_hash: str,
    ) -> ProjectInvitationRow:
        invitation = (
            await self.session.execute(
                select(ProjectInvitationRow)
                .where(
                    ProjectInvitationRow.project_id == project_id,
                    ProjectInvitationRow.id == invitation_id,
                    ProjectInvitationRow.token_hash == token_hash,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if invitation is None:
            raise ProjectInvitationInvalid()
        return invitation

    async def redeem_locked(
        self,
        project: ProjectRow,
        invitation: ProjectInvitationRow,
        *,
        user_id: uuid.UUID,
        now: datetime,
    ) -> RedeemedInvitation:
        membership = (
            await self.session.execute(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == project.id,
                    ProjectMembershipRow.user_id == str(user_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        try:
            role = ProjectRole(invitation.role)
        except ValueError:
            raise ProjectInvitationInvalid() from None

        if membership is None:
            membership = ProjectMembershipRow(
                id=uuid.uuid4(),
                project_id=project.id,
                user_id=str(user_id),
                role=role.value,
                status="active",
                version=1,
                created_at=now,
                updated_at=now,
            )
            self.session.add(membership)
        elif membership.status in {"left", "removed"}:
            membership.role = role.value
            membership.status = "active"
            membership.ended_at = None
            membership.retention_until = None
            membership.ended_by_user_id = None
            membership.end_reason = None
            membership.version += 1
            membership.activation_generation += 1
            membership.updated_at = now
        else:
            raise ProjectInvitationConflict()

        invitation.status = "redeemed"
        invitation.redeemed_by_user_id = str(user_id)
        invitation.redeemed_at = now
        invitation.version += 1
        project.membership_version += 1
        await self.session.flush()
        return RedeemedInvitation(
            invitation_id=invitation.id,
            project_id=project.id,
            project_slug=project.slug,
            membership_id=membership.id,
            role=role,
        )

    async def lock_membership(
        self,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ProjectMembershipRow:
        membership = (
            await self.session.execute(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == project_id,
                    ProjectMembershipRow.user_id == str(user_id),
                )
                .with_for_update(of=ProjectMembershipRow)
            )
        ).scalar_one_or_none()
        if membership is None:
            raise ProjectInvitationInvalid()
        return membership

    @staticmethod
    def _view(invitation: ProjectInvitationRow) -> InvitationView:
        try:
            role = ProjectRole(invitation.role)
        except ValueError:
            raise ProjectInvitationInvalid() from None
        if invitation.status not in {"pending", "redeemed", "revoked", "expired"}:
            raise ProjectInvitationInvalid()
        return InvitationView(
            id=invitation.id,
            project_id=invitation.project_id,
            invited_email=invitation.invited_email,
            role=role,
            status=invitation.status,
            expires_at=invitation.expires_at,
            version=invitation.version,
            created_at=invitation.created_at,
        )
