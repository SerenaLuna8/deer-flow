from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from deerflow.persistence.bootstrap import bootstrap_schema


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_postgres_bootstrap_is_serialized(postgres_database_url: str) -> None:
    first = create_async_engine(postgres_database_url)
    second = create_async_engine(postgres_database_url)
    try:
        await asyncio.gather(bootstrap_schema(first), bootstrap_schema(second))
        async with first.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) is not None
    finally:
        await first.dispose()
        await second.dispose()
