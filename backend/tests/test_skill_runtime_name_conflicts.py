from __future__ import annotations

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import binding_service as binding_service_module
from app.shared_assets.binding_repository import BindingRepository, BindingTarget
from app.shared_assets.errors import (
    AssetResolutionUnavailable,
    SkillRuntimeNameConflict,
)
from app.shared_assets.models import AssetKind, AssetScope, AssetSelection
from app.shared_assets.resolver import ProjectAssetResolver, _ResolvedRecord
from app.shared_assets.skill_repository import SkillRepository
from deerflow.persistence.shared_assets import SkillRow, SkillVersionRow


def _project_admin() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="req-skill-runtime-name",
    )


def _skill_record(
    scope: AssetScope,
    slug: str,
    *,
    asset_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> _ResolvedRecord:
    selected_asset_id = asset_id or uuid.uuid4()
    asset = SkillRow(
        id=selected_asset_id,
        scope=scope.value,
        project_id=project_id if scope is AssetScope.PROJECT else None,
        slug=slug,
        display_name=slug,
        status="active",
        current_version_id=None,
        revision=1,
        created_by_user_id=str(uuid.uuid4()),
    )
    version = SkillVersionRow(
        id=uuid.uuid4(),
        skill_id=selected_asset_id,
        version_number=1,
        description="runtime name test",
        frontmatter={"name": slug},
        compatibility=None,
        secret_requirements=[],
        scan_decision="allow",
        scan_summary={"rule_ids": []},
        payload_checksum="a" * 64,
        created_by_user_id=str(uuid.uuid4()),
    )
    asset.current_version_id = version.id
    return _ResolvedRecord(scope, asset, version)


def test_run_closure_rejects_cross_scope_skill_runtime_name_collision() -> None:
    project_id = uuid.uuid4()
    records = (
        _skill_record(AssetScope.PROJECT, "shared-name", project_id=project_id),
        _skill_record(AssetScope.SYSTEM, "shared-name"),
    )

    with pytest.raises(AssetResolutionUnavailable):
        ProjectAssetResolver._assert_unique_skill_runtime_names(
            records,
            "req-runtime-name-conflict",
        )


def test_run_closure_allows_historical_versions_of_the_same_skill_asset() -> None:
    asset_id = uuid.uuid4()
    first = _skill_record(AssetScope.PROJECT, "same-asset", asset_id=asset_id)
    second = _skill_record(AssetScope.PROJECT, "same-asset", asset_id=asset_id)

    ProjectAssetResolver._assert_unique_skill_runtime_names(
        (first, second),
        "req-runtime-name-history",
    )


class _ScalarSession:
    def __init__(self, value: bool) -> None:
        self.value = value
        self.statements: list[object] = []

    async def scalar(self, statement: object) -> bool:
        self.statements.append(statement)
        return self.value


class _TransactionSession:
    async def __aenter__(self) -> _TransactionSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _TransactionSession:
        return self


class _ConflictBindingRepository:
    target: BindingTarget
    events: list[str] = []

    def __init__(self, session: _TransactionSession) -> None:
        self.session = session

    async def lock_project(self, _actor: object) -> None:
        self.events.append("project")

    async def get_binding(self, *_args: object, **_kwargs: object) -> None:
        self.events.append("binding")
        return None

    async def lock_target(
        self,
        _actor: object,
        _selection: AssetSelection,
    ) -> BindingTarget:
        self.events.append("target")
        return self.target

    async def ensure_system_skill_runtime_name_available(
        self,
        actor: ProjectContext,
        _target: BindingTarget,
    ) -> None:
        self.events.append("runtime-name")
        raise SkillRuntimeNameConflict(actor.request_id)

    async def validate_target_dependencies(self, *_args: object) -> None:
        self.events.append("dependencies")

    async def add_binding(self, *_args: object) -> object:
        raise AssertionError("a conflicting Skill must not be bound")


def _postgres_sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


@pytest.mark.asyncio
async def test_system_skill_enable_rejects_active_project_runtime_name() -> None:
    actor = _project_admin()
    target_record = _skill_record(AssetScope.SYSTEM, "shared-name")
    session = _ScalarSession(True)
    repository = BindingRepository(session)  # type: ignore[arg-type]

    with pytest.raises(SkillRuntimeNameConflict):
        await repository.ensure_system_skill_runtime_name_available(
            actor,
            BindingTarget(target_record.asset, target_record.version),
        )

    assert len(session.statements) == 1
    sql = _postgres_sql(session.statements[0])
    assert "skills.scope = 'project'" in sql
    assert "skills.project_id" in sql
    assert "skills.status = 'active'" in sql
    assert "lower(skills.slug) = 'shared-name'" in sql


@pytest.mark.asyncio
async def test_system_skill_enable_allows_distinct_project_runtime_names() -> None:
    actor = _project_admin()
    target_record = _skill_record(AssetScope.SYSTEM, "system-only")
    session = _ScalarSession(False)

    await BindingRepository(session).ensure_system_skill_runtime_name_available(  # type: ignore[arg-type]
        actor,
        BindingTarget(target_record.asset, target_record.version),
    )


@pytest.mark.asyncio
async def test_binding_service_checks_runtime_name_before_creating_skill_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _project_admin()
    record = _skill_record(AssetScope.SYSTEM, "shared-name")
    _ConflictBindingRepository.target = BindingTarget(record.asset, record.version)
    _ConflictBindingRepository.events = []
    monkeypatch.setattr(
        binding_service_module,
        "BindingRepository",
        _ConflictBindingRepository,
    )
    service = binding_service_module.BindingService(lambda: _TransactionSession())  # type: ignore[arg-type]

    with pytest.raises(SkillRuntimeNameConflict):
        await service.enable(
            actor,
            AssetSelection(AssetKind.SKILL, record.asset.id, record.version.id),
        )

    assert _ConflictBindingRepository.events == [
        "project",
        "binding",
        "target",
        "runtime-name",
    ]


@pytest.mark.asyncio
async def test_project_skill_activation_rejects_enabled_system_runtime_name() -> None:
    actor = _project_admin()
    record = _skill_record(
        AssetScope.PROJECT,
        "shared-name",
        project_id=actor.project_id,
    )
    session = _ScalarSession(True)
    repository = SkillRepository(session)  # type: ignore[arg-type]

    with pytest.raises(SkillRuntimeNameConflict):
        await repository.ensure_project_skill_runtime_name_available(
            actor,
            record.asset,
        )

    assert len(session.statements) == 1
    sql = _postgres_sql(session.statements[0])
    assert "project_system_skill_bindings" in sql
    assert "project_system_skill_bindings.enabled IS true" in sql
    assert "skills.scope = 'system'" in sql
    assert "lower(skills.slug) = 'shared-name'" in sql


@pytest.mark.asyncio
async def test_project_skill_activation_allows_distinct_system_runtime_names() -> None:
    actor = _project_admin()
    record = _skill_record(
        AssetScope.PROJECT,
        "project-only",
        project_id=actor.project_id,
    )
    session = _ScalarSession(False)

    await SkillRepository(session).ensure_project_skill_runtime_name_available(  # type: ignore[arg-type]
        actor,
        record.asset,
    )
