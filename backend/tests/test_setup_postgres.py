from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from base64 import b64encode
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from postgres_utils import RedactedURL, replace_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.projects.errors import ProjectBootstrapFailed
from deerflow.persistence.final_schema_contract import (
    FINAL_SCHEMA_V1_CATALOG_SIGNATURE,
    read_schema_v1_catalog_signature,
)
from scripts import check_postgres, setup_postgres


@pytest.fixture(autouse=True)
def _default_model_bootstrap_environment(monkeypatch) -> None:
    encoded = b64encode(b"s" * 32).decode("ascii")
    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", encoded)
    monkeypatch.setenv(
        "ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY",
        "unit-bootstrap-secret",
    )


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
async def test_langgraph_bootstrap_uses_explicit_url_in_saver_then_store_order(monkeypatch) -> None:
    calls = []
    database_url = "postgresql://owner:private-password@localhost/deerflow_test_1_abc"

    class Context:
        def __init__(self, name):
            self.name = name

        async def __aenter__(self):
            calls.append(f"{self.name}:enter")
            return self

        async def setup(self):
            calls.append(f"{self.name}:setup")

        async def __aexit__(self, *_args):
            calls.append(f"{self.name}:exit")

    class Provider:
        def __init__(self, name):
            self.name = name

        def from_conn_string(self, url):
            assert url == setup_postgres._asyncpg_url(database_url)
            calls.append(f"{self.name}:factory")
            return Context(self.name)

    monkeypatch.setattr(setup_postgres, "AsyncPostgresSaver", Provider("saver"))
    monkeypatch.setattr(setup_postgres, "AsyncPostgresStore", Provider("store"))

    async def comment(url):
        assert url == setup_postgres._asyncpg_url(database_url)
        calls.append("comments")

    monkeypatch.setattr(setup_postgres, "_comment_langgraph_schemas", comment)

    await setup_postgres._bootstrap_langgraph_schemas(database_url)
    await setup_postgres._bootstrap_langgraph_schemas(database_url)

    assert (
        calls
        == [
            "saver:factory",
            "saver:enter",
            "saver:setup",
            "saver:exit",
            "store:factory",
            "store:enter",
            "store:setup",
            "store:exit",
            "comments",
        ]
        * 2
    )


@pytest.mark.asyncio
async def test_langgraph_bootstrap_failure_is_sanitized_and_closes_context(monkeypatch) -> None:
    calls = []

    class Context:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        async def __aenter__(self):
            calls.append(f"{self.name}:enter")
            return self

        async def setup(self):
            calls.append(f"{self.name}:setup")
            if self.fail:
                raise RuntimeError("postgresql://owner:private-password@localhost/private-db")

        async def __aexit__(self, *_args):
            calls.append(f"{self.name}:exit")

    class Provider:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        def from_conn_string(self, _url):
            return Context(self.name, self.fail)

    monkeypatch.setattr(setup_postgres, "AsyncPostgresSaver", Provider("saver"))
    monkeypatch.setattr(setup_postgres, "AsyncPostgresStore", Provider("store", fail=True))
    database_url = "postgresql://owner:private-password@localhost/deerflow_test_1_abc"

    with pytest.raises(setup_postgres.PostgresSetupError) as captured:
        await setup_postgres._bootstrap_langgraph_schemas(database_url)

    rendered = "".join(__import__("traceback").format_exception(captured.value))
    assert "private-password" not in rendered
    assert "postgresql://" not in rendered
    assert calls == ["saver:enter", "saver:setup", "saver:exit", "store:enter", "store:setup", "store:exit"]


@pytest.mark.asyncio
async def test_bootstrap_existing_runs_orm_before_langgraph_and_disposes(monkeypatch) -> None:
    calls = []
    connection = AsyncMock()

    async def execute(statement, *_args):
        calls.append(str(statement))

    connection.execute.side_effect = execute
    connection_context = MagicMock()
    connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = connection_context
    engine.dispose = AsyncMock(side_effect=lambda: calls.append("dispose"))
    monkeypatch.setattr(setup_postgres, "_create_setup_engine", lambda _config: engine)

    @asynccontextmanager
    async def coordination_lock(_database_url):
        calls.append("lock:enter")
        try:
            yield
        finally:
            calls.append("lock:exit")

    monkeypatch.setattr(setup_postgres, "_complete_bootstrap_lock", coordination_lock)

    async def bootstrap(_engine):
        calls.append("orm")
        return setup_postgres.CURRENT_SCHEMA_REVISION

    async def langgraph(_database_url):
        calls.append("langgraph")

    async def builtin(_engine):
        calls.append("builtin")

    async def default_model(_engine, material):
        assert material is bootstrap_material
        calls.append("default-model")

    async def runtime_policy(_engine):
        calls.append("runtime-policy")

    async def projects(_engine):
        calls.append("projects")

    async def finalize(_engine):
        calls.append("finalize")

    bootstrap_material = MagicMock(
        spec=setup_postgres.DefaultSystemModelBootstrapMaterial,
    )
    monkeypatch.setattr(setup_postgres, "stage_schema_for_setup", bootstrap)
    monkeypatch.setattr(setup_postgres, "finalize_staged_schema", finalize)
    monkeypatch.setattr(setup_postgres, "_bootstrap_builtin_catalog", builtin)
    monkeypatch.setattr(
        setup_postgres,
        "_bootstrap_default_model_schema",
        default_model,
    )
    monkeypatch.setattr(
        setup_postgres,
        "_bootstrap_runtime_policy_schema",
        runtime_policy,
    )
    monkeypatch.setattr(setup_postgres, "_bootstrap_langgraph_schemas", langgraph)
    monkeypatch.setattr(setup_postgres, "_bootstrap_default_project_schema", projects)

    assert (
        await setup_postgres._bootstrap_existing(
            "postgresql://owner:private-password@localhost/deerflow_test_1_abc",
            default_model_bootstrap=bootstrap_material,
        )
        == setup_postgres.CURRENT_SCHEMA_REVISION
    )
    assert calls == [
        "lock:enter",
        "SELECT 1",
        "orm",
        "builtin",
        "default-model",
        "runtime-policy",
        "langgraph",
        "projects",
        "finalize",
        "lock:exit",
        "dispose",
    ]
    connection_context.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_existing_preserves_ambiguous_admin_code(monkeypatch) -> None:
    connection = AsyncMock()
    connection_context = MagicMock()
    connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = connection_context
    engine.dispose = AsyncMock()
    monkeypatch.setattr(setup_postgres, "_create_setup_engine", lambda _config: engine)

    @asynccontextmanager
    async def coordination_lock(_database_url):
        yield

    monkeypatch.setattr(setup_postgres, "_complete_bootstrap_lock", coordination_lock)
    monkeypatch.setattr(setup_postgres, "stage_schema_for_setup", AsyncMock())
    monkeypatch.setattr(setup_postgres, "finalize_staged_schema", AsyncMock())
    monkeypatch.setattr(setup_postgres, "_bootstrap_builtin_catalog", AsyncMock())
    monkeypatch.setattr(setup_postgres, "_bootstrap_default_model_schema", AsyncMock())
    monkeypatch.setattr(setup_postgres, "_bootstrap_runtime_policy_schema", AsyncMock())
    monkeypatch.setattr(setup_postgres, "_bootstrap_langgraph_schemas", AsyncMock())
    monkeypatch.setattr(
        setup_postgres,
        "_bootstrap_default_project_schema",
        AsyncMock(side_effect=ProjectBootstrapFailed("AMBIGUOUS_BOOTSTRAP_ADMIN")),
    )

    with pytest.raises(setup_postgres.PostgresSetupError) as exc_info:
        await setup_postgres._bootstrap_existing("postgresql://owner:private-password@localhost/deerflow_test_1_abc")
    assert str(exc_info.value) == "AMBIGUOUS_BOOTSTRAP_ADMIN"
    assert "private-password" not in str(exc_info.value)
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_existing_rejects_unknown_schema_without_mutation(monkeypatch) -> None:
    connection = AsyncMock()
    connection_context = MagicMock()
    connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = connection_context
    engine.dispose = AsyncMock()
    monkeypatch.setattr(setup_postgres, "_create_setup_engine", lambda _config: engine)

    @asynccontextmanager
    async def coordination_lock(_database_url):
        yield

    monkeypatch.setattr(setup_postgres, "_complete_bootstrap_lock", coordination_lock)
    monkeypatch.setattr(
        setup_postgres,
        "stage_schema_for_setup",
        AsyncMock(side_effect=setup_postgres.SchemaRecreateRequired()),
    )

    with pytest.raises(setup_postgres.PostgresSetupError) as exc_info:
        await setup_postgres._bootstrap_existing("postgresql://owner:private-password@localhost/deerflow_test_1_abc")

    assert str(exc_info.value).startswith("SCHEMA_RECREATE_REQUIRED:")
    assert setup_postgres.CURRENT_SCHEMA_REVISION in str(exc_info.value)
    assert "重建目标数据库" in str(exc_info.value)
    assert "private-password" not in str(exc_info.value)
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_existing_preserves_setup_required_state(
    monkeypatch,
) -> None:
    connection = AsyncMock()
    connection_context = MagicMock()
    connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = connection_context
    engine.dispose = AsyncMock()
    monkeypatch.setattr(setup_postgres, "_create_setup_engine", lambda _config: engine)

    @asynccontextmanager
    async def coordination_lock(_database_url):
        yield

    monkeypatch.setattr(setup_postgres, "_complete_bootstrap_lock", coordination_lock)
    monkeypatch.setattr(
        setup_postgres,
        "stage_schema_for_setup",
        AsyncMock(side_effect=setup_postgres.SchemaSetupRequired()),
    )

    with pytest.raises(setup_postgres.PostgresSetupError) as exc_info:
        await setup_postgres._bootstrap_existing("postgresql://owner:private-password@localhost/deerflow_test_1_abc")

    assert str(exc_info.value) == "DATABASE_SETUP_REQUIRED: 目标库尚未初始化；请运行 `make setup-db`"
    assert "private-password" not in str(exc_info.value)
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_existing_cleanup_failure_does_not_override_bootstrap_code(monkeypatch) -> None:
    connection = AsyncMock()
    connection_context = MagicMock()
    connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = connection_context
    engine.dispose = AsyncMock(side_effect=RuntimeError("postgresql://owner:private-password@localhost/private-db"))
    monkeypatch.setattr(setup_postgres, "_create_setup_engine", lambda _config: engine)

    @asynccontextmanager
    async def coordination_lock(_database_url):
        yield

    monkeypatch.setattr(setup_postgres, "_complete_bootstrap_lock", coordination_lock)
    monkeypatch.setattr(setup_postgres, "stage_schema_for_setup", AsyncMock())
    monkeypatch.setattr(setup_postgres, "finalize_staged_schema", AsyncMock())
    monkeypatch.setattr(setup_postgres, "_bootstrap_builtin_catalog", AsyncMock())
    monkeypatch.setattr(setup_postgres, "_bootstrap_default_model_schema", AsyncMock())
    monkeypatch.setattr(setup_postgres, "_bootstrap_runtime_policy_schema", AsyncMock())
    monkeypatch.setattr(setup_postgres, "_bootstrap_langgraph_schemas", AsyncMock())
    monkeypatch.setattr(
        setup_postgres,
        "_bootstrap_default_project_schema",
        AsyncMock(side_effect=ProjectBootstrapFailed("AMBIGUOUS_BOOTSTRAP_ADMIN")),
    )

    with pytest.raises(setup_postgres.PostgresSetupError) as exc_info:
        await setup_postgres._bootstrap_existing("postgresql://owner:private-password@localhost/deerflow_test_1_abc")
    assert str(exc_info.value) == "AMBIGUOUS_BOOTSTRAP_ADMIN"
    assert "private-password" not in str(exc_info.value)
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_bootstrap_lock_uses_dedicated_autocommit_session_and_cleanup(monkeypatch) -> None:
    calls = []
    lock_results = iter([False, False, True])
    connection = MagicMock()

    async def execute(statement, *_args):
        calls.append(str(statement))

    async def scalar(statement, *_args):
        calls.append(str(statement))
        if "current_setting" in str(statement):
            return "0"
        if "pg_try_advisory_lock" in str(statement):
            return next(lock_results)
        return True

    connection.execute = AsyncMock(side_effect=execute)
    connection.scalar = AsyncMock(side_effect=scalar)
    connection_context = MagicMock()
    connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=None)
    lock_engine = MagicMock()
    lock_engine.connect.return_value = connection_context
    lock_engine.dispose = AsyncMock(side_effect=lambda: calls.append("dispose"))
    monkeypatch.setattr(setup_postgres, "_create_bootstrap_lock_engine", lambda _url: lock_engine)

    async def sleep(_seconds):
        calls.append("sleep")

    monkeypatch.setattr(setup_postgres.asyncio, "sleep", sleep)

    async with setup_postgres._complete_bootstrap_lock("postgresql://owner:private-password@localhost/deerflow_test_1_abc"):
        calls.append("bootstrap")

    assert calls == [
        "SET statement_timeout = 0",
        "SET idle_in_transaction_session_timeout = 0",
        "SELECT current_setting('idle_session_timeout', true)",
        "SET idle_session_timeout = 0",
        "SELECT pg_try_advisory_lock(:lock_key)",
        "sleep",
        "SELECT pg_try_advisory_lock(:lock_key)",
        "sleep",
        "SELECT pg_try_advisory_lock(:lock_key)",
        "bootstrap",
        "SELECT pg_advisory_unlock(:lock_key)",
        "dispose",
    ]
    connection.begin.assert_not_called()
    assert all("SELECT pg_advisory_lock" not in call for call in calls)
    connection_context.__aexit__.assert_awaited_once()


def test_bootstrap_lock_engine_is_nullpool_autocommit(monkeypatch) -> None:
    factory = MagicMock(return_value=object())
    monkeypatch.setattr(setup_postgres, "create_async_engine", factory)

    setup_postgres._create_bootstrap_lock_engine("postgresql://owner:private-password@localhost/deerflow_test_1_abc")

    assert factory.call_args.kwargs == {
        "poolclass": setup_postgres.NullPool,
        "isolation_level": "AUTOCOMMIT",
    }


@pytest.mark.asyncio
async def test_complete_bootstrap_lock_skips_idle_session_timeout_when_server_lacks_setting(monkeypatch) -> None:
    statements = []
    connection = MagicMock()

    async def execute(statement, *_args):
        statements.append(str(statement))

    async def scalar(statement, *_args):
        statements.append(str(statement))
        if "current_setting" in str(statement):
            return None
        if "pg_try_advisory_lock" in str(statement):
            return True
        return True

    connection.execute = AsyncMock(side_effect=execute)
    connection.scalar = AsyncMock(side_effect=scalar)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=connection)
    context.__aexit__ = AsyncMock(return_value=None)
    lock_engine = MagicMock()
    lock_engine.connect.return_value = context
    lock_engine.dispose = AsyncMock()
    monkeypatch.setattr(setup_postgres, "_create_bootstrap_lock_engine", lambda _url: lock_engine)

    async with setup_postgres._complete_bootstrap_lock("postgresql://owner:private-password@localhost/deerflow_test_1_abc"):
        pass

    assert "SELECT current_setting('idle_session_timeout', true)" in statements
    assert "SET idle_session_timeout = 0" not in statements


@pytest.mark.asyncio
async def test_complete_bootstrap_lock_cancellation_while_polling_closes_without_unlock(monkeypatch) -> None:
    statements = []
    connection = MagicMock()
    connection.execute = AsyncMock()

    async def scalar(statement, *_args):
        statements.append(str(statement))
        if "current_setting" in str(statement):
            return None
        return False

    connection.scalar = AsyncMock(side_effect=scalar)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=connection)
    context.__aexit__ = AsyncMock(return_value=None)
    lock_engine = MagicMock()
    lock_engine.connect.return_value = context
    lock_engine.dispose = AsyncMock()
    monkeypatch.setattr(setup_postgres, "_create_bootstrap_lock_engine", lambda _url: lock_engine)
    monkeypatch.setattr(setup_postgres.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        async with setup_postgres._complete_bootstrap_lock("postgresql://owner:private-password@localhost/deerflow_test_1_abc"):
            pytest.fail("lock acquired")

    assert any("pg_try_advisory_lock" in statement for statement in statements)
    assert all("pg_advisory_unlock" not in statement for statement in statements)
    context.__aexit__.assert_awaited_once()
    lock_engine.dispose.assert_awaited_once()


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
    assert ('CREATE DATABASE "deerflow_test_1_abc" OWNER "app_owner" TEMPLATE template0',) in [call.args for call in connection.execute.await_args_list]
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
    monkeypatch.setattr(setup_postgres, "_database_exists", AsyncMock(return_value=False))

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
    material = bootstrap.await_args.kwargs["default_model_bootstrap"]
    assert isinstance(
        material,
        setup_postgres.DefaultSystemModelBootstrapMaterial,
    )
    assert "unit-bootstrap-secret" not in repr(material)


@pytest.mark.asyncio
async def test_setup_against_current_schema_is_read_only(monkeypatch) -> None:
    validate = AsyncMock(return_value=setup_postgres.CURRENT_SCHEMA_REVISION)
    ensure = AsyncMock()
    bootstrap = AsyncMock()
    prepare = MagicMock()
    monkeypatch.setattr(setup_postgres, "_database_exists", AsyncMock(return_value=True))

    @asynccontextmanager
    async def coordination_lock(_database_url):
        yield

    monkeypatch.setattr(
        setup_postgres,
        "_complete_bootstrap_lock",
        coordination_lock,
    )
    monkeypatch.setattr(
        setup_postgres,
        "_validate_existing_schema",
        validate,
        raising=False,
    )
    monkeypatch.setattr(setup_postgres, "ensure_database", ensure)
    monkeypatch.setattr(setup_postgres, "_bootstrap_existing", bootstrap)
    monkeypatch.setattr(
        setup_postgres,
        "prepare_default_system_model_bootstrap",
        prepare,
    )

    result = await setup_postgres.setup_postgres(
        "postgresql://admin:secret@localhost/postgres",
        "postgresql://owner:secret@localhost/deerflow_test_1_abc",
    )

    assert result.created is False
    assert result.revision == setup_postgres.CURRENT_SCHEMA_REVISION
    validate.assert_awaited_once()
    ensure.assert_not_awaited()
    bootstrap.assert_not_awaited()
    prepare.assert_not_called()


@pytest.mark.asyncio
async def test_setup_preflights_default_model_before_creating_database(
    monkeypatch,
) -> None:
    ensure = AsyncMock()
    monkeypatch.setattr(setup_postgres, "ensure_database", ensure)
    monkeypatch.setattr(setup_postgres, "_database_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(
        setup_postgres,
        "prepare_default_system_model_bootstrap",
        MagicMock(side_effect=setup_postgres.DefaultSystemModelBootstrapConfigurationInvalid()),
        raising=False,
    )

    with pytest.raises(
        setup_postgres.PostgresSetupError,
        match="ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY",
    ):
        await setup_postgres.setup_postgres(
            "postgresql://admin:secret@localhost/postgres",
            "postgresql://owner:secret@localhost/deerflow_test_1_abc",
        )

    ensure.assert_not_awaited()


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
    monkeypatch.setattr(setup_postgres, "stage_schema_for_setup", AsyncMock())
    monkeypatch.setattr(setup_postgres, "finalize_staged_schema", AsyncMock())

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
    bootstrap = AsyncMock(return_value=setup_postgres.CURRENT_SCHEMA_REVISION)
    monkeypatch.setattr(setup_postgres, "ensure_database", ensure)
    monkeypatch.setattr(setup_postgres, "_bootstrap_existing", bootstrap)
    monkeypatch.setattr(
        setup_postgres,
        "_database_exists",
        AsyncMock(side_effect=[False, False]),
    )
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
    for target in (
        "setup-db:",
        "check-db:",
    ):
        assert target in backend_makefile
        assert target in root_makefile
    for removed in (
        "migrate-db:",
        "--migrate-only",
        "migrate-sqlite:",
        "migrate-assets:",
        "migrate-private-work:",
        "migrate-automations:",
        "migrate-reliability:",
    ):
        assert removed not in backend_makefile
        assert removed not in root_makefile
    assert "SETUP_ENV_FILE := $(wildcard ../.env)" in backend_makefile
    assert "uv run $(if $(SETUP_ENV_FILE),--env-file $(SETUP_ENV_FILE)) python scripts/setup_postgres.py" in backend_makefile


def test_backend_setup_db_only_passes_env_file_when_it_exists(
    tmp_path: Path,
) -> None:
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    source = (Path(__file__).resolve().parents[1] / "Makefile").read_text(encoding="utf-8")
    (backend_dir / "Makefile").write_text(source, encoding="utf-8")

    without_env = subprocess.run(
        ["make", "-n", "setup-db"],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "--env-file" not in without_env

    (tmp_path / ".env").write_text("", encoding="utf-8")
    with_env = subprocess.run(
        ["make", "-n", "setup-db"],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "--env-file ../.env" in with_env


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
    assert first.revision == second.revision == setup_postgres.CURRENT_SCHEMA_REVISION
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

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            assert await read_schema_v1_catalog_signature(connection) == FINAL_SCHEMA_V1_CATALOG_SIGNATURE
            vision_timeout_seconds = await connection.scalar(
                text(
                    """SELECT (version.value->'vision_bridge'->>'timeout_seconds')::integer
                    FROM system_runtime_policies AS policy
                    JOIN system_runtime_policy_versions AS version
                      ON version.section = policy.section
                     AND version.id = policy.current_version_id
                    WHERE policy.section = 'agent_runtime'"""
                )
            )
            assert vision_timeout_seconds == 60
    finally:
        await engine.dispose()

    async with setup_postgres._complete_bootstrap_lock(database_url):
        inspector = await setup_postgres.asyncpg.connect(setup_postgres._asyncpg_url(admin_url))
        try:
            coordination_state = await inspector.fetchval(
                "SELECT state FROM pg_stat_activity WHERE datname=$1 AND query LIKE '%advisory_lock%' ORDER BY backend_start DESC LIMIT 1",
                database,
            )
        finally:
            await inspector.close()
        assert coordination_state == "idle"


@pytest.mark.asyncio
async def test_failed_bootstrap_never_publishes_schema_v1_marker(
    uncreated_setup_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_url, database_url, _database, _owner = uncreated_setup_database
    monkeypatch.setattr(
        setup_postgres,
        "_bootstrap_default_project_schema",
        AsyncMock(side_effect=ProjectBootstrapFailed("BOOTSTRAP_FAILED")),
    )

    with pytest.raises(setup_postgres.PostgresSetupError, match="BOOTSTRAP_FAILED"):
        await setup_postgres.setup_postgres(admin_url, database_url)

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            marker_count = await connection.scalar(
                text("SELECT count(*) FROM alembic_version"),
            )
    finally:
        await engine.dispose()
    assert marker_count == 0

    with pytest.raises(setup_postgres.PostgresSetupError, match="SCHEMA_RECREATE_REQUIRED"):
        await setup_postgres._validate_existing_schema(database_url)
