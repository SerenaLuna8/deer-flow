from __future__ import annotations

import dataclasses
import importlib
import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetValidationFailed,
)
from app.shared_assets.models import AssetKind, AssetSelection


def _context(role: ProjectRole = ProjectRole.ADMIN) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-skill-credential-unit",
    )


def _must_not_open_session():
    raise AssertionError("validation and authorization must run before storage")


def test_skill_credential_binding_service_exposes_secret_free_frozen_contracts() -> None:
    package = importlib.import_module("app.shared_assets")
    module = importlib.import_module("app.shared_assets.skill_credential_service")
    repository_module = importlib.import_module("app.shared_assets.skill_credential_repository")

    assert package.SkillCredentialBindingService is module.SkillCredentialBindingService
    for view_type in (
        module.EligibleSkillCredentialView,
        module.SkillCredentialRequirementView,
        module.SkillCredentialBindingSetView,
    ):
        assert dataclasses.is_dataclass(view_type)
        assert view_type.__dataclass_params__.frozen is True
        names = {field.name for field in dataclasses.fields(view_type)}
        assert names.isdisjoint(
            {
                "ciphertext",
                "nonce",
                "key_id",
                "payload",
                "plaintext",
                "envelope",
            }
        )

    for name, method in inspect.getmembers(
        repository_module.SkillCredentialRepository,
        predicate=inspect.isfunction,
    ):
        if not name.startswith("_"):
            assert "project_id" not in inspect.signature(method).parameters, name


def test_skill_credential_orm_scope_and_historical_snapshot_contract() -> None:
    from deerflow.persistence.private_work import RunSkillCredentialSnapshotRow
    from deerflow.persistence.shared_assets import (
        CredentialRow,
        ProjectSkillCredentialBindingRow,
        ProjectSkillCredentialConfigRow,
    )

    credential_uniques = {tuple(column.name for column in constraint.columns) for constraint in CredentialRow.__table__.constraints if isinstance(constraint, sa.UniqueConstraint)}
    assert ("project_id", "id") in credential_uniques

    binding_foreign_keys = {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in ProjectSkillCredentialBindingRow.__table__.foreign_key_constraints
    }
    assert (
        ("project_id", "credential_id"),
        ("credentials.project_id", "credentials.id"),
    ) in binding_foreign_keys
    config_primary_key = tuple(column.name for column in ProjectSkillCredentialConfigRow.__table__.primary_key.columns)
    assert config_primary_key == (
        "project_id",
        "skill_id",
        "skill_version_id",
    )
    assert (
        ("project_id", "skill_id", "skill_version_id"),
        (
            "project_skill_credential_configs.project_id",
            "project_skill_credential_configs.skill_id",
            "project_skill_credential_configs.skill_version_id",
        ),
    ) in binding_foreign_keys
    skill_version_foreign_key = next(constraint for constraint in ProjectSkillCredentialBindingRow.__table__.foreign_key_constraints if tuple(constraint.column_keys) == ("skill_id", "skill_version_id"))
    assert skill_version_foreign_key.ondelete == "CASCADE"

    config_indexes = {tuple(column.name for column in index.columns) for index in ProjectSkillCredentialConfigRow.__table__.indexes}
    assert ("skill_id", "skill_version_id") in config_indexes
    binding_indexes = {tuple(column.name for column in index.columns) for index in ProjectSkillCredentialBindingRow.__table__.indexes}
    assert {
        ("project_id", "skill_id", "skill_version_id"),
        ("skill_id", "skill_version_id"),
        ("project_id", "credential_id"),
        ("credential_id", "credential_version_id", "status"),
    }.issubset(binding_indexes)
    active_name_index = next(index for index in ProjectSkillCredentialBindingRow.__table__.indexes if index.name == "uq_project_skill_credential_bindings_active_name")
    assert tuple(column.name for column in active_name_index.columns) == (
        "project_id",
        "skill_id",
        "skill_version_id",
        "secret_name",
    )
    binding_uniques = {tuple(column.name for column in constraint.columns) for constraint in ProjectSkillCredentialBindingRow.__table__.constraints if isinstance(constraint, sa.UniqueConstraint)}
    assert (
        "project_id",
        "skill_id",
        "skill_version_id",
        "id",
    ) in binding_uniques

    snapshot_table = RunSkillCredentialSnapshotRow.__table__
    snapshot_targets = {element.target_fullname.split(".", maxsplit=1)[0] for constraint in snapshot_table.foreign_key_constraints for element in constraint.elements}
    assert snapshot_targets == {
        "projects",
        "users",
        "project_memberships",
        "runs",
    }
    snapshot_indexes = {tuple(column.name for column in index.columns) for index in snapshot_table.indexes}
    assert (
        "project_id",
        "owner_user_id",
        "thread_id",
        "run_id",
    ) in snapshot_indexes


def test_skill_credential_migration_matches_scope_and_snapshot_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("deerflow.persistence.migrations.versions.0003_skill_credentials")
    operations: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class Operations:
        def __getattr__(self, name: str):
            def record(*args, **kwargs):
                operations.append((name, args, kwargs))

            return record

    monkeypatch.setattr(migration, "op", Operations())
    migration.upgrade()

    assert (
        "create_unique_constraint",
        (
            "uq_credentials_project_asset_id",
            "credentials",
            ["project_id", "id"],
        ),
        {},
    ) in operations

    tables = {args[0]: args[1:] for name, args, _kwargs in operations if name == "create_table"}
    binding_foreign_keys = {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in tables["project_skill_credential_bindings"]
        if isinstance(constraint, sa.ForeignKeyConstraint)
    }
    assert (
        ("project_id", "credential_id"),
        ("credentials.project_id", "credentials.id"),
    ) in binding_foreign_keys
    config_primary_key = next(constraint for constraint in tables["project_skill_credential_configs"] if isinstance(constraint, sa.PrimaryKeyConstraint))
    assert tuple(config_primary_key._pending_colargs) == (
        "project_id",
        "skill_id",
        "skill_version_id",
    )
    assert (
        ("project_id", "skill_id", "skill_version_id"),
        (
            "project_skill_credential_configs.project_id",
            "project_skill_credential_configs.skill_id",
            "project_skill_credential_configs.skill_version_id",
        ),
    ) in binding_foreign_keys
    skill_version_foreign_key = next(constraint for constraint in tables["project_skill_credential_bindings"] if isinstance(constraint, sa.ForeignKeyConstraint) and tuple(constraint.column_keys) == ("skill_id", "skill_version_id"))
    assert skill_version_foreign_key.ondelete == "CASCADE"
    binding_uniques = {tuple(constraint._pending_colargs) for constraint in tables["project_skill_credential_bindings"] if isinstance(constraint, sa.UniqueConstraint)}
    assert (
        "project_id",
        "skill_id",
        "skill_version_id",
        "id",
    ) in binding_uniques

    snapshot_targets = {element.target_fullname.split(".", maxsplit=1)[0] for constraint in tables["run_skill_credential_snapshots"] if isinstance(constraint, sa.ForeignKeyConstraint) for element in constraint.elements}
    assert snapshot_targets == {
        "projects",
        "users",
        "project_memberships",
        "runs",
    }

    indexes = {(args[1], tuple(args[2])) for name, args, _kwargs in operations if name == "create_index"}
    assert {
        (
            "project_skill_credential_configs",
            ("skill_id", "skill_version_id"),
        ),
        (
            "project_skill_credential_bindings",
            ("project_id", "skill_id", "skill_version_id"),
        ),
        (
            "project_skill_credential_bindings",
            ("skill_id", "skill_version_id"),
        ),
        (
            "project_skill_credential_bindings",
            ("project_id", "credential_id"),
        ),
        (
            "run_skill_credential_snapshots",
            ("project_id", "owner_user_id", "thread_id", "run_id"),
        ),
    }.issubset(indexes)
    active_name_index = next(args for name, args, _kwargs in operations if name == "create_index" and args[0] == "uq_project_skill_credential_bindings_active_name")
    assert tuple(active_name_index[2]) == (
        "project_id",
        "skill_id",
        "skill_version_id",
        "secret_name",
    )


@pytest.mark.asyncio
async def test_skill_credential_binding_read_requires_asset_read_before_storage() -> None:
    module = importlib.import_module("app.shared_assets.skill_credential_service")
    service = module.SkillCredentialBindingService(_must_not_open_session)

    with pytest.raises(AssetForbidden):
        await service.get(
            dataclasses.replace(
                _context(ProjectRole.VIEWER),
                capabilities=frozenset(),
            ),
            uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_skill_credential_binding_replace_requires_credential_approval_before_storage() -> None:
    module = importlib.import_module("app.shared_assets.skill_credential_service")
    service = module.SkillCredentialBindingService(_must_not_open_session)

    with pytest.raises(AssetForbidden):
        await service.replace(
            _context(ProjectRole.EDITOR),
            uuid.uuid4(),
            (),
            expected_revision=0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("expected_revision", [-1, True, "1"])
async def test_skill_credential_binding_rejects_invalid_revision_before_storage(
    expected_revision: object,
) -> None:
    module = importlib.import_module("app.shared_assets.skill_credential_service")
    service = module.SkillCredentialBindingService(_must_not_open_session)

    with pytest.raises(AssetConflict):
        await service.replace(
            _context(),
            uuid.uuid4(),
            (),
            expected_revision=expected_revision,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_skill_credential_binding_rejects_duplicate_names_before_storage() -> None:
    module = importlib.import_module("app.shared_assets.skill_credential_service")
    service = module.SkillCredentialBindingService(_must_not_open_session)
    version_id = uuid.uuid4()

    with pytest.raises(AssetValidationFailed):
        await service.replace(
            _context(),
            uuid.uuid4(),
            (
                module.SkillCredentialBindingInput("API_KEY", version_id),
                module.SkillCredentialBindingInput("API_KEY", version_id),
            ),
            expected_revision=0,
        )


@pytest.mark.asyncio
async def test_skill_credential_api_reads_current_published_version_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets.skill_credential_repository import SkillCredentialTarget
    from app.shared_assets.skill_credential_service import (
        SkillCredentialBindingService,
    )

    context = _context()
    skill_id = uuid.uuid4()
    current_version_id = uuid.uuid4()
    target = SkillCredentialTarget(
        SimpleNamespace(id=skill_id),
        SimpleNamespace(
            id=current_version_id,
            secret_requirements=[],
        ),
    )

    class Repository:
        async def lock_project(self, actor, *, read=False):
            assert actor is context
            assert read is True

        async def lock_configurable_current_published_skill(
            self,
            actor,
            selected_skill_id,
            *,
            read=False,
        ):
            assert actor is context
            assert selected_skill_id == skill_id
            assert read is True
            return target

        async def get_config(
            self,
            actor,
            selected_skill_id,
            selected_version_id,
        ):
            assert actor is context
            assert selected_skill_id == skill_id
            assert selected_version_id == current_version_id
            return None

        async def active_bindings(
            self,
            actor,
            selected_skill_id,
            selected_version_id,
        ):
            assert actor is context
            assert selected_skill_id == skill_id
            assert selected_version_id == current_version_id
            return ()

        async def eligible_credentials(self, actor):
            assert actor is context
            return ()

    service = SkillCredentialBindingService(lambda: None)

    async def execute(_actor, operation, governance=None):
        del governance
        return await operation(Repository())

    monkeypatch.setattr(service, "_execute", execute)
    result = await service.get(context, skill_id)

    assert result.skill_id == skill_id
    assert result.skill_version_id == current_version_id
    assert result.revision == 0


@pytest.mark.asyncio
async def test_skill_credential_configuration_target_allows_project_preconfiguration_only() -> None:
    from sqlalchemy.dialects import postgresql

    from app.shared_assets.skill_credential_repository import (
        SkillCredentialRepository,
    )

    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=Mock(return_value=None),
            )
        )
    )
    context = _context()

    with pytest.raises(AssetNotFound):
        await SkillCredentialRepository(
            session,
        ).lock_configurable_current_published_skill(
            context,
            uuid.uuid4(),
        )

    statement = session.execute.await_args.args[0]
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "skills.scope = 'project'" in sql
    assert "skills.status IN ('active', 'suspended')" in sql
    assert "skills.scope = 'system'" in sql
    assert "skills.status = 'active'" in sql
    assert "project_system_skill_bindings" not in sql


@pytest.mark.asyncio
async def test_skill_credential_closure_rejects_invalid_target_before_storage() -> None:
    module = importlib.import_module("app.shared_assets.skill_credential_closure")

    with pytest.raises(module.SkillCredentialClosureInvalid):
        await module.lock_skill_credential_closures(
            object(),
            uuid.uuid4(),
            (
                module.SkillCredentialClosureTarget(
                    skill_id=uuid.uuid4(),
                    skill_version_id="not-a-uuid",
                ),
            ),
        )


def test_project_skill_credential_binding_api_never_returns_secret_values() -> None:
    from app.gateway.routers import project_assets
    from app.shared_assets.skill_credential_service import (
        EligibleSkillCredentialView,
        SkillCredentialBindingSetView,
        SkillCredentialRequirementView,
    )

    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    credential_version_id = uuid.uuid4()
    service = AsyncMock()
    service.get.return_value = SkillCredentialBindingSetView(
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        revision=3,
        requirements=(
            SkillCredentialRequirementView(
                name="API_KEY",
                optional=False,
                configured=True,
                credential_id=credential_id,
                credential_version_id=credential_version_id,
                credential_display_name="Weather API",
                credential_version_number=2,
                eligible_credentials=(
                    EligibleSkillCredentialView(
                        credential_id=credential_id,
                        credential_version_id=credential_version_id,
                        display_name="Weather API",
                        version_number=2,
                    ),
                ),
            ),
        ),
    )
    app = FastAPI()
    app.include_router(project_assets.project_router)
    context = _context()
    app.dependency_overrides[project_assets.project_asset_context] = lambda: context
    app.dependency_overrides[project_assets.get_skill_credential_binding_service] = lambda: service

    response = TestClient(app).get(f"/api/projects/{context.project_id}/skills/{skill_id}/credential-bindings")

    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 3
    assert body["requirements"][0]["configured"] is True
    assert body["requirements"][0]["credential_display_name"] == "Weather API"
    serialized = str(body).casefold()
    assert "ciphertext" not in serialized
    assert "nonce" not in serialized
    assert "secret-value" not in serialized
    service.get.assert_awaited_once_with(context, skill_id)


def test_project_skill_credential_binding_put_requires_explicit_whole_set() -> None:
    from app.gateway.routers import project_assets

    skill_id = uuid.uuid4()
    service = AsyncMock()
    app = FastAPI()
    app.include_router(project_assets.project_router)
    context = _context()
    app.dependency_overrides[project_assets.project_asset_context] = lambda: context
    app.dependency_overrides[project_assets.get_skill_credential_binding_service] = lambda: service

    response = TestClient(app).put(
        f"/api/projects/{context.project_id}/skills/{skill_id}/credential-bindings",
        json={"expected_revision": 0},
    )

    assert response.status_code == 422
    service.replace.assert_not_awaited()


def test_project_skill_credential_binding_put_accepts_explicit_empty_set() -> None:
    from app.gateway.routers import project_assets
    from app.shared_assets.skill_credential_service import (
        SkillCredentialBindingSetView,
    )

    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()
    service = AsyncMock()
    service.replace.return_value = SkillCredentialBindingSetView(
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        revision=4,
        requirements=(),
    )
    app = FastAPI()
    app.include_router(project_assets.project_router)
    context = _context()
    app.dependency_overrides[project_assets.project_asset_context] = lambda: context
    app.dependency_overrides[project_assets.get_skill_credential_binding_service] = lambda: service

    response = TestClient(app).put(
        f"/api/projects/{context.project_id}/skills/{skill_id}/credential-bindings",
        json={"expected_revision": 3, "bindings": []},
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 4
    service.replace.assert_awaited_once_with(
        context,
        skill_id,
        (),
        expected_revision=3,
    )


def test_project_skill_credential_binding_put_maps_revision_conflict() -> None:
    from app.gateway.routers import project_assets

    skill_id = uuid.uuid4()
    service = AsyncMock()
    service.replace.side_effect = AssetConflict("req-skill-credential-unit")
    app = FastAPI()
    app.include_router(project_assets.project_router)
    context = _context()
    app.dependency_overrides[project_assets.project_asset_context] = lambda: context
    app.dependency_overrides[project_assets.get_skill_credential_binding_service] = lambda: service

    response = TestClient(app).put(
        f"/api/projects/{context.project_id}/skills/{skill_id}/credential-bindings",
        json={"expected_revision": 2, "bindings": []},
    )

    assert response.status_code == 409
    assert "req-skill-credential-unit" in response.text


@pytest.mark.asyncio
async def test_system_skill_binding_fails_closed_when_required_secret_closure_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import binding_repository
    from app.shared_assets.skill_credential_closure import (
        SkillCredentialClosureInvalid,
    )

    async def fail_closure(*_args, **_kwargs):
        raise SkillCredentialClosureInvalid

    monkeypatch.setattr(
        binding_repository,
        "lock_skill_credential_closure",
        fail_closure,
    )
    context = _context()
    selection = AssetSelection(
        AssetKind.SKILL,
        uuid.uuid4(),
        uuid.uuid4(),
    )

    with pytest.raises(AssetValidationFailed):
        await binding_repository.BindingRepository(
            object(),  # type: ignore[arg-type]
        ).validate_target_dependencies(context, selection)


@pytest.mark.asyncio
async def test_project_skill_activation_fails_closed_when_required_secret_is_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import skill_service
    from app.shared_assets.skill_credential_closure import (
        SkillCredentialClosureInvalid,
    )
    from deerflow.persistence.shared_assets import SkillRow, SkillVersionRow

    context = _context()
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset = SkillRow(
        id=skill_id,
        scope="project",
        project_id=context.project_id,
        slug="protected-skill",
        display_name="Protected Skill",
        status="suspended",
        current_published_version_id=version_id,
        version=3,
        created_by_user_id=str(context.user_id),
    )
    version = SkillVersionRow(
        id=version_id,
        skill_id=skill_id,
        version_number=1,
        workflow_status="published",
        description="Protected",
        frontmatter={},
        compatibility=None,
        secret_requirements=[{"name": "API_KEY", "optional": False}],
        scan_decision="allow",
        scan_summary={},
        payload_checksum="a" * 64,
        created_by_user_id=str(context.user_id),
    )

    class Session:
        async def flush(self):
            raise AssertionError("invalid closure must fail before activation")

    class Repository:
        session = Session()

        async def get_project_asset(self, actor, selected_id, *, for_update=False):
            assert actor is context
            assert selected_id == skill_id
            assert for_update is True
            return asset

        async def get_project_version(
            self,
            actor,
            selected_skill_id,
            selected_version_id,
            *,
            for_update=False,
        ):
            assert actor is context
            assert selected_skill_id == skill_id
            assert selected_version_id == version_id
            assert for_update is True
            return skill_service.SkillVersionRecord(version, ())

    async def fail_closure(*_args, **_kwargs):
        raise SkillCredentialClosureInvalid

    monkeypatch.setattr(
        skill_service,
        "lock_skill_credential_closure",
        fail_closure,
    )
    service = skill_service.SkillService(lambda: None)

    async def execute(_actor, operation, governance=None):
        del governance
        return await operation(Repository())

    monkeypatch.setattr(service, "_execute", execute)
    with pytest.raises(AssetValidationFailed):
        await service.activate(
            context,
            skill_id,
            expected_asset_version=3,
        )
    assert asset.status == "suspended"
    assert asset.version == 3


@pytest.mark.asyncio
async def test_credential_grant_migration_includes_compatible_skill_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import credential_service

    context = _context()
    credential_id = uuid.uuid4()
    current_version_id = uuid.uuid4()
    previous_version_id = uuid.uuid4()
    credential = SimpleNamespace(
        id=credential_id,
        project_id=context.project_id,
        scope="project",
        status="active",
        version=2,
        current_version_id=current_version_id,
    )
    current = SimpleNamespace(
        id=current_version_id,
        credential_id=credential_id,
        status="active",
        payload_schema={"env": ["API_KEY"]},
    )
    active_binding = SimpleNamespace(
        binding=SimpleNamespace(
            project_id=context.project_id,
            secret_name="API_KEY",
            credential_id=credential_id,
            credential_version_id=previous_version_id,
        )
    )

    class Repository:
        migrated = ()

        async def get_project_credential(
            self,
            actor,
            selected_id,
            *,
            for_update=False,
        ):
            assert actor is context
            assert selected_id == credential_id
            assert for_update is True
            return credential

        async def lock_current_version(self, selected, *, request_id):
            assert selected is credential
            assert request_id == context.request_id
            return current

        async def lock_active_grants(self, selected):
            assert selected is credential
            return ()

        async def lock_active_skill_bindings(self, selected):
            assert selected is credential
            return (active_binding,)

        async def migrate_grants(self, *_args, **_kwargs):
            return ()

        async def migrate_skill_bindings(self, bindings, target, **_kwargs):
            assert bindings == (active_binding,)
            assert target is current
            assert _kwargs["credential_id"] == credential_id
            self.migrated = bindings
            return ()

    repository = Repository()
    service = credential_service.CredentialService(lambda: None)

    async def execute(_actor, operation, governance=None):
        del governance
        return await operation(repository)

    monkeypatch.setattr(service, "_execute", execute)
    result = await service.migrate_grants(
        context,
        credential_id,
        expected_credential_version=2,
    )

    assert result.migrated_count == 1
    assert repository.migrated == (active_binding,)


@pytest.mark.asyncio
async def test_credential_revoke_rewrites_whole_skill_binding_revision() -> None:
    from app.shared_assets.credential_repository import (
        ActiveSkillCredentialBinding,
        CredentialRepository,
    )
    from deerflow.persistence.shared_assets import (
        ProjectSkillCredentialBindingRow,
        ProjectSkillCredentialConfigRow,
    )

    project_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    retained_credential_id = uuid.uuid4()
    user_id = uuid.uuid4()
    config = ProjectSkillCredentialConfigRow(
        project_id=project_id,
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        revision=4,
        created_by_user_id=str(user_id),
        updated_by_user_id=str(user_id),
    )
    removed = ProjectSkillCredentialBindingRow(
        project_id=project_id,
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        secret_name="API_KEY",
        credential_id=credential_id,
        credential_version_id=uuid.uuid4(),
        config_revision=4,
        status="active",
        created_by_user_id=str(user_id),
    )
    retained = ProjectSkillCredentialBindingRow(
        project_id=project_id,
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        secret_name="API_URL",
        credential_id=retained_credential_id,
        credential_version_id=uuid.uuid4(),
        config_revision=4,
        status="active",
        created_by_user_id=str(user_id),
    )
    session = SimpleNamespace(
        flush=AsyncMock(),
        add_all=Mock(),
    )
    repository = CredentialRepository(session)

    await repository.revoke_skill_bindings(
        (
            ActiveSkillCredentialBinding(removed, config),
            ActiveSkillCredentialBinding(retained, config),
        ),
        credential_id=credential_id,
        user_id=user_id,
        revoked_at=datetime.now(UTC),
    )

    assert config.revision == 5
    assert removed.status == "revoked"
    assert retained.status == "revoked"
    created = session.add_all.call_args.args[0]
    assert len(created) == 1
    assert created[0].secret_name == "API_URL"
    assert created[0].credential_id == retained_credential_id
    assert created[0].credential_version_id == retained.credential_version_id
    assert created[0].config_revision == 5


@pytest.mark.asyncio
async def test_credential_migration_preserves_other_skill_bindings() -> None:
    from app.shared_assets.credential_repository import (
        ActiveSkillCredentialBinding,
        CredentialRepository,
    )
    from deerflow.persistence.shared_assets import (
        CredentialVersionRow,
        ProjectSkillCredentialBindingRow,
        ProjectSkillCredentialConfigRow,
    )

    project_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    retained_credential_id = uuid.uuid4()
    user_id = uuid.uuid4()
    config = ProjectSkillCredentialConfigRow(
        project_id=project_id,
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        revision=2,
        created_by_user_id=str(user_id),
        updated_by_user_id=str(user_id),
    )
    migrated = ProjectSkillCredentialBindingRow(
        project_id=project_id,
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        secret_name="API_KEY",
        credential_id=credential_id,
        credential_version_id=uuid.uuid4(),
        config_revision=2,
        status="active",
        created_by_user_id=str(user_id),
    )
    retained = ProjectSkillCredentialBindingRow(
        project_id=project_id,
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        secret_name="API_URL",
        credential_id=retained_credential_id,
        credential_version_id=uuid.uuid4(),
        config_revision=2,
        status="active",
        created_by_user_id=str(user_id),
    )
    target = CredentialVersionRow(
        id=uuid.uuid4(),
        credential_id=credential_id,
        version_number=2,
        status="active",
        payload_schema_version=1,
        payload_schema={"env": ["API_KEY"]},
        created_by_user_id=str(user_id),
    )
    session = SimpleNamespace(
        flush=AsyncMock(),
        add_all=Mock(),
    )
    repository = CredentialRepository(session)

    created = await repository.migrate_skill_bindings(
        (
            ActiveSkillCredentialBinding(migrated, config),
            ActiveSkillCredentialBinding(retained, config),
        ),
        target,
        credential_id=credential_id,
        user_id=user_id,
        migrated_at=datetime.now(UTC),
    )

    assert config.revision == 3
    assert migrated.status == "revoked"
    assert retained.status == "revoked"
    created_by_name = {row.secret_name: row for row in created}
    assert created_by_name["API_KEY"].credential_version_id == target.id
    assert created_by_name["API_URL"].credential_version_id == retained.credential_version_id
    assert {row.config_revision for row in created} == {3}


@pytest.mark.asyncio
async def test_credential_migration_preserves_old_and_new_skill_version_configs() -> None:
    from app.shared_assets.credential_repository import (
        ActiveSkillCredentialBinding,
        CredentialRepository,
    )
    from deerflow.persistence.shared_assets import (
        CredentialVersionRow,
        ProjectSkillCredentialBindingRow,
        ProjectSkillCredentialConfigRow,
    )

    project_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    retained_credential_id = uuid.uuid4()
    user_id = uuid.uuid4()
    old_credential_version_id = uuid.uuid4()
    version_specs = (
        (uuid.uuid4(), 2),
        (uuid.uuid4(), 7),
    )
    configs: list[ProjectSkillCredentialConfigRow] = []
    items: list[ActiveSkillCredentialBinding] = []
    retained_versions: dict[uuid.UUID, uuid.UUID] = {}
    for skill_version_id, revision in version_specs:
        config = ProjectSkillCredentialConfigRow(
            project_id=project_id,
            skill_id=skill_id,
            skill_version_id=skill_version_id,
            revision=revision,
            created_by_user_id=str(user_id),
            updated_by_user_id=str(user_id),
        )
        configs.append(config)
        migrated = ProjectSkillCredentialBindingRow(
            project_id=project_id,
            skill_id=skill_id,
            skill_version_id=skill_version_id,
            secret_name="API_KEY",
            credential_id=credential_id,
            credential_version_id=old_credential_version_id,
            config_revision=revision,
            status="active",
            created_by_user_id=str(user_id),
        )
        retained_version_id = uuid.uuid4()
        retained_versions[skill_version_id] = retained_version_id
        retained = ProjectSkillCredentialBindingRow(
            project_id=project_id,
            skill_id=skill_id,
            skill_version_id=skill_version_id,
            secret_name="API_URL",
            credential_id=retained_credential_id,
            credential_version_id=retained_version_id,
            config_revision=revision,
            status="active",
            created_by_user_id=str(user_id),
        )
        items.extend(
            (
                ActiveSkillCredentialBinding(migrated, config),
                ActiveSkillCredentialBinding(retained, config),
            )
        )

    target = CredentialVersionRow(
        id=uuid.uuid4(),
        credential_id=credential_id,
        version_number=2,
        status="active",
        payload_schema_version=1,
        payload_schema={"env": ["API_KEY"]},
        created_by_user_id=str(user_id),
    )
    session = SimpleNamespace(
        flush=AsyncMock(),
        add_all=Mock(),
    )

    created = await CredentialRepository(session).migrate_skill_bindings(
        tuple(items),
        target,
        credential_id=credential_id,
        user_id=user_id,
        migrated_at=datetime.now(UTC),
    )

    assert [config.revision for config in configs] == [3, 8]
    assert len(created) == 4
    by_version_and_name = {(row.skill_version_id, row.secret_name): row for row in created}
    for skill_version_id, expected_revision in (
        (version_specs[0][0], 3),
        (version_specs[1][0], 8),
    ):
        assert by_version_and_name[(skill_version_id, "API_KEY")].credential_version_id == target.id
        assert by_version_and_name[(skill_version_id, "API_URL")].credential_version_id == retained_versions[skill_version_id]
        assert {
            by_version_and_name[(skill_version_id, "API_KEY")].config_revision,
            by_version_and_name[(skill_version_id, "API_URL")].config_revision,
        } == {expected_revision}
