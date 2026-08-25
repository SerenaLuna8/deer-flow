from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import agent_service as agent_service_module
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.agent_repository import AgentDefinitionRecord, AgentRepository
from app.shared_assets.agent_service import AgentInstructions, AgentService
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetForbidden, AssetNotFound
from app.shared_assets.models import AgentModelSettings, AgentPayload
from deerflow.persistence.shared_assets import AgentRow


def _context(role: ProjectRole = ProjectRole.EDITOR) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id=f"agent-definition-{role.value}",
    )


def _record(actor: ProjectContext, *, status: str = "active") -> AgentDefinitionRecord:
    payload = AgentPayload(
        description="Reviews changes",
        agents_instructions="Review carefully.",
        soul="Be precise.",
        identity="Reviewer",
        user_context="Use Chinese.",
        model_ref="default",
        model_settings=AgentModelSettings(),
        tool_groups=(),
        skill_refs=(),
        mcp_version_ids=(),
        payload_schema_version=4,
    )
    now = datetime.now(UTC)
    row = AgentRow(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        slug="reviewer",
        display_name="Reviewer",
        status=status,
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
        revision=3,
        source_key=None,
        created_by_user_id=str(actor.user_id),
        updated_by_user_id=str(actor.user_id),
        created_at=now,
        updated_at=now,
    )
    return AgentDefinitionRecord(row, (), ())


class _AcceptingCatalogValidator:
    async def validate(self, *_args: object, **_kwargs: object) -> None:
        return None


def test_project_agent_service_has_no_version_or_activation_commands() -> None:
    assert not hasattr(AgentService, "create_version")
    assert not hasattr(AgentService, "get_version_history")
    assert not hasattr(AgentService, "activate_version")


@pytest.mark.asyncio
async def test_dependency_repository_rejects_wrong_context_before_empty_shortcuts() -> None:
    repository = AgentRepository(object())  # type: ignore[arg-type]
    invalid_context = object()

    with pytest.raises(AssetForbidden):
        await repository.resolve_project_mcp_versions(  # type: ignore[arg-type]
            invalid_context,
            (),
        )
    with pytest.raises(AssetForbidden):
        await repository.resolve_system_skill_refs(  # type: ignore[arg-type]
            invalid_context,
            (),
            require_runnable=False,
        )
    with pytest.raises(AssetForbidden):
        await repository.resolve_system_mcp_versions(  # type: ignore[arg-type]
            invalid_context,
            (),
        )
    with pytest.raises(AssetForbidden):
        await repository.archive_project_asset(  # type: ignore[arg-type]
            invalid_context,
            object(),  # type: ignore[arg-type]
        )

    project_override = SystemAssetGovernanceContext(
        user_id=uuid.uuid4(),
        request_id="agent-system-resolution-project-override",
        project_id=uuid.uuid4(),
    )
    with pytest.raises(AssetNotFound):
        await repository.resolve_system_skill_refs(
            project_override,
            (),
            require_runnable=False,
        )
    with pytest.raises(AssetNotFound):
        await repository.resolve_system_mcp_versions(project_override, ())


@pytest.mark.asyncio
async def test_instruction_save_replaces_definition_and_immediately_advances_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context()
    current = _record(actor)
    old_definition_id = current.row.definition_id
    old_revision = current.row.revision

    class _Repository:
        def __init__(self, session: AsyncSession) -> None:
            self.session = session

        async def get_project_asset(self, *_args: object, **_kwargs: object) -> AgentRow:
            return current.row

        async def get_definition(self, *_args: object, **_kwargs: object) -> AgentDefinitionRecord:
            return current

        async def resolve_project_skill_refs(self, *_args: object, **_kwargs: object) -> tuple[object, ...]:
            return ()

        async def resolve_project_mcp_versions(self, *_args: object, **_kwargs: object) -> tuple[uuid.UUID, ...]:
            return ()

        async def lock_skill_asset_slugs(self, *_args: object, **_kwargs: object) -> tuple[str, ...]:
            return ()

        async def replace_definition(
            self,
            row: AgentRow,
            payload: AgentPayload,
            *,
            definition_id: uuid.UUID,
            payload_checksum: str,
            updated_by_user_id: str,
        ) -> AgentDefinitionRecord:
            row.definition_id = definition_id
            row.agents_instructions = payload.agents_instructions
            row.soul = payload.soul
            row.identity = payload.identity
            row.user_context = payload.user_context
            row.payload_checksum = payload_checksum
            row.updated_by_user_id = updated_by_user_id
            row.revision += 1
            return AgentDefinitionRecord(row, payload.skill_refs, payload.mcp_version_ids)

    monkeypatch.setattr(agent_service_module, "AgentRepository", _Repository)
    service = AgentService(
        AsyncSession,
        catalog_validator=_AcceptingCatalogValidator(),
    )

    result = await service.update_instructions(
        actor,
        current.row.id,
        AgentInstructions(
            agents_instructions="Use the saved Definition for every new Run.",
            soul="Stay precise.",
            identity="Definition reviewer",
            user_context="Reply in Chinese.",
        ),
        expected_asset_version=old_revision,
    )

    assert result.asset.status == "active"
    assert result.asset.revision == old_revision + 1
    assert result.asset.definition_id != old_definition_id
    assert result.definition.definition_id == result.asset.definition_id
    assert result.definition.agents_instructions == "Use the saved Definition for every new Run."


@pytest.mark.asyncio
async def test_lifecycle_status_change_does_not_rotate_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context(ProjectRole.ADMIN)
    current = _record(actor)
    definition_id = current.row.definition_id
    revision = current.row.revision

    class _Repository:
        def __init__(self, session: AsyncSession) -> None:
            self.session = session

        async def ensure_not_current_project_default(self, *_args: object) -> None:
            return None

        async def get_project_asset(self, *_args: object, **_kwargs: object) -> AgentRow:
            return current.row

    monkeypatch.setattr(agent_service_module, "AgentRepository", _Repository)

    result = await AgentService(AsyncSession).suspend(
        actor,
        current.row.id,
        expected_asset_version=revision,
    )

    assert result.status == "suspended"
    assert result.revision == revision + 1
    assert result.definition_id == definition_id
