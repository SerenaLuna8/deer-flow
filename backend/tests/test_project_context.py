from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, InvalidRequestError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.projects.context import resolve_project_context
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound
from app.projects.models import ProjectRole


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [
            SimpleNamespace(
                project_id=uuid.uuid4(),
                membership_id=uuid.uuid4(),
                role="viewer",
                membership_version=1,
            ),
            SimpleNamespace(
                project_id=uuid.uuid4(),
                membership_id=uuid.uuid4(),
                role="viewer",
                membership_version=1,
            ),
        ],
        [
            SimpleNamespace(
                project_id=uuid.uuid4(),
                membership_id=uuid.uuid4(),
                role="unknown-role",
                membership_version=1,
            )
        ],
    ],
)
async def test_resolver_fails_closed_on_ambiguous_or_unknown_database_rows(rows) -> None:
    result = SimpleNamespace(all=lambda: rows)
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    with pytest.raises(ProjectNotFound) as exc_info:
        await resolve_project_context(
            session,
            uuid.uuid4(),
            uuid.uuid4(),
            "req-corrupt",
        )

    session.execute.assert_awaited_once()
    assert exc_info.value.code == "project_not_found"
    assert str(exc_info.value) == "Project not found"
    assert exc_info.value.__dict__ == {}


@pytest.mark.asyncio
async def test_resolver_maps_only_dbapi_failures_to_safe_error() -> None:
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=DBAPIError(
            "SELECT secret",
            {"url": "postgresql://owner:password@db/deerflow"},
            Exception("driver failed"),
            False,
        )
    )
    with pytest.raises(ProjectDatabaseUnavailable) as exc_info:
        await resolve_project_context(session, uuid.uuid4(), uuid.uuid4(), "req")
    assert str(exc_info.value) == "Project storage unavailable"
    assert "SELECT" not in str(exc_info.value)

    misuse = MagicMock()
    misuse.begin.side_effect = InvalidRequestError("transaction already begun")
    with pytest.raises(InvalidRequestError, match="already begun"):
        await resolve_project_context(misuse, uuid.uuid4(), uuid.uuid4(), "req")


@pytest.mark.asyncio
async def test_resolver_maps_transaction_enter_dbapi_failure_to_safe_error() -> None:
    failure = DBAPIError(
        "BEGIN secret",
        {"url": "postgresql://owner:password@db/deerflow"},
        Exception("driver failed"),
        False,
    )

    class FailingTransaction:
        async def __aenter__(self):
            raise failure

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    session = MagicMock()
    session.begin.return_value = FailingTransaction()
    session.execute = AsyncMock()

    with pytest.raises(ProjectDatabaseUnavailable) as exc_info:
        await resolve_project_context(session, uuid.uuid4(), uuid.uuid4(), "req-enter")

    assert str(exc_info.value) == "Project storage unavailable"
    assert exc_info.value.__cause__ is None
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolver_maps_transaction_exit_dbapi_failure_to_safe_error() -> None:
    failure = DBAPIError(
        "COMMIT secret",
        {"url": "postgresql://owner:password@db/deerflow"},
        Exception("driver failed"),
        False,
    )

    class FailingTransaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, exc_type, exc, traceback):
            raise failure

    result = SimpleNamespace(
        all=lambda: [
            SimpleNamespace(
                project_id=uuid.uuid4(),
                membership_id=uuid.uuid4(),
                role="viewer",
                membership_version=1,
            )
        ]
    )
    session = MagicMock()
    session.begin.return_value = FailingTransaction()
    session.execute = AsyncMock(return_value=result)

    with pytest.raises(ProjectDatabaseUnavailable) as exc_info:
        await resolve_project_context(session, uuid.uuid4(), uuid.uuid4(), "req-exit")

    assert str(exc_info.value) == "Project storage unavailable"
    assert exc_info.value.__cause__ is None
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_project_context_is_single_statement_and_fail_closed(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id, outsider_id, system_admin_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    active_id, slug_collision_id = uuid.uuid4(), uuid.uuid4()
    suspended_id, inactive_project_id, inactive_membership_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    membership_id = uuid.uuid4()
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE projects DROP CONSTRAINT ck_projects_status"))
            await connection.execute(text("ALTER TABLE project_memberships DROP CONSTRAINT ck_project_memberships_status"))
            for row_id, email, system_role in (
                (user_id, "member@example.com", "user"),
                (outsider_id, "outsider@example.com", "user"),
                (system_admin_id, "system@example.com", "system_admin"),
            ):
                await connection.execute(
                    text("""INSERT INTO users
                        (id,email,system_role,created_at,needs_setup,token_version)
                        VALUES (:id,:email,:role,:now,false,0)"""),
                    {"id": str(row_id), "email": email, "role": system_role, "now": now},
                )
            for project_id, slug, status, suspended in (
                (active_id, "active-project", "active", False),
                (slug_collision_id, str(active_id), "active", False),
                (suspended_id, "suspended-project", "active", True),
                (inactive_project_id, "inactive-project", "archived", False),
                (inactive_membership_id, "inactive-membership", "active", False),
            ):
                await connection.execute(
                    text("""INSERT INTO projects
                        (id,slug,display_name,status,is_suspended,created_by_user_id)
                        VALUES (:id,:slug,'Project',:status,:suspended,:user_id)"""),
                    {
                        "id": project_id,
                        "slug": slug,
                        "status": status,
                        "suspended": suspended,
                        "user_id": str(user_id),
                    },
                )
            for row_id, project_id, role, status, version in (
                (membership_id, active_id, "editor", "active", 7),
                (uuid.uuid4(), slug_collision_id, "viewer", "active", 1),
                (uuid.uuid4(), suspended_id, "admin", "active", 1),
                (uuid.uuid4(), inactive_project_id, "admin", "active", 1),
                (uuid.uuid4(), inactive_membership_id, "admin", "disabled", 1),
            ):
                await connection.execute(
                    text("""INSERT INTO project_memberships
                        (id,project_id,user_id,role,status,version)
                        VALUES (:id,:project_id,:user_id,:role,:status,:version)"""),
                    {
                        "id": row_id,
                        "project_id": project_id,
                        "user_id": str(user_id),
                        "role": role,
                        "status": status,
                        "version": version,
                    },
                )

        statements: list[str] = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with session_factory() as session:
            by_id = await resolve_project_context(session, user_id, active_id, "req-id")
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert len(statements) == 1
        assert by_id.user_id == user_id
        assert by_id.project_id == active_id
        assert by_id.membership_id == membership_id
        assert by_id.role is ProjectRole.EDITOR
        assert by_id.membership_version == 7
        assert by_id.request_id == "req-id"

        async with session_factory() as session:
            by_slug = await resolve_project_context(session, user_id, "active-project", "req-slug")
            ambiguous_string = await resolve_project_context(session, user_id, str(active_id), "req-string")
        assert by_slug.project_id == active_id
        assert ambiguous_string.project_id == slug_collision_id

        hidden_cases = (
            (outsider_id, active_id),
            (system_admin_id, active_id),
            (user_id, suspended_id),
            (user_id, inactive_project_id),
            (user_id, inactive_membership_id),
            (user_id, uuid.uuid4()),
            (user_id, "missing-project"),
        )
        errors = []
        for hidden_user_id, identifier in hidden_cases:
            async with session_factory() as session:
                with pytest.raises(ProjectNotFound) as exc_info:
                    await resolve_project_context(session, hidden_user_id, identifier, "req-hidden")
            errors.append((exc_info.value.code, str(exc_info.value), exc_info.value.__dict__))
        assert errors == [("project_not_found", "Project not found", {})] * len(hidden_cases)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolver_dbapi_failure_rolls_back_and_session_is_reusable(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            original_execute = session.execute

            async def fail_statement(*_args, **_kwargs):
                return await original_execute(text("SELECT 1 / 0"))

            session.execute = fail_statement  # type: ignore[method-assign]
            with pytest.raises(ProjectDatabaseUnavailable):
                await resolve_project_context(session, uuid.uuid4(), uuid.uuid4(), "req")
            assert session.in_transaction() is False
            session.execute = original_execute  # type: ignore[method-assign]
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.parametrize("status", ["left", "removed"])
async def test_ended_membership_cannot_resolve_project_context(
    migrated_postgres_database_url: str,
    status: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id, project_id, membership_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'user',:now,false,0)"""
                ),
                {"id": str(user_id), "email": f"{status}@example.com", "now": now},
            )
            await connection.execute(
                text(
                    """INSERT INTO projects (id,slug,display_name,created_by_user_id)
                    VALUES (:id,:slug,'Ended membership',:user_id)"""
                ),
                {"id": project_id, "slug": f"ended-{status}", "user_id": str(user_id)},
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                    (id,project_id,user_id,role,status,ended_at,retention_until,ended_by_user_id,end_reason)
                    VALUES (:id,:project_id,:user_id,'admin',:status,:now,:retention,:user_id,:status)"""
                ),
                {
                    "id": membership_id,
                    "project_id": project_id,
                    "user_id": str(user_id),
                    "status": status,
                    "now": now,
                    "retention": now + timedelta(days=30),
                },
            )

        async with session_factory() as session:
            with pytest.raises(ProjectNotFound):
                await resolve_project_context(session, user_id, project_id, "req-ended")
    finally:
        await engine.dispose()
