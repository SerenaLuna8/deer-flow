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
from app.shared_assets.agent_catalog import (
    AgentCatalogValidator,
    StaticToolGroupCatalog,
    require_agent_catalog_validation,
)
from app.shared_assets.agent_repository import AgentVersionRecord
from app.shared_assets.agent_service import AgentService, CreateAgent
from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.models import AgentModelSettings, AgentPayload, WorkflowStatus
from deerflow.persistence.shared_assets import AgentVersionRow


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def begin(self):
        return self

    async def flush(self) -> None:
        return None


class _ModelCatalog:
    def __init__(self, active_refs: set[str]) -> None:
        self.active_refs = active_refs
        self.calls: list[tuple[str | None, bool]] = []

    async def resolve_active_model(
        self,
        model_ref: str | None,
        *,
        load_envelope: bool,
    ) -> object | None:
        self.calls.append((model_ref, load_envelope))
        return object() if model_ref in self.active_refs else None


def _validator(
    session: _Session,
    *,
    groups: tuple[str, ...] = ("file:read", "task"),
    active_models: set[str] | None = None,
) -> tuple[AgentCatalogValidator, _ModelCatalog]:
    models = _ModelCatalog(active_models if active_models is not None else {"default"})
    factory_sessions: list[object] = []

    def model_catalog_factory(received_session):
        factory_sessions.append(received_session)
        assert received_session is session
        return models

    validator = AgentCatalogValidator(
        StaticToolGroupCatalog(groups),
        model_catalog_factory=model_catalog_factory,
    )
    # Keep the observation list reachable without weakening the production port.
    models.factory_sessions = factory_sessions  # type: ignore[attr-defined]
    return validator, models


def _context(role: ProjectRole = ProjectRole.ADMIN) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id=f"agent-catalog-{role.value}",
    )


def _payload(
    *,
    model_ref: str = "default",
    tool_groups: tuple[str, ...] = ("file:read",),
) -> AgentPayload:
    return AgentPayload(
        description="Reviews changes",
        agents_instructions="# AGENTS\n\nReview carefully.",
        soul="# SOUL\n\nBe precise.",
        identity="# IDENTITY\n\nReviewer.",
        user_context="# USER\n\nUse Chinese.",
        model_ref=model_ref,
        model_settings=AgentModelSettings(),
        tool_groups=tool_groups,
        skill_version_ids=(),
        mcp_version_ids=(),
    )


def _asset(
    actor: ProjectContext,
    *,
    status: str,
    version: int,
    current_published_version_id: uuid.UUID | None,
):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        slug="reviewer",
        display_name="Reviewer",
        status=status,
        current_published_version_id=current_published_version_id,
        version=version,
        created_by_user_id=str(actor.user_id),
        created_at=now,
        updated_at=now,
    )


def _version(
    asset_id: uuid.UUID,
    *,
    workflow_status: WorkflowStatus,
    model_ref: str,
    tool_groups: tuple[str, ...],
    supersedes_version_id: uuid.UUID | None = None,
) -> AgentVersionRecord:
    row = AgentVersionRow(
        id=uuid.uuid4(),
        agent_id=asset_id,
        version_number=2,
        workflow_status=workflow_status.value,
        description="Reviews changes",
        agents_instructions="# AGENTS\n\nReview carefully.",
        soul="# SOUL\n\nBe precise.",
        identity="# IDENTITY\n\nReviewer.",
        user_context="# USER\n\nUse Chinese.",
        model_ref=model_ref,
        model_settings=AgentModelSettings().model_dump(exclude_none=True),
        tool_groups=list(tool_groups),
        supersedes_version_id=supersedes_version_id,
        payload_schema_version=2,
        payload_checksum="0" * 64,
        created_by_user_id=str(uuid.uuid4()),
        created_at=datetime.now(UTC),
    )
    record = AgentVersionRecord(row, (), ())
    row.payload_checksum = AgentService._payload_checksum(
        AgentPayload(
            description=row.description,
            agents_instructions=row.agents_instructions,
            soul=row.soul,
            identity=row.identity,
            user_context=row.user_context,
            model_ref=row.model_ref,
            model_settings=AgentModelSettings.model_validate(row.model_settings),
            tool_groups=tuple(row.tool_groups),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
        payload_schema_version=row.payload_schema_version,
    )
    return record


def _create_repository(session: _Session):
    return SimpleNamespace(
        session=session,
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
        create_project_asset=AsyncMock(side_effect=AssertionError("catalog validation must precede writes")),
    )


@pytest.mark.asyncio
async def test_catalog_validator_accepts_exact_groups_and_active_default_model() -> None:
    session = _Session()
    validator, models = _validator(session)

    await validator.validate(
        session,  # type: ignore[arg-type]
        request_id="catalog-valid",
        model_ref="default",
        tool_groups=("file:read", "task"),
    )

    assert models.calls == [("default", False)]
    assert models.factory_sessions == [session]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_catalog_validator_rejects_unknown_group_before_model_lookup() -> None:
    session = _Session()
    validator, models = _validator(session)

    with pytest.raises(AssetValidationFailed) as caught:
        await validator.validate(
            session,  # type: ignore[arg-type]
            request_id="catalog-unknown-group",
            model_ref="default",
            tool_groups=("file:read", "unknown"),
        )

    assert caught.value.request_id == "catalog-unknown-group"
    assert models.calls == []


@pytest.mark.asyncio
async def test_catalog_validator_rejects_inactive_or_missing_model() -> None:
    session = _Session()
    validator, models = _validator(session, active_models=set())

    with pytest.raises(AssetValidationFailed) as caught:
        await validator.validate(
            session,  # type: ignore[arg-type]
            request_id="catalog-inactive-model",
            model_ref="retired-model",
            tool_groups=("file:read",),
        )

    assert caught.value.request_id == "catalog-inactive-model"
    assert models.calls == [("retired-model", False)]


@pytest.mark.asyncio
async def test_missing_catalog_authority_fails_closed() -> None:
    with pytest.raises(AssetValidationFailed) as caught:
        await require_agent_catalog_validation(
            None,
            _Session(),  # type: ignore[arg-type]
            request_id="catalog-unwired",
            model_ref="default",
            tool_groups=("file:read",),
        )

    assert caught.value.request_id == "catalog-unwired"


def test_catalog_validator_requires_an_explicit_tool_group_catalog() -> None:
    with pytest.raises(ValueError, match="tool-group catalog is required"):
        AgentCatalogValidator(None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_complete_create_rejects_unknown_tool_group_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    actor = _context(ProjectRole.EDITOR)
    repository = _create_repository(session)
    monkeypatch.setattr(
        agent_service_module,
        "AgentRepository",
        lambda _session: repository,
    )
    validator, models = _validator(session)

    with pytest.raises(AssetValidationFailed):
        await AgentService(
            lambda: session,
            governance_sink=SimpleNamespace(append_project=AsyncMock()),
            catalog_validator=validator,
        ).create_project(
            actor,
            CreateAgent(slug="reviewer", display_name="Reviewer"),
            _payload(tool_groups=("unknown",)),
        )

    repository.create_project_asset.assert_not_awaited()
    assert models.calls == []


@pytest.mark.asyncio
async def test_builder_package_rejects_inactive_model_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    actor = _context(ProjectRole.EDITOR)
    repository = _create_repository(session)
    monkeypatch.setattr(
        agent_service_module,
        "AgentRepository",
        lambda _session: repository,
    )
    validator, models = _validator(session, active_models=set())

    with pytest.raises(AssetValidationFailed):
        await AgentService(
            lambda: session,
            governance_sink=SimpleNamespace(append_project=AsyncMock()),
            catalog_validator=validator,
        ).create_project_from_design_in_session(
            session,  # type: ignore[arg-type]
            actor,
            CreateAgent(slug="reviewer", display_name="Reviewer"),
            _payload(model_ref="retired-model"),
        )

    repository.create_project_asset.assert_not_awaited()
    assert models.calls == [("retired-model", False)]


@pytest.mark.asyncio
async def test_publish_revalidates_the_drafts_model_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    actor = _context()
    live_version_id = uuid.uuid4()
    asset = _asset(
        actor,
        status="active",
        version=8,
        current_published_version_id=live_version_id,
    )
    draft = _version(
        asset.id,
        workflow_status=WorkflowStatus.DRAFT,
        model_ref="retired-model",
        tool_groups=("file:read",),
        supersedes_version_id=live_version_id,
    )
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
    validator, models = _validator(session, active_models=set())

    with pytest.raises(AssetValidationFailed):
        await AgentService(
            lambda: session,
            governance_sink=SimpleNamespace(append_project=AsyncMock()),
            catalog_validator=validator,
        ).publish(
            actor,
            asset.id,
            draft.row.id,
            expected_asset_version=8,
        )

    assert draft.row.workflow_status == WorkflowStatus.DRAFT.value
    assert asset.current_published_version_id == live_version_id
    assert asset.version == 8
    assert models.calls == [("retired-model", False)]


@pytest.mark.asyncio
async def test_activate_revalidates_the_published_tool_group_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    actor = _context()
    published_id = uuid.uuid4()
    asset = _asset(
        actor,
        status="suspended",
        version=4,
        current_published_version_id=published_id,
    )
    published = _version(
        asset.id,
        workflow_status=WorkflowStatus.PUBLISHED,
        model_ref="default",
        tool_groups=("retired-group",),
    )
    published.row.id = published_id
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_project_version=AsyncMock(return_value=published),
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
    )
    monkeypatch.setattr(
        agent_service_module,
        "AgentRepository",
        lambda _session: repository,
    )
    validator, models = _validator(session)

    with pytest.raises(AssetValidationFailed):
        await AgentService(
            lambda: session,
            governance_sink=SimpleNamespace(append_project=AsyncMock()),
            catalog_validator=validator,
        ).activate(
            actor,
            asset.id,
            expected_asset_version=4,
        )

    assert asset.status == "suspended"
    assert asset.version == 4
    assert models.calls == []
