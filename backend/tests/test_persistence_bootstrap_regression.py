"""PostgreSQL legacy-schema regressions for GitHub issue #3682."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.bootstrap import (
    _PRIVATE_WORK_PRE_EXPAND_REVISION,
    _run_baseline_create_all_sync,
)
from deerflow.persistence.engine import close_engine, init_engine

pytestmark = pytest.mark.asyncio


async def _seed_legacy_schema(database_url: str, *, drop_token_usage: bool) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_run_baseline_create_all_sync)
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

    with pytest.raises(RuntimeError) as captured:
        await init_engine(DatabaseConfig(url=postgres_database_url))
    assert "migrate-private-work" in str(captured.value.__cause__)
    assert "token_usage_by_model" in await _column_names(postgres_database_url)
    assert await _version(postgres_database_url) == _PRIVATE_WORK_PRE_EXPAND_REVISION
    await close_engine()


async def test_legacy_database_with_manual_alter_still_bootstraps(postgres_database_url: str) -> None:
    await _seed_legacy_schema(postgres_database_url, drop_token_usage=False)

    with pytest.raises(RuntimeError) as captured:
        await init_engine(DatabaseConfig(url=postgres_database_url))
    assert "migrate-private-work" in str(captured.value.__cause__)
    columns = await _column_names(postgres_database_url)
    assert columns.count("token_usage_by_model") == 1
    assert await _version(postgres_database_url) == _PRIVATE_WORK_PRE_EXPAND_REVISION
    await close_engine()
