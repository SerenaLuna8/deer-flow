from __future__ import annotations

import inspect

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import deerflow.persistence.models  # noqa: F401
from deerflow.persistence import bootstrap as bootstrap_module
from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import _BASELINE_TABLE_NAMES, _get_head_revision, bootstrap_schema


def test_bootstrap_schema_no_longer_accepts_backend_keyword() -> None:
    assert "backend" not in inspect.signature(bootstrap_schema).parameters


def test_baseline_table_names_are_pinned() -> None:
    assert _BASELINE_TABLE_NAMES == {
        "channel_connections",
        "channel_conversations",
        "channel_credentials",
        "channel_oauth_states",
        "feedback",
        "run_events",
        "runs",
        "threads_meta",
        "users",
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_bootstrap_empty_database_creates_schema_and_stamps_head(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == _get_head_revision()
            tables = await connection.run_sync(lambda sync_connection: set(sa_inspect(sync_connection).get_table_names()))
            assert {"runs", "users", "threads_meta"} <= tables
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_bootstrap_legacy_database_backfills_baseline_and_upgrades(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("DROP TABLE channel_conversations CASCADE"))

        await bootstrap_schema(engine)

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == _get_head_revision()
            tables = await connection.run_sync(lambda sync_connection: set(sa_inspect(sync_connection).get_table_names()))
            assert "channel_conversations" in tables
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_bootstrap_versioned_database_is_idempotent(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        await bootstrap_schema(engine)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == _get_head_revision()
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_bootstrap_failure_releases_lock_and_retry_converges(postgres_database_url: str, monkeypatch) -> None:
    engine = create_async_engine(postgres_database_url)
    original_stamp = bootstrap_module._stamp

    def fail_stamp(*_args, **_kwargs):
        raise RuntimeError("stamp failed")

    monkeypatch.setattr(bootstrap_module, "_stamp", fail_stamp)
    try:
        with pytest.raises(RuntimeError, match="stamp failed"):
            await bootstrap_schema(engine)
        monkeypatch.setattr(bootstrap_module, "_stamp", original_stamp)
        await bootstrap_schema(engine)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == _get_head_revision()
    finally:
        await engine.dispose()
