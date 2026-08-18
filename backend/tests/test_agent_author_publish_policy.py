from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import agent_service as agent_service_module
from app.shared_assets.agent_repository import AgentVersionRecord
from app.shared_assets.agent_service import AgentInstructions, AgentService, CreateAgent
from app.shared_assets.errors import AssetConflict, AssetForbidden
from app.shared_assets.models import AgentModelSettings, AgentPayload, WorkflowStatus
from deerflow.persistence.shared_assets import AgentVersionRow


def _context(role: ProjectRole) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id=f"agent-policy-{role.value}",
    )


def _version(
    asset_id: uuid.UUID,
    *,
    workflow_status: WorkflowStatus,
    version_number: int = 1,
) -> AgentVersionRecord:
    row = AgentVersionRow(
        id=uuid.uuid4(),
        agent_id=asset_id,
        version_number=version_number,
        workflow_status=workflow_status.value,
        description="Review changes",
        agents_instructions="# AGENTS\n\nReview carefully.",
        soul="# SOUL\n\nBe precise.",
        identity="# IDENTITY\n\nReviewer.",
        user_context="# USER\n\nUse Chinese.",
        model_ref="default",
        model_settings=AgentModelSettings().model_dump(exclude_none=True),
        tool_groups=["file:read"],
        supersedes_version_id=None,
        payload_schema_version=2,
        payload_checksum="a" * 64,
        created_by_user_id=str(uuid.uuid4()),
        created_at=datetime.now(UTC),
    )
    return AgentVersionRecord(row, (), ())


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def begin(self):
        return self

    async def flush(self) -> None:
        return None


def _catalog_validator() -> SimpleNamespace:
    return SimpleNamespace(validate=AsyncMock())


@pytest.mark.asyncio
async def test_editor_instruction_update_creates_draft_without_moving_live_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context(ProjectRole.EDITOR)
    asset_id = uuid.uuid4()
    current = _version(asset_id, workflow_status=WorkflowStatus.PUBLISHED)
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        project_id=actor.project_id,
        status="active",
        current_published_version_id=current.row.id,
        version=7,
    )
    session = _Session()
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_project_version=AsyncMock(return_value=current),
        get_latest_project_version=AsyncMock(return_value=current),
        next_project_version_number=AsyncMock(return_value=2),
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
    )

    async def create_version(_actor, _asset_id, row, skill_ids, mcp_ids):
        row.id = uuid.uuid4()
        row.created_at = datetime.now(UTC)
        return AgentVersionRecord(row, tuple(skill_ids), tuple(mcp_ids))

    repository.create_project_version = AsyncMock(side_effect=create_version)
    monkeypatch.setattr(
        agent_service_module,
        "AgentRepository",
        lambda _session: repository,
    )

    result = await AgentService(
        lambda: session,
        governance_sink=SimpleNamespace(append_project=AsyncMock()),
        catalog_validator=_catalog_validator(),
    ).update_instructions(
        actor,
        asset_id,
        AgentInstructions(
            agents_instructions="# AGENTS\n\nNew rules.",
            soul="# SOUL\n\nNew soul.",
            identity="# IDENTITY\n\nNew identity.",
            user_context="# USER\n\nNew context.",
        ),
        expected_asset_version=7,
    )

    assert result.workflow_status is WorkflowStatus.DRAFT
    assert asset.current_published_version_id == current.row.id
    assert asset.version == 8


@pytest.mark.asyncio
async def test_editor_cannot_publish_agent_version() -> None:
    class _ExplodingFactory:
        def __call__(self):
            raise AssertionError("authorization must fail before storage is opened")

    with pytest.raises(AssetForbidden):
        await AgentService(_ExplodingFactory()).publish(
            _context(ProjectRole.EDITOR),
            uuid.uuid4(),
            uuid.uuid4(),
            expected_asset_version=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["suspend", "activate"])
async def test_editor_cannot_change_agent_runtime_lifecycle(operation: str) -> None:
    class _ExplodingFactory:
        def __call__(self):
            raise AssertionError("authorization must fail before storage is opened")

    with pytest.raises(AssetForbidden):
        await getattr(AgentService(_ExplodingFactory()), operation)(
            _context(ProjectRole.EDITOR),
            uuid.uuid4(),
            expected_asset_version=1,
        )


@pytest.mark.asyncio
async def test_editing_an_unpublished_agent_keeps_draft_publishable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context(ProjectRole.EDITOR)
    asset_id = uuid.uuid4()
    first_draft = _version(asset_id, workflow_status=WorkflowStatus.DRAFT)
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        project_id=actor.project_id,
        status="suspended",
        current_published_version_id=None,
        version=2,
    )
    session = _Session()
    created: list[AgentVersionRecord] = []
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_latest_project_version=AsyncMock(return_value=first_draft),
        next_project_version_number=AsyncMock(return_value=2),
    )

    async def create_version(_actor, _asset_id, row, skill_ids, mcp_ids):
        row.id = uuid.uuid4()
        row.created_at = datetime.now(UTC)
        record = AgentVersionRecord(row, tuple(skill_ids), tuple(mcp_ids))
        created.append(record)
        return record

    repository.create_project_version = AsyncMock(side_effect=create_version)
    monkeypatch.setattr(
        agent_service_module,
        "AgentRepository",
        lambda _session: repository,
    )

    await AgentService(
        lambda: session,
        governance_sink=SimpleNamespace(append_project=AsyncMock()),
        catalog_validator=_catalog_validator(),
    ).update_instructions(
        actor,
        asset_id,
        AgentInstructions(
            agents_instructions="# AGENTS\n\nSecond draft.",
            soul="# SOUL\n\nSecond draft.",
            identity="# IDENTITY\n\nSecond draft.",
            user_context="# USER\n\nSecond draft.",
        ),
        expected_asset_version=2,
    )

    assert len(created) == 1
    assert created[0].row.workflow_status == WorkflowStatus.DRAFT.value
    assert created[0].row.supersedes_version_id is None
    assert asset.current_published_version_id is None


def _set_valid_checksum(record: AgentVersionRecord) -> None:
    payload = AgentPayload(
        description=record.row.description,
        agents_instructions=record.row.agents_instructions,
        soul=record.row.soul,
        identity=record.row.identity,
        user_context=record.row.user_context,
        model_ref=record.row.model_ref,
        model_settings=AgentModelSettings.model_validate(record.row.model_settings),
        tool_groups=tuple(record.row.tool_groups),
        skill_version_ids=record.skill_version_ids,
        mcp_version_ids=record.mcp_version_ids,
    )
    record.row.payload_checksum = AgentService._payload_checksum(
        payload,
        payload_schema_version=record.row.payload_schema_version,
    )


@pytest.mark.asyncio
async def test_admin_publishes_only_a_draft_based_on_the_live_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context(ProjectRole.ADMIN)
    asset_id = uuid.uuid4()
    live_version_id = uuid.uuid4()
    draft = _version(asset_id, workflow_status=WorkflowStatus.DRAFT, version_number=2)
    draft.row.supersedes_version_id = live_version_id
    _set_valid_checksum(draft)
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        project_id=actor.project_id,
        status="active",
        current_published_version_id=live_version_id,
        version=5,
    )
    session = _Session()
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_project_version=AsyncMock(return_value=draft),
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
    )
    monkeypatch.setattr(
        agent_service_module,
        "AgentRepository",
        lambda _session: repository,
    )

    result = await AgentService(
        lambda: session,
        governance_sink=SimpleNamespace(append_project=AsyncMock()),
        catalog_validator=_catalog_validator(),
    ).publish(
        actor,
        asset_id,
        draft.row.id,
        expected_asset_version=5,
    )

    assert result.id == draft.row.id
    assert result.workflow_status is WorkflowStatus.PUBLISHED
    assert asset.current_published_version_id == draft.row.id
    assert asset.version == 6

    stale = _version(asset_id, workflow_status=WorkflowStatus.DRAFT, version_number=3)
    stale.row.supersedes_version_id = live_version_id
    _set_valid_checksum(stale)
    repository.get_project_version = AsyncMock(return_value=stale)

    with pytest.raises(AssetConflict):
        await AgentService(
            lambda: session,
            catalog_validator=_catalog_validator(),
        ).publish(
            actor,
            asset_id,
            stale.row.id,
            expected_asset_version=6,
        )


@pytest.mark.asyncio
async def test_complete_authoring_create_is_suspended_with_draft_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context(ProjectRole.EDITOR)
    now = datetime.now(UTC)
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        slug="reviewer",
        display_name="Reviewer",
        status="active",
        current_published_version_id=None,
        version=1,
        created_by_user_id=str(actor.user_id),
        created_at=now,
        updated_at=now,
    )
    session = _Session()

    async def create_version(_actor, _asset_id, row, skill_ids, mcp_ids):
        row.id = uuid.uuid4()
        row.created_at = now
        return AgentVersionRecord(row, tuple(skill_ids), tuple(mcp_ids))

    repository = SimpleNamespace(
        session=session,
        create_project_asset=AsyncMock(return_value=asset),
        create_project_version=AsyncMock(side_effect=create_version),
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
    )
    monkeypatch.setattr(
        agent_service_module,
        "AgentRepository",
        lambda _session: repository,
    )
    audit = AsyncMock()

    result = await AgentService(
        lambda: session,
        governance_sink=SimpleNamespace(append_project=audit),
        catalog_validator=_catalog_validator(),
    ).create_project_from_design_in_session(
        session,
        actor,
        CreateAgent(slug="reviewer", display_name="Reviewer"),
        AgentPayload(
            description="Reviews changes",
            agents_instructions="# AGENTS\n\nReview carefully.",
            soul="# SOUL\n\nBe precise.",
            identity="# IDENTITY\n\nReviewer.",
            user_context="# USER\n\nUse Chinese.",
            model_ref="default",
            tool_groups=("file:read",),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
    )

    assert result.asset.status == "suspended"
    assert result.asset.current_published_version_id is None
    assert result.asset.version == 2
    assert result.version.version_number == 1
    assert result.version.workflow_status is WorkflowStatus.DRAFT
    assert [call.kwargs["action"] for call in audit.await_args_list] == [
        "agent.create",
        "agent.version.create",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        (ProjectRole.EDITOR, False),
        (ProjectRole.ADMIN, True),
    ],
)
async def test_published_agent_delete_requires_publisher_capability(
    monkeypatch: pytest.MonkeyPatch,
    role: ProjectRole,
    allowed: bool,
) -> None:
    actor = _context(role)
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        version=4,
        current_published_version_id=uuid.uuid4(),
    )
    session = _Session()
    repository = SimpleNamespace(
        clear_current_project_default=AsyncMock(return_value=False),
        get_project_asset=AsyncMock(return_value=asset),
        archive_project_asset=AsyncMock(),
    )
    monkeypatch.setattr(
        agent_service_module,
        "AgentRepository",
        lambda _session: repository,
    )
    service = AgentService(
        lambda: session,
        governance_sink=SimpleNamespace(append_project=AsyncMock()),
    )

    if not allowed:
        with pytest.raises(AssetForbidden):
            await service.delete(actor, asset.id, expected_asset_version=4)
        repository.archive_project_asset.assert_not_awaited()
        return

    await service.delete(actor, asset.id, expected_asset_version=4)
    repository.clear_current_project_default.assert_awaited_once_with(
        actor,
        asset.id,
    )
    repository.archive_project_asset.assert_awaited_once_with(actor, asset)


@pytest.mark.asyncio
async def test_default_agent_archive_audits_pointer_clear_and_delete_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context(ProjectRole.ADMIN)
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        version=3,
        current_published_version_id=uuid.uuid4(),
    )
    session = _Session()
    repository = SimpleNamespace(
        clear_current_project_default=AsyncMock(return_value=True),
        get_project_asset=AsyncMock(return_value=asset),
        archive_project_asset=AsyncMock(),
    )
    monkeypatch.setattr(
        agent_service_module,
        "AgentRepository",
        lambda _session: repository,
    )
    audit = AsyncMock()

    await AgentService(
        lambda: session,
        governance_sink=SimpleNamespace(append_project=audit),
    ).delete(
        actor,
        asset.id,
        expected_asset_version=3,
    )

    assert [call.kwargs["action"] for call in audit.await_args_list] == [
        "agent.default.clear",
        "agent.delete",
    ]
    assert all(call.args == (session,) for call in audit.await_args_list)
