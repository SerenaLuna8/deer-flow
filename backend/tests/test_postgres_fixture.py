from __future__ import annotations

import asyncio
import re
import traceback
from importlib import import_module
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from postgres_utils import RedactedURL, _validate_test_database_name, replace_database, temporary_postgres_database
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


def test_replace_database_preserves_credentials_without_exposing_them() -> None:
    replaced = replace_database("postgresql+asyncpg://user:p%40ss@localhost/original", "postgres")
    parsed = make_url(replaced)
    assert parsed.database == "postgres"
    assert parsed.password == "p@ss"


def test_redacted_url_repr_never_contains_credentials() -> None:
    url = RedactedURL("postgresql+asyncpg://user:secret@localhost/postgres")
    assert repr(url) == "<redacted-postgres-url>"
    assert "secret" not in repr(url)


@pytest.mark.parametrize("database", ["postgres", "deerflow", "other", "deerflow_test_user_supplied"])
def test_test_database_name_validation_rejects_reserved_or_non_generated_names(database: str) -> None:
    with pytest.raises(RuntimeError, match="unsafe"):
        _validate_test_database_name(database)


def test_postgres_marker_fixture_requires_explicit_test_url(monkeypatch) -> None:
    import conftest

    monkeypatch.delenv("POSTGRES_TEST_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://application-role@localhost/deerflow")
    with pytest.raises(pytest.skip.Exception, match="POSTGRES_TEST_URL is required"):
        conftest.postgres_admin_url.__wrapped__()


@pytest.mark.asyncio
async def test_create_failure_does_not_attempt_drop_and_disposes_admin_engine() -> None:
    sensitive_url = "postgresql://user:secret@localhost/postgres"
    connection = MagicMock()
    connection.execute = AsyncMock(side_effect=RuntimeError(sensitive_url))
    context = AsyncMock()
    context.__aenter__.return_value = connection
    engine = MagicMock()
    engine.connect.return_value = context
    engine.dispose = AsyncMock()

    with patch("postgres_utils.create_async_engine", return_value=engine):
        with pytest.raises(RuntimeError, match="unable to create") as exc_info:
            async with temporary_postgres_database(sensitive_url):
                pass

    formatted = "".join(traceback.format_exception(exc_info.value))
    assert "secret" not in formatted
    assert "postgresql://user" not in formatted
    assert engine.connect.call_count == 1
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_failure_is_sanitized_and_disposes_admin_engine() -> None:
    sensitive_url = "postgresql://user:secret@localhost/postgres"
    create_connection = MagicMock()
    create_connection.execute = AsyncMock()
    create_context = AsyncMock()
    create_context.__aenter__.return_value = create_connection
    cleanup_connection = MagicMock()
    cleanup_connection.execute = AsyncMock(side_effect=RuntimeError(sensitive_url))
    cleanup_context = AsyncMock()
    cleanup_context.__aenter__.return_value = cleanup_connection
    engine = MagicMock()
    engine.connect.side_effect = [create_context, cleanup_context]
    engine.dispose = AsyncMock()

    with patch("postgres_utils.create_async_engine", return_value=engine):
        with pytest.raises(RuntimeError, match="unable to clean up") as exc_info:
            async with temporary_postgres_database(sensitive_url):
                pass

    assert "secret" not in str(exc_info.value)
    formatted = "".join(traceback.format_exception(exc_info.value))
    assert "secret" not in formatted
    assert "postgresql://user" not in formatted
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_body_failure_is_preserved_when_cleanup_also_fails() -> None:
    sensitive_url = "postgresql://user:secret@localhost/postgres"
    create_connection = MagicMock()
    create_connection.execute = AsyncMock()
    create_context = AsyncMock()
    create_context.__aenter__.return_value = create_connection
    cleanup_connection = MagicMock()
    cleanup_connection.execute = AsyncMock(side_effect=RuntimeError(sensitive_url))
    cleanup_context = AsyncMock()
    cleanup_context.__aenter__.return_value = cleanup_connection
    engine = MagicMock()
    engine.connect.side_effect = [create_context, cleanup_context]
    engine.dispose = AsyncMock()
    sentinel = ValueError("body sentinel")

    with patch("postgres_utils.create_async_engine", return_value=engine):
        with pytest.raises(ValueError, match="body sentinel") as exc_info:
            async with temporary_postgres_database(sensitive_url):
                raise sentinel

    assert exc_info.value is sentinel
    formatted = "".join(traceback.format_exception(exc_info.value))
    assert "secret" not in formatted
    assert "postgresql://user" not in formatted
    assert "cleanup of isolated PostgreSQL test database also failed" in formatted
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_body_failure_is_preserved_when_cleanup_is_cancelled() -> None:
    engine = _fixture_engine_with_cleanup_error(asyncio.CancelledError("secondary cancel"))
    sentinel = ValueError("body sentinel")

    with patch("postgres_utils.create_async_engine", return_value=engine):
        with pytest.raises(ValueError, match="body sentinel") as exc_info:
            async with temporary_postgres_database("postgresql://user:secret@localhost/postgres"):
                raise sentinel

    assert exc_info.value is sentinel
    assert "cleanup of isolated PostgreSQL test database also failed" in "".join(traceback.format_exception(exc_info.value))


@pytest.mark.asyncio
async def test_body_cancellation_identity_survives_second_cleanup_cancellation() -> None:
    engine = _fixture_engine_with_cleanup_error(asyncio.CancelledError("secondary cancel"))
    sentinel = asyncio.CancelledError("primary cancel")

    with patch("postgres_utils.create_async_engine", return_value=engine):
        with pytest.raises(asyncio.CancelledError) as exc_info:
            async with temporary_postgres_database("postgresql://user:secret@localhost/postgres"):
                raise sentinel

    assert exc_info.value is sentinel
    formatted = "".join(traceback.format_exception(exc_info.value))
    assert "primary cancel" in formatted
    assert "secondary cancel" not in formatted
    assert "secret" not in formatted


@pytest.mark.asyncio
async def test_cleanup_only_cancellation_is_preserved_without_sensitive_text() -> None:
    sensitive_url = "postgresql://user:secret@localhost/postgres"
    sentinel = asyncio.CancelledError(sensitive_url)
    engine = _fixture_engine_with_cleanup_error(sentinel)

    with patch("postgres_utils.create_async_engine", return_value=engine):
        with pytest.raises(asyncio.CancelledError) as exc_info:
            async with temporary_postgres_database(sensitive_url):
                pass

    assert exc_info.value is sentinel
    assert "secret" not in "".join(traceback.format_exception(exc_info.value))


def _fixture_engine_with_cleanup_error(cleanup_error: BaseException):
    create_connection = MagicMock()
    create_connection.execute = AsyncMock()
    create_context = AsyncMock()
    create_context.__aenter__.return_value = create_connection
    cleanup_connection = MagicMock()
    cleanup_connection.execute = AsyncMock(side_effect=cleanup_error)
    cleanup_context = AsyncMock()
    cleanup_context.__aenter__.return_value = cleanup_connection
    engine = MagicMock()
    engine.connect.side_effect = [create_context, cleanup_context]
    engine.dispose = AsyncMock()
    return engine


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_temporary_database_is_generated_and_dropped(postgres_admin_url: str) -> None:
    async with temporary_postgres_database(postgres_admin_url) as database_url:
        database = make_url(database_url).database
        assert database is not None
        assert re.fullmatch(r"deerflow_test_\d+_[0-9a-f]{32}", database)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT current_database()")) == database
        await engine.dispose()

    admin_engine = create_async_engine(postgres_admin_url)
    async with admin_engine.connect() as connection:
        assert await connection.scalar(text("SELECT 1 FROM pg_database WHERE datname = :database"), {"database": database}) is None
    await admin_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_temporary_database_drops_after_exception_and_terminates_connections(postgres_admin_url: str) -> None:
    lingering_connection = None
    database = None
    with pytest.raises(RuntimeError, match="boom"):
        async with temporary_postgres_database(postgres_admin_url) as database_url:
            database = make_url(database_url).database
            lingering_connection = await asyncpg.connect(database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
            assert await lingering_connection.fetchval("SELECT 1") == 1
            raise RuntimeError("boom")

    assert database is not None
    if lingering_connection is not None and not lingering_connection.is_closed():
        await lingering_connection.close()
    admin_engine = create_async_engine(postgres_admin_url)
    async with admin_engine.connect() as connection:
        assert await connection.scalar(text("SELECT 1 FROM pg_database WHERE datname = :database"), {"database": database}) is None
    await admin_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name",
    [
        "test_feedback",
        "test_run_event_store",
        "test_run_repository",
        "test_scheduled_task_claims",
        "test_scheduled_task_repository",
    ],
)
async def test_repository_fixture_failure_closes_global_engine_before_database_drop(
    postgres_admin_url: str,
    module_name: str,
) -> None:
    from deerflow.config.database_config import DatabaseConfig
    from deerflow.persistence.bootstrap import bootstrap_schema
    from deerflow.persistence.engine import close_engine, get_engine, get_session_factory, init_engine

    module = import_module(module_name)
    try:
        async with temporary_postgres_database(postgres_admin_url) as database_url:
            schema_engine = create_async_engine(database_url)
            try:
                await bootstrap_schema(schema_engine)
            finally:
                await schema_engine.dispose()

            fixture = module._postgres_database.__wrapped__(RedactedURL(database_url))
            await fixture.__anext__()
            await init_engine(DatabaseConfig(url=database_url))

            with pytest.raises(RuntimeError, match="fixture body failed"):
                await fixture.athrow(RuntimeError("fixture body failed"))

        assert get_engine() is None
        with pytest.raises(RuntimeError, match="not initialized"):
            get_session_factory()
    finally:
        await close_engine()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "helper_args"),
    [
        ("test_additional_channel_connections", (None, "test")),
        ("test_channel_connections_router", (None,)),
        ("test_slack_channel_connections", (None,)),
    ],
)
async def test_channel_fixture_failure_disposes_owned_engine(
    monkeypatch,
    module_name: str,
    helper_args: tuple,
) -> None:
    module = import_module(module_name)
    engine = MagicMock()
    engine.dispose = AsyncMock()
    monkeypatch.setattr(module, "create_async_engine", MagicMock(return_value=engine))

    fixture = module._postgres_database.__wrapped__(RedactedURL("postgresql://unused@localhost/test"))
    await fixture.__anext__()
    await module._make_repo(*helper_args)

    with pytest.raises(RuntimeError, match="fixture body failed"):
        await fixture.athrow(RuntimeError("fixture body failed"))

    engine.dispose.assert_awaited_once()
