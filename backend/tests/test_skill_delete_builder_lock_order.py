"""Project-gate contract for deleting a Skill under Builder concurrency."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import skill_service as skill_service_module
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


class _TransactionSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def begin(self):
        return self


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
async def test_delete_scope_takes_project_update_lock() -> None:
    context = _context()
    session = _GateSession(context.project_id)

    await SkillRepository(session).lock_project_delete_scope(context)  # type: ignore[arg-type]

    assert len(session.statements) == 1
    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]
    assert "FOR UPDATE OF projects" in sql
    assert "FOR SHARE" not in sql


@pytest.mark.asyncio
async def test_delete_takes_project_gate_before_asset_and_builder_cleanup(
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

    async def plan_project_asset_deletion(
        _actor: ProjectContext,
        _asset: object,
    ) -> tuple[()]:
        events.append("plan")
        return ()

    async def delete_project_asset(
        _actor: ProjectContext,
        _asset: object,
        _version_ids: tuple[uuid.UUID, ...],
    ) -> None:
        events.append("cleanup")

    transaction = _TransactionSession()
    repository = SimpleNamespace(
        session=transaction,
        lock_project_delete_scope=lock_project_delete_scope,
        get_project_asset=get_project_asset,
        plan_project_asset_deletion=plan_project_asset_deletion,
        delete_project_asset=delete_project_asset,
    )
    monkeypatch.setattr(
        skill_service_module,
        "SkillRepository",
        lambda _session: repository,
    )

    await skill_service_module.SkillService(
        lambda: transaction,
        governance_sink=SimpleNamespace(
            append_project=AsyncMock(),
        ),
    ).delete(
        context,
        asset.id,
        expected_asset_version=asset.revision,
    )

    assert events == ["project", "asset", "plan", "cleanup"]
