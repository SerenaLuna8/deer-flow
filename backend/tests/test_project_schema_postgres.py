from __future__ import annotations

import asyncio
import importlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.gateway.auth.models import User, UserResponse
from deerflow.persistence.bootstrap import _get_alembic_config
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow


def test_platform_role_contract_accepts_only_system_admin_and_user() -> None:
    base = {"id": uuid.uuid4(), "email": "admin@example.com"}
    assert User(**base, system_role="system_admin").system_role == "system_admin"
    assert UserResponse(id=str(base["id"]), email=base["email"], system_role="user").system_role == "user"
    with pytest.raises(ValueError):
        User(**base, system_role="admin")


def test_project_metadata_uses_uuid_projects_and_string_user_foreign_keys() -> None:
    assert ProjectRow.__table__.c.id.type.python_type is uuid.UUID
    assert ProjectMembershipRow.__table__.c.project_id.type.python_type is uuid.UUID
    assert ProjectMembershipRow.__table__.c.user_id.type.length == 36
    assert ProjectRow.__table__.c.created_by_user_id.type.length == 36
    assert {fk.target_fullname for fk in ProjectMembershipRow.__table__.c.user_id.foreign_keys} == {"users.id"}


def test_downgrade_checks_for_project_data_before_any_mutation(monkeypatch) -> None:
    migration = importlib.import_module("deerflow.persistence.migrations.versions.0005_project_foundation")
    mutations: list[str] = []
    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(execute=lambda _statement: SimpleNamespace(scalar_one=lambda: True)),
        drop_index=lambda *_args, **_kwargs: mutations.append("drop_index"),
        drop_table=lambda *_args, **_kwargs: mutations.append("drop_table"),
        drop_constraint=lambda *_args, **_kwargs: mutations.append("drop_constraint"),
        execute=lambda *_args, **_kwargs: mutations.append("execute"),
    )
    monkeypatch.setattr(migration, "op", fake_op)

    with pytest.raises(RuntimeError, match="project data exists"):
        migration.downgrade()
    assert mutations == []


@pytest.mark.asyncio
async def test_empty_database_head_has_project_constraints(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            checks = await conn.run_sync(lambda sync: inspect(sync).get_check_constraints("projects"))
            membership_checks = await conn.run_sync(lambda sync: inspect(sync).get_check_constraints("project_memberships"))
            membership_uniques = await conn.run_sync(lambda sync: inspect(sync).get_unique_constraints("project_memberships"))
            membership_fks = await conn.run_sync(lambda sync: inspect(sync).get_foreign_keys("project_memberships"))
            membership_indexes = await conn.run_sync(lambda sync: inspect(sync).get_indexes("project_memberships"))
        assert {"projects", "project_memberships"} <= tables
        names = {item["name"] for item in checks}
        assert {"ck_projects_slug_format", "ck_projects_status", "ck_projects_membership_version"} <= names
        assert {
            "ck_project_memberships_role",
            "ck_project_memberships_status",
            "ck_project_memberships_version",
        } <= {item["name"] for item in membership_checks}
        assert "uq_project_memberships_project_user" in {item["name"] for item in membership_uniques}
        assert {item["options"].get("ondelete") for item in membership_fks} == {"CASCADE"}
        assert "ix_project_memberships_user_id" in {item["name"] for item in membership_indexes}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_from_0004_maps_legacy_admin(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(command.upgrade, cfg, "0004_migration_ledger")
        async with engine.begin() as conn:
            await conn.execute(text("CREATE SCHEMA constraint_collision"))
            await conn.execute(text("CREATE TABLE constraint_collision.users (system_role text)"))
            await conn.execute(
                text("""ALTER TABLE constraint_collision.users
                    ADD CONSTRAINT ck_users_system_role CHECK (system_role = 'other')""")
            )
            await conn.execute(
                text("""INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'admin',:now,false,0)"""),
                {"id": str(uuid.uuid4()), "email": "legacy@example.com", "now": datetime.now(UTC)},
            )
        await asyncio.to_thread(command.upgrade, cfg, "head")
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT system_role FROM users"))).scalar_one() == "system_admin"
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("""INSERT INTO users
                        (id,email,system_role,created_at,needs_setup,token_version)
                        VALUES (:id,'invalid@example.com','admin',:now,false,0)"""),
                    {"id": str(uuid.uuid4()), "now": datetime.now(UTC)},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_defaults_constraints_and_cascades(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    owner_id, member_id = str(uuid.uuid4()), str(uuid.uuid4())
    project_id = uuid.uuid4()
    now = datetime.now(UTC)
    try:
        async with engine.begin() as conn:
            for user_id, email in ((owner_id, "owner@example.com"), (member_id, "member@example.com")):
                await conn.execute(
                    text("""INSERT INTO users
                        (id,email,system_role,created_at,needs_setup,token_version)
                        VALUES (:id,:email,'user',:now,false,0)"""),
                    {"id": user_id, "email": email, "now": now},
                )
            await conn.execute(
                text("INSERT INTO projects (id,slug,display_name,created_by_user_id) VALUES (:id,'valid-slug','Valid',:owner)"),
                {"id": project_id, "owner": owner_id},
            )
            inserted_after = datetime.now(UTC)
            defaults = (
                await conn.execute(
                    text("""SELECT description,icon,status,is_suspended,membership_version,
                        created_at,updated_at FROM projects WHERE id=:id"""),
                    {"id": project_id},
                )
            ).one()
            assert defaults[:5] == ("", "folder", "active", False, 1)
            assert now <= defaults.created_at <= inserted_after
            assert now <= defaults.updated_at <= inserted_after
            await conn.execute(
                text("INSERT INTO project_memberships (id,project_id,user_id,role) VALUES (:id,:project,:user,'viewer')"),
                {"id": uuid.uuid4(), "project": project_id, "user": member_id},
            )
            membership_defaults = (
                await conn.execute(
                    text("""SELECT status,version,is_pinned,created_at,updated_at
                        FROM project_memberships WHERE project_id=:id"""),
                    {"id": project_id},
                )
            ).one()
            membership_inserted_after = datetime.now(UTC)
            assert membership_defaults[:3] == ("active", 1, False)
            assert now <= membership_defaults.created_at <= membership_inserted_after
            assert now <= membership_defaults.updated_at <= membership_inserted_after

        for bad_slug in ("Abc", "-abc", "abc-", "a--b", "ab", "abc_def"):
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("INSERT INTO projects (id,slug,display_name,created_by_user_id) VALUES (:id,:slug,'Bad',:owner)"),
                        {"id": uuid.uuid4(), "slug": bad_slug, "owner": owner_id},
                    )

        invalid_project_cases = (
            ("valid-slug", "active", 1, owner_id),  # duplicate slug
            ("bad-status", "disabled", 1, owner_id),
            ("bad-version", "active", 0, owner_id),
            ("missing-owner", "active", 1, str(uuid.uuid4())),
        )
        for slug, status, membership_version, creator in invalid_project_cases:
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("""INSERT INTO projects
                            (id,slug,display_name,status,membership_version,created_by_user_id)
                            VALUES (:id,:slug,'Invalid',:status,:version,:creator)"""),
                        {
                            "id": uuid.uuid4(),
                            "slug": slug,
                            "status": status,
                            "version": membership_version,
                            "creator": creator,
                        },
                    )

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO project_memberships (id,project_id,user_id,role) VALUES (:id,:project,:user,'viewer')"),
                    {"id": uuid.uuid4(), "project": project_id, "user": member_id},
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO project_memberships (id,project_id,user_id,role) VALUES (:id,:project,:user,'owner')"),
                    {"id": uuid.uuid4(), "project": project_id, "user": owner_id},
                )
        invalid_membership_cases = (
            (project_id, owner_id, "viewer", "disabled", 1),
            (project_id, owner_id, "viewer", "active", 0),
            (uuid.uuid4(), owner_id, "viewer", "active", 1),
            (project_id, str(uuid.uuid4()), "viewer", "active", 1),
        )
        for target_project, target_user, role, status, version in invalid_membership_cases:
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("""INSERT INTO project_memberships
                            (id,project_id,user_id,role,status,version)
                            VALUES (:id,:project,:user,:role,:status,:version)"""),
                        {
                            "id": uuid.uuid4(),
                            "project": target_project,
                            "user": target_user,
                            "role": role,
                            "status": status,
                            "version": version,
                        },
                    )

        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id=:id"), {"id": member_id})
            assert (await conn.execute(text("SELECT count(*) FROM project_memberships WHERE project_id=:id"), {"id": project_id})).scalar_one() == 0
            await conn.execute(
                text("INSERT INTO project_memberships (id,project_id,user_id,role) VALUES (:id,:project,:user,'admin')"),
                {"id": uuid.uuid4(), "project": project_id, "user": owner_id},
            )
            await conn.execute(text("DELETE FROM projects WHERE id=:id"), {"id": project_id})
            assert (await conn.execute(text("SELECT count(*) FROM project_memberships"))).scalar_one() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("include_membership", [False, True])
async def test_downgrade_with_project_data_fails_without_mutation(
    migrated_postgres_database_url: str,
    include_membership: bool,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    user_id, project_id = str(uuid.uuid4()), uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,'owner@example.com','system_admin',:now,false,0)"""),
                {"id": user_id, "now": datetime.now(UTC)},
            )
            await conn.execute(
                text("""INSERT INTO projects (id,slug,display_name,created_by_user_id)
                    VALUES (:id,'keep-project','Keep',:user_id)"""),
                {"id": project_id, "user_id": user_id},
            )
            if include_membership:
                await conn.execute(
                    text("""INSERT INTO project_memberships (id,project_id,user_id,role)
                        VALUES (:id,:project_id,:user_id,'admin')"""),
                    {"id": uuid.uuid4(), "project_id": project_id, "user_id": user_id},
                )

        with pytest.raises(RuntimeError, match="project data exists"):
            await asyncio.to_thread(command.downgrade, _get_alembic_config(engine), "0004_migration_ledger")

        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM projects"))).scalar_one() == 1
            assert (await conn.execute(text("SELECT count(*) FROM project_memberships"))).scalar_one() == int(include_membership)
            assert (await conn.execute(text("SELECT system_role FROM users WHERE id=:id"), {"id": user_id})).scalar_one() == "system_admin"
            assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0005_project_foundation"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_with_empty_project_tables_returns_to_0004(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    user_id = str(uuid.uuid4())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,'admin@example.com','system_admin',:now,false,0)"""),
                {"id": user_id, "now": datetime.now(UTC)},
            )

        await asyncio.to_thread(command.downgrade, _get_alembic_config(engine), "0004_migration_ledger")

        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            assert "projects" not in tables
            assert "project_memberships" not in tables
            assert (await conn.execute(text("SELECT system_role FROM users WHERE id=:id"), {"id": user_id})).scalar_one() == "admin"
            assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0004_migration_ledger"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_fails_closed_on_users_role_constraint_definition_drift(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(command.upgrade, cfg, "0004_migration_ledger")
        async with engine.begin() as conn:
            await conn.execute(
                text("""ALTER TABLE users ADD CONSTRAINT ck_users_system_role
                    CHECK (system_role IN ('system_admin', 'user', 'guest'))""")
            )

        with pytest.raises(Exception, match="constraint definition drift"):
            await asyncio.to_thread(command.upgrade, cfg, "head")

        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0004_migration_ledger"
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            assert "projects" not in tables
            assert "project_memberships" not in tables
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_validates_matching_not_valid_users_role_constraint(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    admin_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(command.upgrade, cfg, "0004_migration_ledger")
        async with engine.begin() as conn:
            for row_id, email, role in (
                (admin_id, "admin@example.com", "admin"),
                (user_id, "user@example.com", "user"),
            ):
                await conn.execute(
                    text("""INSERT INTO users
                        (id,email,system_role,created_at,needs_setup,token_version)
                        VALUES (:id,:email,:role,:now,false,0)"""),
                    {"id": row_id, "email": email, "role": role, "now": datetime.now(UTC)},
                )
            await conn.execute(
                text("""ALTER TABLE users ADD CONSTRAINT ck_users_system_role
                    CHECK (system_role IN ('system_admin', 'user')) NOT VALID""")
            )

        await asyncio.to_thread(command.upgrade, cfg, "head")

        async with engine.connect() as conn:
            validated = (
                await conn.execute(
                    text("""SELECT convalidated FROM pg_constraint
                        WHERE conrelid = 'users'::regclass
                          AND conname = 'ck_users_system_role'
                          AND contype = 'c'""")
                )
            ).scalar_one()
            roles = (await conn.execute(text("SELECT system_role FROM users ORDER BY email"))).scalars().all()
            assert validated is True
            assert roles == ["system_admin", "user"]
            assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0005_project_foundation"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_rolls_back_when_matching_not_valid_constraint_has_legacy_guest(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(command.upgrade, cfg, "0004_migration_ledger")
        async with engine.begin() as conn:
            for email, role in (("admin@example.com", "admin"), ("guest@example.com", "guest")):
                await conn.execute(
                    text("""INSERT INTO users
                        (id,email,system_role,created_at,needs_setup,token_version)
                        VALUES (:id,:email,:role,:now,false,0)"""),
                    {
                        "id": str(uuid.uuid4()),
                        "email": email,
                        "role": role,
                        "now": datetime.now(UTC),
                    },
                )
            await conn.execute(
                text("""ALTER TABLE users ADD CONSTRAINT ck_users_system_role
                    CHECK (system_role IN ('system_admin', 'user')) NOT VALID""")
            )

        with pytest.raises(Exception, match="ck_users_system_role"):
            await asyncio.to_thread(command.upgrade, cfg, "head")

        async with engine.connect() as conn:
            roles = (await conn.execute(text("SELECT system_role FROM users ORDER BY email"))).scalars().all()
            validated = (
                await conn.execute(
                    text("""SELECT convalidated FROM pg_constraint
                        WHERE conrelid = 'users'::regclass
                          AND conname = 'ck_users_system_role'
                          AND contype = 'c'""")
                )
            ).scalar_one()
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            assert roles == ["admin", "guest"]
            assert validated is False
            assert "projects" not in tables
            assert "project_memberships" not in tables
            assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0004_migration_ledger"
    finally:
        await engine.dispose()
