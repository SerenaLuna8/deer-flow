"""PostgreSQL legacy-schema regressions for GitHub issue #3682."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

import deerflow.persistence.models  # noqa: F401 -- registers ORM models
from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.base import Base
from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
from deerflow.persistence.run import RunRepository

pytestmark = pytest.mark.asyncio


async def _seed_legacy_schema(database_url: str, *, drop_token_usage: bool) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            if drop_token_usage:
                await connection.execute(text("ALTER TABLE runs DROP COLUMN token_usage_by_model"))
    finally:
        await engine.dispose()


async def _column_names(database_url: str) -> list[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(lambda sync_connection: [column["name"] for column in inspect(sync_connection).get_columns("runs")])
    finally:
        await engine.dispose()


async def _version(database_url: str) -> str:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    finally:
        await engine.dispose()


async def test_legacy_database_recovers_token_usage_column(postgres_database_url: str) -> None:
    await _seed_legacy_schema(postgres_database_url, drop_token_usage=True)
    assert "token_usage_by_model" not in await _column_names(postgres_database_url)

    await init_engine(DatabaseConfig(url=postgres_database_url))
    try:
        assert "token_usage_by_model" in await _column_names(postgres_database_url)
        assert await _version(postgres_database_url) == "0004_migration_ledger"

        repo = RunRepository(get_session_factory())
        result = await repo.aggregate_tokens_by_thread(thread_id=str(uuid4()))
        assert result["total_tokens"] == 0
        assert result["by_model"] == {}
    finally:
        await close_engine()


async def test_legacy_database_with_manual_alter_still_bootstraps(postgres_database_url: str) -> None:
    await _seed_legacy_schema(postgres_database_url, drop_token_usage=False)

    await init_engine(DatabaseConfig(url=postgres_database_url))
    try:
        columns = await _column_names(postgres_database_url)
        assert columns.count("token_usage_by_model") == 1
        assert await _version(postgres_database_url) == "0004_migration_ledger"
    finally:
        await close_engine()
