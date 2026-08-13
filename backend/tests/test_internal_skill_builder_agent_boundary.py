from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_repository import AgentRepository
from app.shared_assets.binding_repository import BindingRepository, BindingTarget
from app.shared_assets.contexts import (
    SystemAssetGovernanceContext,
    SystemAssetReadContext,
)
from app.shared_assets.errors import AssetNotFound, AssetResolutionUnavailable
from app.shared_assets.internal_assets import (
    BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
)
from app.shared_assets.models import AssetKind, AssetScope, AssetSelection
from app.shared_assets.resolver import ProjectAssetResolver
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="internal-skill-builder-boundary",
    )


def _builder_rows() -> tuple[AgentRow, AgentVersionRow]:
    now = datetime.now(UTC)
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset = AgentRow(
        id=asset_id,
        scope="system",
        project_id=None,
        slug="skill-builder",
        display_name="Skill Builder",
        status="active",
        current_published_version_id=version_id,
        version=1,
        source_key=BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
        created_by_user_id="system",
        created_at=now,
        updated_at=now,
    )
    version = AgentVersionRow(
        id=version_id,
        agent_id=asset_id,
        version_number=1,
        workflow_status="published",
        description="Internal Skill Builder",
        agents_instructions="Build a Skill.",
        soul="",
        identity="",
        user_context="",
        model_ref="default",
        model_settings={},
        tool_groups=[],
        supersedes_version_id=None,
        payload_schema_version=3,
        payload_checksum="a" * 64,
        created_by_user_id="system",
        created_at=now,
    )
    return asset, version


class _Result:
    def __init__(self, *, value: object | None = None, rows: tuple[object, ...] = ()) -> None:
        self.value = value
        self.rows = rows

    def scalar_one_or_none(self) -> object | None:
        return self.value

    def one_or_none(self) -> object | None:
        return self.value

    def scalars(self) -> _Result:
        return self

    def tuples(self) -> _Result:
        return self

    def all(self) -> list[object]:
        return list(self.rows)


class _QueueSession:
    def __init__(self, *results: _Result) -> None:
        self.results = list(results)
        self.execute_count = 0
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _Result:
        self.execute_count += 1
        self.statements.append(statement)
        if not self.results:
            raise AssertionError("unexpected query")
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_project_agent_catalog_omits_internal_skill_builder_defensively() -> None:
    context = _context()
    builder, _version = _builder_rows()
    ordinary = AgentRow(
        id=uuid.uuid4(),
        scope="system",
        project_id=None,
        slug="ordinary-system-agent",
        display_name="Ordinary System Agent",
        status="active",
        current_published_version_id=uuid.uuid4(),
        version=1,
        source_key="builtin:agent:ordinary-system-agent",
        created_by_user_id="system",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session = _QueueSession(
        _Result(value=context.project_id),
        _Result(rows=()),
        _Result(rows=(builder, ordinary)),
    )

    visible = await AgentRepository(session).list_project_visible(context)  # type: ignore[arg-type]

    assert visible == (ordinary,)
    system_catalog_sql = str(
        session.statements[2].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "agents.source_key" in system_catalog_sql
    assert BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY in system_catalog_sql


@pytest.mark.asyncio
async def test_project_agent_version_history_hides_internal_skill_builder() -> None:
    context = _context()
    builder, _version = _builder_rows()
    session = _QueueSession(_Result(rows=()))

    with pytest.raises(AssetNotFound):
        await AgentRepository(session).get_project_version_history(  # type: ignore[arg-type]
            context,
            builder.id,
        )

    statement_sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY in statement_sql


@pytest.mark.asyncio
async def test_authenticated_system_catalog_hides_internal_skill_builder() -> None:
    builder, _version = _builder_rows()
    ordinary = AgentRow(
        id=uuid.uuid4(),
        scope="system",
        project_id=None,
        slug="ordinary-system-agent",
        display_name="Ordinary System Agent",
        status="active",
        current_published_version_id=uuid.uuid4(),
        version=1,
        source_key="builtin:agent:ordinary-system-agent",
        created_by_user_id="system",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session = _QueueSession(_Result(rows=(builder, ordinary)))
    actor = SystemAssetReadContext(
        user_id=uuid.uuid4(),
        request_id="authenticated-system-catalog",
    )

    visible = await AgentRepository(session).list_system_visible(actor)  # type: ignore[arg-type]

    assert visible == (ordinary,)
    statement_sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY in statement_sql


@pytest.mark.asyncio
async def test_global_governance_catalog_retains_internal_skill_builder() -> None:
    builder, _version = _builder_rows()
    session = _QueueSession(_Result(rows=(builder,)))
    actor = SystemAssetGovernanceContext(
        user_id=uuid.uuid4(),
        request_id="global-governance-catalog",
    )

    visible = await AgentRepository(session).list_system_visible(actor)  # type: ignore[arg-type]

    assert visible == (builder,)
    statement_sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY not in statement_sql


@pytest.mark.asyncio
async def test_project_binding_target_hides_internal_skill_builder() -> None:
    context = _context()
    builder, version = _builder_rows()
    session = _QueueSession(_Result(value=builder))

    with pytest.raises(AssetNotFound):
        await BindingRepository(session).lock_target(
            context,
            AssetSelection(AssetKind.AGENT, builder.id, version.id),
        )

    assert session.execute_count == 1


class _BoundBuilderRepository:
    def __init__(self, builder: AgentRow, version: AgentVersionRow) -> None:
        self.target = BindingTarget(builder, version)

    async def get_binding(self, *_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(enabled=True, agent_version_id=self.target.version.id)

    async def lock_target(self, *_args: object, **_kwargs: object) -> BindingTarget:
        return self.target


@pytest.mark.asyncio
@pytest.mark.parametrize("exact_run", [False, True])
async def test_ordinary_resolver_rejects_bound_internal_skill_builder(
    exact_run: bool,
) -> None:
    context = _context()
    builder, version = _builder_rows()
    resolver = ProjectAssetResolver(lambda: None)  # type: ignore[arg-type]
    repository = _BoundBuilderRepository(builder, version)
    session = _QueueSession(_Result(), _Result())
    selection = AssetSelection(AssetKind.AGENT, builder.id, version.id)

    with pytest.raises(AssetResolutionUnavailable):
        if exact_run:
            await resolver._resolve_run_record(  # noqa: SLF001
                session,  # type: ignore[arg-type]
                repository,  # type: ignore[arg-type]
                context,
                selection,
            )
        else:
            await resolver._resolve_record(  # noqa: SLF001
                session,  # type: ignore[arg-type]
                repository,  # type: ignore[arg-type]
                context,
                selection,
            )


@pytest.mark.asyncio
async def test_main_delegate_pool_fails_closed_on_legacy_builder_binding() -> None:
    context = _context()
    builder, version = _builder_rows()
    resolver = ProjectAssetResolver(lambda: None)  # type: ignore[arg-type]
    session = _QueueSession(
        _Result(rows=()),
        _Result(rows=((builder, version),)),
    )

    with pytest.raises(AssetResolutionUnavailable):
        await resolver._main_pool_records(  # noqa: SLF001
            session,  # type: ignore[arg-type]
            context,
            AssetKind.AGENT,
        )


@pytest.mark.asyncio
async def test_internal_resolver_still_accepts_exact_skill_builder_source() -> None:
    context = _context()
    builder, version = _builder_rows()
    resolver = ProjectAssetResolver(lambda: None)  # type: ignore[arg-type]
    session = _QueueSession(_Result(value=(builder, version)))

    record = await resolver._internal_system_record(  # noqa: SLF001
        session,  # type: ignore[arg-type]
        context,
        kind=AssetKind.AGENT,
        source_key=BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
        asset_id=builder.id,
        version_id=version.id,
    )

    assert record.scope is AssetScope.SYSTEM
    assert record.asset is builder
    assert record.version is version
