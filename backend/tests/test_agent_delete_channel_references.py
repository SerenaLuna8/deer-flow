from __future__ import annotations

import re
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_repository import AgentRepository
from app.shared_assets.errors import AssetConflict


class _ScalarRows:
    def __init__(self, values: tuple[uuid.UUID, ...]) -> None:
        self._values = values

    def all(self) -> tuple[uuid.UUID, ...]:
        return self._values


class _VersionResult:
    def __init__(self, values: tuple[uuid.UUID, ...]) -> None:
        self._values = values

    def scalars(self) -> _ScalarRows:
        return _ScalarRows(self._values)


class _Session:
    def __init__(
        self,
        *,
        version_ids: tuple[uuid.UUID, ...],
        retained_reference_exists: bool,
    ) -> None:
        self._version_ids = version_ids
        self._retained_reference_exists = retained_reference_exists
        self.executed: list[object] = []
        self.scalar_statements: list[object] = []

    async def execute(self, statement):
        self.executed.append(statement)
        if len(self.executed) == 1:
            return _VersionResult(self._version_ids)
        return SimpleNamespace()

    async def scalar(self, statement):
        self.scalar_statements.append(statement)
        return self._retained_reference_exists


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="agent-delete-channel-references",
    )


def _asset(context: ProjectContext):
    return SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=context.project_id,
    )


def _sql(statement: object) -> str:
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return re.sub(r"\s+", " ", str(compiled)).strip()


@pytest.mark.asyncio
async def test_deletion_plan_cleans_only_released_challenges() -> None:
    context = _context()
    asset = _asset(context)
    version_ids = (uuid.uuid4(), uuid.uuid4())
    session = _Session(
        version_ids=version_ids,
        retained_reference_exists=False,
    )

    planned = await AgentRepository(session).plan_project_asset_deletion(  # type: ignore[arg-type]
        context,
        asset,
    )

    assert planned == version_ids
    assert len(session.executed) == 2
    challenge_cleanup = _sql(session.executed[1])
    assert challenge_cleanup.startswith("DELETE FROM project_channel_group_binding_challenges")
    assert f"project_channel_group_binding_challenges.project_id = '{context.project_id}'" in challenge_cleanup
    assert f"project_channel_group_binding_challenges.agent_asset_id = '{asset.id}'" in challenge_cleanup
    assert "project_channel_group_binding_challenges.agent_scope = 'project'" in challenge_cleanup
    assert "project_channel_group_binding_challenges.consumed_at IS NOT NULL" in challenge_cleanup
    assert "project_channel_group_binding_challenges.expires_at <= now()" in challenge_cleanup


@pytest.mark.asyncio
async def test_deletion_plan_retains_original_and_live_channel_references() -> None:
    context = _context()
    asset = _asset(context)
    session = _Session(
        version_ids=(),
        retained_reference_exists=False,
    )

    await AgentRepository(session).plan_project_asset_deletion(  # type: ignore[arg-type]
        context,
        asset,
    )

    assert len(session.scalar_statements) == 1
    retained = _sql(session.scalar_statements[0])
    for table in (
        "threads_meta",
        "scheduled_tasks",
        "run_asset_versions",
        "project_channel_group_bindings",
        "project_channel_group_binding_challenges",
    ):
        assert table in retained
    assert "project_channel_group_bindings.deleted_at IS NULL" in retained
    assert "project_channel_group_bindings.status" not in retained
    assert "project_channel_group_binding_challenges.consumed_at IS NULL" in retained
    assert "project_channel_group_binding_challenges.expires_at > now()" in retained


@pytest.mark.asyncio
async def test_deletion_plan_cleans_released_rows_before_reporting_retained_conflict() -> None:
    context = _context()
    asset = _asset(context)
    session = _Session(
        version_ids=(),
        retained_reference_exists=True,
    )

    with pytest.raises(AssetConflict) as exc_info:
        await AgentRepository(session).plan_project_asset_deletion(  # type: ignore[arg-type]
            context,
            asset,
        )

    assert exc_info.value.request_id == context.request_id
    assert len(session.executed) == 2
    assert len(session.scalar_statements) == 1
