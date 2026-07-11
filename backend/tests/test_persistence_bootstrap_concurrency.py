from __future__ import annotations

import asyncio
import threading

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence import bootstrap as bootstrap_module
from deerflow.persistence.bootstrap import _PG_LOCK_KEY, _postgres_lock, bootstrap_schema
from deerflow.persistence.engine import close_engine, get_session_factory, init_engine


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


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_advisory_lock_wait_ignores_normal_statement_timeout(postgres_database_url: str) -> None:
    holder_engine = create_async_engine(postgres_database_url)
    waiter_engine = create_async_engine(postgres_database_url, connect_args={"server_settings": {"statement_timeout": "100"}})
    try:
        async with holder_engine.connect() as holder:
            await holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _PG_LOCK_KEY})

            acquired = asyncio.Event()

            async def wait_for_lock() -> None:
                async with _postgres_lock(waiter_engine):
                    acquired.set()

            waiter = asyncio.create_task(wait_for_lock())
            await asyncio.sleep(0.25)
            assert not acquired.is_set()
            await holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _PG_LOCK_KEY})
            await asyncio.wait_for(waiter, timeout=2)

        async with holder_engine.connect() as probe:
            assert await probe.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": _PG_LOCK_KEY}) is True
            await probe.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _PG_LOCK_KEY})
    finally:
        await holder_engine.dispose()
        await waiter_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cancelled_lock_holder_releases_session_lock(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    entered = asyncio.Event()

    async def hold_lock() -> None:
        async with _postgres_lock(engine):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold_lock())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with engine.connect() as probe:
            assert await probe.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": _PG_LOCK_KEY}) is True
            await probe.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _PG_LOCK_KEY})
    finally:
        if not task.done():
            task.cancel()
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cancelled_alembic_offload_keeps_lock_until_worker_finishes(postgres_database_url: str, monkeypatch) -> None:
    engine = create_async_engine(postgres_database_url)
    probe_engine = create_async_engine(postgres_database_url)
    await bootstrap_schema(engine)
    started = threading.Event()
    release = threading.Event()
    original_upgrade = bootstrap_module._upgrade

    def blocking_upgrade(config, revision):
        started.set()
        release.wait(timeout=5)
        original_upgrade(config, revision)

    monkeypatch.setattr(bootstrap_module, "_upgrade", blocking_upgrade)
    task = asyncio.create_task(bootstrap_schema(engine))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.1)

        async with probe_engine.connect() as probe:
            assert await probe.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": _PG_LOCK_KEY}) is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        async with probe_engine.connect() as probe:
            assert await probe.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": _PG_LOCK_KEY}) is True
            await probe.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _PG_LOCK_KEY})
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await engine.dispose()
        await probe_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_bootstrap_succeeds_with_single_connection_application_pool(postgres_database_url: str) -> None:
    config = DatabaseConfig(
        url=postgres_database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout_seconds=1,
    )
    try:
        await init_engine(config)
        async with get_session_factory()() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    finally:
        await close_engine()
