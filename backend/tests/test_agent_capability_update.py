from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import agent_service as agent_service_module
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.agent_repository import AgentDefinitionRecord
from app.shared_assets.agent_service import AgentService
from app.shared_assets.models import AgentModelSettings, AgentPayload, AssetScope, SkillAssetRef
from deerflow.persistence.shared_assets import AgentRow


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=frozenset({Capability.SHARED_ASSETS_READ, Capability.SHARED_ASSETS_EDIT}),
        membership_version=1,
        request_id="skill-delete-agent-unbind",
    )


def _record(
    actor: ProjectContext,
    status: str,
    deleted_skill_id: uuid.UUID,
    retained_skill_id: uuid.UUID,
) -> AgentDefinitionRecord:
    skill_refs = (
        SkillAssetRef(AssetScope.PROJECT, deleted_skill_id),
        SkillAssetRef(AssetScope.PROJECT, retained_skill_id),
    )
    payload = AgentPayload(
        description=f"{status} Agent",
        agents_instructions="Review the task.",
        soul="Be precise.",
        identity="Reviewer",
        user_context="Use Chinese.",
        model_ref="default",
        model_settings=AgentModelSettings(),
        tool_groups=(),
        skill_refs=skill_refs,
        mcp_version_ids=(),
        payload_schema_version=4,
    )
    now = datetime.now(UTC)
    row = AgentRow(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        slug=f"{status}-agent",
        display_name=f"{status.title()} Agent",
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
        revision=7,
        source_key=None,
        created_by_user_id=str(actor.user_id),
        updated_by_user_id=str(actor.user_id),
        created_at=now,
        updated_at=now,
    )
    return AgentDefinitionRecord(row, skill_refs, ())


@pytest.mark.asyncio
async def test_skill_delete_unbinds_all_project_agent_definitions_without_changing_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context()
    deleted_skill_id = uuid.uuid4()
    retained_skill_id = uuid.uuid4()
    records = tuple(_record(actor, status, deleted_skill_id, retained_skill_id) for status in ("active", "suspended", "archived"))
    before = {record.row.id: (record.row.status, record.row.definition_id, record.row.revision) for record in records}
    replacements: list[AgentDefinitionRecord] = []

    class _Repository:
        def __init__(self, session: AsyncSession) -> None:
            self.session = session

        async def lock_project_agents_referencing_skill(
            self,
            _actor: ProjectContext,
            _skill_id: uuid.UUID,
        ) -> tuple[AgentDefinitionRecord, ...]:
            return records

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
            row.payload_checksum = payload_checksum
            row.updated_by_user_id = updated_by_user_id
            row.revision += 1
            changed = AgentDefinitionRecord(row, payload.skill_refs, payload.mcp_version_ids)
            replacements.append(changed)
            return changed

    monkeypatch.setattr(agent_service_module, "AgentRepository", _Repository)
    session = AsyncSession()
    try:
        async with session.begin():
            affected = await AgentService(lambda: session).remove_project_skill_from_definitions_in_session(
                session,
                actor,
                deleted_skill_id,
            )
    finally:
        await session.close()

    assert {item.id for item in affected} == {record.row.id for record in records}
    assert len(replacements) == 3
    for changed in replacements:
        status, old_definition_id, old_revision = before[changed.row.id]
        assert changed.row.status == status
        assert changed.row.definition_id != old_definition_id
        assert changed.row.revision == old_revision + 1
        assert changed.skill_refs == (SkillAssetRef(AssetScope.PROJECT, retained_skill_id),)
