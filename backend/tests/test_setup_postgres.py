from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from postgres_utils import RedactedURL, replace_database

from scripts import check_postgres, setup_postgres


def _connection(*, database_exists: bool = False, owner_exists: bool = True):
    connection = AsyncMock()

    async def fetchval(statement, *_args):
        if "pg_roles" in statement:
            return owner_exists
        if "pg_database" in statement:
            return database_exists
        raise AssertionError(f"unexpected query: {statement}")

    connection.fetchval.side_effect = fetchval
    return connection


@pytest.mark.parametrize(
    "name",
    ["DeerFlow", "1deerflow", "deer-flow", "deerflow$", "a" * 64, "deerflow;drop_database"],
)
def test_identifier_validation_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        setup_postgres.validate_identifier(name, kind="database")


@pytest.mark.parametrize("name", ["deerflow", "deerflow_test_123_abc", "postgres"])
def test_identifier_validation_accepts_strict_lowercase_names(name: str) -> None:
    assert setup_postgres.validate_identifier(name, kind="database") == name


def test_parse_target_rejects_non_postgresql_and_missing_components() -> None:
    for url in (
        "sqlite:///tmp/deerflow.db",
        "postgresql://user@localhost",
        "postgresql://localhost/deerflow",
    ):
        with pytest.raises(ValueError, match="PostgreSQL"):
            setup_postgres.parse_target(url)


def test_parse_target_supports_percent_encoded_password_without_exposing_it() -> None:
    target = setup_postgres.parse_target("postgresql://owner:p%40ss@127.0.0.1:5432/deerflow_test_1_abc")
    assert target.username == "owner"
    assert target.database == "deerflow_test_1_abc"
    assert target.host == "127.0.0.1"
    assert target.port == 5432
    assert "p@ss" not in repr(target)
    assert "%40" not in repr(target)


def test_setup_engine_factory_returns_a_fresh_engine_per_call(monkeypatch) -> None:
    first_engine = object()
    second_engine = object()
    factory = MagicMock(side_effect=[first_engine, second_engine])
    monkeypatch.setattr(setup_postgres, "create_async_engine", factory, raising=False)
    config = setup_postgres.DatabaseConfig(url="postgresql://owner:secret@localhost/deerflow_test_1_abc")

    assert setup_postgres._create_setup_engine(config) is first_engine
    assert setup_postgres._create_setup_engine(config) is second_engine
    assert factory.call_count == 2


@pytest.mark.asyncio
async def test_ensure_database_uses_parameterized_lookups_and_quoted_identifiers(monkeypatch) -> None:
    connection = _connection()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(setup_postgres.asyncpg, "connect", connect)

    created = await setup_postgres.ensure_database(
        "postgresql://admin:secret@localhost/postgres",
        "deerflow_test_1_abc",
        owner_name="app_owner",
    )

    assert created is True
    connection.fetchval.assert_any_await("SELECT 1 FROM pg_database WHERE datname = $1", "deerflow_test_1_abc")
    connection.fetchval.assert_any_await("SELECT 1 FROM pg_roles WHERE rolname = $1", "app_owner")
    assert ('CREATE DATABASE "deerflow_test_1_abc" OWNER "app_owner"',) in [call.args for call in connection.execute.await_args_list]
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_database_is_idempotent(monkeypatch) -> None:
    connection = _connection(database_exists=True)
    monkeypatch.setattr(setup_postgres.asyncpg, "connect", AsyncMock(return_value=connection))

    assert await setup_postgres.ensure_database("postgresql://admin:secret@localhost/postgres", "deerflow_test_1_abc") is False
    connection.fetchval.assert_awaited_once_with("SELECT 1 FROM pg_database WHERE datname = $1", "deerflow_test_1_abc")
    assert not any(call.args[0].startswith("CREATE DATABASE") for call in connection.execute.await_args_list)


@pytest.mark.asyncio
async def test_existing_database_still_requires_explicit_owner_to_exist(monkeypatch) -> None:
    connection = _connection(database_exists=True, owner_exists=False)
    monkeypatch.setattr(setup_postgres.asyncpg, "connect", AsyncMock(return_value=connection))
    admin_url = "postgresql://admin:secret@localhost/postgres"

    with pytest.raises(setup_postgres.PostgresSetupError, match="role") as exc_info:
        await setup_postgres.ensure_database(
            admin_url,
            "deerflow_test_1_abc",
            owner_name="missing_owner",
        )

    connection.fetchval.assert_awaited_once_with("SELECT 1 FROM pg_roles WHERE rolname = $1", "missing_owner")
    assert not any(call.args[0].startswith("CREATE DATABASE") for call in connection.execute.await_args_list)
    rendered = "".join(__import__("traceback").format_exception(exc_info.value))
    assert "secret" not in rendered
    assert "postgresql://" not in rendered


@pytest.mark.asyncio
async def test_duplicate_database_race_is_the_only_swallowed_sqlstate(monkeypatch) -> None:
    connection = _connection()
    duplicate = RuntimeError("duplicate")
    duplicate.sqlstate = "42P04"

    async def duplicate_on_create(statement, *_args):
        if statement.startswith("CREATE DATABASE"):
            raise duplicate

    connection.execute.side_effect = duplicate_on_create
    connection.fetchval.side_effect = [False, True]
    monkeypatch.setattr(setup_postgres.asyncpg, "connect", AsyncMock(return_value=connection))

    assert await setup_postgres.ensure_database("postgresql://admin:secret@localhost/postgres", "deerflow_test_1_abc") is False

    other_connection = _connection()
    other = RuntimeError("secret postgresql://admin:secret@localhost/postgres")
    other.sqlstate = "42501"

    async def other_on_create(statement, *_args):
        if statement.startswith("CREATE DATABASE"):
            raise other

    other_connection.execute.side_effect = other_on_create
    monkeypatch.setattr(setup_postgres.asyncpg, "connect", AsyncMock(return_value=other_connection))
    admin_url = "postgresql://admin:secret@localhost/postgres"
    with pytest.raises(setup_postgres.PostgresSetupError) as exc_info:
        await setup_postgres.ensure_database(admin_url, "deerflow_test_1_def")
    rendered = "".join(__import__("traceback").format_exception(exc_info.value))
    assert "secret" not in rendered
    assert "postgresql://" not in rendered


@pytest.mark.asyncio
async def test_missing_owner_is_actionable_and_sanitized(monkeypatch) -> None:
    connection = _connection(owner_exists=False)
    monkeypatch.setattr(setup_postgres.asyncpg, "connect", AsyncMock(return_value=connection))

    with pytest.raises(setup_postgres.PostgresSetupError, match="role") as exc_info:
        await setup_postgres.ensure_database(
            "postgresql://admin:secret@localhost/postgres",
            "deerflow_test_1_abc",
            owner_name="missing_owner",
        )
    assert "secret" not in str(exc_info.value)
    assert not any(call.args[0].startswith("CREATE DATABASE") for call in connection.execute.await_args_list)


@pytest.mark.asyncio
async def test_setup_validates_explicit_database_and_always_closes_engine(monkeypatch) -> None:
    ensure = AsyncMock(return_value=True)
    bootstrap = AsyncMock(side_effect=setup_postgres.PostgresSetupError("sanitized"))
    monkeypatch.setattr(setup_postgres, "ensure_database", ensure)
    monkeypatch.setattr(setup_postgres, "_bootstrap_existing", bootstrap)

    with pytest.raises(ValueError, match="does not match"):
        await setup_postgres.setup_postgres(
            "postgresql://admin:secret@localhost/postgres",
            "postgresql://owner:secret@localhost/deerflow_test_1_abc",
            expected_database="deerflow_test_1_def",
        )
    ensure.assert_not_awaited()

    with pytest.raises(setup_postgres.PostgresSetupError) as exc_info:
        await setup_postgres.setup_postgres(
            "postgresql://admin:secret@localhost/postgres",
            "postgresql://owner:secret@localhost/deerflow_test_1_abc",
        )
    assert "secret" not in str(exc_info.value)
    bootstrap.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_cleanup_failure_is_sanitized(monkeypatch) -> None:
    monkeypatch.setattr(setup_postgres, "ensure_database", AsyncMock(return_value=False))
    connection = AsyncMock()
    connection_context = MagicMock()
    connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = connection_context
    engine.dispose = AsyncMock(side_effect=RuntimeError("postgresql://owner:cleanup-secret@host/db"))
    monkeypatch.setattr(setup_postgres, "_create_setup_engine", lambda _config: engine)
    monkeypatch.setattr(setup_postgres, "bootstrap_schema", AsyncMock())

    with pytest.raises(setup_postgres.PostgresSetupError) as exc_info:
        await setup_postgres.setup_postgres(
            "postgresql://admin:secret@localhost/postgres",
            "postgresql://owner:secret@localhost/deerflow_test_1_abc",
        )
    rendered = "".join(__import__("traceback").format_exception(exc_info.value))
    assert "cleanup-secret" not in rendered
    assert "postgresql://" not in rendered


@pytest.mark.asyncio
async def test_two_concurrent_setup_calls_continue_to_bootstrap(monkeypatch) -> None:
    ensure = AsyncMock(side_effect=[True, False])
    bootstrap = AsyncMock(return_value="0003_scheduled_tasks")
    monkeypatch.setattr(setup_postgres, "ensure_database", ensure)
    monkeypatch.setattr(setup_postgres, "_bootstrap_existing", bootstrap)
    args = (
        "postgresql://admin:secret@localhost/postgres",
        "postgresql://owner:secret@localhost/deerflow_test_1_abc",
    )

    first, second = await asyncio.gather(setup_postgres.setup_postgres(*args), setup_postgres.setup_postgres(*args))

    assert {first.created, second.created} == {True, False}
    assert bootstrap.await_count == 2


def test_cli_requires_explicit_environment_and_never_prints_secret(monkeypatch, capsys) -> None:
    monkeypatch.delenv("POSTGRES_ADMIN_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:cli-secret@localhost/deerflow_test_1_abc")
    assert setup_postgres.main([]) != 0
    output = capsys.readouterr()
    rendered = output.out + output.err
    assert "POSTGRES_ADMIN_URL" in rendered
    assert "cli-secret" not in rendered
    assert "postgresql://" not in rendered


def test_makefiles_expose_database_targets() -> None:
    backend_makefile = setup_postgres.BACKEND_ROOT.joinpath("Makefile").read_text()
    root_makefile = setup_postgres.BACKEND_ROOT.parent.joinpath("Makefile").read_text()
    for target in ("setup-db:", "migrate-db:", "check-db:"):
        assert target in backend_makefile
        assert target in root_makefile
    assert "setup_postgres.py --migrate-only" in backend_makefile


@pytest_asyncio.fixture
async def uncreated_setup_database(postgres_admin_url: str):
    database = f"deerflow_test_{os.getpid()}_{uuid.uuid4().hex}"
    setup_postgres.validate_identifier(database, kind="database")
    target_url = RedactedURL(replace_database(postgres_admin_url, database))
    admin_target = setup_postgres.parse_target(postgres_admin_url, maintenance=True)
    try:
        yield postgres_admin_url, target_url, database, admin_target.username
    finally:
        connection = await setup_postgres.asyncpg.connect(setup_postgres._asyncpg_url(postgres_admin_url))
        try:
            await connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
                database,
            )
            await connection.execute(f'DROP DATABASE IF EXISTS "{database}"')
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_real_postgres_concurrent_setup_owner_bootstrap_and_check(
    uncreated_setup_database,
) -> None:
    admin_url, database_url, database, owner = uncreated_setup_database

    first, second = await asyncio.gather(
        setup_postgres.setup_postgres(admin_url, database_url),
        setup_postgres.setup_postgres(admin_url, database_url),
    )

    assert {first.created, second.created} == {True, False}
    assert first.database == second.database == database
    assert first.revision == second.revision == setup_postgres._get_head_revision()
    assert await setup_postgres.ensure_database(admin_url, database, owner_name=owner) is False

    admin_connection = await setup_postgres.asyncpg.connect(setup_postgres._asyncpg_url(admin_url))
    try:
        actual_owner = await admin_connection.fetchval(
            "SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_database WHERE datname = $1",
            database,
        )
    finally:
        await admin_connection.close()
    assert actual_owner == owner

    result = await check_postgres.check_postgres(database_url)
    assert result.healthy is True
    assert result.current_revision == result.head_revision
    assert result.missing_tables == ()
