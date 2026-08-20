from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.gateway.routers.project_agent_builder import _all_internal_tool_groups
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import agent_design_service as agent_design_service_module
from app.shared_assets.agent_design_service import AgentDesignService
from app.shared_assets.agent_repository import AgentRepository
from app.shared_assets.models import AssetScope, SkillAssetRef


def test_all_internal_tool_groups_follow_runtime_config_and_include_task() -> None:
    config = SimpleNamespace(
        tool_groups=(
            SimpleNamespace(name="web"),
            SimpleNamespace(name="file:read"),
            SimpleNamespace(name="custom"),
            SimpleNamespace(name="task"),
        ),
        tools=(
            SimpleNamespace(group="web"),
            SimpleNamespace(group="configured-only-on-tool"),
        ),
    )

    assert _all_internal_tool_groups(config) == (
        "web",
        "file:read",
        "custom",
        "task",
        "configured-only-on-tool",
    )


@pytest.mark.asyncio
async def test_default_blueprint_freezes_all_enabled_system_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_ids = (uuid.uuid4(), uuid.uuid4())
    skill_refs = tuple(SkillAssetRef(AssetScope.SYSTEM, skill_id) for skill_id in skill_ids)
    mcp_ids = (uuid.uuid4(),)
    captured_contexts: list[object] = []

    class _AgentRepository:
        def __init__(self, session: object) -> None:
            assert session is fake_session

        async def list_enabled_system_dependencies(
            self,
            context: object,
        ) -> tuple[tuple[uuid.UUID, ...], tuple[uuid.UUID, ...]]:
            captured_contexts.append(context)
            return skill_refs, mcp_ids

    fake_session = object()
    context = object()
    monkeypatch.setattr(
        agent_design_service_module,
        "AgentRepository",
        _AgentRepository,
    )
    service = AgentDesignService(
        lambda: None,  # type: ignore[arg-type]
        generator=SimpleNamespace(),  # type: ignore[arg-type]
        default_tool_groups_provider=lambda: (
            "web",
            "file:read",
            "file:write",
            "bash",
            "task",
        ),
    )

    blueprint = await service._default_blueprint_with_system_dependencies(  # noqa: SLF001 - focused service contract test
        fake_session,  # type: ignore[arg-type]
        context,  # type: ignore[arg-type]
        "审查代码",
    )

    assert blueprint.tool_groups == (
        "web",
        "file:read",
        "file:write",
        "bash",
        "task",
    )
    assert blueprint.skill_refs == skill_refs
    assert blueprint.mcp_version_ids == mcp_ids
    assert captured_contexts == [context]


@pytest.mark.asyncio
async def test_default_system_dependencies_query_enabled_active_current_assets() -> None:
    skill_ids = (uuid.uuid4(), uuid.uuid4())
    mcp_ids = (uuid.uuid4(),)

    class _ScalarResult:
        def __init__(self, values: tuple[uuid.UUID, ...]) -> None:
            self._values = values

        def scalars(self) -> _ScalarResult:
            return self

        def all(self) -> list[uuid.UUID]:
            return list(self._values)

    class _Session:
        def __init__(self) -> None:
            self.statements: list[object] = []
            self.results = iter((_ScalarResult(skill_ids), _ScalarResult(mcp_ids)))

        async def execute(self, statement: object) -> _ScalarResult:
            self.statements.append(statement)
            return next(self.results)

    class _Repository(AgentRepository):
        async def _lock_project_context(self, context: ProjectContext) -> None:
            self.locked_context = context

    context = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(Capability),
        membership_version=1,
        request_id="request-default-capabilities",
    )
    session = _Session()
    repository = _Repository(session)  # type: ignore[arg-type]

    resolved = await repository.list_enabled_system_dependencies(context)

    assert resolved == (
        tuple(SkillAssetRef(AssetScope.SYSTEM, skill_id) for skill_id in skill_ids),
        mcp_ids,
    )
    assert repository.locked_context is context
    skill_sql, mcp_sql = (str(statement.compile(compile_kwargs={"literal_binds": True})).lower() for statement in session.statements)
    assert "project_system_skill_bindings.enabled is true" in skill_sql
    assert "skills.status = 'active'" in skill_sql
    assert "skill_versions.id = skills.current_version_id" in skill_sql
    assert "skill_versions.revoked_at is null" in skill_sql
    assert "project_system_mcp_bindings.enabled is true" in mcp_sql
    assert "mcp_servers.status = 'active'" in mcp_sql
    assert "mcp_server_versions.workflow_status = 'published'" in mcp_sql
