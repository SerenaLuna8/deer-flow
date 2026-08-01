from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from app.private_work.authorization import AUTHORIZATION_REVOKED_REASON
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectForbidden, ProjectLastAdmin, ProjectMembershipVersionConflict, ProjectNotFound
from app.projects.membership_models import MembershipView
from app.projects.membership_repository import MembershipRepository
from app.projects.membership_service import MembershipService
from app.projects.models import ProjectRole

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)


def _context(role: ProjectRole = ProjectRole.ADMIN) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-membership",
    )


def _repository() -> AsyncMock:
    repository = AsyncMock()

    @asynccontextmanager
    async def transaction():
        yield

    repository.transaction = transaction
    return repository


def _member(
    *,
    role: ProjectRole = ProjectRole.ADMIN,
    version: int = 1,
    activation_generation: int = 1,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=str(uuid.uuid4()),
        role=role.value,
        status="active",
        version=version,
        activation_generation=activation_generation,
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_active_members_can_list_members() -> None:
    context = _context(ProjectRole.VIEWER)
    repository = _repository()
    expected = (
        MembershipView(
            membership_id=context.membership_id,
            user_id=context.user_id,
            account_email="viewer@example.com",
            role=ProjectRole.VIEWER,
            status="active",
            version=1,
            joined_at=datetime(2026, 7, 12, tzinfo=UTC),
        ),
    )
    repository.list_members.return_value = expected

    assert await MembershipService(repository).list_members(context) == expected
    repository.list_members.assert_awaited_once_with(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [ProjectRole.EDITOR, ProjectRole.RUNNER, ProjectRole.VIEWER])
async def test_non_admin_cannot_change_or_remove_members(role: ProjectRole) -> None:
    context = _context(role)
    repository = _repository()
    service = MembershipService(repository)

    with pytest.raises(ProjectForbidden):
        await service.change_role(context, uuid.uuid4(), ProjectRole.VIEWER, expected_version=1)
    with pytest.raises(ProjectForbidden):
        await service.remove(context, uuid.uuid4(), expected_version=1)

    repository.lock_project_and_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_last_admin_cannot_leave() -> None:
    context = _context()
    repository = _repository()
    project = SimpleNamespace(id=context.project_id)
    target = _member(role=ProjectRole.ADMIN)
    target.id = context.membership_id
    repository.lock_project_and_member.return_value = (project, target)
    repository.require_another_active_admin.side_effect = ProjectLastAdmin()

    with pytest.raises(ProjectLastAdmin):
        await MembershipService(repository).leave(context, expected_version=1)

    repository.end_membership.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["change_role", "remove"])
async def test_last_admin_cannot_be_demoted_or_removed(operation: str) -> None:
    context = _context()
    repository = _repository()
    target = _member(role=ProjectRole.ADMIN)
    repository.lock_project_and_member.return_value = (SimpleNamespace(id=context.project_id), target)
    repository.require_another_active_admin.side_effect = ProjectLastAdmin()
    service = MembershipService(repository)

    with pytest.raises(ProjectLastAdmin):
        if operation == "change_role":
            await service.change_role(context, target.id, ProjectRole.EDITOR, expected_version=1)
        else:
            await service.remove(context, target.id, expected_version=1)

    repository.set_role.assert_not_awaited()
    repository.end_membership.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_role_noop_does_not_require_another_admin() -> None:
    context = _context()
    repository = _repository()
    target = _member(role=ProjectRole.ADMIN)
    project = SimpleNamespace(id=context.project_id)
    repository.lock_project_and_member.return_value = (project, target)
    repository.set_role.return_value = AsyncMock(spec=MembershipView)

    await MembershipService(repository).change_role(context, target.id, ProjectRole.ADMIN, expected_version=1)

    repository.require_another_active_admin.assert_not_awaited()
    repository.set_role.assert_awaited_once_with(project, target, ProjectRole.ADMIN)


@pytest.mark.asyncio
async def test_membership_version_conflict_prevents_mutation() -> None:
    context = _context()
    repository = _repository()
    target = _member(role=ProjectRole.VIEWER, version=4)
    repository.lock_project_and_member.return_value = (SimpleNamespace(id=context.project_id), target)

    with pytest.raises(ProjectMembershipVersionConflict) as exc_info:
        await MembershipService(repository).change_role(context, target.id, ProjectRole.EDITOR, expected_version=3)

    assert exc_info.value.code == "project_membership_version_conflict"
    assert exc_info.value.__dict__ == {}
    repository.set_role.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_project_membership_is_not_found() -> None:
    context = _context()
    repository = _repository()
    repository.lock_project_and_member.side_effect = ProjectNotFound()

    with pytest.raises(ProjectNotFound):
        await MembershipService(repository).change_role(context, uuid.uuid4(), ProjectRole.VIEWER, expected_version=1)


@pytest.mark.asyncio
async def test_remove_and_leave_record_distinct_end_metadata() -> None:
    events: list[str] = []
    now = datetime(2026, 7, 12, 8, 30, tzinfo=UTC)
    context = _context()
    project = SimpleNamespace(id=context.project_id)
    repository = _repository()
    target = _member(
        role=ProjectRole.VIEWER,
        version=4,
        activation_generation=2,
    )
    repository.lock_project_and_member.return_value = (project, target)
    authorization = AsyncMock()
    retention = AsyncMock()
    quota = AsyncMock()

    async def mark_revoked(*_args, **_kwargs):
        events.append("authorization-mark")
        return ()

    async def freeze_owner(*_args, **_kwargs):
        events.append("retention-freeze")

    authorization.mark_revoked.side_effect = mark_revoked
    retention.freeze_owner.side_effect = freeze_owner
    service = MembershipService(
        repository,
        clock=lambda: now,
        authorization=authorization,
        retention=retention,
        quota=quota,
    )

    await service.remove(context, target.id, expected_version=4)
    assert events == ["retention-freeze", "authorization-mark"]
    retention.freeze_owner.assert_awaited_once_with(
        repository.session,
        project_id=project.id,
        owner_user_id=target.user_id,
        now=now,
    )
    repository.end_membership.assert_awaited_once_with(
        project,
        target,
        status="removed",
        ended_at=now,
        retention_until=now + timedelta(days=30),
        ended_by_user_id=context.user_id,
    )
    quota.release_member.assert_awaited_once()
    assert quota.release_member.await_args.kwargs == {
        "membership_id": target.id,
        "activation_generation": 2,
    }
    assert quota.release_member.await_args.args[1].membership_version == 4

    repository.reset_mock()
    retention.reset_mock()
    quota.reset_mock()
    events.clear()
    leaving = _member(
        role=ProjectRole.VIEWER,
        version=9,
        activation_generation=3,
    )
    leaving.id = context.membership_id
    repository.lock_project_and_member.return_value = (project, leaving)
    await service.leave(context, expected_version=9)
    assert events == ["retention-freeze", "authorization-mark"]
    retention.freeze_owner.assert_awaited_once_with(
        repository.session,
        project_id=project.id,
        owner_user_id=leaving.user_id,
        now=now,
    )
    repository.end_membership.assert_awaited_once_with(
        project,
        leaving,
        status="left",
        ended_at=now,
        retention_until=now + timedelta(days=30),
        ended_by_user_id=context.user_id,
    )
    quota.release_member.assert_awaited_once()
    assert quota.release_member.await_args.kwargs == {
        "membership_id": leaving.id,
        "activation_generation": 3,
    }
    assert quota.release_member.await_args.args[1].membership_version == 9


@pytest.mark.asyncio
async def test_repository_revalidates_actor_after_project_lock_before_target_lock() -> None:
    context = _context()
    target = _member(role=ProjectRole.VIEWER)
    project = SimpleNamespace(id=context.project_id)
    session = AsyncMock()
    session.execute.side_effect = [
        SimpleNamespace(scalar_one_or_none=lambda: project),
        SimpleNamespace(scalar_one=lambda: True),
        SimpleNamespace(scalar_one_or_none=lambda: target),
    ]

    assert await MembershipRepository(session).lock_project_and_member(context, target.id) == (project, target)

    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert len(statements) == 3
    assert "FROM projects" in statements[0]
    assert "project_memberships" not in statements[0]
    assert "FOR UPDATE" in statements[0]
    assert "FROM project_memberships" in statements[1]
    assert "FOR UPDATE" not in statements[1]
    assert "FROM project_memberships" in statements[2]
    assert "FOR UPDATE" in statements[2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_role", "new_role"),
    [
        (ProjectRole.ADMIN, ProjectRole.EDITOR),
        (ProjectRole.ADMIN, ProjectRole.RUNNER),
        (ProjectRole.EDITOR, ProjectRole.RUNNER),
    ],
)
async def test_capability_reducing_role_change_revokes_active_runs(
    current_role: ProjectRole,
    new_role: ProjectRole,
) -> None:
    context = _context()
    repository = _repository()
    target = _member(role=current_role)
    repository.lock_project_and_member.return_value = (SimpleNamespace(id=context.project_id), target)
    authorization = AsyncMock()
    retention = AsyncMock()

    await MembershipService(
        repository,
        authorization=authorization,
        retention=retention,
    ).change_role(context, target.id, new_role, expected_version=1)

    authorization.mark_revoked.assert_awaited_once_with(
        repository.session,
        project_id=context.project_id,
        owner_user_id=target.user_id,
        reason=AUTHORIZATION_REVOKED_REASON,
        now=ANY,
    )
    retention.freeze_owner.assert_not_awaited()


@pytest.mark.asyncio
async def test_viewer_downgrade_revokes_runs_without_freezing_existing_private_data() -> None:
    events: list[str] = []
    repository = _repository()

    @asynccontextmanager
    async def transaction():
        events.append("transaction-enter")
        yield
        events.append("transaction-commit")

    repository.transaction = transaction
    context = _context()
    target = _member(role=ProjectRole.EDITOR)
    repository.lock_project_and_member.return_value = (SimpleNamespace(id=context.project_id), target)
    authorization = AsyncMock()
    retention = AsyncMock()

    async def mark_revoked(*_args, **_kwargs):
        events.append("authorization-mark")
        return ("run-1",)

    authorization.mark_revoked.side_effect = mark_revoked

    async def restrict_owner(*_args, **_kwargs):
        events.append("retention-restrict")

    retention.restrict_owner_to_viewer.side_effect = restrict_owner

    await MembershipService(
        repository,
        clock=lambda: NOW,
        authorization=authorization,
        retention=retention,
    ).change_role(context, target.id, ProjectRole.VIEWER, expected_version=1)

    assert events == [
        "transaction-enter",
        "retention-restrict",
        "authorization-mark",
        "transaction-commit",
    ]
    authorization.mark_revoked.assert_awaited_once()
    retention.restrict_owner_to_viewer.assert_awaited_once_with(
        repository.session,
        project_id=context.project_id,
        owner_user_id=target.user_id,
        now=NOW,
    )
    retention.freeze_owner.assert_not_awaited()


@pytest.mark.asyncio
async def test_viewer_downgrade_never_uses_departed_member_retention() -> None:
    repository = _repository()
    context = _context()
    target = _member(role=ProjectRole.RUNNER)
    repository.lock_project_and_member.return_value = (
        SimpleNamespace(id=context.project_id),
        target,
    )
    authorization = AsyncMock()
    authorization.mark_revoked.return_value = ("run-1",)
    retention = AsyncMock()
    retention.freeze_owner.side_effect = RuntimeError("retention unavailable")
    await MembershipService(
        repository,
        clock=lambda: NOW,
        authorization=authorization,
        retention=retention,
    ).change_role(
        context,
        target.id,
        ProjectRole.VIEWER,
        expected_version=1,
    )

    repository.set_role.assert_awaited_once_with(
        repository.lock_project_and_member.return_value[0],
        target,
        ProjectRole.VIEWER,
    )
    authorization.mark_revoked.assert_awaited_once()
    retention.restrict_owner_to_viewer.assert_awaited_once()
    retention.freeze_owner.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_marks_runs_and_freezes_owner() -> None:
    context = _context()
    repository = _repository()
    target = _member(role=ProjectRole.VIEWER)
    repository.lock_project_and_member.return_value = (SimpleNamespace(id=context.project_id), target)
    authorization = AsyncMock()
    authorization.mark_revoked.return_value = ("run-1",)
    retention = AsyncMock()
    await MembershipService(
        repository,
        authorization=authorization,
        retention=retention,
    ).remove(context, target.id, expected_version=1)

    authorization.mark_revoked.assert_awaited_once()
    retention.freeze_owner.assert_awaited_once()
