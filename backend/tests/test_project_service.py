from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectDatabaseUnavailable, ProjectForbidden, ProjectValidationFailed
from app.projects.models import CreateProject, ProjectChanges, ProjectRole
from app.projects.repository import ProjectRepository, _decode_cursor
from app.projects.service import ProjectService, normalize_slug


def _context(role: ProjectRole) -> ProjectContext:
    return ProjectContext(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), role, capabilities_for(role), 1, "req")


def test_normalize_slug_is_strict() -> None:
    assert normalize_slug("  Research-Lab ") == "research-lab"
    for value in ("ab", "a" * 64, "-abc", "abc-", "a--b", "abc_def", "abc def"):
        with pytest.raises(ProjectValidationFailed) as exc_info:
            normalize_slug(value)
        assert exc_info.value.code == "project_validation_failed"
        assert value not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [ProjectRole.EDITOR, ProjectRole.RUNNER, ProjectRole.VIEWER])
async def test_only_admin_can_update(role: ProjectRole) -> None:
    repository = AsyncMock()
    service = ProjectService(repository)
    with pytest.raises(ProjectForbidden):
        await service.update(_context(role), ProjectChanges(display_name="Changed"))
    repository.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_and_update_validate_field_lengths_and_whitelist() -> None:
    repository = AsyncMock()
    repository.create_with_admin.return_value = _context(ProjectRole.ADMIN)
    service = ProjectService(repository)
    await service.create(uuid.uuid4(), CreateProject(" Alpha ", " Alpha "), "req")
    command = repository.create_with_admin.await_args.args[1]
    assert command.slug == "alpha"
    assert command.display_name == "Alpha"
    for changes in (ProjectChanges(), ProjectChanges(display_name=""), ProjectChanges(description="x" * 501), ProjectChanges(icon="x" * 33)):
        with pytest.raises(ProjectValidationFailed):
            await service.update(_context(ProjectRole.ADMIN), changes)


def test_privileged_capabilities_remain_admin_only() -> None:
    for capability in (Capability.PROJECT_UPDATE, Capability.PROJECT_MEMBERS_MANAGE, Capability.MCP_CREDENTIALS_APPROVE):
        assert [role for role in ProjectRole if capability in capabilities_for(role)] == [ProjectRole.ADMIN]


@pytest.mark.asyncio
async def test_database_error_is_stable_and_does_not_leak_sql_or_url() -> None:
    session = AsyncMock()
    session.execute.side_effect = SQLAlchemyError("SELECT secret FROM projects postgresql://owner:password@db/deerflow")
    with pytest.raises(ProjectDatabaseUnavailable) as exc_info:
        await ProjectRepository(session).get(_context(ProjectRole.ADMIN))
    assert str(exc_info.value) == "Project storage unavailable"
    assert "SELECT" not in str(exc_info.value)
    assert "postgresql" not in str(exc_info.value)


@pytest.mark.parametrize("cursor", ["", "%%%", "e30", "not-a-cursor", "YWJj$"])
def test_cursor_parser_is_strict(cursor: str) -> None:
    with pytest.raises(ProjectValidationFailed):
        _decode_cursor(cursor)
