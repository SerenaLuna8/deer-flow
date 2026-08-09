from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import agent_service as agent_service_module
from app.shared_assets.agent_repository import AgentVersionRecord
from app.shared_assets.agent_service import (
    AgentCapabilityBindings,
    AgentService,
)
from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.models import AgentModelSettings, AgentPayload
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(
            {
                Capability.SHARED_ASSETS_READ,
                Capability.SHARED_ASSETS_EDIT,
            }
        ),
        membership_version=1,
        request_id="request-1",
    )


def _asset(context: ProjectContext, version_id: uuid.UUID) -> AgentRow:
    now = datetime.now(UTC)
    return AgentRow(
        id=uuid.uuid4(),
        scope="project",
        project_id=context.project_id,
        source_key=None,
        slug="reviewer",
        display_name="Reviewer",
        status="active",
        current_published_version_id=version_id,
        version=3,
        created_by_user_id=str(context.user_id),
        created_at=now,
        updated_at=now,
    )


def _version(asset_id: uuid.UUID, version_id: uuid.UUID) -> AgentVersionRecord:
    now = datetime.now(UTC)
    row = AgentVersionRow(
        id=version_id,
        agent_id=asset_id,
        version_number=1,
        workflow_status="published",
        description="审查代码并输出建议",
        agents_instructions="# AGENTS.md\n\nReview code.",
        soul="# SOUL.md\n\nBe precise.",
        identity="# IDENTITY.md\n\nReviewer.",
        user_context="# USER.md\n\nUse Chinese.",
        model_ref="default",
        model_settings=AgentModelSettings().model_dump(exclude_none=True),
        tool_groups=["web", "file:read", "task"],
        supersedes_version_id=None,
        payload_schema_version=2,
        payload_checksum="a" * 64,
        created_by_user_id="creator",
        created_at=now,
    )
    return AgentVersionRecord(row, (uuid.uuid4(),), ())


@pytest.mark.asyncio
async def test_update_capability_bindings_publishes_a_replacement_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    current_version_id = uuid.uuid4()
    asset = _asset(context, current_version_id)
    current = _version(asset.id, current_version_id)
    selected_skills = (uuid.uuid4(), uuid.uuid4())
    selected_mcps = (uuid.uuid4(),)
    created: list[AgentVersionRecord] = []
    audit_actions: list[str] = []

    class _Session:
        async def flush(self) -> None:
            return None

    class _Repository:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get_project_asset(self, *_args, **_kwargs) -> AgentRow:
            return asset

        async def get_project_version(
            self,
            _actor: object,
            _asset_id: uuid.UUID,
            version_id: uuid.UUID,
            **_kwargs,
        ) -> AgentVersionRecord:
            assert version_id == current_version_id
            return current

        async def resolve_project_skill_versions(self, *_args) -> tuple[uuid.UUID, ...]:
            return selected_skills

        async def resolve_project_mcp_versions(self, *_args) -> tuple[uuid.UUID, ...]:
            return selected_mcps

        async def lock_skill_version_slugs(self, values) -> tuple[str, ...]:
            assert tuple(values) == selected_skills
            return ("first-skill", "second-skill")

        async def next_project_version_number(self, *_args) -> int:
            return 2

        async def create_project_version(
            self,
            _actor: object,
            asset_id: uuid.UUID,
            row: AgentVersionRow,
            skill_version_ids,
            mcp_version_ids,
        ) -> AgentVersionRecord:
            assert asset_id == asset.id
            row.id = uuid.uuid4()
            record = AgentVersionRecord(
                row,
                tuple(skill_version_ids),
                tuple(mcp_version_ids),
            )
            created.append(record)
            return record

    class _SessionContext:
        async def __aenter__(self) -> _Session:
            return _Session()

        async def __aexit__(self, *_args) -> None:
            return None

    class _Transaction:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args) -> None:
            return None

    class _SessionWithBegin(_Session):
        def begin(self) -> _Transaction:
            return _Transaction()

    class _FactoryContext:
        async def __aenter__(self) -> _SessionWithBegin:
            return _SessionWithBegin()

        async def __aexit__(self, *_args) -> None:
            return None

    class _AuditSink:
        async def append_project(self, _session, **kwargs) -> None:
            audit_actions.append(kwargs["action"])

    monkeypatch.setattr(agent_service_module, "AgentRepository", _Repository)
    service = AgentService(lambda: _FactoryContext(), governance_sink=_AuditSink())  # type: ignore[arg-type]

    result = await service.update_capability_bindings(
        context,
        asset.id,
        AgentCapabilityBindings(selected_skills, selected_mcps),
        expected_asset_version=3,
    )

    assert result.version_number == 2
    assert result.workflow_status.value == "published"
    assert result.skill_version_ids == selected_skills
    assert result.mcp_version_ids == selected_mcps
    assert result.agents_instructions == current.row.agents_instructions
    assert result.soul == current.row.soul
    assert result.identity == current.row.identity
    assert result.user_context == current.row.user_context
    assert result.tool_groups == tuple(current.row.tool_groups)
    assert len(created) == 1
    assert created[0].row.supersedes_version_id == current_version_id
    assert asset.current_published_version_id == created[0].row.id
    assert asset.version == 4
    assert audit_actions == ["agent.capability_bindings.update"]


@pytest.mark.asyncio
async def test_update_capability_bindings_rejects_out_of_scope_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    current_version_id = uuid.uuid4()
    asset = _asset(context, current_version_id)
    current = _version(asset.id, current_version_id)
    selected_skill = uuid.uuid4()

    class _Session:
        async def flush(self) -> None:
            return None

        def begin(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

    class _Repository:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get_project_asset(self, *_args, **_kwargs) -> AgentRow:
            return asset

        async def get_project_version(self, *_args, **_kwargs) -> AgentVersionRecord:
            return current

        async def resolve_project_skill_versions(self, *_args) -> tuple[uuid.UUID, ...]:
            return ()

        async def resolve_project_mcp_versions(self, *_args) -> tuple[uuid.UUID, ...]:
            return ()

    monkeypatch.setattr(agent_service_module, "AgentRepository", _Repository)
    service = AgentService(lambda: _Session())  # type: ignore[arg-type]

    with pytest.raises(AssetValidationFailed):
        await service.update_capability_bindings(
            context,
            asset.id,
            AgentCapabilityBindings((selected_skill,), ()),
            expected_asset_version=3,
        )


@pytest.mark.asyncio
async def test_restore_version_publishes_a_new_copy_without_rewriting_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    current_version_id = uuid.uuid4()
    target_version_id = uuid.uuid4()
    asset = _asset(context, current_version_id)
    asset.version = 7
    target = _version(asset.id, target_version_id)
    target.row.version_number = 2
    target.row.description = "历史版本"
    target.row.payload_checksum = AgentService._payload_checksum(  # noqa: SLF001 - focused immutable-version contract
        AgentPayload(
            description=target.row.description,
            soul=target.row.soul,
            model_ref=target.row.model_ref,
            tool_groups=tuple(target.row.tool_groups),
            skill_version_ids=target.skill_version_ids,
            mcp_version_ids=target.mcp_version_ids,
            agents_instructions=target.row.agents_instructions,
            identity=target.row.identity,
            user_context=target.row.user_context,
            payload_schema_version=target.row.payload_schema_version,
            model_settings=AgentModelSettings(),
        ),
        payload_schema_version=target.row.payload_schema_version,
    )
    created: list[AgentVersionRecord] = []

    class _Session:
        async def flush(self) -> None:
            return None

        def begin(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

    class _Repository:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get_project_asset(self, *_args, **_kwargs) -> AgentRow:
            return asset

        async def get_project_version(
            self,
            _actor: object,
            _asset_id: uuid.UUID,
            version_id: uuid.UUID,
            **_kwargs,
        ) -> AgentVersionRecord:
            assert version_id == target_version_id
            return target

        async def resolve_project_skill_versions(self, *_args) -> tuple[uuid.UUID, ...]:
            return target.skill_version_ids

        async def resolve_project_mcp_versions(self, *_args) -> tuple[uuid.UUID, ...]:
            return target.mcp_version_ids

        async def lock_skill_version_slugs(self, _values) -> tuple[str, ...]:
            return ("restored-skill",)

        async def next_project_version_number(self, *_args) -> int:
            return 5

        async def create_project_version(
            self,
            _actor: object,
            _asset_id: uuid.UUID,
            row: AgentVersionRow,
            skill_version_ids,
            mcp_version_ids,
        ) -> AgentVersionRecord:
            row.id = uuid.uuid4()
            record = AgentVersionRecord(
                row,
                tuple(skill_version_ids),
                tuple(mcp_version_ids),
            )
            created.append(record)
            return record

    monkeypatch.setattr(agent_service_module, "AgentRepository", _Repository)
    service = AgentService(lambda: _Session())  # type: ignore[arg-type]

    restored = await service.restore_version(
        context,
        asset.id,
        target_version_id,
        expected_asset_version=7,
    )

    assert restored.version_number == 5
    assert restored.workflow_status.value == "published"
    assert restored.description == "历史版本"
    assert restored.skill_version_ids == target.skill_version_ids
    assert restored.mcp_version_ids == target.mcp_version_ids
    assert created[0].row.supersedes_version_id == current_version_id
    assert target.row.version_number == 2
    assert target.row.workflow_status == "published"
    assert asset.current_published_version_id == created[0].row.id
    assert asset.version == 8
