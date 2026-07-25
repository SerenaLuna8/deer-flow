from __future__ import annotations

import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.notifications.models import InvitationNotificationView, NotificationPage
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectForbidden, ProjectValidationFailed
from app.projects.invitation_models import (
    InvitationView,
    ProjectInvitationConflict,
    ProjectInvitationInvalid,
    RedeemedInvitation,
)
from app.projects.invitation_service import InvitationService, hash_invitation_token
from app.projects.models import ProjectRole

NOW = datetime(2026, 7, 12, 8, 30, tzinfo=UTC)


def _context(role: ProjectRole = ProjectRole.ADMIN) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-invitation",
    )


def _repository() -> AsyncMock:
    repository = AsyncMock()

    @asynccontextmanager
    async def transaction():
        yield

    repository.transaction = transaction
    return repository


def _invitation(
    *,
    status: str = "pending",
    expires_at: datetime = NOW + timedelta(days=7),
    invited_email: str = "member@example.com",
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        invited_email=invited_email,
        role=ProjectRole.EDITOR.value,
        status=status,
        expires_at=expires_at,
        version=1,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
    )


def _view(row) -> InvitationView:
    return InvitationView(
        id=row.id,
        project_id=row.project_id,
        invited_email=row.invited_email,
        role=ProjectRole(row.role),
        status=row.status,
        expires_at=row.expires_at,
        version=row.version,
        created_at=row.created_at,
    )


@pytest.mark.asyncio
async def test_create_normalizes_email_hashes_generated_token_and_hides_plaintext_from_repr(monkeypatch) -> None:
    context = _context()
    repository = _repository()
    row = _invitation(invited_email="member@example.com")
    repository.create.return_value = _view(row)
    monkeypatch.setattr("app.projects.invitation_service.secrets.token_urlsafe", lambda size: "plain-token" if size == 32 else "wrong")

    created = await InvitationService(repository).create(
        context,
        "  MEMBER@Example.COM ",
        ProjectRole.EDITOR,
        NOW,
    )

    assert created.token == "plain-token"
    assert created.invitation == _view(row)
    assert created.token not in repr(created)
    repository.create.assert_awaited_once_with(
        context,
        invited_email="member@example.com",
        role=ProjectRole.EDITOR,
        token_hash=hashlib.sha256(created.token.encode("utf-8")).hexdigest(),
        now=NOW,
        expires_at=NOW + timedelta(days=7),
    )


@pytest.mark.asyncio
async def test_create_rejects_admin_invalid_email_and_non_admin() -> None:
    repository = _repository()
    service = InvitationService(repository)

    with pytest.raises(ProjectValidationFailed):
        await service.create(_context(), "member@example.com", ProjectRole.ADMIN, NOW)
    with pytest.raises(ProjectValidationFailed):
        await service.create(_context(), "not-an-email", ProjectRole.VIEWER, NOW)
    with pytest.raises(ProjectForbidden):
        await service.create(_context(ProjectRole.EDITOR), "member@example.com", ProjectRole.VIEWER, NOW)

    repository.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_contains_only_invitation_identity_and_hash() -> None:
    repository = _repository()
    row = _invitation()
    repository.get_by_token_hash.return_value = row
    token = "claim-token"

    claim = await InvitationService(repository).claim(token, NOW)

    assert claim.invitation_id == row.id
    assert claim.token_hash == hash_invitation_token(token)
    assert not hasattr(claim, "token")
    repository.get_by_token_hash.assert_awaited_once_with(hash_invitation_token(token))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "now"),
    [
        (None, NOW),
        (_invitation(status="revoked"), NOW),
        (_invitation(expires_at=NOW), NOW),
    ],
)
async def test_claim_rejects_unknown_non_pending_and_expired_invitation(row, now) -> None:
    repository = _repository()
    repository.get_by_token_hash.return_value = row

    with pytest.raises(ProjectInvitationInvalid) as exc_info:
        await InvitationService(repository).claim("claim-token", now)

    assert exc_info.value.__dict__ == {}


@pytest.mark.asyncio
async def test_redeem_validates_email_inside_locked_transaction() -> None:
    repository = _repository()
    row = _invitation(invited_email="member@example.com")
    project = SimpleNamespace(id=row.project_id, slug="example-project")
    repository.locate_invitation_project.return_value = row.project_id
    repository.lock_project.return_value = project
    repository.lock_invitation.return_value = row
    claim = SimpleNamespace(invitation_id=row.id, token_hash="a" * 64)

    with pytest.raises(ProjectInvitationInvalid):
        await InvitationService(repository).redeem(
            uuid.uuid4(),
            "other@example.com",
            claim,
            NOW,
        )

    repository.locate_invitation_project.assert_awaited_once_with(row.id, "a" * 64)
    repository.lock_project.assert_awaited_once_with(row.project_id)
    repository.lock_invitation.assert_awaited_once_with(row.project_id, row.id, "a" * 64)
    repository.redeem_locked.assert_not_awaited()


@pytest.mark.asyncio
async def test_redeem_passes_locked_pending_invitation_to_repository() -> None:
    repository = _repository()
    row = _invitation(invited_email="member@example.com")
    project = SimpleNamespace(id=row.project_id, slug="example-project")
    repository.locate_invitation_project.return_value = row.project_id
    repository.lock_project.return_value = project
    repository.lock_invitation.return_value = row
    expected = RedeemedInvitation(
        invitation_id=row.id,
        project_id=row.project_id,
        project_slug="example-project",
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
    )
    repository.redeem_locked.return_value = expected
    membership = SimpleNamespace(
        id=expected.membership_id,
        version=7,
        activation_generation=3,
    )
    repository.lock_membership.return_value = membership
    claim = SimpleNamespace(invitation_id=row.id, token_hash="b" * 64)
    user_id = uuid.uuid4()

    retention = AsyncMock()
    retention_jobs = AsyncMock()
    quota = AsyncMock()
    notifications = AsyncMock()
    result = await InvitationService(
        repository,
        retention=retention,
        retention_jobs=retention_jobs,
        quota=quota,
        notifications=notifications,
    ).redeem(
        user_id,
        " MEMBER@example.com ",
        claim,
        NOW,
    )

    assert result == expected
    repository.redeem_locked.assert_awaited_once_with(project, row, user_id=user_id, now=NOW)
    quota.reserve_member.assert_awaited_once()
    assert quota.reserve_member.await_args.kwargs == {
        "membership_id": membership.id,
        "activation_generation": 3,
    }
    assert quota.reserve_member.await_args.args[1].membership_version == 7
    retention.restore_owner.assert_awaited_once_with(
        repository.session,
        project_id=row.project_id,
        owner_user_id=str(user_id),
        now=NOW,
    )
    retention_jobs.cancel_former_owner.assert_awaited_once_with(
        repository.session,
        project_id=row.project_id,
        owner_user_id=str(user_id),
        now=NOW,
    )
    notifications.mark_invitation_acted.assert_awaited_once_with(
        user_id,
        row.id,
        NOW,
    )


@pytest.mark.asyncio
async def test_list_notifications_returns_only_current_recipient_notifications() -> None:
    repository = _repository()
    user_id = uuid.uuid4()
    notification = InvitationNotificationView(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        project_slug="research-lab",
        project_display_name="Research Lab",
        inviter_email="owner@example.com",
        role=ProjectRole.VIEWER,
        status="pending",
        is_read=False,
        created_at=NOW,
        expires_at=NOW + timedelta(days=7),
        version=1,
    )
    notifications = AsyncMock()
    notifications.list_for_recipient.return_value = NotificationPage(
        items=(notification,),
        unread_count=1,
    )

    result = await InvitationService(
        repository,
        notifications=notifications,
    ).list_notifications(
        user_id,
        NOW,
    )

    assert result.items == (notification,)
    assert result.unread_count == 1
    notifications.list_for_recipient.assert_awaited_once_with(
        user_id,
        NOW,
        cursor=None,
        limit=50,
    )


@pytest.mark.asyncio
async def test_list_notifications_rejects_invalid_cursor_before_query() -> None:
    repository = _repository()
    notifications = AsyncMock()

    with pytest.raises(ProjectValidationFailed):
        await InvitationService(
            repository,
            notifications=notifications,
        ).list_notifications(
            uuid.uuid4(),
            NOW,
            cursor="not-a-valid-cursor",
            limit=10,
        )

    notifications.list_for_recipient.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_all_notifications_read_is_account_scoped() -> None:
    repository = _repository()
    notifications = AsyncMock()
    notifications.mark_all_read.return_value = 4
    user_id = uuid.uuid4()

    marked_count = await InvitationService(
        repository,
        notifications=notifications,
    ).mark_all_notifications_read(user_id, NOW)

    assert marked_count == 4
    notifications.mark_all_read.assert_awaited_once_with(user_id, NOW)


@pytest.mark.asyncio
async def test_accept_notification_redeems_recipient_invitation_without_token() -> None:
    repository = _repository()
    row = _invitation(invited_email="member@example.com")
    project = SimpleNamespace(id=row.project_id, slug="example-project")
    repository.lock_project.return_value = project
    notifications = AsyncMock()
    notifications.locate_project_for_accept.return_value = row.project_id
    notifications.lock_invitation_for_accept.return_value = row
    expected = RedeemedInvitation(
        invitation_id=row.id,
        project_id=row.project_id,
        project_slug=project.slug,
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
    )
    repository.redeem_locked.return_value = expected
    repository.lock_membership.return_value = SimpleNamespace(
        id=expected.membership_id,
        version=2,
        activation_generation=1,
    )
    user_id = uuid.uuid4()

    result = await InvitationService(
        repository,
        retention=AsyncMock(),
        retention_jobs=AsyncMock(),
        quota=AsyncMock(),
        notifications=notifications,
    ).accept_notification(
        user_id,
        row.id,
        expected_version=1,
        now=NOW,
    )

    assert result == expected
    notifications.locate_project_for_accept.assert_awaited_once_with(
        user_id,
        row.id,
    )
    notifications.lock_invitation_for_accept.assert_awaited_once_with(
        user_id,
        row.id,
        row.project_id,
    )
    repository.redeem_locked.assert_awaited_once_with(
        project,
        row,
        user_id=user_id,
        now=NOW,
    )


@pytest.mark.asyncio
async def test_accept_notification_rejects_stale_invitation_version() -> None:
    repository = _repository()
    row = _invitation()
    row.version = 2
    notifications = AsyncMock()
    notifications.locate_project_for_accept.return_value = row.project_id
    repository.lock_project.return_value = SimpleNamespace(
        id=row.project_id,
        slug="example-project",
    )
    notifications.lock_invitation_for_accept.return_value = row

    with pytest.raises(ProjectInvitationConflict):
        await InvitationService(
            repository,
            notifications=notifications,
        ).accept_notification(
            uuid.uuid4(),
            row.id,
            expected_version=1,
            now=NOW,
        )

    repository.redeem_locked.assert_not_awaited()


def test_hash_invitation_token_is_lowercase_sha256_hexdigest() -> None:
    digest = hash_invitation_token("plain-token")
    assert digest == hashlib.sha256(b"plain-token").hexdigest()
    assert len(digest) == 64
    assert digest == digest.lower()
