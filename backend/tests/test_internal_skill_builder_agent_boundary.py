from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.agent_repository import AgentRepository
from app.shared_assets.binding_repository import BindingRepository
from app.shared_assets.catalog_provider import PostgresAssetCatalogProvider
from app.shared_assets.contexts import SystemAssetReadContext
from app.shared_assets.errors import AssetNotFound
from app.shared_assets.internal_assets import BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY
from app.shared_assets.models import AgentModelSettings, AgentPayload, AssetKind, AssetSelection
from app.shared_assets.resolver import ProjectAssetResolver
from deerflow.persistence.shared_assets import AgentRow


class _Scalars:
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return list(self._values)


class _Result:
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = values

    def scalars(self) -> _Scalars:
        return _Scalars(self._values)

    def scalar_one_or_none(self) -> object | None:
        return self._values[0] if self._values else None


class _QueueSession:
    def __init__(self, *results: _Result) -> None:
        self._results = list(results)

    async def execute(self, _statement) -> _Result:
        return self._results.pop(0)


def _builder() -> AgentRow:
    payload = AgentPayload(
        description="Internal Skill Builder",
        agents_instructions="Build a Skill.",
        soul="Be exact.",
        identity="Builder",
        user_context="Internal only.",
        model_ref="default",
        model_settings=AgentModelSettings(),
        tool_groups=(),
        skill_refs=(),
        mcp_version_ids=(),
        payload_schema_version=4,
    )
    now = datetime.now(UTC)
    return AgentRow(
        id=uuid.uuid4(),
        scope="system",
        project_id=None,
        slug="skill-builder",
        display_name="Skill Builder",
        status="active",
        definition_id=uuid.uuid4(),
        description=payload.description,
        agents_instructions=payload.agents_instructions,
        soul=payload.soul,
        identity=payload.identity,
        user_context=payload.user_context,
        model_ref=payload.model_ref,
        model_settings=payload.model_settings.model_dump(exclude_none=True),
        tool_groups=list(payload.tool_groups),
        payload_schema_version=4,
        payload_checksum=agent_payload_checksum(payload),
        revision=1,
        source_key=BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
        created_by_user_id=str(uuid.uuid4()),
        updated_by_user_id=str(uuid.uuid4()),
        created_at=now,
        updated_at=now,
    )


def _context() -> ProjectContext:
    role = ProjectRole.ADMIN
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=frozenset((*Capability,)),
        membership_version=1,
        request_id="internal-skill-builder-boundary",
    )


@pytest.mark.asyncio
async def test_system_agent_catalog_hides_internal_skill_builder_defensively() -> None:
    builder = _builder()
    actor = SystemAssetReadContext(user_id=uuid.uuid4(), request_id="system-catalog")
    repository = AgentRepository(_QueueSession(_Result((builder,))))  # type: ignore[arg-type]

    visible = await repository.list_system_visible(actor)

    assert visible == ()


@pytest.mark.asyncio
async def test_runtime_catalog_hides_internal_skill_builder_defensively() -> None:
    builder = _builder()

    loaded = await PostgresAssetCatalogProvider._load_agents(  # noqa: SLF001
        _QueueSession(_Result((builder,))),  # type: ignore[arg-type]
        1,
    )

    assert loaded == ()


@pytest.mark.asyncio
async def test_project_binding_cannot_expose_internal_skill_builder() -> None:
    builder = _builder()
    context = _context()
    repository = BindingRepository(_QueueSession(_Result((builder,))))  # type: ignore[arg-type]

    with pytest.raises(AssetNotFound):
        await repository.lock_target(
            context,
            AssetSelection(AssetKind.AGENT, builder.id, builder.definition_id),
        )


@pytest.mark.asyncio
async def test_internal_resolver_accepts_exact_builder_definition_identity() -> None:
    builder = _builder()
    context = _context()

    record = await ProjectAssetResolver._internal_system_record(  # noqa: SLF001
        _QueueSession(_Result((builder,))),  # type: ignore[arg-type]
        context,
        kind=AssetKind.AGENT,
        source_key=BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
        asset_id=builder.id,
        version_id=builder.definition_id,
    )

    assert record.asset is builder
    assert record.version is builder
    assert record.version_id == builder.definition_id
