from __future__ import annotations

import dataclasses
import importlib
import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetConflict, AssetStorageUnavailable, AssetValidationFailed
from app.shared_assets.models import AgentPayload
from deerflow.persistence.shared_assets import AgentVersionRow


def _editor_context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="req-agent-unit",
    )


def _admin_context() -> ProjectContext:
    context = _editor_context()
    return dataclasses.replace(
        context,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
    )


@pytest.mark.asyncio
async def test_project_agent_governance_uses_transactional_project_audit_port() -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    sink = SimpleNamespace(append_project=AsyncMock())
    service = service_module.AgentService(lambda: None, governance_sink=sink)
    context = _editor_context()
    session = object()
    asset_id = uuid.uuid4()

    await service._record_governance(
        session,
        context,
        asset_id,
        None,
        "agent.create",
    )

    sink.append_project.assert_awaited_once_with(
        session,
        actor=context.user_id,
        project_id=context.project_id,
        asset_id=asset_id,
        version_id=None,
        action="agent.create",
        request_id=context.request_id,
        asset_kind="agent",
    )


@pytest.mark.asyncio
async def test_agent_list_includes_current_published_description() -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    context = _editor_context()
    asset_id = uuid.uuid4()
    row = SimpleNamespace(
        id=asset_id,
        scope="project",
        project_id=context.project_id,
        slug="test-agent",
        display_name="Test Agent",
        status="suspended",
        current_published_version_id=uuid.uuid4(),
        version=1,
        created_by_user_id=str(context.user_id),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    repository = AsyncMock()
    repository.list_project_visible.return_value = (row,)
    repository.current_published_descriptions.return_value = {
        asset_id: "Generate and execute focused regression tests.",
    }
    service = service_module.AgentService(lambda: None)

    async def execute(actor, operation):
        assert actor is context
        return await operation(repository)

    service._execute = execute

    result = await service.list_visible(context)

    assert result[0].description == ("Generate and execute focused regression tests.")
    repository.current_published_descriptions.assert_awaited_once_with(
        (asset_id,),
    )


def test_agent_service_exposes_frozen_typed_contracts_and_scoped_repository_api() -> None:
    package = importlib.import_module("app.shared_assets")
    service_module = importlib.import_module("app.shared_assets.agent_service")
    repository_module = importlib.import_module("app.shared_assets.agent_repository")

    assert package.AgentService is service_module.AgentService
    assert package.CreateAgent is service_module.CreateAgent
    assert package.AgentAssetView is service_module.AgentAssetView
    assert package.AgentVersionView is service_module.AgentVersionView

    create = service_module.CreateAgent(slug="analyst", display_name="Analyst")
    assert dataclasses.is_dataclass(create)
    with pytest.raises(dataclasses.FrozenInstanceError):
        create.slug = "changed"

    for view_type in (service_module.AgentAssetView, service_module.AgentVersionView):
        assert dataclasses.is_dataclass(view_type)
        assert view_type.__dataclass_params__.frozen is True

    public_methods = inspect.getmembers(repository_module.AgentRepository, predicate=inspect.isfunction)
    for name, method in public_methods:
        if name.startswith("_"):
            continue
        assert "project_id" not in inspect.signature(method).parameters, name

    project_get = inspect.signature(repository_module.AgentRepository.get_project_asset)
    assert list(project_get.parameters) == ["self", "context", "asset_id", "for_update"]


@pytest.mark.asyncio
async def test_invalid_agent_command_is_rejected_before_storage_is_opened() -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")

    class ExplodingSessionFactory:
        def __call__(self):
            raise AssertionError("invalid input must not open a database session")

    service = service_module.AgentService(ExplodingSessionFactory())
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service.create_asset(
            _editor_context(),
            service_module.CreateAgent(slug="Not Valid", display_name="Analyst"),
        )
    assert exc_info.value.request_id == "req-agent-unit"


@pytest.mark.asyncio
async def test_oversized_agent_model_ref_is_rejected_before_storage_is_opened() -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")

    class ExplodingSessionFactory:
        def __call__(self):
            raise AssertionError("invalid input must not open a database session")

    payload = AgentPayload(
        description="",
        soul="Stay within the schema.",
        model_ref="m" * 256,
        tool_groups=(),
        skill_version_ids=(),
        mcp_version_ids=(),
    )
    service = service_module.AgentService(ExplodingSessionFactory())
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service.create_version(
            _editor_context(),
            uuid.uuid4(),
            payload,
            expected_asset_version=1,
        )
    assert exc_info.value.request_id == "req-agent-unit"


@pytest.mark.asyncio
async def test_agent_dependency_closure_rejects_duplicate_skill_slugs() -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    actor = _editor_context()
    system_version_id = uuid.uuid4()
    project_version_id = uuid.uuid4()
    repository = AsyncMock()
    repository.resolve_project_skill_versions.return_value = (
        system_version_id,
        project_version_id,
    )
    repository.resolve_project_mcp_versions.return_value = ()
    repository.lock_skill_version_slugs.return_value = (
        "duplicate-skill",
        "duplicate-skill",
    )

    with pytest.raises(AssetValidationFailed) as exc_info:
        await service_module.AgentService(lambda: None)._validate_dependency_closure(
            repository,
            actor,
            (system_version_id, project_version_id),
            (),
        )

    assert exc_info.value.request_id == actor.request_id
    repository.lock_skill_version_slugs.assert_awaited_once_with(
        (system_version_id, project_version_id),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constraint_name", "error_type"),
    [
        ("uq_agents_project_slug", AssetConflict),
        ("uq_agent_versions_asset_number", AssetConflict),
        ("ck_agent_versions_checksum", AssetStorageUnavailable),
        (None, AssetStorageUnavailable),
    ],
)
async def test_agent_integrity_errors_only_map_known_business_conflicts_to_409(
    constraint_name: str | None,
    error_type: type[Exception],
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")

    class EmptySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def begin(self):
            return self

    class ConstraintViolation(Exception):
        def __init__(self, name: str | None):
            self.constraint_name = name

    async def fail_with_integrity_error(_repository):
        raise IntegrityError(
            "sensitive SQL must not escape",
            {"secret": "hidden"},
            ConstraintViolation(constraint_name),
        )

    service = service_module.AgentService(EmptySession)
    with pytest.raises(error_type) as exc_info:
        await service._execute(_editor_context(), fail_with_integrity_error)
    assert "sensitive SQL" not in str(exc_info.value)
    assert "hidden" not in str(exc_info.value)


class _UnitSession:
    def __init__(self) -> None:
        self.flush = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return self


@pytest.mark.asyncio
async def test_design_commit_boundary_creates_complete_suspended_published_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    actor = _editor_context()
    session = _UnitSession()
    now = datetime.now(UTC)
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        slug="code-test",
        display_name="code-test",
        status="active",
        current_published_version_id=None,
        version=1,
        source_key=None,
        created_by_user_id=str(actor.user_id),
        created_at=now,
        updated_at=now,
    )
    repository = SimpleNamespace(
        session=session,
        create_project_asset=AsyncMock(return_value=asset),
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
    )

    repository_workflow_statuses = []

    async def create_version(_actor, _asset_id, row, skill_ids, mcp_ids):
        repository_workflow_statuses.append(row.workflow_status)
        row.id = uuid.uuid4()
        row.created_at = now
        return service_module.AgentVersionRecord(
            row,
            tuple(skill_ids),
            tuple(mcp_ids),
        )

    repository.create_project_version = AsyncMock(side_effect=create_version)
    monkeypatch.setattr(
        service_module,
        "AgentRepository",
        lambda _session: repository,
    )
    sink = SimpleNamespace(append_project=AsyncMock())
    service = service_module.AgentService(
        lambda: session,
        governance_sink=sink,
    )
    payload = AgentPayload(
        description="Test Agent",
        agents_instructions="# AGENTS",
        soul="# SOUL",
        identity="# IDENTITY",
        user_context="# USER",
        model_ref="default",
        tool_groups=("web", "file:read"),
        skill_version_ids=(),
        mcp_version_ids=(),
    )

    result = await service.create_project_from_design_in_session(
        session,
        actor,
        service_module.CreateAgent("code-test", "code-test"),
        payload,
    )

    assert result.asset.status == "suspended"
    assert repository_workflow_statuses == [service_module.WorkflowStatus.DRAFT.value]
    assert result.version.workflow_status is service_module.WorkflowStatus.PUBLISHED
    assert result.version.agents_instructions == "# AGENTS"
    assert result.version.soul == "# SOUL"
    assert result.version.identity == "# IDENTITY"
    assert result.version.user_context == "# USER"
    assert asset.current_published_version_id == result.version.id
    assert asset.version == 2
    assert [call.kwargs["action"] for call in sink.append_project.await_args_list] == ["agent.create", "agent.version.create", "agent.publish"]


@pytest.mark.asyncio
async def test_suspended_published_agent_can_be_activated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    actor = _admin_context()
    session = _UnitSession()
    asset_id = uuid.uuid4()
    published = _agent_version_row(
        agent_id=asset_id,
        workflow_status="published",
        agents_instructions="# AGENTS",
        soul="# SOUL",
        identity="# IDENTITY",
        user_context="# USER",
    )
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        project_id=actor.project_id,
        slug="code-test",
        display_name="code-test",
        status="suspended",
        current_published_version_id=published.id,
        version=2,
        source_key=None,
        created_by_user_id=str(actor.user_id),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_project_version=AsyncMock(
            return_value=service_module.AgentVersionRecord(
                published,
                (),
                (),
            )
        ),
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
    )
    monkeypatch.setattr(
        service_module,
        "AgentRepository",
        lambda _session: repository,
    )
    sink = SimpleNamespace(append_project=AsyncMock())

    result = await service_module.AgentService(
        lambda: session,
        governance_sink=sink,
    ).activate(
        actor,
        asset_id,
        expected_asset_version=2,
    )

    assert result.status == "active"
    assert result.version == 3
    assert sink.append_project.await_args.kwargs["action"] == "agent.activate"


@pytest.mark.asyncio
async def test_project_agent_delete_removes_the_complete_locked_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    actor = _editor_context()
    session = _UnitSession()
    asset_id = uuid.uuid4()
    version_ids = (uuid.uuid4(), uuid.uuid4())
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        project_id=actor.project_id,
        slug="delete-me",
        display_name="Delete Me",
        status="active",
        current_published_version_id=version_ids[-1],
        version=6,
        source_key=None,
        created_by_user_id=str(actor.user_id),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    repository = SimpleNamespace(
        session=session,
        ensure_not_current_project_default=AsyncMock(),
        get_project_asset=AsyncMock(return_value=asset),
        plan_project_asset_deletion=AsyncMock(return_value=version_ids),
        delete_project_asset=AsyncMock(),
    )
    monkeypatch.setattr(
        service_module,
        "AgentRepository",
        lambda _session: repository,
    )
    sink = SimpleNamespace(append_project=AsyncMock())

    await service_module.AgentService(
        lambda: session,
        governance_sink=sink,
    ).delete(
        actor,
        asset_id,
        expected_asset_version=6,
    )

    repository.get_project_asset.assert_awaited_once_with(
        actor,
        asset_id,
        for_update=True,
    )
    repository.plan_project_asset_deletion.assert_awaited_once_with(
        actor,
        asset,
    )
    repository.delete_project_asset.assert_awaited_once_with(
        actor,
        asset,
        version_ids,
    )
    sink.append_project.assert_awaited_once_with(
        session,
        actor=actor.user_id,
        project_id=actor.project_id,
        asset_id=asset_id,
        version_id=None,
        action="agent.delete",
        request_id=actor.request_id,
        asset_kind="agent",
    )


@pytest.mark.asyncio
async def test_historical_archived_agent_cannot_be_moved_back_to_suspended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    actor = _admin_context()
    session = _UnitSession()
    asset_id = uuid.uuid4()
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        project_id=actor.project_id,
        slug="historical-agent",
        display_name="Historical Agent",
        status="archived",
        current_published_version_id=uuid.uuid4(),
        version=4,
        source_key=None,
        created_by_user_id=str(actor.user_id),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    repository = SimpleNamespace(
        session=session,
        ensure_not_current_project_default=AsyncMock(),
        get_project_asset=AsyncMock(return_value=asset),
    )
    monkeypatch.setattr(
        service_module,
        "AgentRepository",
        lambda _session: repository,
    )
    sink = SimpleNamespace(append_project=AsyncMock())

    with pytest.raises(AssetConflict):
        await service_module.AgentService(
            lambda: session,
            governance_sink=sink,
        ).suspend(
            actor,
            asset_id,
            expected_asset_version=4,
        )

    assert asset.status == "archived"
    sink.append_project.assert_not_awaited()


def _agent_version_row(
    *,
    agent_id: uuid.UUID,
    workflow_status: str,
    agents_instructions: str,
    soul: str,
    identity: str,
    user_context: str,
) -> AgentVersionRow:
    return AgentVersionRow(
        id=uuid.uuid4(),
        agent_id=agent_id,
        version_number=1,
        workflow_status=workflow_status,
        description="Existing runtime configuration",
        agents_instructions=agents_instructions,
        soul=soul,
        identity=identity,
        user_context=user_context,
        model_ref="model-a",
        tool_groups=["web"],
        supersedes_version_id=None,
        payload_schema_version=2,
        payload_checksum="a" * 64,
        created_by_user_id=str(uuid.uuid4()),
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_update_agent_instructions_clones_current_published_version_and_moves_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    actor = _editor_context()
    asset_id = uuid.uuid4()
    current = _agent_version_row(
        agent_id=asset_id,
        workflow_status="published",
        agents_instructions="old agents",
        soul="old soul",
        identity="old identity",
        user_context="old user",
    )
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        status="active",
        current_published_version_id=current.id,
        version=7,
    )
    session = _UnitSession()
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_project_version=AsyncMock(return_value=service_module.AgentVersionRecord(current, (), ())),
        next_project_version_number=AsyncMock(return_value=2),
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
    )

    async def create_version(_actor, _asset_id, row, skill_ids, mcp_ids):
        row.id = uuid.uuid4()
        row.created_at = datetime.now(UTC)
        return service_module.AgentVersionRecord(row, tuple(skill_ids), tuple(mcp_ids))

    repository.create_project_version = AsyncMock(side_effect=create_version)
    monkeypatch.setattr(service_module, "AgentRepository", lambda _session: repository)
    sink = SimpleNamespace(append_project=AsyncMock())
    service = service_module.AgentService(lambda: session, governance_sink=sink)

    result = await service.update_instructions(
        actor,
        asset_id,
        service_module.AgentInstructions(
            agents_instructions="# Agent rules",
            soul="# Soul",
            identity="# Identity",
            user_context="# User",
        ),
        expected_asset_version=7,
    )

    created = repository.create_project_version.await_args.args[2]
    assert created.workflow_status == "published"
    assert created.payload_schema_version == 2
    assert created.agents_instructions == "# Agent rules"
    assert created.soul == "# Soul"
    assert created.identity == "# Identity"
    assert created.user_context == "# User"
    assert created.description == current.description
    assert created.model_ref == current.model_ref
    assert created.tool_groups == current.tool_groups
    assert created.supersedes_version_id == current.id
    assert asset.current_published_version_id == created.id
    assert asset.version == 8
    assert result.id == created.id
    assert current.soul == "old soul"
    sink.append_project.assert_awaited_once()
    assert sink.append_project.await_args.kwargs["action"] == "agent.instructions.update"


@pytest.mark.asyncio
async def test_update_agent_instructions_without_versions_creates_hidden_empty_runtime_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    actor = _editor_context()
    asset_id = uuid.uuid4()
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        status="active",
        current_published_version_id=None,
        version=1,
    )
    session = _UnitSession()
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_latest_project_version=AsyncMock(return_value=None),
        next_project_version_number=AsyncMock(return_value=1),
    )

    async def create_version(_actor, _asset_id, row, skill_ids, mcp_ids):
        row.id = uuid.uuid4()
        row.created_at = datetime.now(UTC)
        return service_module.AgentVersionRecord(row, tuple(skill_ids), tuple(mcp_ids))

    repository.create_project_version = AsyncMock(side_effect=create_version)
    monkeypatch.setattr(service_module, "AgentRepository", lambda _session: repository)
    service = service_module.AgentService(lambda: session)

    result = await service.update_instructions(
        actor,
        asset_id,
        service_module.AgentInstructions(
            agents_instructions="",
            soul="",
            identity="Analyst",
            user_context="",
        ),
        expected_asset_version=1,
    )

    created = repository.create_project_version.await_args.args[2]
    assert created.workflow_status == "draft"
    assert created.description == ""
    assert created.model_ref == ""
    assert created.tool_groups == []
    assert created.identity == "Analyst"
    assert asset.current_published_version_id is None
    assert asset.version == 2
    assert result.workflow_status.value == "draft"


@pytest.mark.asyncio
async def test_create_runtime_version_inherits_instructions_from_latest_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    actor = _editor_context()
    asset_id = uuid.uuid4()
    latest = _agent_version_row(
        agent_id=asset_id,
        workflow_status="draft",
        agents_instructions="preserve agents",
        soul="preserve soul",
        identity="preserve identity",
        user_context="preserve user",
    )
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        status="active",
        current_published_version_id=None,
        version=2,
    )
    session = _UnitSession()
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_latest_project_version=AsyncMock(return_value=service_module.AgentVersionRecord(latest, (), ())),
        next_project_version_number=AsyncMock(return_value=2),
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
    )

    async def create_version(_actor, _asset_id, row, skill_ids, mcp_ids):
        row.id = uuid.uuid4()
        row.created_at = datetime.now(UTC)
        return service_module.AgentVersionRecord(row, tuple(skill_ids), tuple(mcp_ids))

    repository.create_project_version = AsyncMock(side_effect=create_version)
    monkeypatch.setattr(service_module, "AgentRepository", lambda _session: repository)
    service = service_module.AgentService(lambda: session)

    await service.create_version(
        actor,
        asset_id,
        AgentPayload(
            description="Configured runtime",
            soul="",
            model_ref="model-b",
            tool_groups=("web",),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
        expected_asset_version=2,
    )

    created = repository.create_project_version.await_args.args[2]
    assert created.agents_instructions == "preserve agents"
    assert created.soul == "preserve soul"
    assert created.identity == "preserve identity"
    assert created.user_context == "preserve user"
    assert created.payload_schema_version == 2


@pytest.mark.asyncio
async def test_create_runtime_version_legacy_soul_only_payload_preserves_other_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    actor = _editor_context()
    asset_id = uuid.uuid4()
    latest = _agent_version_row(
        agent_id=asset_id,
        workflow_status="draft",
        agents_instructions="preserve agents",
        soul="saved soul",
        identity="preserve identity",
        user_context="preserve user",
    )
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        status="active",
        current_published_version_id=None,
        version=2,
    )
    session = _UnitSession()
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_latest_project_version=AsyncMock(return_value=service_module.AgentVersionRecord(latest, (), ())),
        next_project_version_number=AsyncMock(return_value=2),
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
    )

    async def create_version(_actor, _asset_id, row, skill_ids, mcp_ids):
        row.id = uuid.uuid4()
        row.created_at = datetime.now(UTC)
        return service_module.AgentVersionRecord(row, tuple(skill_ids), tuple(mcp_ids))

    repository.create_project_version = AsyncMock(side_effect=create_version)
    monkeypatch.setattr(service_module, "AgentRepository", lambda _session: repository)

    await service_module.AgentService(lambda: session).create_version(
        actor,
        asset_id,
        AgentPayload(
            description="Legacy runtime form",
            soul="legacy form soul",
            model_ref="model-b",
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
        expected_asset_version=2,
    )

    created = repository.create_project_version.await_args.args[2]
    assert created.agents_instructions == "preserve agents"
    assert created.soul == "legacy form soul"
    assert created.identity == "preserve identity"
    assert created.user_context == "preserve user"


@pytest.mark.asyncio
async def test_create_runtime_version_partial_instruction_payload_only_replaces_explicit_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    actor = _editor_context()
    asset_id = uuid.uuid4()
    latest = _agent_version_row(
        agent_id=asset_id,
        workflow_status="draft",
        agents_instructions="saved agents",
        soul="preserve soul",
        identity="saved identity",
        user_context="preserve user",
    )
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        status="active",
        current_published_version_id=None,
        version=2,
    )
    session = _UnitSession()
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_latest_project_version=AsyncMock(return_value=service_module.AgentVersionRecord(latest, (), ())),
        next_project_version_number=AsyncMock(return_value=2),
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
    )

    async def create_version(_actor, _asset_id, row, skill_ids, mcp_ids):
        row.id = uuid.uuid4()
        row.created_at = datetime.now(UTC)
        return service_module.AgentVersionRecord(row, tuple(skill_ids), tuple(mcp_ids))

    repository.create_project_version = AsyncMock(side_effect=create_version)
    monkeypatch.setattr(service_module, "AgentRepository", lambda _session: repository)

    await service_module.AgentService(lambda: session).create_version(
        actor,
        asset_id,
        AgentPayload(
            description="Partial runtime form",
            soul="",
            model_ref="model-b",
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
            agents_instructions="new agents",
            identity="",
        ),
        expected_asset_version=2,
        provided_instruction_fields=frozenset(
            {
                "agents_instructions",
                "identity",
            }
        ),
    )

    created = repository.create_project_version.await_args.args[2]
    assert created.agents_instructions == "new agents"
    assert created.soul == "preserve soul"
    assert created.identity == ""
    assert created.user_context == "preserve user"


def test_agent_payload_checksum_preserves_v1_and_covers_instructions_in_v2() -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    base = AgentPayload(
        description="Description",
        soul="Soul",
        model_ref="model",
        tool_groups=("web",),
        skill_version_ids=(),
        mcp_version_ids=(),
    )
    changed = dataclasses.replace(base, identity="Different")

    assert service_module.AgentService._payload_checksum(
        base,
        payload_schema_version=1,
    ) == service_module.AgentService._payload_checksum(
        changed,
        payload_schema_version=1,
    )
    assert service_module.AgentService._payload_checksum(
        base,
        payload_schema_version=2,
    ) != service_module.AgentService._payload_checksum(
        changed,
        payload_schema_version=2,
    )


@pytest.mark.asyncio
async def test_agent_instruction_size_limit_is_rejected_before_storage_is_opened() -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")

    class ExplodingSessionFactory:
        def __call__(self):
            raise AssertionError("invalid input must not open a database session")

    with pytest.raises(AssetValidationFailed):
        await service_module.AgentService(ExplodingSessionFactory()).update_instructions(
            _editor_context(),
            uuid.uuid4(),
            service_module.AgentInstructions(
                agents_instructions="x" * (service_module.MAX_AGENT_INSTRUCTION_FIELD_BYTES + 1),
                soul="",
                identity="",
                user_context="",
            ),
            expected_asset_version=1,
        )


@pytest.mark.asyncio
async def test_publish_accepts_legacy_v1_checksum_and_large_soul_after_instruction_columns_are_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    actor = _editor_context()
    asset_id = uuid.uuid4()
    payload = AgentPayload(
        description="Legacy",
        soul="L" * (service_module.MAX_AGENT_INSTRUCTION_FIELD_BYTES + 1),
        model_ref="legacy-model",
        tool_groups=(),
        skill_version_ids=(),
        mcp_version_ids=(),
    )
    row = AgentVersionRow(
        id=uuid.uuid4(),
        agent_id=asset_id,
        version_number=1,
        workflow_status="draft",
        description=payload.description,
        agents_instructions="",
        soul=payload.soul,
        identity="",
        user_context="",
        model_ref=payload.model_ref,
        tool_groups=[],
        supersedes_version_id=None,
        payload_schema_version=1,
        payload_checksum=service_module.AgentService._payload_checksum(
            payload,
            payload_schema_version=1,
        ),
        created_by_user_id=str(actor.user_id),
        created_at=datetime.now(UTC),
    )
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        status="active",
        current_published_version_id=None,
        version=2,
    )
    session = _UnitSession()
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_project_version=AsyncMock(return_value=service_module.AgentVersionRecord(row, (), ())),
        get_latest_project_version=AsyncMock(return_value=service_module.AgentVersionRecord(row, (), ())),
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
    )
    monkeypatch.setattr(service_module, "AgentRepository", lambda _session: repository)
    sink = SimpleNamespace(append_project=AsyncMock())

    result = await service_module.AgentService(
        lambda: session,
        governance_sink=sink,
    ).publish(
        actor,
        asset_id,
        row.id,
        expected_asset_version=2,
    )

    assert result.workflow_status is service_module.WorkflowStatus.PUBLISHED
    assert result.payload_schema_version == 1
    assert asset.current_published_version_id == row.id
    assert asset.version == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("has_published_settings", [False, True])
async def test_publish_stale_runtime_draft_synthesizes_new_version_with_current_instructions(
    monkeypatch: pytest.MonkeyPatch,
    has_published_settings: bool,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    actor = _editor_context()
    asset_id = uuid.uuid4()
    runtime_draft = _agent_version_row(
        agent_id=asset_id,
        workflow_status="draft",
        agents_instructions="settings A agents",
        soul="settings A soul",
        identity="settings A identity",
        user_context="settings A user",
    )
    runtime_draft.version_number = 2
    runtime_payload = AgentPayload(
        description=runtime_draft.description,
        soul=runtime_draft.soul,
        model_ref=runtime_draft.model_ref,
        tool_groups=tuple(runtime_draft.tool_groups),
        skill_version_ids=(),
        mcp_version_ids=(),
        agents_instructions=runtime_draft.agents_instructions,
        identity=runtime_draft.identity,
        user_context=runtime_draft.user_context,
    )
    runtime_draft.payload_checksum = service_module.AgentService._payload_checksum(
        runtime_payload,
        payload_schema_version=2,
    )
    current_settings = _agent_version_row(
        agent_id=asset_id,
        workflow_status="published" if has_published_settings else "draft",
        agents_instructions="settings B agents",
        soul="settings B soul",
        identity="settings B identity",
        user_context="settings B user",
    )
    current_settings.version_number = 3
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        status="active",
        current_published_version_id=(current_settings.id if has_published_settings else None),
        version=4,
    )
    session = _UnitSession()

    async def get_version(_actor, _asset_id, version_id, **_kwargs):
        row = current_settings if version_id == current_settings.id else runtime_draft
        return service_module.AgentVersionRecord(row, (), ())

    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_project_version=AsyncMock(side_effect=get_version),
        get_latest_project_version=AsyncMock(
            return_value=service_module.AgentVersionRecord(
                current_settings,
                (),
                (),
            )
        ),
        next_project_version_number=AsyncMock(return_value=4),
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
    )

    async def create_version(_actor, _asset_id, row, skill_ids, mcp_ids):
        row.id = uuid.uuid4()
        row.created_at = datetime.now(UTC)
        return service_module.AgentVersionRecord(
            row,
            tuple(skill_ids),
            tuple(mcp_ids),
        )

    repository.create_project_version = AsyncMock(side_effect=create_version)
    monkeypatch.setattr(service_module, "AgentRepository", lambda _session: repository)
    sink = SimpleNamespace(append_project=AsyncMock())

    service = service_module.AgentService(
        lambda: session,
        governance_sink=sink,
    )
    result = await service.publish(
        actor,
        asset_id,
        runtime_draft.id,
        expected_asset_version=4,
    )

    synthesized = repository.create_project_version.await_args.args[2]
    assert result.id == synthesized.id
    assert result.id != runtime_draft.id
    assert result.version_number == 4
    assert result.workflow_status is service_module.WorkflowStatus.PUBLISHED
    assert synthesized.description == runtime_draft.description
    assert synthesized.model_ref == runtime_draft.model_ref
    assert synthesized.tool_groups == runtime_draft.tool_groups
    assert synthesized.agents_instructions == current_settings.agents_instructions
    assert synthesized.soul == current_settings.soul
    assert synthesized.identity == current_settings.identity
    assert synthesized.user_context == current_settings.user_context
    assert synthesized.payload_schema_version == 2
    assert synthesized.supersedes_version_id == (current_settings.id if has_published_settings else None)
    synthesized_payload = AgentPayload(
        description=synthesized.description,
        soul=synthesized.soul,
        model_ref=synthesized.model_ref,
        tool_groups=tuple(synthesized.tool_groups),
        skill_version_ids=(),
        mcp_version_ids=(),
        agents_instructions=synthesized.agents_instructions,
        identity=synthesized.identity,
        user_context=synthesized.user_context,
    )
    assert synthesized.payload_checksum == service_module.AgentService._payload_checksum(
        synthesized_payload,
        payload_schema_version=2,
    )
    assert runtime_draft.workflow_status == "rejected"
    assert asset.current_published_version_id == synthesized.id
    assert asset.version == 5
    repository.resolve_project_skill_versions.assert_awaited_once_with(actor, ())
    repository.resolve_project_mcp_versions.assert_awaited_once_with(actor, ())
    sink.append_project.assert_awaited_once()
    assert sink.append_project.await_args.kwargs["version_id"] == synthesized.id
    assert sink.append_project.await_args.kwargs["action"] == "agent.publish"

    with pytest.raises(AssetConflict):
        await service.publish(
            actor,
            asset_id,
            runtime_draft.id,
            expected_asset_version=5,
        )
    repository.create_project_version.assert_awaited_once()
    sink.append_project.assert_awaited_once()


def test_agent_payload_validation_preserves_v1_soul_requirement_but_v2_allows_empty_instructions() -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    actor = _editor_context()
    payload = AgentPayload(
        description="",
        soul="",
        model_ref="model",
        tool_groups=(),
        skill_version_ids=(),
        mcp_version_ids=(),
    )

    with pytest.raises(AssetValidationFailed):
        service_module.AgentService._validate_payload(
            actor,
            payload,
            payload_schema_version=1,
        )

    assert (
        service_module.AgentService._validate_payload(
            actor,
            payload,
            payload_schema_version=2,
        ).soul
        == ""
    )


def test_agent_instruction_governance_action_maps_to_asset_update() -> None:
    audit_module = importlib.import_module("app.shared_assets.audit")
    audit_models = importlib.import_module("app.audit.models")

    assert audit_module._ACTIONS["agent.instructions.update"] is audit_models.AuditAction.ASSET_UPDATED
    assert "agent.archive" not in audit_module._ACTIONS


def test_agent_service_does_not_expose_archive_lifecycle() -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")

    assert not hasattr(service_module.AgentService, "archive")
