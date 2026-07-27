from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.credential_closure import (
    McpCredentialClosureInvalid,
    McpCredentialClosureTarget,
    lock_mcp_credential_closures,
)
from app.shared_assets.credential_repository import CredentialRepository
from app.shared_assets.errors import AssetNotFound
from app.shared_assets.mcp_repository import McpRepository
from app.shared_assets.models import AssetScope
from app.shared_assets.skill_credential_closure import (
    SkillCredentialClosureInvalid,
    SkillCredentialClosureTarget,
    lock_skill_credential_closures,
)
from app.shared_assets.skill_credential_repository import SkillCredentialRepository
from deerflow.persistence.shared_assets import CredentialRow


def _project_context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="credential-soft-delete",
    )


def _system_context() -> SystemAssetGovernanceContext:
    return SystemAssetGovernanceContext(
        user_id=uuid.uuid4(),
        request_id="credential-soft-delete",
    )


def _sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class _EmptyResult:
    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []

    def one_or_none(self):
        return None

    def one(self):
        return (0, 0)


class _CaptureSession:
    def __init__(self, results: list[object] | None = None) -> None:
        self.statements: list[object] = []
        self._results = list(results or [])
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if self._results:
            return self._results.pop(0)
        return _EmptyResult()

    async def scalar(self, statement):
        self.statements.append(statement)
        return False

    async def flush(self):
        self.flush_count += 1


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _RowsResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def all(self):
        return self.rows


class _ScalarsResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


def test_credential_soft_delete_orm_and_migration_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = CredentialRow.__table__
    assert table.c.is_delete.nullable is False
    assert str(table.c.is_delete.server_default.arg) == "false"

    indexes = {index.name: index for index in table.indexes}
    assert tuple(column.name for column in indexes["ix_credentials_scope_project_is_delete"].columns) == (
        "scope",
        "project_id",
        "is_delete",
    )
    for name, scope in (
        ("uq_credentials_project_name", "project"),
        ("uq_credentials_system_name", "system"),
    ):
        predicate = str(indexes[name].dialect_options["postgresql"]["where"])
        assert f"scope = '{scope}'" in predicate
        assert "is_delete = false" in predicate

    migration = importlib.import_module("deerflow.persistence.migrations.versions.0004_credential_soft_delete")
    operations: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class Operations:
        def __getattr__(self, name: str):
            def record(*args, **kwargs):
                operations.append((name, args, kwargs))

            return record

    monkeypatch.setattr(migration, "op", Operations())
    migration.upgrade()

    assert migration.revision == "0004_credential_soft_delete"
    assert migration.down_revision == "0003_skill_credentials"
    add_column = next(args[1] for name, args, _kwargs in operations if name == "add_column" and args[0] == "credentials")
    assert isinstance(add_column, sa.Column)
    assert add_column.name == "is_delete"
    assert add_column.nullable is False
    assert str(add_column.server_default.arg) == "false"
    dropped_indexes = {args[0] for name, args, _kwargs in operations if name == "drop_index"}
    assert {
        "uq_credentials_project_name",
        "uq_credentials_system_name",
    }.issubset(dropped_indexes)
    created_indexes = {args[0]: kwargs for name, args, kwargs in operations if name == "create_index"}
    assert "is_delete = false" in str(created_indexes["uq_credentials_project_name"]["postgresql_where"])
    assert "is_delete = false" in str(created_indexes["uq_credentials_system_name"]["postgresql_where"])
    assert "ix_credentials_scope_project_is_delete" in created_indexes
    trigger_statements = {args[0] for name, args, _kwargs in operations if name == "execute"}
    assert "DROP TRIGGER trg_credentials_generation ON credentials" in trigger_statements
    assert any("AFTER UPDATE OF status, current_version_id, is_delete" in statement for statement in trigger_statements)
    with pytest.raises(RuntimeError, match="Credential soft-delete downgrade is unsupported"):
        migration.downgrade()


@pytest.mark.asyncio
async def test_credential_repository_business_queries_hide_deleted_rows() -> None:
    project_context = _project_context()
    project_session = _CaptureSession()
    with pytest.raises(AssetNotFound):
        await CredentialRepository(project_session).get_project_credential(
            project_context,
            uuid.uuid4(),
        )
    assert "credentials.is_delete IS false" in _sql(project_session.statements[-1])

    system_session = _CaptureSession()
    assert await CredentialRepository(system_session).list_system_visible(_system_context()) == ()
    assert "credentials.is_delete IS false" in _sql(system_session.statements[-1])

    rotation_session = _CaptureSession()
    assert await CredentialRepository(rotation_session).rotation_status(
        _system_context(),
        active_key_id="active-key",
    ) == (0, 0)
    assert "credentials.is_delete IS false" in _sql(rotation_session.statements[-1])


@pytest.mark.asyncio
async def test_credential_repository_marks_rows_deleted_once() -> None:
    session = _CaptureSession()
    row = CredentialRow(
        scope="project",
        project_id=uuid.uuid4(),
        name="replaceable",
        display_name="Replaceable",
        credential_type="token",
        is_delete=False,
        version=1,
        created_by_user_id=str(uuid.uuid4()),
    )

    result = await CredentialRepository(session).mark_deleted(
        row,
        request_id="credential-soft-delete",
    )

    assert result is row
    assert row.is_delete is True
    assert row.version == 2
    assert session.flush_count == 1
    with pytest.raises(AssetNotFound):
        await CredentialRepository(session).mark_deleted(
            row,
            request_id="credential-soft-delete",
        )
    assert row.version == 2
    assert session.flush_count == 1


@pytest.mark.asyncio
async def test_skill_credential_repository_queries_hide_deleted_rows() -> None:
    context = _project_context()
    eligible_session = _CaptureSession()
    assert await SkillCredentialRepository(eligible_session).eligible_credentials(context) == ()
    assert "credentials.is_delete IS false" in _sql(eligible_session.statements[-1])

    selected_session = _CaptureSession()
    with pytest.raises(AssetNotFound):
        await SkillCredentialRepository(selected_session).lock_selected_credentials(
            context,
            (uuid.uuid4(),),
        )
    assert "credentials.is_delete IS false" in _sql(selected_session.statements[-1])

    envelope_session = _CaptureSession()
    assert (
        await SkillCredentialRepository(envelope_session).active_envelope_exists(
            uuid.uuid4(),
        )
        is False
    )
    envelope_sql = _sql(envelope_session.statements[-1])
    assert "JOIN credentials" in envelope_sql
    assert "credentials.is_delete IS false" in envelope_sql


@pytest.mark.asyncio
async def test_runtime_closures_hide_deleted_credentials_before_materialization() -> None:
    mcp_version_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    credential_version_id = uuid.uuid4()
    slot_id = uuid.uuid4()
    mcp_session = _CaptureSession(
        [
            _ScalarsResult(
                [
                    SimpleNamespace(
                        id=slot_id,
                        name="TOKEN",
                        required=True,
                        payload_schema={"env": ["TOKEN"]},
                    )
                ]
            ),
            _RowsResult(
                [
                    SimpleNamespace(
                        id=uuid.uuid4(),
                        credential_slot_id=slot_id,
                        credential_version_id=credential_version_id,
                        credential_id=credential_id,
                    )
                ]
            ),
            _ScalarResult(None),
        ]
    )
    with pytest.raises(McpCredentialClosureInvalid):
        await lock_mcp_credential_closures(
            mcp_session,  # type: ignore[arg-type]
            (
                McpCredentialClosureTarget(
                    mcp_version_id,
                    AssetScope.PROJECT,
                    uuid.uuid4(),
                ),
            ),
        )
    assert "credentials.is_delete IS false" in _sql(mcp_session.statements[2])

    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()
    skill_credential_id = uuid.uuid4()
    skill_credential_version_id = uuid.uuid4()
    config = SimpleNamespace(revision=1)
    binding = SimpleNamespace(
        id=uuid.uuid4(),
        credential_id=skill_credential_id,
        credential_version_id=skill_credential_version_id,
        secret_name="TOKEN",
    )
    skill_session = _CaptureSession(
        [
            _ScalarResult(SimpleNamespace(secret_requirements=[{"name": "TOKEN", "optional": False}])),
            _ScalarResult(config),
            _ScalarsResult([binding]),
            _ScalarResult(None),
        ]
    )
    with pytest.raises(SkillCredentialClosureInvalid):
        await lock_skill_credential_closures(
            skill_session,  # type: ignore[arg-type]
            uuid.uuid4(),
            (SkillCredentialClosureTarget(skill_id, skill_version_id),),
        )
    assert "credentials.is_delete IS false" in _sql(skill_session.statements[3])


@pytest.mark.asyncio
async def test_mcp_grant_authorization_queries_hide_deleted_credentials() -> None:
    session = _CaptureSession()
    with pytest.raises(AssetNotFound):
        await McpRepository(session).project_grant_state(
            _project_context(),
            uuid.uuid4(),
        )
    assert "credentials.is_delete IS false" in _sql(session.statements[-1])


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_deleted_credential_names_can_be_reused_and_old_rows_stay_hidden(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    first_system_credential_id = uuid.uuid4()
    second_system_credential_id = uuid.uuid4()
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'user',:now,false,0)"""
                ),
                {
                    "id": str(user_id),
                    "email": f"soft-delete-{user_id}@example.com",
                    "now": now,
                },
            )
            await connection.execute(
                sa.text(
                    """INSERT INTO projects
                       (id,slug,display_name,created_by_user_id,created_at,updated_at)
                       VALUES (:id,:slug,'Soft delete',:user,:now,:now)"""
                ),
                {
                    "id": project_id,
                    "slug": f"soft-delete-{str(project_id)[:8]}",
                    "user": str(user_id),
                    "now": now,
                },
            )
            await connection.execute(
                sa.text(
                    """INSERT INTO project_memberships
                       (id,project_id,user_id,role,status,version)
                       VALUES (:id,:project,:user,'admin','active',1)"""
                ),
                {
                    "id": membership_id,
                    "project": project_id,
                    "user": str(user_id),
                },
            )
            for credential_id, is_delete in (
                (first_system_credential_id, True),
                (second_system_credential_id, False),
            ):
                await connection.execute(
                    sa.text(
                        """INSERT INTO credentials
                           (id,scope,project_id,name,display_name,credential_type,
                           created_by_user_id,is_delete)
                           VALUES
                           (:id,'system',NULL,'reusable','Reusable','token',
                            :user,:is_delete)"""
                    ),
                    {
                        "id": credential_id,
                        "user": str(user_id),
                        "is_delete": is_delete,
                    },
                )

        context = ProjectContext(
            user_id=user_id,
            project_id=project_id,
            membership_id=membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="credential-soft-delete-postgres",
        )
        from app.shared_assets.credential_service import (
            CreateCredential,
            CredentialService,
        )
        from app.shared_assets.keyring import CredentialKeyring

        service = CredentialService(
            factory,
            keyring=CredentialKeyring(
                active_key_id="soft-delete-key",
                _keys={"soft-delete-key": b"s" * 32},
            ),
        )
        first_project_credential = await service.create(
            context,
            CreateCredential("reusable", "Reusable", "token"),
            {"env": {"TOKEN": "first-value"}},
        )
        await service.delete(
            context,
            first_project_credential.id,
            expected_credential_version=1,
        )
        second_project_credential = await service.create(
            context,
            CreateCredential("reusable", "Reusable", "token"),
            {"env": {"TOKEN": "second-value"}},
        )

        async with factory() as session:
            visible = await CredentialRepository(session).list_project_visible(context)
            assert second_project_credential.id in {row.id for row in visible}
            assert second_system_credential_id in {row.id for row in visible}
            assert first_project_credential.id not in {row.id for row in visible}
            assert first_system_credential_id not in {row.id for row in visible}
            deleted_project_credential = await session.get(
                CredentialRow,
                first_project_credential.id,
            )
            assert deleted_project_credential is not None
            assert deleted_project_credential.is_delete is True
            with pytest.raises(AssetNotFound):
                await CredentialRepository(session).get_project_credential(
                    context,
                    first_project_credential.id,
                )
    finally:
        await engine.dispose()
