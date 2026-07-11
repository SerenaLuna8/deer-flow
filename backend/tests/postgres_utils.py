"""Safe helpers for per-test PostgreSQL databases."""

from __future__ import annotations

import os
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


def replace_database(url: str, database: str) -> str:
    """Replace only the database component of a PostgreSQL URL."""
    parsed = make_url(url)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+asyncpg")
    return parsed.set(database=database).render_as_string(hide_password=False)


@asynccontextmanager
async def temporary_postgres_database(admin_url: str) -> AsyncIterator[str]:
    """Create a generated test database and always terminate/drop it."""
    database = f"deerflow_test_{os.getpid()}_{uuid.uuid4().hex}"
    if not database.startswith("deerflow_test_") or database in {"deerflow", "postgres"}:
        raise RuntimeError("refusing unsafe PostgreSQL test database name")

    admin_engine = create_async_engine(replace_database(admin_url, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        try:
            async with admin_engine.connect() as connection:
                await connection.execute(text(f'CREATE DATABASE "{database}"'))
        except Exception as exc:
            raise RuntimeError("unable to create isolated PostgreSQL test database") from exc

        try:
            yield replace_database(admin_url, database)
        finally:
            async with admin_engine.connect() as connection:
                await connection.execute(
                    text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :database AND pid <> pg_backend_pid()"),
                    {"database": database},
                )
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
    finally:
        await admin_engine.dispose()
