from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import CHAR, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

import deerflow.persistence.models as persistence_models
from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION

PROJECT_COLUMNS = {
    "deletion_requested_at",
    "deletion_effective_at",
    "deletion_requested_by_user_id",
}
MEMBERSHIP_COLUMNS = {
    "activation_generation",
    "ended_at",
    "retention_until",
    "ended_by_user_id",
    "end_reason",
}
INVITATION_COLUMNS = {
    "id",
    "project_id",
    "invited_email",
    "role",
    "token_hash",
    "status",
    "expires_at",
    "version",
    "created_by_user_id",
    "redeemed_by_user_id",
    "redeemed_at",
    "revoked_at",
    "created_at",
}
RATE_LIMIT_COLUMNS = {
    "key_hash",
    "failure_count",
    "window_started_at",
    "expires_at",
    "updated_at",
}


def _foreign_key_targets(foreign_keys: list[dict]) -> set[tuple[str, str, str]]:
    return {
        (
            foreign_key["constrained_columns"][0],
            foreign_key["referred_table"],
            foreign_key["referred_columns"][0],
        )
        for foreign_key in foreign_keys
    }


def _assert_token_hash_is_char_64(columns: list[dict]) -> None:
    token_hash = next(column for column in columns if column["name"] == "token_hash")
    assert isinstance(token_hash["type"], CHAR)
    assert token_hash["type"].length == 64


@pytest.mark.asyncio
async def test_final_baseline_schema_has_governance_constraints(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            assert "project_invitations" in tables
            assert "project_invitation_rate_limits" in tables
            project_columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("projects"))
            membership_columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("project_memberships"))
            invitation_columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("project_invitations"))
            project_checks = await conn.run_sync(lambda sync: inspect(sync).get_check_constraints("projects"))
            membership_checks = await conn.run_sync(lambda sync: inspect(sync).get_check_constraints("project_memberships"))
            invitation_checks = await conn.run_sync(lambda sync: inspect(sync).get_check_constraints("project_invitations"))
            project_fks = await conn.run_sync(lambda sync: inspect(sync).get_foreign_keys("projects"))
            membership_fks = await conn.run_sync(lambda sync: inspect(sync).get_foreign_keys("project_memberships"))
            invitation_fks = await conn.run_sync(lambda sync: inspect(sync).get_foreign_keys("project_invitations"))
            invitation_indexes = await conn.run_sync(lambda sync: inspect(sync).get_indexes("project_invitations"))
            rate_limit_columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("project_invitation_rate_limits"))
            rate_limit_checks = await conn.run_sync(lambda sync: inspect(sync).get_check_constraints("project_invitation_rate_limits"))
            rate_limit_indexes = await conn.run_sync(lambda sync: inspect(sync).get_indexes("project_invitation_rate_limits"))
            version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()

        assert PROJECT_COLUMNS <= {column["name"] for column in project_columns}
        assert MEMBERSHIP_COLUMNS <= {column["name"] for column in membership_columns}
        assert INVITATION_COLUMNS == {column["name"] for column in invitation_columns}
        assert RATE_LIMIT_COLUMNS == {column["name"] for column in rate_limit_columns}
        assert {
            "ck_projects_status",
            "ck_projects_membership_version",
        } <= {constraint["name"] for constraint in project_checks}
        assert {
            "ck_project_memberships_status",
            "ck_project_memberships_end_reason",
            "ck_project_memberships_activation_generation",
            "ck_project_memberships_version",
        } <= {constraint["name"] for constraint in membership_checks}
        assert {
            "ck_project_invitations_role",
            "ck_project_invitations_status",
            "ck_project_invitations_token_hash",
            "ck_project_invitations_version",
        } <= {constraint["name"] for constraint in invitation_checks}
        assert {
            "ck_project_invitation_rate_limits_key_hash",
            "ck_project_invitation_rate_limits_failure_count",
        } <= {constraint["name"] for constraint in rate_limit_checks}
        assert "ix_project_invitation_rate_limits_expires_at" in {index["name"] for index in rate_limit_indexes}
        _assert_token_hash_is_char_64(invitation_columns)
        assert (
            "deletion_requested_by_user_id",
            "users",
            "id",
        ) in _foreign_key_targets(project_fks)
        assert ("ended_by_user_id", "users", "id") in _foreign_key_targets(membership_fks)
        assert {
            ("project_id", "projects", "id"),
            ("created_by_user_id", "users", "id"),
            ("redeemed_by_user_id", "users", "id"),
        } <= _foreign_key_targets(invitation_fks)
        pending_email_index = next(index for index in invitation_indexes if index["name"] == "uq_project_invitations_pending_email")
        assert pending_email_index["unique"] is True
        assert version == CURRENT_SCHEMA_REVISION
        assert "project_invitations" in Base.metadata.tables
        assert persistence_models.ProjectInvitationRow.__tablename__ == ("project_invitations")
        assert persistence_models.ProjectInvitationRateLimitRow.__tablename__ == ("project_invitation_rate_limits")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_governance_defaults_enums_and_partial_unique_index(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    owner_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    invitation_id = uuid.uuid4()
    now = datetime.now(UTC)
    deletion_effective_at = now + timedelta(days=30)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,'owner@example.com','user',:now,false,0)"""
                ),
                {"id": owner_id, "now": now},
            )
            await conn.execute(
                text(
                    """INSERT INTO projects
                    (id,slug,display_name,created_by_user_id)
                    VALUES (:id,'governed-project','Governed',:owner_id)"""
                ),
                {"id": project_id, "owner_id": owner_id},
            )
            await conn.execute(
                text(
                    """INSERT INTO project_memberships
                    (id,project_id,user_id,role)
                    VALUES (:id,:project_id,:user_id,'admin')"""
                ),
                {
                    "id": membership_id,
                    "project_id": project_id,
                    "user_id": owner_id,
                },
            )
            await conn.execute(
                text(
                    """INSERT INTO project_invitations
                    (id,project_id,invited_email,role,token_hash,expires_at,
                     created_by_user_id)
                    VALUES (:id,:project_id,'member@example.com','viewer',
                            :token_hash,:expires_at,:created_by_user_id)"""
                ),
                {
                    "id": invitation_id,
                    "project_id": project_id,
                    "token_hash": "a" * 64,
                    "expires_at": now + timedelta(days=7),
                    "created_by_user_id": owner_id,
                },
            )
            invitation = (
                await conn.execute(
                    text(
                        """SELECT status,version,created_at
                        FROM project_invitations WHERE id=:id"""
                    ),
                    {"id": invitation_id},
                )
            ).one()
        assert invitation.status == "pending"
        assert invitation.version == 1
        assert now <= invitation.created_at

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """UPDATE projects SET status='pending_deletion',
                    deletion_requested_at=:now,
                    deletion_effective_at=:effective_at,
                    deletion_requested_by_user_id=:owner_id
                    WHERE id=:project_id"""
                ),
                {
                    "now": now,
                    "effective_at": deletion_effective_at,
                    "owner_id": owner_id,
                    "project_id": project_id,
                },
            )
            await conn.execute(
                text(
                    """UPDATE project_memberships SET status='left',
                    ended_at=:now,retention_until=:retention_until,
                    ended_by_user_id=:owner_id,end_reason='left'
                    WHERE id=:membership_id"""
                ),
                {
                    "now": now,
                    "retention_until": now + timedelta(days=30),
                    "owner_id": owner_id,
                    "membership_id": membership_id,
                },
            )

        invalid_statements = (
            (
                "UPDATE projects SET status='deleted' WHERE id=:id",
                {"id": project_id},
            ),
            (
                "UPDATE project_memberships SET status='disabled' WHERE id=:id",
                {"id": membership_id},
            ),
            (
                "UPDATE project_memberships SET end_reason='disabled' WHERE id=:id",
                {"id": membership_id},
            ),
            (
                "UPDATE project_invitations SET role='admin' WHERE id=:id",
                {"id": invitation_id},
            ),
            (
                "UPDATE project_invitations SET status='invalid' WHERE id=:id",
                {"id": invitation_id},
            ),
            (
                "UPDATE project_invitations SET version=0 WHERE id=:id",
                {"id": invitation_id},
            ),
            (
                "UPDATE project_invitations SET token_hash=:token_hash WHERE id=:id",
                {"id": invitation_id, "token_hash": "a" * 63},
            ),
            (
                "UPDATE project_invitations SET token_hash=:token_hash WHERE id=:id",
                {"id": invitation_id, "token_hash": "g" * 64},
            ),
        )
        for statement, parameters in invalid_statements:
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(text(statement), parameters)

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        """INSERT INTO project_invitations
                        (id,project_id,invited_email,role,token_hash,expires_at,
                         created_by_user_id)
                        VALUES (:id,:project_id,'member@example.com','viewer',
                                :token_hash,:expires_at,:created_by_user_id)"""
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_id,
                        "token_hash": "b" * 64,
                        "expires_at": now + timedelta(days=7),
                        "created_by_user_id": owner_id,
                    },
                )

        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE project_invitations SET status='revoked' WHERE id=:id"),
                {"id": invitation_id},
            )
            await conn.execute(
                text(
                    """INSERT INTO project_invitations
                    (id,project_id,invited_email,role,token_hash,expires_at,
                     created_by_user_id)
                    VALUES (:id,:project_id,'member@example.com','runner',
                            :token_hash,:expires_at,:created_by_user_id)"""
                ),
                {
                    "id": uuid.uuid4(),
                    "project_id": project_id,
                    "token_hash": "c" * 64,
                    "expires_at": now + timedelta(days=7),
                    "created_by_user_id": owner_id,
                },
            )
    finally:
        await engine.dispose()
