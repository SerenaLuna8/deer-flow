"""Project-gate contract for deleting a Skill under Builder concurrency."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import skill_deletion as skill_deletion_module
from app.shared_assets.skill_deletion import SkillDeleteResult, SkillDeletionCoordinator
from app.shared_assets.skill_repository import SkillRepository


class _Rows:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _GateSession:
    def __init__(self, project_id: uuid.UUID) -> None:
        self._project_id = project_id
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _Rows:
        self.statements.append(statement)
        return _Rows(self._project_id)


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="req-skill-delete-project-gate",
    )


@pytest.mark.asyncio
async def test_delete_scope_locks_project_then_exact_membership() -> None:
    context = _context()
    session = _GateSession(context.project_id)

    await SkillRepository(session).lock_project_delete_scope(context)  # type: ignore[arg-type]

    assert len(session.statements) == 2
    project_sql = str(
        session.statements[0].compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    )
    membership_sql = str(
        session.statements[1].compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    )
    assert "FOR UPDATE OF projects" in project_sql
    assert "project_memberships" not in project_sql
    assert "FOR UPDATE OF project_memberships" in membership_sql
    assert "projects" not in membership_sql


@pytest.mark.asyncio
async def test_delete_coordinates_project_asset_agents_secrets_then_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=context.project_id,
        status="suspended",
        current_version_id=None,
        revision=3,
    )
    events: list[str] = []

    async def lock_project_delete_scope(_actor: ProjectContext) -> None:
        events.append("project")

    async def get_project_asset(
        _actor: ProjectContext,
        _asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ):
        assert for_update is True
        events.append("asset")
        return asset

    async def destroy_project_asset_secrets(
        _actor: ProjectContext,
        _asset: object,
    ) -> int:
        events.append("secrets")
        return 0

    async def archive_project_asset(
        _actor: ProjectContext,
        _asset: object,
    ) -> None:
        events.append("archive")

    session = AsyncMock(spec=AsyncSession)
    session.in_transaction.return_value = True
    repository = SimpleNamespace(
        lock_project_delete_scope=lock_project_delete_scope,
        get_project_asset=get_project_asset,
        destroy_project_asset_secrets=destroy_project_asset_secrets,
        archive_project_asset=archive_project_asset,
    )
    monkeypatch.setattr(
        skill_deletion_module,
        "SkillRepository",
        lambda _session: repository,
    )

    async def remove_project_skill_from_definitions_in_session(
        _session: AsyncSession,
        _actor: ProjectContext,
        _skill_id: uuid.UUID,
    ) -> tuple[object, ...]:
        events.append("agents")
        return (object(), object())

    result = await SkillDeletionCoordinator(
        SimpleNamespace(
            remove_project_skill_from_definitions_in_session=(remove_project_skill_from_definitions_in_session),
        )
    ).delete_in_session(
        session,
        context,
        asset.id,
        asset.revision,
    )

    assert result == SkillDeleteResult(affected_agent_count=2)
    assert events == ["project", "asset", "agents", "secrets", "archive"]
