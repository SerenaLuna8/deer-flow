from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.notifications.models import (
    InvitationNotificationView,
    NotificationCursor,
    NotificationPage,
    encode_notification_cursor,
)
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound
from app.projects.invitation_models import ProjectInvitationInvalid
from app.projects.models import ProjectRole
from deerflow.persistence.notifications import UserNotificationRow
from deerflow.persistence.projects.invitation_model import ProjectInvitationRow
from deerflow.persistence.projects.model import ProjectRow
from deerflow.persistence.user.model import UserRow


class NotificationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _base_query():
        inviter = aliased(UserRow)
        return (
            select(
                UserNotificationRow,
                ProjectInvitationRow,
                ProjectRow,
                inviter.email.label("inviter_email"),
            )
            .join(
                ProjectInvitationRow,
                ProjectInvitationRow.id == UserNotificationRow.project_invitation_id,
            )
            .join(ProjectRow, ProjectRow.id == ProjectInvitationRow.project_id)
            .join(inviter, inviter.id == ProjectInvitationRow.created_by_user_id)
        )

    @staticmethod
    def _view(row, now: datetime) -> InvitationNotificationView:
        invitation = row.ProjectInvitationRow
        status = invitation.status
        if status == "pending" and invitation.expires_at <= now:
            status = "expired"
        try:
            role = ProjectRole(invitation.role)
        except ValueError:
            raise ProjectInvitationInvalid() from None
        if status not in {"pending", "redeemed", "revoked", "expired"}:
            raise ProjectInvitationInvalid()
        return InvitationNotificationView(
            id=row.UserNotificationRow.id,
            project_id=row.ProjectRow.id,
            project_slug=row.ProjectRow.slug,
            project_display_name=row.ProjectRow.display_name,
            inviter_email=row.inviter_email,
            role=role,
            status=status,
            is_read=row.UserNotificationRow.read_at is not None,
            created_at=row.UserNotificationRow.created_at,
            expires_at=invitation.expires_at,
            version=invitation.version,
        )

    async def list_for_recipient(
        self,
        recipient_user_id: uuid.UUID,
        now: datetime,
        *,
        cursor: NotificationCursor | None = None,
        limit: int = 50,
    ) -> NotificationPage:
        try:
            async with self.session.begin():
                query = self._base_query().where(UserNotificationRow.recipient_user_id == str(recipient_user_id))
                if cursor is not None:
                    query = query.where(
                        or_(
                            UserNotificationRow.created_at < cursor.created_at,
                            and_(
                                UserNotificationRow.created_at == cursor.created_at,
                                UserNotificationRow.id < cursor.notification_id,
                            ),
                        )
                    )
                rows = (
                    await self.session.execute(
                        query.order_by(
                            UserNotificationRow.created_at.desc(),
                            UserNotificationRow.id.desc(),
                        ).limit(limit + 1)
                    )
                ).all()
                unread_count = (
                    await self.session.execute(
                        select(func.count())
                        .select_from(UserNotificationRow)
                        .where(
                            UserNotificationRow.recipient_user_id == str(recipient_user_id),
                            UserNotificationRow.read_at.is_(None),
                        )
                    )
                ).scalar_one()
                page_rows = rows[:limit]
                next_cursor = None
                if len(rows) > limit:
                    last = page_rows[-1].UserNotificationRow
                    next_cursor = encode_notification_cursor(
                        last.created_at,
                        last.id,
                    )
                return NotificationPage(
                    items=tuple(self._view(row, now) for row in page_rows),
                    unread_count=int(unread_count),
                    next_cursor=next_cursor,
                )
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    async def mark_read(
        self,
        recipient_user_id: uuid.UUID,
        notification_id: uuid.UUID,
        now: datetime,
    ) -> InvitationNotificationView:
        try:
            async with self.session.begin():
                notification = (
                    await self.session.execute(
                        select(UserNotificationRow)
                        .where(
                            UserNotificationRow.id == notification_id,
                            UserNotificationRow.recipient_user_id == str(recipient_user_id),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if notification is None:
                    raise ProjectNotFound()
                if notification.read_at is None:
                    notification.read_at = now
                    notification.version += 1
                    await self.session.flush()
                row = (
                    await self.session.execute(
                        self._base_query().where(
                            UserNotificationRow.id == notification_id,
                            UserNotificationRow.recipient_user_id == str(recipient_user_id),
                        )
                    )
                ).one()
                return self._view(row, now)
        except IntegrityError:
            raise ProjectInvitationInvalid() from None
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    async def mark_all_read(
        self,
        recipient_user_id: uuid.UUID,
        now: datetime,
    ) -> int:
        try:
            async with self.session.begin():
                result = await self.session.execute(
                    update(UserNotificationRow)
                    .where(
                        UserNotificationRow.recipient_user_id == str(recipient_user_id),
                        UserNotificationRow.read_at.is_(None),
                    )
                    .values(
                        read_at=now,
                        version=UserNotificationRow.version + 1,
                    )
                )
                return int(result.rowcount or 0)
        except IntegrityError:
            raise ProjectInvitationInvalid() from None
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    async def locate_project_for_accept(
        self,
        recipient_user_id: uuid.UUID,
        notification_id: uuid.UUID,
    ) -> uuid.UUID:
        project_id = (
            await self.session.execute(
                select(ProjectInvitationRow.project_id)
                .join(
                    UserNotificationRow,
                    UserNotificationRow.project_invitation_id == ProjectInvitationRow.id,
                )
                .where(
                    UserNotificationRow.id == notification_id,
                    UserNotificationRow.recipient_user_id == str(recipient_user_id),
                )
            )
        ).scalar_one_or_none()
        if project_id is None:
            raise ProjectNotFound()
        return project_id

    async def lock_invitation_for_accept(
        self,
        recipient_user_id: uuid.UUID,
        notification_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> ProjectInvitationRow:
        invitation = (
            await self.session.execute(
                select(ProjectInvitationRow)
                .join(
                    UserNotificationRow,
                    UserNotificationRow.project_invitation_id == ProjectInvitationRow.id,
                )
                .where(
                    UserNotificationRow.id == notification_id,
                    UserNotificationRow.recipient_user_id == str(recipient_user_id),
                    ProjectInvitationRow.project_id == project_id,
                )
                .with_for_update(of=ProjectInvitationRow)
            )
        ).scalar_one_or_none()
        if invitation is None:
            raise ProjectNotFound()
        return invitation

    async def mark_invitation_acted(
        self,
        recipient_user_id: uuid.UUID,
        invitation_id: uuid.UUID,
        now: datetime,
    ) -> None:
        notification = (
            await self.session.execute(
                select(UserNotificationRow)
                .where(
                    UserNotificationRow.recipient_user_id == str(recipient_user_id),
                    UserNotificationRow.project_invitation_id == invitation_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if notification is None:
            return
        notification.read_at = notification.read_at or now
        notification.acted_at = notification.acted_at or now
        notification.version += 1
        await self.session.flush()
