from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.audit.models import AuditAction
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetConflict, AssetForbidden, AssetValidationFailed
from app.shared_assets.models import AssetKind, AssetSelection


def _context(role: ProjectRole) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-binding-unit",
    )


def _must_not_open_session():
    raise AssertionError("authorization and input validation must run before database access")


class _UnitTransaction:
    def __init__(self) -> None:
        self.enter_count = 0
        self.exit_error: type[BaseException] | None = None

    async def __aenter__(self):
        self.enter_count += 1
        return self

    async def __aexit__(self, exc_type, _exc, _traceback) -> None:
        self.exit_error = exc_type


class _UnitSession:
    def __init__(self) -> None:
        self.transaction = _UnitTransaction()
        self.begin_count = 0
        self.flush_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    def begin(self) -> _UnitTransaction:
        self.begin_count += 1
        return self.transaction

    async def flush(self) -> None:
        self.flush_count += 1


def _binding_row(
    actor: ProjectContext,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    enabled: bool,
    version: int,
):
    now = datetime.now(UTC)
    return SimpleNamespace(
        project_id=actor.project_id,
        system_mcp_server_id=asset_id,
        mcp_server_version_id=version_id,
        enabled=enabled,
        version=version,
        created_by_user_id=str(actor.user_id),
        updated_by_user_id=str(actor.user_id),
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_only_project_admin_can_manage_system_bindings() -> None:
    from app.shared_assets.binding_service import BindingService

    service = BindingService(_must_not_open_session)
    selection = AssetSelection(AssetKind.AGENT, uuid.uuid4(), uuid.uuid4())

    with pytest.raises(AssetForbidden):
        await service.enable(_context(ProjectRole.EDITOR), selection)


@pytest.mark.asyncio
async def test_binding_requires_an_explicit_version() -> None:
    from app.shared_assets.binding_service import BindingService

    service = BindingService(_must_not_open_session)
    selection = AssetSelection(AssetKind.SKILL, uuid.uuid4())

    with pytest.raises(AssetValidationFailed):
        await service.enable(_context(ProjectRole.ADMIN), selection)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing_state", "expected_binding_version", "expected_action"),
    [
        (None, None, "binding.enable"),
        ((False, "current"), 4, "binding.enable"),
        ((True, "previous"), 4, "binding.sync_current"),
    ],
)
async def test_sync_current_mcp_resolves_server_current_and_mutates_atomically(
    monkeypatch: pytest.MonkeyPatch,
    existing_state: tuple[bool, str] | None,
    expected_binding_version: int | None,
    expected_action: str,
) -> None:
    from app.shared_assets import binding_service as service_module

    actor = _context(ProjectRole.ADMIN)
    asset_id = uuid.uuid4()
    current_version_id = uuid.uuid4()
    previous_version_id = uuid.uuid4()
    existing = (
        None
        if existing_state is None
        else _binding_row(
            actor,
            asset_id,
            current_version_id if existing_state[1] == "current" else previous_version_id,
            enabled=existing_state[0],
            version=4,
        )
    )
    session = _UnitSession()
    events: list[str] = []
    target = SimpleNamespace(
        asset=SimpleNamespace(
            id=asset_id,
            current_published_version_id=current_version_id,
        ),
        version=SimpleNamespace(id=current_version_id),
    )

    class Repository:
        def __init__(self, repository_session) -> None:
            assert repository_session is session
            self.session = repository_session

        async def lock_project(self, context) -> None:
            assert context is actor
            events.append("project")

        async def get_binding(self, context, kind, selected_asset_id, *, for_update: bool, required: bool):
            assert context is actor
            assert kind is AssetKind.MCP
            assert selected_asset_id == asset_id
            assert for_update is True
            assert required is False
            events.append("binding")
            return existing

        async def lock_current_system_mcp_target(self, context, selected_asset_id):
            assert context is actor
            assert selected_asset_id == asset_id
            events.append("target")
            return target

        async def validate_target_dependencies(self, context, selection) -> None:
            assert context is actor
            assert selection == AssetSelection(
                AssetKind.MCP,
                asset_id,
                current_version_id,
            )
            events.append("dependencies")

        async def add_binding(self, context, selection):
            assert context is actor
            assert selection.version_id == current_version_id
            events.append("add")
            return _binding_row(
                actor,
                asset_id,
                current_version_id,
                enabled=True,
                version=1,
            )

    monkeypatch.setattr(service_module, "BindingRepository", Repository)
    governance_sink = SimpleNamespace(append_project=AsyncMock())
    service = service_module.BindingService(lambda: session, governance_sink)

    result = await service.sync_current_mcp(
        actor,
        asset_id,
        expected_binding_version=expected_binding_version,
    )

    assert session.begin_count == 1
    assert session.transaction.enter_count == 1
    assert session.transaction.exit_error is None
    expected_events = [
        "project",
        "binding",
        "target",
        "dependencies",
    ]
    if existing is None:
        expected_events.append("add")
    assert events == expected_events
    assert result.asset_id == asset_id
    assert result.version_id == current_version_id
    assert result.enabled is True
    assert result.version == (1 if existing is None else 5)
    governance_sink.append_project.assert_awaited_once()
    assert governance_sink.append_project.await_args.args == (session,)
    assert governance_sink.append_project.await_args.kwargs["asset_id"] == asset_id
    assert governance_sink.append_project.await_args.kwargs["version_id"] == current_version_id
    assert governance_sink.append_project.await_args.kwargs["action"] == expected_action


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing_enabled", "existing_version", "expected_binding_version", "target_locked"),
    [
        (None, None, 1, False),
        (False, 4, None, False),
        (True, 4, None, False),
        (True, 4, 3, False),
        (True, 4, 4, True),
    ],
)
async def test_sync_current_mcp_enforces_exact_existing_revision_and_rejects_enabled_noop(
    existing_enabled: bool | None,
    existing_version: int | None,
    expected_binding_version: int | None,
    target_locked: bool,
) -> None:
    from app.shared_assets.binding_service import BindingService

    actor = _context(ProjectRole.ADMIN)
    asset_id = uuid.uuid4()
    current_version_id = uuid.uuid4()
    target_calls = 0
    existing = (
        None
        if existing_enabled is None
        else _binding_row(
            actor,
            asset_id,
            current_version_id,
            enabled=existing_enabled,
            version=existing_version,
        )
    )

    class Repository:
        session = SimpleNamespace(flush=AsyncMock())

        async def lock_project(self, _actor) -> None:
            return None

        async def get_binding(self, _actor, _kind, _asset_id, *, for_update: bool, required: bool):
            assert for_update is True
            assert required is False
            return existing

        async def lock_current_system_mcp_target(self, _actor, _asset_id):
            nonlocal target_calls
            target_calls += 1
            return SimpleNamespace(
                asset=SimpleNamespace(id=asset_id),
                version=SimpleNamespace(id=current_version_id),
            )

        async def validate_target_dependencies(self, _actor, _selection) -> None:
            raise AssertionError("conflicting sync must not validate dependencies")

    service = BindingService(lambda: None)

    async def execute(_actor, operation, governance=None):
        del governance
        return await operation(Repository())

    service._execute = execute

    with pytest.raises(AssetConflict):
        await service.sync_current_mcp(
            actor,
            asset_id,
            expected_binding_version=expected_binding_version,
        )

    assert target_calls == int(target_locked)
    Repository.session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_current_mcp_is_project_admin_only_before_storage() -> None:
    from app.shared_assets.binding_service import BindingService

    service = BindingService(_must_not_open_session)
    asset_id = uuid.uuid4()
    with pytest.raises(AssetForbidden):
        await service.sync_current_mcp(
            _context(ProjectRole.EDITOR),
            asset_id,
        )
    with pytest.raises(AssetForbidden):
        await service.sync_current_mcp(
            SystemAssetGovernanceContext(
                user_id=uuid.uuid4(),
                request_id="req-system",
                project_id=uuid.uuid4(),
            ),
            asset_id,
        )


def test_sync_current_binding_audit_action_is_registered() -> None:
    from app.shared_assets import audit

    assert audit._ACTIONS["binding.sync_current"] is AuditAction.ASSET_BOUND


@pytest.mark.asyncio
async def test_sync_current_mcp_validates_dependencies_before_mutating_existing_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import binding_service as service_module

    actor = _context(ProjectRole.ADMIN)
    asset_id = uuid.uuid4()
    previous_version_id = uuid.uuid4()
    current_version_id = uuid.uuid4()
    existing = _binding_row(
        actor,
        asset_id,
        previous_version_id,
        enabled=False,
        version=4,
    )
    session = _UnitSession()

    class Repository:
        def __init__(self, repository_session) -> None:
            assert repository_session is session
            self.session = repository_session

        async def lock_project(self, _actor) -> None:
            return None

        async def get_binding(self, _actor, _kind, _asset_id, *, for_update: bool, required: bool):
            assert for_update is True
            assert required is False
            return existing

        async def lock_current_system_mcp_target(self, _actor, _asset_id):
            return SimpleNamespace(
                asset=SimpleNamespace(id=asset_id),
                version=SimpleNamespace(id=current_version_id),
            )

        async def validate_target_dependencies(self, _actor, _selection) -> None:
            raise AssetValidationFailed(actor.request_id)

    monkeypatch.setattr(service_module, "BindingRepository", Repository)
    governance_sink = SimpleNamespace(append_project=AsyncMock())

    with pytest.raises(AssetValidationFailed):
        await service_module.BindingService(
            lambda: session,
            governance_sink,
        ).sync_current_mcp(
            actor,
            asset_id,
            expected_binding_version=4,
        )

    assert existing.mcp_server_version_id == previous_version_id
    assert existing.enabled is False
    assert existing.version == 4
    assert session.flush_count == 0
    assert session.transaction.exit_error is AssetValidationFailed
    governance_sink.append_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_binding_repository_locks_system_mcp_asset_then_its_current_published_version() -> None:
    from app.shared_assets.binding_repository import BindingRepository

    actor = _context(ProjectRole.ADMIN)
    asset_id = uuid.uuid4()
    current_version_id = uuid.uuid4()
    asset = SimpleNamespace(
        id=asset_id,
        status="active",
        current_published_version_id=current_version_id,
    )
    version = SimpleNamespace(
        id=current_version_id,
        mcp_server_id=asset_id,
        workflow_status="published",
    )

    class Result:
        def __init__(self, value) -> None:
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result(asset),
                Result(version),
            ]
        )
    )

    target = await BindingRepository(session).lock_current_system_mcp_target(
        actor,
        asset_id,
    )

    assert target.asset is asset
    assert target.version is version
    assert session.execute.await_count == 2
    asset_statement = session.execute.await_args_list[0].args[0]
    version_statement = session.execute.await_args_list[1].args[0]
    assert asset_statement._for_update_arg is not None
    assert version_statement._for_update_arg is not None
    asset_sql = str(asset_statement).lower()
    version_sql = str(version_statement).lower()
    assert "mcp_servers.scope" in asset_sql
    assert "mcp_servers.project_id is null" in asset_sql
    assert "mcp_server_versions.mcp_server_id" in version_sql
    assert "mcp_server_versions.workflow_status" in version_sql
    assert asset_id in asset_statement.compile().params.values()
    assert asset_id in version_statement.compile().params.values()
    assert current_version_id in version_statement.compile().params.values()
