from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetConflict, AssetForbidden
from app.shared_assets.models import WorkflowStatus
from deerflow.mcp.definition import ExactMcpEndpointPolicy
from deerflow.persistence.shared_assets import McpServerRow


def _context(role: ProjectRole = ProjectRole.ADMIN) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-mcp-lifecycle",
    )


class _UnitSession:
    def __init__(self) -> None:
        self.flush = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return self


def _asset(actor: ProjectContext, *, status: str, current_version_id: uuid.UUID | None, version: int = 3):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        slug="lifecycle-mcp",
        display_name="Lifecycle MCP",
        status=status,
        current_published_version_id=current_version_id,
        version=version,
        source_key=None,
        created_by_user_id=str(actor.user_id),
        created_at=now,
        updated_at=now,
    )


def _published_record(
    service,
    actor: ProjectContext,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    url: str,
):
    module = importlib.import_module("app.shared_assets.mcp_service")
    definition = service._validate_definition(
        actor,
        module.McpDefinition(
            description="Lifecycle tools",
            transport="http",
            url=url,
        ),
        endpoint_policy=service._endpoint_policy,
    )
    row = SimpleNamespace(
        id=version_id,
        mcp_server_id=asset_id,
        version_number=1,
        workflow_status=WorkflowStatus.PUBLISHED.value,
        description=definition.description,
        transport=definition.transport,
        command=definition.command,
        args=list(definition.args),
        url=definition.url,
        non_secret_env=dict(definition.env),
        non_secret_headers=dict(definition.headers),
        oauth_metadata=dict(definition.oauth),
        routing=dict(definition.routing),
        tool_overrides=dict(definition.tool_overrides),
        timeout_seconds=definition.timeout_seconds,
        supersedes_version_id=None,
        payload_checksum=service._checksum(definition),
        submitted_at=None,
        reviewed_at=None,
        reviewed_by_user_id=None,
        created_by_user_id=str(uuid.uuid4()),
        created_at=datetime.now(UTC),
    )
    return SimpleNamespace(row=row, slots=(), grants=())


@pytest.mark.asyncio
async def test_mcp_activate_requires_suspended_published_current_version_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context()
    session = _UnitSession()
    current_version_id = uuid.uuid4()
    asset = _asset(actor, status="suspended", current_version_id=current_version_id)
    url = "https://lifecycle.example.test/mcp"
    sink = SimpleNamespace(append_project=AsyncMock())
    service = module.McpService(
        lambda: session,
        governance_sink=sink,
        endpoint_policy=ExactMcpEndpointPolicy(frozenset({url})),
    )
    record = _published_record(service, actor, asset.id, current_version_id, url)
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_project_version=AsyncMock(return_value=record),
    )
    monkeypatch.setattr(module, "McpRepository", lambda _session: repository)

    result = await service.activate(
        actor,
        asset.id,
        expected_asset_version=3,
    )

    assert result.status == "active"
    assert result.version == 4
    repository.get_project_version.assert_awaited_once_with(
        actor,
        asset.id,
        current_version_id,
        for_update=True,
    )
    sink.append_project.assert_awaited_once_with(
        session,
        actor=actor.user_id,
        project_id=actor.project_id,
        asset_id=asset.id,
        version_id=current_version_id,
        action="mcp.activate",
        request_id=actor.request_id,
        asset_kind="mcp",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("archived", "suspended"))
async def test_mcp_suspend_rejects_every_non_active_source_status(
    status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context()
    session = _UnitSession()
    asset = _asset(actor, status=status, current_version_id=uuid.uuid4())
    sink = SimpleNamespace(append_project=AsyncMock())
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
    )
    monkeypatch.setattr(module, "McpRepository", lambda _session: repository)

    with pytest.raises(AssetConflict):
        await module.McpService(lambda: session, governance_sink=sink).suspend(
            actor,
            asset.id,
            expected_asset_version=asset.version,
        )

    assert asset.status == status
    sink.append_project.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_status", (None, WorkflowStatus.DRAFT.value))
async def test_mcp_suspend_requires_a_locked_published_current_version(
    workflow_status: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context()
    session = _UnitSession()
    current_version_id = uuid.uuid4() if workflow_status is not None else None
    asset = _asset(actor, status="active", current_version_id=current_version_id)
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        get_project_version=AsyncMock(
            return_value=SimpleNamespace(
                row=SimpleNamespace(workflow_status=workflow_status),
            ),
        ),
    )
    monkeypatch.setattr(module, "McpRepository", lambda _session: repository)

    with pytest.raises(AssetConflict):
        await module.McpService(lambda: session).suspend(
            actor,
            asset.id,
            expected_asset_version=asset.version,
        )

    assert asset.status == "active"
    if current_version_id is None:
        repository.get_project_version.assert_not_awaited()
    else:
        repository.get_project_version.assert_awaited_once_with(
            actor,
            asset.id,
            current_version_id,
            for_update=True,
        )


@pytest.mark.asyncio
async def test_project_mcp_delete_removes_the_complete_locked_package_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context(ProjectRole.EDITOR)
    session = _UnitSession()
    asset = _asset(actor, status="suspended", current_version_id=uuid.uuid4(), version=7)
    version_ids = (uuid.uuid4(), uuid.uuid4())
    repository = SimpleNamespace(
        session=session,
        get_project_asset=AsyncMock(return_value=asset),
        plan_project_asset_deletion=AsyncMock(return_value=version_ids),
        delete_project_asset=AsyncMock(),
    )
    monkeypatch.setattr(module, "McpRepository", lambda _session: repository)
    sink = SimpleNamespace(append_project=AsyncMock())

    result = await module.McpService(
        lambda: session,
        governance_sink=sink,
    ).delete(
        actor,
        asset.id,
        expected_asset_version=7,
    )

    assert result is None
    repository.plan_project_asset_deletion.assert_awaited_once_with(actor, asset)
    repository.delete_project_asset.assert_awaited_once_with(actor, asset, version_ids)
    sink.append_project.assert_awaited_once_with(
        session,
        actor=actor.user_id,
        project_id=actor.project_id,
        asset_id=asset.id,
        version_id=None,
        action="mcp.delete",
        request_id=actor.request_id,
        asset_kind="mcp",
    )


@pytest.mark.asyncio
async def test_mcp_delete_rejects_system_and_admin_override_contexts_before_storage() -> None:
    module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("project-only delete must not open storage")

    service = module.McpService(ExplodingFactory())
    for actor in (
        SystemAssetGovernanceContext(uuid.uuid4(), "req-system-mcp-delete"),
        SystemAssetGovernanceContext(
            uuid.uuid4(),
            "req-override-mcp-delete",
            uuid.uuid4(),
        ),
    ):
        with pytest.raises(AssetForbidden) as exc_info:
            await service.delete(
                actor,
                uuid.uuid4(),
                expected_asset_version=1,
            )
        assert exc_info.value.request_id == actor.request_id


@pytest.mark.asyncio
async def test_mcp_delete_plan_locks_owned_rows_and_checks_every_external_reference() -> None:
    repository_module = importlib.import_module("app.shared_assets.mcp_repository")
    actor = _context(ProjectRole.EDITOR)
    asset = McpServerRow(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        slug="delete-plan",
        display_name="Delete Plan",
        status="active",
        version=1,
        created_by_user_id=str(actor.user_id),
    )
    version_id = uuid.uuid4()
    slot_id = uuid.uuid4()
    grant_id = uuid.uuid4()

    class ScalarRows:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class Session:
        def __init__(self):
            self.execute_statements = []
            self.scalar_statements = []

        async def execute(self, statement):
            self.execute_statements.append(statement)
            values = ([version_id], [slot_id], [grant_id])[len(self.execute_statements) - 1]
            return ScalarRows(values)

        async def scalar(self, statement):
            self.scalar_statements.append(statement)
            return False

    session = Session()

    plan = await repository_module.McpRepository(session).plan_project_asset_deletion(
        actor,
        asset,
    )

    assert plan == (version_id,)
    assert len(session.execute_statements) == 3
    assert all("FOR UPDATE" in str(statement) for statement in session.execute_statements)
    retained_sql = str(session.scalar_statements[0])
    for table in (
        "agent_version_mcp_refs",
        "run_asset_versions",
        "run_mcp_grant_snapshots",
        "project_system_mcp_bindings",
    ):
        assert table in retained_sql


def test_mcp_hard_delete_bypass_is_transaction_local_and_exact_asset_scoped() -> None:
    binding_model = importlib.import_module(
        "deerflow.persistence.shared_assets.binding_model",
    )

    ddl = binding_model._CREATE_CHILD_IMMUTABILITY_FUNCTION
    assert "deerflow.mcp_hard_delete_asset_id" in ddl
    assert "asset.status = 'archived'" in ddl
    assert "asset.current_published_version_id IS NULL" in ddl
    assert "current_setting(" in ddl
