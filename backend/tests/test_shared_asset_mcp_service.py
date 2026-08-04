from __future__ import annotations

import dataclasses
import importlib
import inspect
import uuid
from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.exc import TimeoutError as SATimeoutError

from app.audit.models import AuditAction
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetForbidden, AssetValidationFailed
from app.shared_assets.models import WorkflowStatus
from deerflow.mcp.definition import ExactMcpEndpointPolicy


def _context(role: ProjectRole = ProjectRole.EDITOR) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-mcp-unit",
    )


def _safe_definition(service_module):
    return service_module.McpDefinition(
        description="Issue tracker",
        transport="http",
        url="https://mcp.example.test",
        oauth={
            "enabled": True,
            "token_url": "https://identity.example.test/oauth/token",
            "client_id": "public-client",
        },
    )


def _endpoint_policy(*endpoints: str) -> ExactMcpEndpointPolicy:
    return ExactMcpEndpointPolicy(frozenset(endpoints))


def _system_context() -> SystemAssetGovernanceContext:
    return SystemAssetGovernanceContext(
        user_id=uuid.uuid4(),
        request_id="req-system-bootstrap-only",
    )


class _UnitTransaction:
    def __init__(self) -> None:
        self.enter_count = 0
        self.exit_count = 0
        self.exit_error: type[BaseException] | None = None

    async def __aenter__(self):
        self.enter_count += 1
        return self

    async def __aexit__(self, exc_type, _exc, _traceback) -> None:
        self.exit_count += 1
        self.exit_error = exc_type


class _UnitSession:
    def __init__(self) -> None:
        self.transaction = _UnitTransaction()
        self.enter_count = 0
        self.exit_count = 0
        self.begin_count = 0
        self.flush_count = 0

    async def __aenter__(self):
        self.enter_count += 1
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        self.exit_count += 1

    def begin(self) -> _UnitTransaction:
        self.begin_count += 1
        return self.transaction

    async def flush(self) -> None:
        self.flush_count += 1


def _configured_service_harness(
    monkeypatch: pytest.MonkeyPatch,
    service_module,
    *,
    url: str,
):
    session = _UnitSession()
    factory_calls = 0
    repository_events: list[str] = []
    created: dict[str, object] = {}
    now = datetime.now(UTC)

    class Repository:
        def __init__(self, repository_session) -> None:
            assert repository_session is session
            self.session = repository_session

        async def create_project_asset(self, actor, row) -> None:
            repository_events.append("asset")
            assert actor.project_id == row.project_id
            row.id = uuid.uuid4()
            row.status = "active"
            row.current_published_version_id = None
            row.version = 1
            row.created_at = now
            row.updated_at = now
            created["asset"] = row

        async def next_version_number(self, asset) -> int:
            repository_events.append("next-version")
            assert asset is created["asset"]
            return 1

        async def add_version(self, asset, version, slots, *, request_id: str):
            repository_events.append("version")
            assert asset is created["asset"]
            assert request_id == "req-mcp-unit"
            version.submitted_at = None
            version.reviewed_at = None
            version.reviewed_by_user_id = None
            version.created_at = now
            for slot in slots:
                slot.id = uuid.uuid4()
            created["version"] = version
            created["slots"] = slots
            return SimpleNamespace(row=version, slots=slots, grants=())

    def session_factory():
        nonlocal factory_calls
        factory_calls += 1
        return session

    monkeypatch.setattr(service_module, "McpRepository", Repository)
    discovery_attempts = SimpleNamespace(
        enqueue=AsyncMock(
            return_value=SimpleNamespace(
                id=uuid.uuid4(),
                status="queued",
            )
        )
    )
    monkeypatch.setattr(
        service_module,
        "McpToolDiscoveryAttemptRepository",
        lambda repository_session: discovery_attempts,
        raising=False,
    )
    governance_sink = SimpleNamespace(append_project=AsyncMock())
    service = service_module.McpService(
        session_factory,
        governance_sink,
        endpoint_policy=_endpoint_policy(url),
    )
    return service, session, governance_sink, repository_events, created, discovery_attempts, lambda: factory_calls


def _updated_service_harness(
    monkeypatch: pytest.MonkeyPatch,
    service_module,
    *,
    url: str,
):
    session = _UnitSession()
    factory_calls = 0
    repository_events: list[str] = []
    created: dict[str, object] = {}
    now = datetime.now(UTC)
    current_version_id = uuid.uuid4()
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=uuid.uuid4(),
        slug="existing-mcp",
        display_name="Existing MCP",
        status="active",
        current_published_version_id=current_version_id,
        version=7,
        created_by_user_id=str(uuid.uuid4()),
        created_at=now,
        updated_at=now,
    )

    class Repository:
        def __init__(self, repository_session) -> None:
            assert repository_session is session
            self.session = repository_session

        async def get_project_asset(self, actor, asset_id, *, for_update: bool):
            repository_events.append("asset")
            assert actor.project_id == asset.project_id
            assert asset_id == asset.id
            assert for_update is True
            return asset

        async def next_version_number(self, selected_asset) -> int:
            repository_events.append("next-version")
            assert selected_asset is asset
            return 2

        async def add_version(self, selected_asset, version, slots, *, request_id: str):
            repository_events.append("version")
            assert selected_asset is asset
            assert request_id == "req-mcp-unit"
            version.submitted_at = None
            version.reviewed_at = None
            version.reviewed_by_user_id = None
            version.created_at = now
            for slot in slots:
                slot.id = uuid.uuid4()
            created["version"] = version
            created["slots"] = slots
            return SimpleNamespace(row=version, slots=slots, grants=())

    def session_factory():
        nonlocal factory_calls
        factory_calls += 1
        return session

    monkeypatch.setattr(service_module, "McpRepository", Repository)
    discovery_attempts = SimpleNamespace(
        enqueue=AsyncMock(
            return_value=SimpleNamespace(
                id=uuid.uuid4(),
                status="queued",
            )
        )
    )
    monkeypatch.setattr(
        service_module,
        "McpToolDiscoveryAttemptRepository",
        lambda repository_session: discovery_attempts,
        raising=False,
    )
    actor = _context()
    actor = dataclasses.replace(actor, project_id=asset.project_id)
    governance_sink = SimpleNamespace(append_project=AsyncMock())
    service = service_module.McpService(
        session_factory,
        governance_sink,
        endpoint_policy=_endpoint_policy(url),
    )
    return service, actor, asset, session, governance_sink, repository_events, created, discovery_attempts, lambda: factory_calls


def test_mcp_service_exposes_frozen_contracts_and_scoped_repository() -> None:
    package = importlib.import_module("app.shared_assets")
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    repository_module = importlib.import_module("app.shared_assets.mcp_repository")
    audit_module = importlib.import_module("app.shared_assets.audit")

    assert package.McpService is service_module.McpService
    for value_type in (
        service_module.CreateMcpServer,
        service_module.McpCredentialSlot,
        service_module.McpDefinition,
        service_module.McpAssetView,
        service_module.McpVersionView,
        service_module.ProjectMcpConfiguredCreateResult,
    ):
        assert dataclasses.is_dataclass(value_type)
        assert value_type.__dataclass_params__.frozen is True
    assert package.ProjectMcpConfiguredCreateResult is service_module.ProjectMcpConfiguredCreateResult
    assert "payload_checksum" in {field.name for field in dataclasses.fields(service_module.McpVersionView)}

    for name, method in inspect.getmembers(repository_module.McpRepository, predicate=inspect.isfunction):
        if not name.startswith("_"):
            assert "project_id" not in inspect.signature(method).parameters, name
    project_get = inspect.signature(repository_module.McpRepository.get_project_asset)
    assert list(project_get.parameters) == ["self", "context", "asset_id", "for_update"]
    approve = inspect.signature(service_module.McpService.approve)
    assert list(approve.parameters)[4] == "credential_versions"
    configure_grants = inspect.signature(service_module.McpService.configure_system_credential_grants)
    assert list(configure_grants.parameters) == [
        "self",
        "actor",
        "asset_id",
        "version_id",
        "credential_versions",
        "expected_active_grant_versions",
    ]
    assert audit_module._ACTIONS["mcp.credential_grants.configure"] is AuditAction.ASSET_UPDATED
    assert audit_module._ACTIONS["mcp.activate"] is AuditAction.ASSET_UPDATED
    assert audit_module._ACTIONS["mcp.delete"] is AuditAction.ASSET_DELETED


@pytest.mark.asyncio
async def test_configured_project_mcp_without_slots_is_created_and_published_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context()
    url = "https://configured.example.test/mcp"
    service, session, governance_sink, repository_events, created, discovery_attempts, factory_calls = _configured_service_harness(
        monkeypatch,
        service_module,
        url=url,
    )

    result = await service.create_project_configured(
        actor,
        service_module.CreateMcpServer("configured-mcp", "Configured MCP"),
        service_module.McpDefinition(
            description="Ready without credentials",
            transport="http",
            url=url,
        ),
    )

    asset = created["asset"]
    version = created["version"]
    assert factory_calls() == 1
    assert session.enter_count == 1
    assert session.begin_count == 1
    assert session.transaction.enter_count == 1
    assert session.transaction.exit_count == 1
    assert session.transaction.exit_error is None
    assert repository_events == ["asset", "next-version", "version"]
    assert asset.scope == "project"
    assert asset.project_id == actor.project_id
    assert asset.slug == "configured-mcp"
    assert asset.current_published_version_id == version.id
    assert asset.version == 3
    assert version.workflow_status == WorkflowStatus.PUBLISHED.value
    assert version.version_number == 1
    assert version.supersedes_version_id is None
    assert version.submitted_at is None
    assert result.asset.id == asset.id
    assert result.asset.version == 3
    assert result.asset.current_published_version_id == version.id
    assert result.version.id == version.id
    assert result.version.workflow_status is WorkflowStatus.PUBLISHED
    assert result.version.credential_slots == ()
    assert [awaited.kwargs["action"] for awaited in governance_sink.append_project.await_args_list] == [
        "mcp.create",
        "mcp.version.create",
        "mcp.publish",
    ]
    assert all(awaited.args == (session,) for awaited in governance_sink.append_project.await_args_list)
    assert all(awaited.kwargs["asset_id"] == asset.id for awaited in governance_sink.append_project.await_args_list)
    assert governance_sink.append_project.await_args_list[0].kwargs["version_id"] is None
    assert all(awaited.kwargs["version_id"] == version.id for awaited in governance_sink.append_project.await_args_list[1:])
    discovery_attempts.enqueue.assert_awaited_once()
    discovery_call = discovery_attempts.enqueue.await_args.kwargs
    assert discovery_call["project_id"] == actor.project_id
    assert discovery_call["requested_by_user_id"] == actor.user_id
    assert discovery_call["mcp_server_id"] == asset.id
    assert discovery_call["mcp_server_version_id"] == version.id
    assert discovery_call["payload_checksum"] == version.payload_checksum
    assert discovery_call["grant_digest"] == service_module.mcp_grant_closure_digest(())
    assert discovery_call["trigger"] == "auto"


@pytest.mark.asyncio
async def test_configured_project_mcp_with_slots_is_created_and_submitted_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context()
    url = "https://credentialed.example.test/mcp"
    service, session, governance_sink, repository_events, created, discovery_attempts, factory_calls = _configured_service_harness(
        monkeypatch,
        service_module,
        url=url,
    )

    result = await service.create_project_configured(
        actor,
        service_module.CreateMcpServer("credentialed-mcp", "Credentialed MCP"),
        service_module.McpDefinition(
            description="Requires project credential approval",
            transport="http",
            url=url,
            credential_slots=(
                service_module.McpCredentialSlot(
                    "api-key",
                    "Amap query credential",
                    {"query": ("key",)},
                ),
            ),
        ),
    )

    asset = created["asset"]
    version = created["version"]
    assert factory_calls() == 1
    assert session.begin_count == 1
    assert session.transaction.exit_error is None
    assert repository_events == ["asset", "next-version", "version"]
    assert asset.current_published_version_id is None
    assert asset.version == 3
    assert version.workflow_status == WorkflowStatus.PENDING_APPROVAL.value
    assert version.submitted_at is not None
    assert result.asset.id == asset.id
    assert result.asset.version == 3
    assert result.asset.current_published_version_id is None
    assert result.version.workflow_status is WorkflowStatus.PENDING_APPROVAL
    assert [slot.name for slot in result.version.credential_slots] == ["api-key"]
    assert [awaited.kwargs["action"] for awaited in governance_sink.append_project.await_args_list] == [
        "mcp.create",
        "mcp.version.create",
        "mcp.submit_approval",
    ]
    assert all(awaited.args == (session,) for awaited in governance_sink.append_project.await_args_list)
    assert all(awaited.kwargs["asset_id"] == asset.id for awaited in governance_sink.append_project.await_args_list)
    assert all(awaited.kwargs["version_id"] == version.id for awaited in governance_sink.append_project.await_args_list[1:])
    discovery_attempts.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_configured_mcp_is_project_context_only_before_storage() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("project-only rejection must not open storage")

    with pytest.raises(AssetForbidden):
        await service_module.McpService(ExplodingFactory()).create_project_configured(
            _system_context(),
            service_module.CreateMcpServer("system-mcp", "System MCP"),
            service_module.McpDefinition(
                transport="http",
                url="https://configured.example.test/mcp",
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("with_slot", "expected_status", "expected_action"),
    [
        (False, WorkflowStatus.PUBLISHED, "mcp.publish"),
        (True, WorkflowStatus.PENDING_APPROVAL, "mcp.submit_approval"),
    ],
)
async def test_update_project_configured_creates_and_advances_one_revision_atomically(
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_slot: bool,
    expected_status: WorkflowStatus,
    expected_action: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    url = "https://updated.example.test/mcp"
    service, actor, asset, session, governance_sink, repository_events, created, discovery_attempts, factory_calls = _updated_service_harness(
        monkeypatch,
        service_module,
        url=url,
    )
    prior_version_id = asset.current_published_version_id
    slots = (
        (
            service_module.McpCredentialSlot(
                "api-key",
                "Updated query credential",
                {"query": ("key",)},
            ),
        )
        if with_slot
        else ()
    )

    result = await service.update_project_configured(
        actor,
        asset.id,
        service_module.McpDefinition(
            description="Updated definition",
            transport="http",
            url=url,
            credential_slots=slots,
        ),
        expected_asset_version=7,
    )

    version = created["version"]
    assert factory_calls() == 1
    assert session.begin_count == 1
    assert session.transaction.enter_count == 1
    assert session.transaction.exit_error is None
    assert repository_events == ["asset", "next-version", "version"]
    assert version.version_number == 2
    assert version.supersedes_version_id == prior_version_id
    assert version.workflow_status == expected_status.value
    assert asset.version == 9
    assert asset.slug == "existing-mcp"
    assert asset.display_name == "Existing MCP"
    assert asset.current_published_version_id == (version.id if expected_status is WorkflowStatus.PUBLISHED else prior_version_id)
    assert result.asset.version == 9
    assert result.version.workflow_status is expected_status
    assert [awaited.kwargs["action"] for awaited in governance_sink.append_project.await_args_list] == [
        "mcp.version.create",
        expected_action,
    ]
    assert all(awaited.args == (session,) for awaited in governance_sink.append_project.await_args_list)
    assert all(awaited.kwargs["asset_id"] == asset.id for awaited in governance_sink.append_project.await_args_list)
    assert all(awaited.kwargs["version_id"] == version.id for awaited in governance_sink.append_project.await_args_list)
    assert discovery_attempts.enqueue.await_count == (0 if with_slot else 1)
    if not with_slot:
        discovery_call = discovery_attempts.enqueue.await_args.kwargs
        assert discovery_call["project_id"] == actor.project_id
        assert discovery_call["requested_by_user_id"] == actor.user_id
        assert discovery_call["mcp_server_id"] == asset.id
        assert discovery_call["mcp_server_version_id"] == version.id
        assert discovery_call["payload_checksum"] == version.payload_checksum
        assert discovery_call["grant_digest"] == service_module.mcp_grant_closure_digest(())
        assert discovery_call["trigger"] == "auto"


@pytest.mark.asyncio
async def test_update_project_configured_is_project_context_only_before_storage() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("project-only rejection must not open storage")

    with pytest.raises(AssetForbidden):
        await service_module.McpService(ExplodingFactory()).update_project_configured(
            _system_context(),
            uuid.uuid4(),
            service_module.McpDefinition(
                transport="http",
                url="https://updated.example.test/mcp",
            ),
            expected_asset_version=1,
        )


@pytest.mark.asyncio
async def test_get_project_configured_locks_and_revalidates_the_current_editable_definition() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context(ProjectRole.EDITOR)
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    now = datetime.now(UTC)
    url = "http://127.0.0.1:8771/api/mcp"
    definition = service_module.McpDefinition(
        description="Current project MCP",
        transport="http",
        url=url,
    )
    service = service_module.McpService(
        lambda: None,
        endpoint_policy=_endpoint_policy(url),
    )
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        project_id=actor.project_id,
        slug="current-project-mcp",
        display_name="Current project MCP",
        status="active",
        current_published_version_id=version_id,
        version=7,
        created_by_user_id=str(actor.user_id),
        created_at=now,
        updated_at=now,
    )
    row = SimpleNamespace(
        id=version_id,
        mcp_server_id=asset_id,
        version_number=2,
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
        created_by_user_id=str(actor.user_id),
        created_at=now,
    )
    record = SimpleNamespace(row=row, slots=(), grants=())
    repository = SimpleNamespace(
        get_project_asset=AsyncMock(return_value=asset),
        get_project_current_configuration=AsyncMock(return_value=record),
    )

    async def execute(_actor, operation, governance=None):
        del governance
        return await operation(repository)

    service._execute = execute

    result = await service.get_project_configured(actor, asset_id)

    assert result.asset.id == asset_id
    assert result.version.id == version_id
    assert result.version.definition.url == url
    repository.get_project_asset.assert_awaited_once_with(
        actor,
        asset_id,
        for_update=True,
    )
    repository.get_project_current_configuration.assert_awaited_once_with(
        actor,
        asset,
        for_update=True,
    )


@pytest.mark.asyncio
async def test_get_project_configured_fails_closed_when_current_endpoint_policy_changed() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context(ProjectRole.EDITOR)
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    url = "http://127.0.0.1:8771/api/mcp"
    definition = service_module.McpDefinition(transport="http", url=url)
    checksum_service = service_module.McpService(
        lambda: None,
        endpoint_policy=_endpoint_policy(url),
    )
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        project_id=actor.project_id,
        status="active",
        current_published_version_id=version_id,
    )
    row = SimpleNamespace(
        id=version_id,
        mcp_server_id=asset_id,
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
        payload_checksum=checksum_service._checksum(definition),
    )
    record = SimpleNamespace(row=row, slots=(), grants=())
    repository = SimpleNamespace(
        get_project_asset=AsyncMock(return_value=asset),
        get_project_current_configuration=AsyncMock(return_value=record),
    )
    service = service_module.McpService(
        lambda: None,
        endpoint_policy=_endpoint_policy("http://127.0.0.1:8772/other"),
    )

    async def execute(_actor, operation, governance=None):
        del governance
        return await operation(repository)

    service._execute = execute

    with pytest.raises(AssetValidationFailed):
        await service.get_project_configured(actor, asset_id)


@pytest.mark.asyncio
async def test_get_project_configured_requires_edit_capability_before_storage() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("read-only members must not load editable MCP paths")

    with pytest.raises(AssetForbidden):
        await service_module.McpService(ExplodingFactory()).get_project_configured(
            _context(ProjectRole.VIEWER),
            uuid.uuid4(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["publish", "submit_approval", "approve"])
async def test_mcp_transition_rejects_stale_lineage_before_replacing_current(
    transition: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context(ProjectRole.ADMIN)
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    current_version_id = uuid.uuid4()
    stale_parent_id = uuid.uuid4()
    slot = SimpleNamespace(
        id=uuid.uuid4(),
        name="api-key",
        purpose="API key",
        payload_schema={"query": ["key"]},
        required=True,
    )
    record = SimpleNamespace(
        row=SimpleNamespace(
            id=version_id,
            workflow_status=(WorkflowStatus.PENDING_APPROVAL.value if transition == "approve" else WorkflowStatus.DRAFT.value),
            supersedes_version_id=stale_parent_id,
        ),
        slots=(slot,) if transition != "publish" else (),
        grants=(),
    )
    asset = SimpleNamespace(
        id=asset_id,
        status="active",
        version=4,
        current_published_version_id=current_version_id,
    )

    class Session:
        async def flush(self) -> None:
            raise AssertionError("stale lineage must not flush")

    class Repository:
        session = Session()

        async def get_project_asset(self, _actor, _asset_id, *, for_update: bool):
            assert for_update is True
            return asset

        async def lock_project(self, _actor) -> None:
            return None

        async def _get_project_asset_after_lock(self, _actor, _asset_id):
            return asset

        async def get_project_version(self, _actor, _asset_id, _version_id, *, for_update: bool):
            assert for_update is True
            return record

    service = service_module.McpService(lambda: None)

    async def execute(_actor, operation, governance=None):
        del governance
        return await operation(Repository())

    service._execute = execute

    with pytest.raises(service_module.AssetConflict):
        if transition == "publish":
            await service.publish(
                actor,
                asset_id,
                version_id,
                expected_asset_version=4,
            )
        elif transition == "submit_approval":
            await service.submit_approval(
                actor,
                asset_id,
                version_id,
                expected_asset_version=4,
            )
        else:
            await service.approve(
                actor,
                asset_id,
                version_id,
                {"api-key": uuid.uuid4()},
                expected_asset_version=4,
            )

    assert asset.current_published_version_id == current_version_id
    assert record.row.supersedes_version_id == stale_parent_id


@pytest.mark.asyncio
async def test_runtime_system_mcp_authoring_and_generic_approval_stop_before_storage() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("bootstrap-only rejection must not open storage")

    service = service_module.McpService(ExplodingFactory())
    actor = _system_context()
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()

    with pytest.raises(AssetForbidden):
        await service.create_asset(
            actor,
            service_module.CreateMcpServer("runtime-system", "Runtime System"),
        )
    with pytest.raises(AssetForbidden):
        await service.create_version(
            actor,
            asset_id,
            _safe_definition(service_module),
            expected_asset_version=1,
        )
    with pytest.raises(AssetForbidden):
        await service.approve(
            actor,
            asset_id,
            version_id,
            {"primary": uuid.uuid4()},
            expected_asset_version=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definition",
    [
        {"transport": "stdio", "command": "uvx", "env": {"API_TOKEN": "never-log-me"}},
        {"transport": "http", "url": "https://mcp.test", "headers": {"Authorization": "Bearer never-log-me"}},
        {"transport": "http", "url": "https://mcp.test", "oauth": {"client_secret": "never-log-me"}},
        {"transport": "http", "url": "https://mcp.test", "oauth": {"refreshToken": "never-log-me"}},
    ],
)
async def test_mcp_definition_rejects_secret_fields_before_storage(
    definition: dict[str, object],
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    service = service_module.McpService(ExplodingFactory())
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service.create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(**definition),
            expected_asset_version=1,
        )
    assert exc_info.value.request_id == "req-mcp-unit"
    assert "never-log-me" not in str(exc_info.value)
    assert "never-log-me" not in repr(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "secret_value"),
    [
        ({"transport": "stdio", "command": "uvx", "env": {"CLIENTSECRET": "client-value"}}, "client-value"),
        ({"transport": "stdio", "command": "uvx", "env": {"PRIVATEKEY": "private-value"}}, "private-value"),
        ({"transport": "stdio", "command": "uvx", "env": {"APIKEY": "api-value"}}, "api-value"),
        ({"transport": "stdio", "command": "uvx", "env": {"ACCESSTOKEN": "access-value"}}, "access-value"),
        ({"transport": "stdio", "command": "uvx", "env": {"PUBLIC_SETTING": "Bearer bearer-value"}}, "bearer-value"),
        ({"transport": "http", "url": "https://mcp.test", "headers": {"X-Mode": "Basic basic-value"}}, "basic-value"),
        ({"transport": "http", "url": "https://mcp.test", "routing": {"auth": "client_secret=client-value"}}, "client-value"),
        ({"transport": "http", "url": "https://mcp.test", "routing": {"auth": ["password=password-value"]}}, "password-value"),
        ({"transport": "http", "url": "https://mcp.test", "tool_overrides": {"auth": {"value": "token=token-value"}}}, "token-value"),
        (
            {
                "transport": "http",
                "url": "https://mcp.test",
                "tool_overrides": {"auth": "-----BEGIN PRIVATE KEY-----\nprivate-value"},
            },
            "private-value",
        ),
    ],
)
async def test_mcp_definition_rejects_compact_keys_and_recursive_secret_values_before_storage(
    definition: dict[str, object],
    secret_value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    with pytest.raises(AssetValidationFailed) as exc_info:
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(**definition),
            expected_asset_version=1,
        )
    assert secret_value not in str(exc_info.value)
    assert secret_value not in repr(exc_info.value)
    assert secret_value not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "secret_value"),
    [
        pytest.param(
            {
                "description": "connect with password=description-marker",
                "transport": "http",
                "url": "https://mcp.test",
            },
            "description-marker",
            id="description-assignment",
        ),
        pytest.param(
            {
                "transport": "stdio",
                "command": "mcp --client-secret=command-marker",
            },
            "command-marker",
            id="command-option",
        ),
        pytest.param(
            {
                "transport": "stdio",
                "command": "mcp",
                "args": ("--verbose", "--api-key=args-marker"),
            },
            "args-marker",
            id="args-option",
        ),
        pytest.param(
            {
                "transport": "http",
                "url": "https://mcp.test/tools?api%5Fkey=query-marker",
            },
            "query-marker",
            id="url-sensitive-query",
        ),
        pytest.param(
            {
                "transport": "http",
                "url": "https://public:userinfo-marker@mcp.test/tools",
            },
            "userinfo-marker",
            id="url-userinfo",
        ),
        pytest.param(
            {
                "transport": "stdio",
                "command": "mcp",
                "env": {"CLIENTSECRET": "env-key-marker"},
            },
            "env-key-marker",
            id="env-key",
        ),
        pytest.param(
            {
                "transport": "stdio",
                "command": "mcp",
                "env": {"PUBLIC_SETTING": "Bearer env-value-marker"},
            },
            "env-value-marker",
            id="env-value",
        ),
        pytest.param(
            {
                "transport": "http",
                "url": "https://mcp.test",
                "headers": {"X-Auth": "header-key-marker"},
            },
            "header-key-marker",
            id="header-auth-carrier",
        ),
        pytest.param(
            {
                "transport": "http",
                "url": "https://mcp.test",
                "oauth": {
                    "enabled": True,
                    "extra_token_params": {"resource": "client_secret=oauth-marker"},
                },
            },
            "oauth-marker",
            id="oauth-recursive-value",
        ),
        pytest.param(
            {
                "transport": "http",
                "url": "https://mcp.test",
                "routing": {
                    "fallback": "https://route.test/api?access%5Ftoken=routing-marker",
                },
            },
            "routing-marker",
            id="routing-url-query",
        ),
        pytest.param(
            {
                "transport": "http",
                "url": "https://mcp.test",
                "tool_overrides": {
                    "search": {"argument": "--private-key=override-marker"},
                },
            },
            "override-marker",
            id="tool-override-option",
        ),
    ],
)
async def test_mcp_definition_field_complete_secret_scan_runs_before_storage(
    definition: dict[str, object],
    secret_value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    with pytest.raises(AssetValidationFailed) as exc_info:
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(**definition),
            expected_asset_version=1,
        )
    assert secret_value not in str(exc_info.value)
    assert secret_value not in repr(exc_info.value)
    assert secret_value not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("--api-key", "args-api-key-marker"), id="api-key"),
        pytest.param(("--token", "args-token-marker"), id="token"),
        pytest.param(("--access-token", "args-access-token-marker"), id="access-token"),
        pytest.param(("--refresh-token", "args-refresh-token-marker"), id="refresh-token"),
        pytest.param(("--client-secret", "args-client-secret-marker"), id="client-secret"),
        pytest.param(("--password", "args-password-marker"), id="password"),
        pytest.param(("--private-key", "args-private-key-marker"), id="private-key"),
    ],
)
async def test_mcp_definition_rejects_separated_secret_carrier_args_before_storage(
    args: tuple[str, ...],
    caplog: pytest.LogCaptureFixture,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    marker = args[1]
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(transport="stdio", command="mcp", args=args),
            expected_asset_version=1,
        )
    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value)
    assert marker not in caplog.text


@pytest.mark.asyncio
async def test_mcp_definition_rejects_separated_secret_carrier_command_before_storage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    command = "mcp --client-secret command-secret-marker"
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(transport="stdio", command=command),
            expected_asset_version=1,
        )
    assert command not in str(exc_info.value)
    assert command not in repr(exc_info.value)
    assert command not in caplog.text
    assert "command-secret-marker" not in caplog.text


@pytest.mark.asyncio
async def test_mcp_definition_rejects_secret_carrier_option_without_inspecting_next_arg() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    observed: list[str] = []

    class ObservedSecret(str):
        def __len__(self) -> int:
            observed.append("length")
            return super().__len__()

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    with pytest.raises(AssetValidationFailed):
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(
                transport="stdio",
                command="mcp",
                args=("--api-key", ObservedSecret("uninspected-secret-marker")),
            ),
            expected_asset_version=1,
        )
    assert observed == []


def test_mcp_sensitive_cli_option_delegates_to_shared_key_taxonomy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    inspected: list[str] = []

    def fake_sensitive_key(value: str) -> bool:
        inspected.append(value)
        return value == "future_carrier"

    monkeypatch.setattr(
        service_module.McpService,
        "_sensitive_key",
        staticmethod(fake_sensitive_key),
    )

    assert service_module.McpService._is_sensitive_cli_option("--future-carrier=marker") is True
    assert inspected == ["future_carrier"]


@pytest.mark.parametrize(
    "carrier",
    [
        "APIKEY",
        "api_key",
        "api-key",
        "ApiKey",
        "CLIENTSECRET",
        "client_secret",
        "client-secret",
        "ClientSecret",
        "ACCESSTOKEN",
        "access_token",
        "access-token",
        "AccessToken",
        "PRIVATEKEY",
        "private_key",
        "private-key",
        "PrivateKey",
        "secret",
        "passwd",
        "access-key",
        "auth",
        "authorization",
        "cookie",
        "credential",
    ],
)
def test_mcp_sensitive_cli_option_normalizes_shared_taxonomy_variants(carrier: str) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    assert service_module.McpService._sensitive_key(carrier) is True
    for token in (
        carrier,
        f"-{carrier}",
        f"--{carrier}",
        f"--{carrier}=taxonomy-assignment-marker",
        f"--{carrier.replace('-', '_').swapcase()}",
    ):
        assert service_module.McpService._is_sensitive_cli_option(token) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "carrier",
    [
        "APIKEY",
        "CLIENTSECRET",
        "ACCESSTOKEN",
        "PRIVATEKEY",
        "secret",
        "passwd",
        "access-key",
        "auth",
        "authorization",
        "cookie",
        "credential",
    ],
)
async def test_mcp_definition_rejects_compact_or_undashed_secret_carrier_before_storage(
    carrier: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    marker = f"{carrier.lower()}-undashed-marker"
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(
                transport="stdio",
                command="mcp",
                args=(carrier, marker),
            ),
            expected_asset_version=1,
        )
    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value)
    assert marker not in caplog.text


@pytest.mark.parametrize(
    "definition",
    [
        pytest.param(
            {
                "description": "Ordinary passwordless issue tracker",
                "transport": "http",
                "url": "https://mcp.test/tools/read",
            },
            id="ordinary-description-url",
        ),
        pytest.param(
            {
                "description": "Remote tools",
                "transport": "http",
                "url": "https://mcp.test/tools",
                "routing": {
                    "strategy": "round_robin",
                    "fallback": "https://route.test/api?mode=read",
                },
                "tool_overrides": {
                    "search": {"enabled": True, "description": "ordinary public search"},
                },
            },
            id="ordinary-routing-overrides",
        ),
    ],
)
def test_mcp_definition_field_complete_scan_allows_nonsecret_metadata(
    definition: dict[str, object],
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    normalized = service_module.McpService._validate_definition(
        _context(),
        service_module.McpDefinition(**definition),
        endpoint_policy=_endpoint_policy(str(definition["url"])),
    )

    assert normalized.description == definition["description"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"command": ["TOKEN"]},
        {"env": "TOKEN"},
        {"env": ["TOKEN", "TOKEN"]},
        {"oauth": [""]},
    ],
)
async def test_mcp_slot_schema_is_validated_before_storage(schema: dict[str, object]) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("invalid slot schema must not open storage")

    definition = dataclasses.replace(
        _safe_definition(service_module),
        credential_slots=(service_module.McpCredentialSlot("primary", "Auth", schema),),
    )
    with pytest.raises(AssetValidationFailed):
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            definition,
            expected_asset_version=1,
        )


@pytest.mark.asyncio
async def test_editor_cannot_approve_credential_mcp_before_storage() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("authorization must happen before storage")

    service = service_module.McpService(ExplodingFactory())
    with pytest.raises(AssetForbidden):
        await service.approve(
            _context(ProjectRole.EDITOR),
            uuid.uuid4(),
            uuid.uuid4(),
            {"primary": uuid.uuid4()},
            expected_asset_version=3,
        )


@pytest.mark.asyncio
async def test_approved_project_mcp_enqueues_discovery_with_the_new_grant_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context(ProjectRole.ADMIN)
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    credential_version_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    slot = SimpleNamespace(
        id=uuid.uuid4(),
        name="primary",
        purpose="Authorization header",
        payload_schema={"headers": ["Authorization"]},
        required=True,
    )
    row = SimpleNamespace(
        id=version_id,
        workflow_status=WorkflowStatus.PENDING_APPROVAL.value,
        supersedes_version_id=None,
        payload_checksum="a" * 64,
        reviewed_at=None,
        reviewed_by_user_id=None,
    )
    record = SimpleNamespace(row=row, slots=(slot,), grants=())
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        project_id=actor.project_id,
        status="active",
        current_published_version_id=None,
        version=3,
    )
    grant = SimpleNamespace(id=grant_id, status="active")
    session = SimpleNamespace(flush=AsyncMock())
    repository = SimpleNamespace(
        session=session,
        create_grants=AsyncMock(return_value=(grant,)),
    )
    discovery_attempts = SimpleNamespace(enqueue=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), status="queued")))
    monkeypatch.setattr(
        service_module,
        "CredentialRepository",
        lambda repository_session: SimpleNamespace(),
    )
    monkeypatch.setattr(
        service_module,
        "McpToolDiscoveryAttemptRepository",
        lambda repository_session: discovery_attempts,
        raising=False,
    )
    service = service_module.McpService(lambda: None)
    service._lock_project_first = AsyncMock()
    service._get_asset = AsyncMock(return_value=asset)
    service._get_version = AsyncMock(return_value=record)
    service._lock_credential_versions = AsyncMock(return_value={credential_version_id: SimpleNamespace(version=SimpleNamespace(id=credential_version_id))})
    service._validate_slot_credential = lambda *_args: None
    service._validate_transition_definition = lambda *_args: None
    service._version_view = lambda value: SimpleNamespace(
        id=value.row.id,
        workflow_status=WorkflowStatus(value.row.workflow_status),
    )

    async def execute(_actor, operation, governance=None):
        del governance
        return await operation(repository)

    service._execute = execute

    result = await service.approve(
        actor,
        asset_id,
        version_id,
        {"primary": credential_version_id},
        expected_asset_version=3,
    )

    assert result.workflow_status is WorkflowStatus.PUBLISHED
    discovery_attempts.enqueue.assert_awaited_once()
    discovery_call = discovery_attempts.enqueue.await_args.kwargs
    assert discovery_call["project_id"] == actor.project_id
    assert discovery_call["requested_by_user_id"] == actor.user_id
    assert discovery_call["mcp_server_id"] == asset_id
    assert discovery_call["mcp_server_version_id"] == version_id
    assert discovery_call["payload_checksum"] == row.payload_checksum
    assert discovery_call["grant_digest"] == service_module.mcp_grant_closure_digest((grant_id,))
    assert discovery_call["trigger"] == "auto"


@pytest.mark.asyncio
async def test_manual_tool_discovery_requires_current_published_closure_and_is_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context(ProjectRole.EDITOR)
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    now = datetime.now(UTC)
    asset = SimpleNamespace(
        id=asset_id,
        scope="project",
        project_id=actor.project_id,
        status="active",
        current_published_version_id=version_id,
    )
    record = SimpleNamespace(
        row=SimpleNamespace(
            id=version_id,
            workflow_status=WorkflowStatus.PUBLISHED.value,
            payload_checksum="b" * 64,
        ),
        slots=(SimpleNamespace(id=uuid.uuid4(), required=True),),
        grants=(SimpleNamespace(id=grant_id, status="active"),),
    )
    stored = SimpleNamespace(
        attempt_id=attempt_id,
        project_id=actor.project_id,
        mcp_server_id=asset_id,
        mcp_server_version_id=version_id,
        requested_by_user_id=actor.user_id,
        trigger="manual",
        payload_checksum=record.row.payload_checksum,
        grant_digest=service_module.mcp_grant_closure_digest((grant_id,)),
        status="queued",
        requested_at=now,
        started_at=None,
        completed_at=None,
        public_error_code=None,
        revision=1,
    )
    discovery_attempts = SimpleNamespace(
        enqueue=AsyncMock(return_value=stored),
        get=AsyncMock(return_value=stored),
        latest_for_version=AsyncMock(return_value=stored),
        active_for_closure=AsyncMock(return_value=None),
    )
    repository = SimpleNamespace(
        session=SimpleNamespace(),
        get_project_visible_version=AsyncMock(return_value=record),
    )
    monkeypatch.setattr(
        service_module,
        "McpToolDiscoveryAttemptRepository",
        lambda repository_session: discovery_attempts,
        raising=False,
    )
    monkeypatch.setattr(
        service_module,
        "lock_mcp_credential_closure",
        AsyncMock(return_value=SimpleNamespace(grant_ids=(grant_id,))),
    )
    service = service_module.McpService(lambda: None)
    service._get_asset = AsyncMock(return_value=asset)
    service._get_version = AsyncMock(return_value=record)
    service._validate_transition_definition = lambda *_args: None

    async def execute(_actor, operation, governance=None):
        del governance
        return await operation(repository)

    service._execute = execute

    requested = await service.request_tool_discovery(actor, asset_id, version_id)
    latest = await service.get_tool_discovery_attempt(
        actor,
        asset_id,
        version_id,
    )
    exact = await service.get_tool_discovery_attempt(
        actor,
        asset_id,
        version_id,
        attempt_id=attempt_id,
    )

    assert requested.id == attempt_id
    assert requested.status == "queued"
    assert latest == requested
    assert exact == requested
    discovery_attempts.enqueue.assert_awaited_once()
    discovery_call = discovery_attempts.enqueue.await_args.kwargs
    assert discovery_call["project_id"] == actor.project_id
    assert discovery_call["requested_by_user_id"] == actor.user_id
    assert discovery_call["mcp_server_id"] == asset_id
    assert discovery_call["mcp_server_version_id"] == version_id
    assert discovery_call["payload_checksum"] == record.row.payload_checksum
    assert discovery_call["grant_digest"] == stored.grant_digest
    assert discovery_call["trigger"] == "manual"
    discovery_attempts.latest_for_version.assert_awaited_once_with(
        actor.project_id,
        asset_id,
        version_id,
    )
    discovery_attempts.get.assert_awaited_once_with(actor.project_id, attempt_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential_versions",
    [
        {"": uuid.uuid4()},
        {"primary": "not-a-uuid"},
        [("primary", uuid.uuid4())],
    ],
)
async def test_mcp_approval_rejects_invalid_slot_mapping_before_storage(
    credential_versions: object,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("invalid slot mapping must not open storage")

    with pytest.raises(AssetValidationFailed):
        await service_module.McpService(ExplodingFactory()).approve(
            _context(ProjectRole.ADMIN),
            uuid.uuid4(),
            uuid.uuid4(),
            credential_versions,
            expected_asset_version=3,
        )


def test_mcp_approval_copies_slot_mapping_before_async_storage() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    original_id = uuid.uuid4()
    caller_mapping = {"primary": original_id}

    normalized = service_module.McpService._validate_credential_bindings(
        _context(ProjectRole.ADMIN),
        caller_mapping,
    )
    caller_mapping["primary"] = uuid.uuid4()

    assert normalized == {"primary": original_id}
    with pytest.raises(TypeError):
        normalized["primary"] = uuid.uuid4()


@pytest.mark.parametrize(
    "field_update",
    [
        {"routing": {"nested": {"apiKey": "never-log-me"}}},
        {"tool_overrides": {"search": {"private_key": "never-log-me"}}},
    ],
)
def test_mcp_definition_rejects_nested_secret_config(field_update: dict[str, object]) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    definition = dataclasses.replace(_safe_definition(service_module), **field_update)

    with pytest.raises(AssetValidationFailed) as exc_info:
        service_module.McpService._validate_definition(
            _context(),
            definition,
            endpoint_policy=_endpoint_policy(str(definition.url)),
        )
    assert "never-log-me" not in str(exc_info.value)


def test_mcp_definition_rejects_project_oauth_until_private_runtime_supports_it() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    definition = dataclasses.replace(
        _safe_definition(service_module),
        oauth={
            "enabled": True,
            "token_url": "https://identity.example.test/oauth/token",
            "grant_type": "client_credentials",
            "client_id": "public-client",
            "token_field": "access_token",
            "token_type_field": "token_type",
            "expires_in_field": "expires_in",
        },
    )

    with pytest.raises(AssetValidationFailed):
        service_module.McpService._validate_definition(
            _context(),
            definition,
            endpoint_policy=_endpoint_policy(str(definition.url)),
        )


def test_mcp_definition_keeps_packaged_system_oauth_read_compatible() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    definition = dataclasses.replace(
        _safe_definition(service_module),
        oauth={
            "enabled": True,
            "token_url": "https://identity.example.test/oauth/token",
            "grant_type": "client_credentials",
            "client_id": "public-client",
            "token_field": "access_token",
        },
    )

    normalized = service_module.McpService._validate_definition(
        _system_context(),
        definition,
    )

    assert normalized.oauth["token_url"] == "https://identity.example.test/oauth/token"


def test_mcp_transition_revalidates_historical_json_mapping_proxies() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context(ProjectRole.ADMIN)
    url = "https://historical.example.test/mcp"
    service = service_module.McpService(
        lambda: None,
        endpoint_policy=_endpoint_policy(url),
    )
    row = SimpleNamespace(
        description="Historical MCP definition",
        transport="http",
        command=None,
        args=[],
        url=url,
        non_secret_env={},
        non_secret_headers={},
        oauth_metadata={},
        routing={"strategy": {"order": ["primary", "secondary"]}},
        tool_overrides={"search": {"enabled": True}},
        timeout_seconds=30,
        payload_checksum="",
    )
    record = SimpleNamespace(row=row, slots=())
    historical = service._definition_from_record(record)
    assert isinstance(historical.routing, MappingProxyType)
    row.payload_checksum = service._checksum(historical)

    normalized = service._validate_transition_definition(actor, record)

    assert normalized.routing == {
        "strategy": {"order": ["primary", "secondary"]},
    }
    assert normalized.tool_overrides == {"search": {"enabled": True}}
    row.routing["strategy"]["order"].append("late-row-mutation")
    assert normalized.routing["strategy"]["order"] == ["primary", "secondary"]


def test_mcp_definition_canonicalizes_slot_order_for_historical_checksum() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context(ProjectRole.ADMIN)
    url = "https://historical.example.test/mcp"
    service = service_module.McpService(
        lambda: None,
        endpoint_policy=_endpoint_policy(url),
    )
    slots = tuple(
        service_module.McpCredentialSlot(
            name,
            f"{name} credential",
            {"headers": (header,)},
            required=name != "refresh",
        )
        for name, header in (
            ("secondary", "X-Secondary"),
            ("primary", "X-Primary"),
            ("refresh", "X-Refresh"),
        )
    )
    normalized = service._validate_definition(
        actor,
        service_module.McpDefinition(
            transport="http",
            url=url,
            credential_slots=slots,
        ),
        endpoint_policy=service._endpoint_policy,
    )
    reordered = service._validate_definition(
        actor,
        dataclasses.replace(
            normalized,
            credential_slots=tuple(reversed(slots)),
        ),
        endpoint_policy=service._endpoint_policy,
    )

    assert [slot.name for slot in normalized.credential_slots] == [
        "primary",
        "refresh",
        "secondary",
    ]
    assert service._checksum(normalized) == service._checksum(reordered)

    row = SimpleNamespace(
        description=normalized.description,
        transport=normalized.transport,
        command=normalized.command,
        args=list(normalized.args),
        url=normalized.url,
        non_secret_env=dict(normalized.env),
        non_secret_headers=dict(normalized.headers),
        oauth_metadata=dict(normalized.oauth),
        routing=dict(normalized.routing),
        tool_overrides=dict(normalized.tool_overrides),
        timeout_seconds=normalized.timeout_seconds,
        payload_checksum=service._checksum(normalized),
    )
    record_slots = tuple(
        SimpleNamespace(
            name=slot.name,
            purpose=slot.purpose,
            payload_schema={key: list(values) for key, values in slot.payload_schema.items()},
            required=slot.required,
        )
        for slot in sorted(slots, key=lambda slot: slot.name)
    )

    historical = service._validate_transition_definition(
        actor,
        SimpleNamespace(row=row, slots=record_slots),
    )

    assert service._checksum(historical) == row.payload_checksum


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["submit_approval", "approve", "publish"])
async def test_mcp_transition_revalidates_historical_project_definition(
    transition: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context(ProjectRole.ADMIN)
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    slot = service_module.McpCredentialSlot(
        "primary",
        "Authorization header",
        {"headers": ("Authorization",)},
    )
    slots = (
        SimpleNamespace(
            id=uuid.uuid4(),
            name=slot.name,
            purpose=slot.purpose,
            payload_schema={"headers": ["Authorization"]},
            required=True,
        ),
    )
    definition = service_module.McpDefinition(
        transport="http",
        url="https://historical.example.test/mcp",
        credential_slots=(slot,) if transition != "publish" else (),
    )
    row = SimpleNamespace(
        id=version_id,
        mcp_server_id=asset_id,
        workflow_status=("pending_approval" if transition == "approve" else "draft"),
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
        payload_checksum=service_module.McpService._checksum(definition),
        supersedes_version_id=None,
    )
    record = SimpleNamespace(
        row=row,
        slots=slots if transition != "publish" else (),
        grants=(),
    )
    asset = SimpleNamespace(
        status="active",
        version=1,
        current_published_version_id=None,
    )

    class Session:
        async def flush(self) -> None:
            return None

    class Repository:
        session = Session()

        async def get_project_asset(
            self,
            _actor,
            _asset_id,
            *,
            for_update: bool,
        ):
            assert for_update is True
            return asset

        async def lock_project(self, _actor) -> None:
            return None

        async def _get_project_asset_after_lock(self, _actor, _asset_id):
            return asset

        async def get_project_version(
            self,
            _actor,
            _asset_id,
            _version_id,
            *,
            for_update: bool,
        ):
            assert for_update is True
            return record

    service = service_module.McpService(
        lambda: None,
        endpoint_policy=_endpoint_policy("https://allowed.example.test/mcp"),
    )

    async def execute(_actor, operation, governance=None):
        del governance
        return await operation(Repository())

    service._execute = execute
    service._version_view = lambda value: value

    with pytest.raises(AssetValidationFailed):
        if transition == "submit_approval":
            await service.submit_approval(
                actor,
                asset_id,
                version_id,
                expected_asset_version=1,
            )
        elif transition == "approve":
            await service.approve(
                actor,
                asset_id,
                version_id,
                {"primary": uuid.uuid4()},
                expected_asset_version=1,
            )
        else:
            await service.publish(
                actor,
                asset_id,
                version_id,
                expected_asset_version=1,
            )


@pytest.mark.asyncio
async def test_mcp_pool_timeout_is_mapped_to_safe_storage_unavailable() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class TimeoutFactory:
        def __call__(self):
            raise SATimeoutError("postgresql://admin:never-log-me@db.example.test/app")

    with pytest.raises(service_module.AssetStorageUnavailable) as exc_info:
        await service_module.McpService(TimeoutFactory()).get(_context(), uuid.uuid4())
    assert exc_info.value.__cause__ is None
    assert "never-log-me" not in str(exc_info.value)
    assert "never-log-me" not in repr(exc_info.value)
    assert "postgresql" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_mcp_programming_session_error_is_not_mapped_to_503() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    programming_error = InvalidRequestError("programming failure")

    class InvalidFactory:
        def __call__(self):
            raise programming_error

    with pytest.raises(InvalidRequestError) as exc_info:
        await service_module.McpService(InvalidFactory()).get(_context(), uuid.uuid4())
    assert exc_info.value is programming_error
