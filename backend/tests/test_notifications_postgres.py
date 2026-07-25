from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.notifications.repository import NotificationRepository
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectNotFound
from app.projects.invitation_models import (
    ProjectInvitationConflict,
    ProjectInvitationInvalid,
    RedeemedInvitation,
)
from app.projects.invitation_repository import InvitationRepository
from app.projects.invitation_service import InvitationService
from app.projects.models import ProjectRole
from deerflow.persistence.notifications import UserNotificationRow
from deerflow.persistence.projects.invitation_model import ProjectInvitationRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.user.model import UserRow


@pytest.mark.asyncio
async def test_registered_invitee_gets_recipient_scoped_persistent_notification_and_can_accept(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = uuid.uuid4()
    recipient_id = uuid.uuid4()
    outsider_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    now = datetime.now(UTC)
    try:
        async with sessions.begin() as session:
            session.add_all(
                [
                    UserRow(
                        id=str(owner_id),
                        email="owner@example.com",
                        system_role="user",
                        needs_setup=False,
                        token_version=0,
                    ),
                    UserRow(
                        id=str(recipient_id),
                        email="member@example.com",
                        system_role="user",
                        needs_setup=False,
                        token_version=0,
                    ),
                    UserRow(
                        id=str(outsider_id),
                        email="outsider@example.com",
                        system_role="user",
                        needs_setup=False,
                        token_version=0,
                    ),
                ]
            )
            await session.flush()
            session.add(
                ProjectRow(
                    id=project_id,
                    slug="notification-project",
                    display_name="Notification Project",
                    created_by_user_id=str(owner_id),
                )
            )
            await session.flush()
            session.add(
                ProjectMembershipRow(
                    id=membership_id,
                    project_id=project_id,
                    user_id=str(owner_id),
                    role="admin",
                )
            )

        context = ProjectContext(
            user_id=owner_id,
            project_id=project_id,
            membership_id=membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="notification-postgres",
        )
        async with sessions() as session:
            service = InvitationService(InvitationRepository(session))
            first = await service.create(
                context,
                " MEMBER@example.com ",
                ProjectRole.VIEWER,
                now - timedelta(minutes=3),
            )
            await service.revoke(
                context,
                first.invitation.id,
                first.invitation.version,
                now - timedelta(minutes=2, seconds=30),
            )
            second = await service.create(
                context,
                "member@example.com",
                ProjectRole.VIEWER,
                now - timedelta(minutes=2),
            )
            await service.revoke(
                context,
                second.invitation.id,
                second.invitation.version,
                now - timedelta(minutes=1, seconds=30),
            )
            created = await service.create(
                context,
                "member@example.com",
                ProjectRole.VIEWER,
                now - timedelta(minutes=1),
            )
            await service.create(
                context,
                "unregistered@example.com",
                ProjectRole.VIEWER,
                now,
            )
        tied_created_at = now - timedelta(minutes=4)
        async with sessions.begin() as session:
            await session.execute(update(UserNotificationRow).where(UserNotificationRow.recipient_user_id == str(recipient_id)).values(created_at=tied_created_at))

        async with sessions() as session:
            recipient_page = await InvitationService(InvitationRepository(session)).list_notifications(
                recipient_id,
                now,
                limit=2,
            )
        assert len(recipient_page.items) == 2
        assert recipient_page.unread_count == 3
        assert recipient_page.next_cursor is not None
        async with sessions() as session:
            recipient_page_2 = await InvitationService(InvitationRepository(session)).list_notifications(
                recipient_id,
                now,
                cursor=recipient_page.next_cursor,
                limit=2,
            )
        assert len(recipient_page_2.items) == 1
        assert recipient_page_2.unread_count == 3
        assert recipient_page_2.next_cursor is None
        paged_ids = [item.id for item in recipient_page.items + recipient_page_2.items]
        assert len(paged_ids) == len(set(paged_ids)) == 3
        assert paged_ids == sorted(paged_ids, reverse=True)
        notification = next(item for item in recipient_page.items + recipient_page_2.items if item.status == "pending")
        assert notification.project_id == project_id
        assert notification.inviter_email == "owner@example.com"
        assert notification.status == "pending"
        assert notification.version == created.invitation.version

        async with sessions() as session:
            outsider_page = await NotificationRepository(session).list_for_recipient(outsider_id, now)
            with pytest.raises(ProjectNotFound):
                await NotificationRepository(session).mark_read(
                    outsider_id,
                    notification.id,
                    now,
                )
            with pytest.raises(ProjectNotFound):
                await InvitationService(InvitationRepository(session)).accept_notification(
                    outsider_id,
                    notification.id,
                    expected_version=notification.version,
                    now=now,
                )
        assert outsider_page.items == ()
        assert outsider_page.unread_count == 0

        failing_quota = AsyncMock()
        failing_quota.reserve_member.side_effect = RuntimeError("quota failure")
        async with sessions() as session:
            with pytest.raises(RuntimeError, match="quota failure"):
                await InvitationService(
                    InvitationRepository(session),
                    quota=failing_quota,
                ).accept_notification(
                    recipient_id,
                    notification.id,
                    expected_version=notification.version,
                    now=now,
                    request_id="notification-rollback-postgres",
                )
        async with sessions() as session:
            rolled_back_invitation = await session.get(
                ProjectInvitationRow,
                created.invitation.id,
            )
            rolled_back_notification = await session.get(
                UserNotificationRow,
                notification.id,
            )
            rolled_back_membership = (
                await session.execute(
                    select(ProjectMembershipRow).where(
                        ProjectMembershipRow.project_id == project_id,
                        ProjectMembershipRow.user_id == str(recipient_id),
                    )
                )
            ).scalar_one_or_none()
        assert rolled_back_invitation is not None
        assert rolled_back_invitation.status == "pending"
        assert rolled_back_notification is not None
        assert rolled_back_notification.read_at is None
        assert rolled_back_notification.acted_at is None
        assert rolled_back_membership is None

        async with sessions() as session:
            outsider_marked = await NotificationRepository(session).mark_all_read(outsider_id, now)
        assert outsider_marked == 0
        async with sessions() as session:
            still_unread = await NotificationRepository(session).list_for_recipient(recipient_id, now)
        assert still_unread.unread_count == 3
        async with sessions() as session:
            marked_count = await NotificationRepository(session).mark_all_read(
                recipient_id,
                now,
            )
        assert marked_count == 3
        async with sessions() as session:
            marked_again = await NotificationRepository(session).mark_all_read(
                recipient_id,
                now,
            )
        assert marked_again == 0

        async with sessions() as session:
            redeemed = await InvitationService(InvitationRepository(session)).accept_notification(
                recipient_id,
                notification.id,
                expected_version=notification.version,
                now=now,
                request_id="notification-accept-postgres",
            )
        assert redeemed.project_id == project_id
        assert redeemed.role is ProjectRole.VIEWER
        async with sessions() as session:
            with pytest.raises(ProjectInvitationInvalid):
                await InvitationService(InvitationRepository(session)).accept_notification(
                    recipient_id,
                    notification.id,
                    expected_version=notification.version,
                    now=now,
                )

        async with sessions() as session:
            invitation = await session.get(
                ProjectInvitationRow,
                created.invitation.id,
            )
            persisted_notification = await session.get(
                UserNotificationRow,
                notification.id,
            )
            memberships = (
                (
                    await session.execute(
                        select(ProjectMembershipRow).where(
                            ProjectMembershipRow.project_id == project_id,
                            ProjectMembershipRow.user_id == str(recipient_id),
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert invitation is not None
        assert invitation.status == "redeemed"
        assert len(memberships) == 1
        assert memberships[0].status == "active"
        assert persisted_notification is not None
        assert persisted_notification.read_at == now
        assert persisted_notification.acted_at == now
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_notification_accept_has_exactly_one_winner(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = uuid.uuid4()
    recipient_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    now = datetime.now(UTC)
    try:
        async with sessions.begin() as session:
            session.add_all(
                [
                    UserRow(
                        id=str(owner_id),
                        email="concurrent-owner@example.com",
                        system_role="user",
                        needs_setup=False,
                        token_version=0,
                    ),
                    UserRow(
                        id=str(recipient_id),
                        email="concurrent-member@example.com",
                        system_role="user",
                        needs_setup=False,
                        token_version=0,
                    ),
                ]
            )
            await session.flush()
            session.add(
                ProjectRow(
                    id=project_id,
                    slug="concurrent-notification-project",
                    display_name="Concurrent Notification Project",
                    created_by_user_id=str(owner_id),
                )
            )
            await session.flush()
            session.add(
                ProjectMembershipRow(
                    id=membership_id,
                    project_id=project_id,
                    user_id=str(owner_id),
                    role="admin",
                )
            )

        context = ProjectContext(
            user_id=owner_id,
            project_id=project_id,
            membership_id=membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="notification-concurrency",
        )
        async with sessions() as session:
            created = await InvitationService(InvitationRepository(session)).create(
                context,
                "concurrent-member@example.com",
                ProjectRole.VIEWER,
                now,
            )
        async with sessions() as session:
            page = await NotificationRepository(session).list_for_recipient(
                recipient_id,
                now,
            )
        notification = page.items[0]

        async def accept_once():
            async with sessions() as session:
                return await InvitationService(InvitationRepository(session)).accept_notification(
                    recipient_id,
                    notification.id,
                    expected_version=notification.version,
                    now=now,
                    request_id="notification-concurrency",
                )

        results = await asyncio.wait_for(
            asyncio.gather(
                accept_once(),
                accept_once(),
                return_exceptions=True,
            ),
            timeout=5,
        )
        successes = [result for result in results if isinstance(result, RedeemedInvitation)]
        failures = [
            result
            for result in results
            if isinstance(
                result,
                (ProjectInvitationInvalid, ProjectInvitationConflict),
            )
        ]
        assert len(successes) == 1
        assert len(failures) == 1

        async with sessions() as session:
            invitation = await session.get(
                ProjectInvitationRow,
                created.invitation.id,
            )
            persisted_notification = await session.get(
                UserNotificationRow,
                notification.id,
            )
            memberships = (
                (
                    await session.execute(
                        select(ProjectMembershipRow).where(
                            ProjectMembershipRow.project_id == project_id,
                            ProjectMembershipRow.user_id == str(recipient_id),
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert invitation is not None
        assert invitation.status == "redeemed"
        assert len(memberships) == 1
        assert persisted_notification is not None
        assert persisted_notification.read_at == now
        assert persisted_notification.acted_at == now
    finally:
        await engine.dispose()
