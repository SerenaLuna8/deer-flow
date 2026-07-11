from __future__ import annotations

import re

import asyncpg
import pytest
from postgres_utils import replace_database, temporary_postgres_database
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


def test_replace_database_preserves_credentials_without_exposing_them() -> None:
    replaced = replace_database("postgresql+asyncpg://user:p%40ss@localhost/original", "postgres")
    parsed = make_url(replaced)
    assert parsed.database == "postgres"
    assert parsed.password == "p@ss"


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
