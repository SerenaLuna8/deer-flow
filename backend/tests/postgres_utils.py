"""Safe helpers for per-test PostgreSQL databases."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


class RedactedURL(str):
    """String URL whose pytest/debug representation never exposes credentials."""

    def __repr__(self) -> str:
        return "<redacted-postgres-url>"


_TEST_DATABASE_PATTERN = re.compile(r"deerflow_test_[0-9]+_[0-9a-f]{32}\Z")
_CLEANUP_FAILURE_NOTE = "cleanup of isolated PostgreSQL test database also failed"


def _validate_test_database_name(database: str) -> None:
    if database in {"postgres", "deerflow"} or _TEST_DATABASE_PATTERN.fullmatch(database) is None:
        raise RuntimeError("refusing unsafe PostgreSQL test database name")


def replace_database(url: str, database: str) -> str:
    """Replace only the database component of a PostgreSQL URL."""
    parsed = make_url(url)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+asyncpg")
    return parsed.set(database=database).render_as_string(hide_password=False)


def _raise_or_note_cleanup_failure(
    primary_error: BaseException | None,
    cleanup_error: BaseException,
) -> None:
    if primary_error is not None:
        if _CLEANUP_FAILURE_NOTE not in getattr(primary_error, "__notes__", ()):
            primary_error.add_note(_CLEANUP_FAILURE_NOTE)
        return
    if isinstance(cleanup_error, asyncio.CancelledError):
        cleanup_error.args = ()
        raise cleanup_error
    if isinstance(cleanup_error, Exception):
        raise RuntimeError("unable to clean up isolated PostgreSQL test database") from None
    raise cleanup_error


@asynccontextmanager
async def temporary_postgres_database(admin_url: str) -> AsyncIterator[str]:
    """Create a generated test database and always terminate/drop it."""
    database = f"deerflow_test_{os.getpid()}_{uuid.uuid4().hex}"
    _validate_test_database_name(database)

    admin_engine = create_async_engine(replace_database(admin_url, "postgres"), isolation_level="AUTOCOMMIT")
    primary_error: BaseException | None = None
    try:
        try:
            async with admin_engine.connect() as connection:
                await connection.execute(text(f'CREATE DATABASE "{database}"'))
        except Exception:
            raise RuntimeError("unable to create isolated PostgreSQL test database") from None

        body_error: BaseException | None = None
        try:
            try:
                yield replace_database(admin_url, database)
            except BaseException as exc:
                body_error = exc
                raise
        finally:
            try:
                async with admin_engine.connect() as connection:
                    await connection.execute(
                        text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :database AND pid <> pg_backend_pid()"),
                        {"database": database},
                    )
                    await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
            except BaseException as cleanup_error:
                _raise_or_note_cleanup_failure(body_error, cleanup_error)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            await admin_engine.dispose()
        except BaseException as cleanup_error:
            _raise_or_note_cleanup_failure(primary_error, cleanup_error)
