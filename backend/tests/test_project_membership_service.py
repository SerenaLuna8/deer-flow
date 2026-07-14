from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectForbidden, ProjectLastAdmin, ProjectMembershipVersionConflict, ProjectNotFound
from app.projects.membership_models import MembershipView
from app.projects.membership_repository import MembershipRepository
from app.projects.membership_service import MembershipService
from app.projects.models import ProjectRole


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


def _member(*, role: ProjectRole = ProjectRole.ADMIN, version: int = 1):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=str(uuid.uuid4()),
        role=role.value,
        status="active",
        version=version,
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
    now = datetime(2026, 7, 12, 8, 30, tzinfo=UTC)
    context = _context()
    project = SimpleNamespace(id=context.project_id)
    repository = _repository()
    target = _member(role=ProjectRole.VIEWER)
    repository.lock_project_and_member.return_value = (project, target)
    authorization = AsyncMock()
    authorization.mark_revoked.return_value = ()
    retention = AsyncMock()
    service = MembershipService(
        repository,
        clock=lambda: now,
        authorization=authorization,
        retention=retention,
    )

    await service.remove(context, target.id, expected_version=1)
    repository.end_membership.assert_awaited_once_with(
        project,
        target,
        status="removed",
        ended_at=now,
        retention_until=now + timedelta(days=30),
        ended_by_user_id=context.user_id,
    )

    repository.reset_mock()
    leaving = _member(role=ProjectRole.VIEWER)
    leaving.id = context.membership_id
    repository.lock_project_and_member.return_value = (project, leaving)
    await service.leave(context, expected_version=1)
    repository.end_membership.assert_awaited_once_with(
        project,
        leaving,
        status="left",
        ended_at=now,
        retention_until=now + timedelta(days=30),
        ended_by_user_id=context.user_id,
    )


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
@pytest.mark.parametrize("new_role", [ProjectRole.EDITOR, ProjectRole.RUNNER])
async def test_executable_role_downgrade_keeps_active_runs(new_role: ProjectRole) -> None:
    context = _context()
    repository = _repository()
    target = _member(role=ProjectRole.ADMIN)
    repository.lock_project_and_member.return_value = (SimpleNamespace(id=context.project_id), target)
    authorization = AsyncMock()
    retention = AsyncMock()

    await MembershipService(
        repository,
        authorization=authorization,
        retention=retention,
    ).change_role(context, target.id, new_role, expected_version=1)

    authorization.mark_revoked.assert_not_awaited()
    retention.freeze_owner.assert_not_awaited()


@pytest.mark.asyncio
async def test_viewer_downgrade_marks_runs_in_transaction_then_notifies_after_commit() -> None:
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
    authorization.mark_revoked.return_value = ("run-1",)
    retention = AsyncMock()

    async def notify(run_ids, reason):
        events.append(f"notify:{run_ids}:{reason}")

    await MembershipService(
        repository,
        authorization=authorization,
        retention=retention,
        notify_local_cancellation=notify,
    ).change_role(context, target.id, ProjectRole.VIEWER, expected_version=1)

    assert events == [
        "transaction-enter",
        "transaction-commit",
        "notify:('run-1',):authorization_revoked",
    ]
    authorization.mark_revoked.assert_awaited_once()
    retention.freeze_owner.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_marks_runs_and_freezes_owner_but_notifier_failure_cannot_rollback() -> None:
    context = _context()
    repository = _repository()
    target = _member(role=ProjectRole.VIEWER)
    repository.lock_project_and_member.return_value = (SimpleNamespace(id=context.project_id), target)
    authorization = AsyncMock()
    authorization.mark_revoked.return_value = ("run-1",)
    retention = AsyncMock()
    notifier = AsyncMock(side_effect=RuntimeError("local task disappeared"))

    await MembershipService(
        repository,
        authorization=authorization,
        retention=retention,
        notify_local_cancellation=notifier,
    ).remove(context, target.id, expected_version=1)

    authorization.mark_revoked.assert_awaited_once()
    retention.freeze_owner.assert_awaited_once()
    notifier.assert_awaited_once_with(("run-1",), "authorization_revoked")
