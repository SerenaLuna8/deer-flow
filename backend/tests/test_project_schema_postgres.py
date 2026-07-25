from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.gateway.auth.models import User, UserResponse
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
            "ck_project_memberships_activation_generation",
            "ck_project_memberships_role",
            "ck_project_memberships_status",
            "ck_project_memberships_version",
        } <= {item["name"] for item in membership_checks}
        assert "uq_project_memberships_project_user" in {item["name"] for item in membership_uniques}
        cascading_fks = {item["constrained_columns"][0] for item in membership_fks if item["options"].get("ondelete") == "CASCADE"}
        assert cascading_fks == {"project_id", "user_id"}
        assert "ix_project_memberships_user_id" in {item["name"] for item in membership_indexes}
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
                    text("""SELECT status,version,activation_generation,is_pinned,created_at,updated_at
                        FROM project_memberships WHERE project_id=:id"""),
                    {"id": project_id},
                )
            ).one()
            membership_inserted_after = datetime.now(UTC)
            assert membership_defaults[:4] == ("active", 1, 1, False)
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
