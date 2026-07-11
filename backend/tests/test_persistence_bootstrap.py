from __future__ import annotations

import inspect

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from deerflow.persistence.bootstrap import bootstrap_schema


def test_bootstrap_schema_no_longer_accepts_backend_keyword() -> None:
    assert "backend" not in inspect.signature(bootstrap_schema).parameters


def test_bootstrap_lock_is_always_postgres_advisory_lock() -> None:
    source = inspect.getsource(bootstrap_schema)
    assert "_postgres_lock" in source
    assert "sqlite" not in source.lower()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_bootstrap_migrates_postgres_database(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) is not None
    finally:
        await engine.dispose()
