from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectForbidden
from app.projects.lifecycle_service import ProjectLifecycleService
from app.projects.models import ProjectRole

NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


def _context(role: ProjectRole = ProjectRole.ADMIN) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-lifecycle",
    )


@pytest.mark.asyncio
async def test_request_deletion_requires_capability_before_repository_access() -> None:
    repository = AsyncMock()
    context = _context(ProjectRole.EDITOR)

    with pytest.raises(ProjectForbidden):
        await ProjectLifecycleService(repository).request_deletion(context, NOW)

    repository.mark_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_deletion_sets_fixed_thirty_day_window() -> None:
    repository = AsyncMock()

    @asynccontextmanager
    async def transaction():
        yield

    repository.transaction = transaction
    context = _context()
    expected = object()
    project = SimpleNamespace(id=context.project_id)
    actor = SimpleNamespace()
    repository.lock_pending_deletion.return_value = (project, actor)
    repository.lock_active_members.return_value = ()
    repository.mark_pending_locked.return_value = expected

    result = await ProjectLifecycleService(repository).request_deletion(context, NOW)

    assert result is expected
    repository.mark_pending_locked.assert_awaited_once_with(
        project,
        actor,
        requested_at=NOW,
        effective_at=NOW + timedelta(days=30),
        requested_by_user_id=context.user_id,
        request_id=context.request_id,
    )


@pytest.mark.asyncio
async def test_restore_uses_dedicated_user_scope_without_project_context() -> None:
    repository = AsyncMock()

    @asynccontextmanager
    async def transaction():
        yield

    repository.transaction = transaction
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    expected = object()
    project = SimpleNamespace(id=project_id)
    actor = SimpleNamespace()
    repository.lock_restore.return_value = (project, actor)
    repository.lock_active_members.return_value = ()
    repository.restore_locked.return_value = expected

    result = await ProjectLifecycleService(repository).restore(
        user_id,
        project_id,
        "req-restore",
        NOW,
    )

    assert result is expected
    repository.lock_restore.assert_awaited_once_with(user_id, project_id, NOW)
    repository.restore_locked.assert_awaited_once_with(
        project,
        actor,
        request_id="req-restore",
    )


@pytest.mark.asyncio
async def test_pending_deletion_revokes_and_freezes_all_members_before_commit_then_notifies() -> None:
    events: list[str] = []
    repository = AsyncMock()

    @asynccontextmanager
    async def transaction():
        events.append("transaction-enter")
        yield
        events.append("transaction-commit")

    repository.transaction = transaction
    context = _context()
    project = SimpleNamespace(id=context.project_id)
    members = (SimpleNamespace(user_id=str(uuid.uuid4())), SimpleNamespace(user_id=str(uuid.uuid4())))
    repository.lock_pending_deletion.return_value = (project, SimpleNamespace())
    repository.lock_active_members.return_value = members
    repository.mark_pending_locked.return_value = object()
    authorization = AsyncMock()
    authorization.mark_revoked.side_effect = [("run-1",), ("run-2",)]
    retention = AsyncMock()

    async def notify(run_ids, reason):
        events.append(f"notify:{run_ids}:{reason}")

    await ProjectLifecycleService(
        repository,
        authorization=authorization,
        retention=retention,
        notify_local_cancellation=notify,
    ).request_deletion(context, NOW)

    assert events == [
        "transaction-enter",
        "transaction-commit",
        "notify:('run-1', 'run-2'):authorization_revoked",
    ]
    assert authorization.mark_revoked.await_count == 2
    assert retention.freeze_owner.await_count == 2


@pytest.mark.asyncio
async def test_restore_only_restores_active_members_of_same_project() -> None:
    repository = AsyncMock()

    @asynccontextmanager
    async def transaction():
        yield

    repository.transaction = transaction
    project_id = uuid.uuid4()
    member = SimpleNamespace(user_id=str(uuid.uuid4()))
    repository.lock_restore.return_value = (SimpleNamespace(id=project_id), SimpleNamespace())
    repository.lock_active_members.return_value = (member,)
    repository.restore_locked.return_value = object()
    retention = AsyncMock()

    await ProjectLifecycleService(repository, retention=retention).restore(uuid.uuid4(), project_id, "req-restore", NOW)

    retention.restore_owners.assert_awaited_once()
    kwargs = retention.restore_owners.await_args.kwargs
    assert kwargs["project_id"] == project_id
    assert kwargs["owner_user_ids"] == (member.user_id,)
