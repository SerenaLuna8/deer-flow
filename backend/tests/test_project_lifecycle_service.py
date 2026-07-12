from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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
    context = _context()
    expected = object()
    repository.mark_pending.return_value = expected

    result = await ProjectLifecycleService(repository).request_deletion(context, NOW)

    assert result is expected
    repository.mark_pending.assert_awaited_once_with(
        context,
        requested_at=NOW,
        effective_at=NOW + timedelta(days=30),
    )


@pytest.mark.asyncio
async def test_restore_uses_dedicated_user_scope_without_project_context() -> None:
    repository = AsyncMock()
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    expected = object()
    repository.restore.return_value = expected

    result = await ProjectLifecycleService(repository).restore(
        user_id,
        project_id,
        "req-restore",
        NOW,
    )

    assert result is expected
    repository.restore.assert_awaited_once_with(
        user_id,
        project_id,
        request_id="req-restore",
        now=NOW,
    )
