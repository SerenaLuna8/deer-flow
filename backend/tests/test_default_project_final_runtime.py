from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.projects.bootstrap import bootstrap_default_project
from app.projects.errors import (
    ProjectBootstrapFailed,
    ProjectDatabaseUnavailable,
    ProjectMemberQuotaExceeded,
)
from app.projects.models import BootstrapStatus


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_default_bootstrap_states_idempotency_and_concurrency(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = str(uuid.uuid4())
    try:
        async with factory() as session:
            assert (await bootstrap_default_project(session)).status is BootstrapStatus.NO_USERS
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,'admin@example.com','user',:now,false,0)"""
                ),
                {"id": admin_id, "now": datetime.now(UTC)},
            )
        async with factory() as session:
            assert (await bootstrap_default_project(session)).status is BootstrapStatus.WAITING_FOR_ADMIN
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE users SET system_role='system_admin' WHERE id=:id"),
                {"id": admin_id},
            )

        async def run_once():
            async with factory() as session:
                return await bootstrap_default_project(session)

        results = await asyncio.gather(run_once(), run_once())
        assert {result.status for result in results} == {
            BootstrapStatus.CREATED,
            BootstrapStatus.EXISTING,
        }
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM projects WHERE slug='default-project'")) == 1
            assert (
                await connection.scalar(
                    text(
                        """SELECT count(*) FROM project_memberships m
                    JOIN projects p ON p.id=m.project_id
                    WHERE p.slug='default-project'"""
                    )
                )
                == 1
            )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_default_bootstrap_reserves_initial_membership_atomically(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = str(uuid.uuid4())

    class RejectMemberReservation:
        async def reserve_member(
            self,
            session,
            context,
            *,
            membership_id,
            activation_generation,
        ) -> None:
            assert session.in_transaction()
            assert str(context.user_id) == admin_id
            assert context.project_id is not None
            assert context.membership_id == membership_id
            assert context.membership_version == 1
            assert activation_generation == 1
            raise ProjectMemberQuotaExceeded()

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,'quota-admin@example.com','system_admin',:now,false,0)"""
                ),
                {"id": admin_id, "now": datetime.now(UTC)},
            )
        async with factory() as session:
            with pytest.raises(ProjectMemberQuotaExceeded):
                await bootstrap_default_project(session, quota=RejectMemberReservation())
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM projects")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM project_memberships")) == 0
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("roles", [("user", "user"), ("system_admin", "system_admin")])
async def test_default_bootstrap_requires_a_unique_admin(
    migrated_postgres_database_url: str,
    roles: tuple[str, str],
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            for index, role in enumerate(roles):
                await connection.execute(
                    text(
                        """INSERT INTO users
                        (id,email,system_role,created_at,needs_setup,token_version)
                        VALUES (:id,:email,:role,:now,false,0)"""
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "email": f"u{index}@example.com",
                        "role": role,
                        "now": datetime.now(UTC),
                    },
                )
        async with factory() as session:
            with pytest.raises(ProjectBootstrapFailed) as exc_info:
                await bootstrap_default_project(session)
        assert exc_info.value.code == "AMBIGUOUS_BOOTSTRAP_ADMIN"
        assert "@example" not in str(exc_info.value)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_default_bootstrap_selects_unique_admin_among_existing_users(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            for index, role in enumerate(("user", "system_admin", "user")):
                user_id = admin_id if role == "system_admin" else str(uuid.uuid4())
                await connection.execute(
                    text(
                        """INSERT INTO users
                        (id,email,system_role,created_at,needs_setup,token_version)
                        VALUES (:id,:email,:role,:now,false,0)"""
                    ),
                    {
                        "id": user_id,
                        "email": f"existing{index}@example.com",
                        "role": role,
                        "now": datetime.now(UTC),
                    },
                )
        async with factory() as session:
            result = await bootstrap_default_project(session)
        assert result.status is BootstrapStatus.CREATED
        async with engine.connect() as connection:
            owner, member, role = (
                await connection.execute(
                    text(
                        """SELECT p.created_by_user_id,m.user_id,m.role
                        FROM projects p JOIN project_memberships m ON m.project_id=p.id
                        WHERE p.slug='default-project'"""
                    )
                )
            ).one()
        assert (owner, member, role) == (admin_id, admin_id, "admin")
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_default_bootstrap_rejects_slug_collision_without_mutation(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id, other_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            for user_id, email, role in (
                (admin_id, "admin2@example.com", "system_admin"),
                (other_id, "other@example.com", "user"),
            ):
                await connection.execute(
                    text(
                        """INSERT INTO users
                        (id,email,system_role,created_at,needs_setup,token_version)
                        VALUES (:id,:email,:role,:now,false,0)"""
                    ),
                    {"id": user_id, "email": email, "role": role, "now": datetime.now(UTC)},
                )
            await connection.execute(
                text(
                    """INSERT INTO projects
                    (id,slug,display_name,created_by_user_id)
                    VALUES (:id,'default-project','Wrong',:other)"""
                ),
                {"id": uuid.uuid4(), "other": other_id},
            )
        async with factory() as session:
            with pytest.raises(ProjectBootstrapFailed) as exc_info:
                await bootstrap_default_project(session)
        assert exc_info.value.code == "DEFAULT_PROJECT_CONFLICT"
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM project_memberships")) == 0
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_default_bootstrap_rejects_partial_default_project(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,'partial@example.com','system_admin',:now,false,0)"""
                ),
                {"id": admin_id, "now": datetime.now(UTC)},
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                    (id,slug,display_name,created_by_user_id)
                    VALUES (:id,'default-project','Partial',:admin_id)"""
                ),
                {"id": uuid.uuid4(), "admin_id": admin_id},
            )
        async with factory() as session:
            with pytest.raises(ProjectBootstrapFailed) as exc_info:
                await bootstrap_default_project(session)
        assert exc_info.value.code == "DEFAULT_PROJECT_CONFLICT"
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM project_memberships")) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_default_bootstrap_database_error_is_sanitized() -> None:
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=DBAPIError(
            "SELECT secret FROM users",
            {"url": "postgresql://owner:password@db/private"},
            Exception("driver failed"),
            False,
        )
    )
    with pytest.raises(ProjectDatabaseUnavailable) as exc_info:
        await bootstrap_default_project(session)
    assert str(exc_info.value) == "Project storage unavailable"
    assert "SELECT" not in str(exc_info.value)
    assert "postgresql" not in str(exc_info.value)
