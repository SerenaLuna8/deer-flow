from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.default_skill_bindings import seed_new_project_system_skill_bindings
from app.projects.models import ProjectRole
from app.shared_assets import binding_service as binding_service_module
from app.shared_assets import skill_service as skill_service_module
from app.shared_assets.binding_repository import BindingRepository
from app.shared_assets.contexts import (
    SystemAssetGovernanceContext,
    SystemAssetReadContext,
)
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetResolutionUnavailable,
)
from app.shared_assets.models import AssetKind, AssetScope, AssetSelection
from app.shared_assets.resolver import ProjectAssetResolver, _ResolvedRecord
from app.shared_assets.skill_repository import SkillVersionRecord
from app.shared_assets.skill_service import SkillVersionView
from app.shared_assets.version_relation import VersionRelation
from deerflow.persistence.shared_assets import (
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)


def test_system_skill_version_revocation_is_a_first_class_persisted_contract() -> None:
    """Revocation is durable governance metadata on the immutable Current v1."""

    assert {
        "revoked_at",
        "revoked_by_user_id",
        "revocation_reason_code",
    } <= set(SkillVersionRow.__table__.columns.keys())
    assert {
        "revoked_at",
        "revoked_by_user_id",
        "revocation_reason_code",
        "governance_status",
        "binding_eligible",
    } <= {field.name for field in dataclasses.fields(SkillVersionView)}


def _global_admin() -> SystemAssetGovernanceContext:
    return SystemAssetGovernanceContext(
        user_id=uuid.uuid4(),
        request_id="req-system-skill-revoke",
    )


def _project_admin() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="req-project-skill-revoke",
    )


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    def __init__(self) -> None:
        self.flush_count = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()

    async def flush(self) -> None:
        self.flush_count += 1


@dataclass
class _Store:
    asset: SkillRow
    record: SkillVersionRecord
    governance: list[dict[str, object]]


class _Repository:
    store: _Store

    def __init__(self, session: _Session) -> None:
        self.session = session

    async def get_system_asset(
        self,
        _actor: object,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> SkillRow:
        del for_update
        assert asset_id == self.store.asset.id
        return self.store.asset

    get_project_asset = get_system_asset
    get_override_asset = get_system_asset

    async def get_system_version(
        self,
        _actor: object,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> SkillVersionRecord:
        del for_update
        assert asset_id == self.store.asset.id
        assert version_id == self.store.record.row.id
        return self.store.record

    get_project_version = get_system_version
    get_override_version = get_system_version

    async def get_system_version_history(
        self,
        _actor: object,
        asset_id: uuid.UUID,
    ) -> tuple[SkillVersionRecord, ...]:
        assert asset_id == self.store.asset.id
        return (self.store.record,)


class _GovernanceSink:
    async def append_override(
        self,
        _session: _Session,
        **kwargs: object,
    ) -> None:
        _Repository.store.governance.append(dict(kwargs))


@pytest.fixture
def revocation_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[skill_service_module.SkillService, _Store, _Session]:
    now = datetime.now(UTC)
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    asset = SkillRow(
        id=asset_id,
        scope="system",
        project_id=None,
        slug="reviewed-system-skill",
        display_name="Reviewed System Skill",
        status="active",
        current_version_id=version_id,
        revision=3,
        source_key="builtin:skill:reviewed-system-skill",
        created_by_user_id=str(actor_id),
        created_at=now,
        updated_at=now,
    )
    version = SkillVersionRow(
        id=version_id,
        skill_id=asset_id,
        version_number=1,
        description="Reviewed package",
        frontmatter={"name": asset.slug},
        compatibility=None,
        secret_requirements=[],
        scan_decision="allow",
        scan_summary={"rule_ids": []},
        supersedes_version_id=None,
        payload_checksum="a" * 64,
        created_by_user_id=str(actor_id),
        created_at=now,
    )
    # These assignments keep this red-test fixture importable before the ORM
    # columns land. The table/mapper assertion above still verifies persistence.
    version.revoked_at = None
    version.revoked_by_user_id = None
    file = SkillVersionFileRow(
        skill_version_id=version_id,
        path="SKILL.md",
        media_type="text/markdown",
        size_bytes=0,
        sha256="b" * 64,
        content=b"",
    )
    store = _Store(asset, SkillVersionRecord(version, (file,)), [])
    session = _Session()
    _Repository.store = store
    monkeypatch.setattr(skill_service_module, "SkillRepository", _Repository)
    service = skill_service_module.SkillService(
        lambda: session,
        governance_sink=_GovernanceSink(),
    )
    return service, store, session


@pytest.mark.asyncio
async def test_global_admin_revokes_current_system_skill_v1_once(
    revocation_harness: tuple[skill_service_module.SkillService, _Store, _Session],
) -> None:
    service, store, session = revocation_harness
    actor = _global_admin()

    revoked = await service.revoke_version(
        actor,
        store.asset.id,
        store.record.row.id,
        expected_asset_version=3,
    )

    assert revoked.revoked_at is not None
    assert revoked.revoked_by_user_id == str(actor.user_id)
    assert revoked.revocation_reason_code == "security"
    assert revoked.governance_status == "revoked"
    assert revoked.binding_eligible is False
    assert store.asset.current_version_id == store.record.row.id
    assert store.asset.revision == 3
    assert session.flush_count == 1
    assert [event["action"] for event in store.governance] == ["skill.version.revoke"]

    history = await service.get_version_history(actor, store.asset.id)
    assert [item.id for item in history] == [store.record.row.id]
    assert history[0].revoked_at == revoked.revoked_at
    assert history[0].binding_eligible is False

    with pytest.raises(AssetConflict):
        await service.revoke_version(
            actor,
            store.asset.id,
            store.record.row.id,
            expected_asset_version=3,
        )


def test_nonrevoked_current_version_is_binding_eligible(
    revocation_harness: tuple[skill_service_module.SkillService, _Store, _Session],
) -> None:
    service, store, _session = revocation_harness

    version = service._version_view(  # noqa: SLF001 - response contract
        store.record,
        relation=VersionRelation.CURRENT,
    )

    assert version.governance_status == "active"
    assert version.revoked_at is None
    assert version.revoked_by_user_id is None
    assert version.binding_eligible is True


@pytest.mark.asyncio
async def test_system_skill_revocation_rejects_stale_revision_or_noncurrent_target(
    revocation_harness: tuple[skill_service_module.SkillService, _Store, _Session],
) -> None:
    service, store, session = revocation_harness
    actor = _global_admin()

    with pytest.raises(AssetConflict):
        await service.revoke_version(
            actor,
            store.asset.id,
            store.record.row.id,
            expected_asset_version=2,
        )
    assert store.asset.revision == 3
    assert store.record.row.revoked_at is None
    assert session.flush_count == 0

    store.asset.current_version_id = uuid.uuid4()
    with pytest.raises(AssetConflict):
        await service.revoke_version(
            actor,
            store.asset.id,
            store.record.row.id,
            expected_asset_version=3,
        )
    assert store.asset.revision == 3
    assert store.record.row.revoked_at is None
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_system_skill_revocation_reason_is_a_closed_contract(
    revocation_harness: tuple[skill_service_module.SkillService, _Store, _Session],
) -> None:
    service, store, session = revocation_harness

    with pytest.raises(skill_service_module.AssetValidationFailed):
        await service.revoke_version(
            _global_admin(),
            store.asset.id,
            store.record.row.id,
            expected_asset_version=3,
            reason_code="free-form incident details",  # type: ignore[arg-type]
        )

    assert store.asset.revision == 3
    assert store.record.row.revoked_at is None
    assert store.record.row.revocation_reason_code is None
    assert session.flush_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor",
    [
        _project_admin(),
        SystemAssetGovernanceContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            request_id="req-override-skill-revoke",
        ),
        SystemAssetReadContext(
            user_id=uuid.uuid4(),
            request_id="req-readonly-skill-revoke",
        ),
    ],
)
async def test_system_skill_revocation_requires_global_governance_actor(
    actor: object,
    revocation_harness: tuple[skill_service_module.SkillService, _Store, _Session],
) -> None:
    service, store, session = revocation_harness

    with pytest.raises(AssetForbidden):
        await service.revoke_version(
            actor,  # type: ignore[arg-type]
            store.asset.id,
            store.record.row.id,
            expected_asset_version=3,
        )

    assert store.asset.revision == 3
    assert store.record.row.revoked_at is None
    assert session.flush_count == 0


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _SequentialStatementSession:
    def __init__(self, results: tuple[object, ...]) -> None:
        self._results = iter(results)
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _ScalarResult:
        self.statements.append(statement)
        return _ScalarResult(next(self._results))


def _postgres_sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


@pytest.mark.asyncio
async def test_skill_binding_target_query_excludes_revoked_versions(
    revocation_harness: tuple[skill_service_module.SkillService, _Store, _Session],
) -> None:
    _service, store, _session = revocation_harness
    store.record.row.revoked_at = datetime.now(UTC)
    store.record.row.revoked_by_user_id = str(uuid.uuid4())
    session = _SequentialStatementSession((store.asset, store.record.row))

    await BindingRepository(session).lock_target(  # type: ignore[arg-type]
        _project_admin(),
        AssetSelection(
            AssetKind.SKILL,
            store.asset.id,
            store.record.row.id,
        ),
    )

    assert len(session.statements) == 2
    assert "skill_versions.revoked_at IS NULL" in _postgres_sql(session.statements[1])


@pytest.mark.asyncio
async def test_current_revoked_skill_binding_version_remains_readable_for_migration(
    revocation_harness: tuple[skill_service_module.SkillService, _Store, _Session],
) -> None:
    _service, store, _session = revocation_harness
    session = _SequentialStatementSession((store.record.row,))

    await BindingRepository(session).lock_system_version(  # type: ignore[arg-type]
        _project_admin(),
        AssetKind.SKILL,
        store.asset.id,
        store.record.row.id,
        read=True,
        allow_revoked=True,
    )

    assert len(session.statements) == 1
    assert "skill_versions.revoked_at IS NULL" not in _postgres_sql(
        session.statements[0],
    )


class _DisableBindingRepository:
    row: ProjectSystemSkillBindingRow

    def __init__(self, session: _Session) -> None:
        self.session = session

    async def lock_project(self, _actor: object) -> None:
        return None

    async def get_binding(
        self,
        _actor: object,
        _kind: AssetKind,
        _asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> ProjectSystemSkillBindingRow:
        assert for_update is True
        return self.row

    async def current_version_id(
        self,
        _actor: object,
        _kind: AssetKind,
        _asset_id: uuid.UUID,
    ) -> uuid.UUID:
        return _Repository.store.record.row.id


class _BindingGovernanceSink:
    async def append_project(self, _session: _Session, **_kwargs: object) -> None:
        return None


@pytest.mark.asyncio
async def test_existing_binding_to_revoked_system_skill_can_still_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _project_admin()
    now = datetime.now(UTC)
    asset_id = uuid.uuid4()
    row = ProjectSystemSkillBindingRow(
        project_id=actor.project_id,
        system_skill_id=asset_id,
        enabled=True,
        version=7,
        created_by_user_id=str(actor.user_id),
        updated_by_user_id=str(actor.user_id),
        created_at=now,
        updated_at=now,
    )
    _DisableBindingRepository.row = row
    monkeypatch.setattr(
        binding_service_module,
        "BindingRepository",
        _DisableBindingRepository,
    )
    session = _Session()
    service = binding_service_module.BindingService(
        lambda: session,
        governance_sink=_BindingGovernanceSink(),
    )

    disabled = await service.disable(
        actor,
        AssetSelection(AssetKind.SKILL, asset_id, None),
        expected_binding_version=7,
    )

    assert disabled.enabled is False
    assert disabled.version == 8
    assert row.system_skill_id == asset_id


class _RowsResult:
    def scalars(self) -> _RowsResult:
        return self

    def all(self) -> list[object]:
        return []


class _DefaultBindingSession:
    def __init__(self) -> None:
        self.statements: list[object] = []
        self.added: list[object] = []

    async def execute(self, statement: object) -> _RowsResult:
        self.statements.append(statement)
        return _RowsResult()

    def add_all(self, rows: list[object]) -> None:
        self.added.extend(rows)

    async def flush(self) -> None:
        raise AssertionError("an empty target set must not flush")


@pytest.mark.asyncio
async def test_new_project_default_binding_query_excludes_revoked_versions() -> None:
    session = _DefaultBindingSession()

    count = await seed_new_project_system_skill_bindings(
        session,  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        actor_user_id=uuid.uuid4(),
    )

    assert count == 0
    assert session.added == []
    assert len(session.statements) == 1
    assert "skill_versions.revoked_at IS NULL" in _postgres_sql(session.statements[0])


class _EmptyScalars:
    def scalars(self) -> _EmptyScalars:
        return self

    def all(self) -> list[object]:
        return []


class _CountingEmptySession:
    def __init__(self) -> None:
        self.execute_count = 0

    async def execute(self, _statement: object) -> _EmptyScalars:
        self.execute_count += 1
        return _EmptyScalars()


@pytest.mark.asyncio
async def test_revoked_system_skill_fails_before_new_run_or_exact_materialization(
    revocation_harness: tuple[skill_service_module.SkillService, _Store, _Session],
) -> None:
    _service, store, _session = revocation_harness
    store.record.row.revoked_at = datetime.now(UTC)
    store.record.row.revoked_by_user_id = str(uuid.uuid4())
    session = _CountingEmptySession()
    resolver = ProjectAssetResolver(lambda: None)  # type: ignore[arg-type]

    with pytest.raises(AssetResolutionUnavailable):
        await resolver._skill_snapshot(  # noqa: SLF001 - shared admission/materialization boundary
            session,  # type: ignore[arg-type]
            _project_admin(),
            _ResolvedRecord(
                AssetScope.SYSTEM,
                store.asset,
                store.record.row,
            ),
            4,
        )

    # Both new admission and Worker exact re-materialization converge on this
    # snapshot boundary, so revocation must fail before reading package bytes.
    assert session.execute_count == 0
