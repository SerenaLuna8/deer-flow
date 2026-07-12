from __future__ import annotations

import asyncio
import inspect
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import deerflow.persistence.models  # noqa: F401
from deerflow.persistence import bootstrap as bootstrap_module
from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import (
    _BASELINE_TABLE_NAMES,
    _get_head_revision,
    _run_baseline_create_all_sync,
    bootstrap_schema,
)


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
async def test_bootstrap_legacy_database_backfills_baseline_and_upgrades(
    postgres_database_url: str,
    monkeypatch,
) -> None:
    engine = create_async_engine(postgres_database_url)
    reset = MagicMock()
    monkeypatch.setattr(bootstrap_module, "_reset_failed_empty_bootstrap_sync", reset)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_run_baseline_create_all_sync)
            await connection.execute(text("DROP TABLE channel_conversations CASCADE"))

        await bootstrap_schema(engine)

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == _get_head_revision()
            tables = await connection.run_sync(lambda sync_connection: set(sa_inspect(sync_connection).get_table_names()))
            assert "channel_conversations" in tables
        reset.assert_not_called()
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_bootstrap_versioned_database_is_idempotent(
    postgres_database_url: str,
    monkeypatch,
) -> None:
    engine = create_async_engine(postgres_database_url)
    reset = MagicMock()
    monkeypatch.setattr(bootstrap_module, "_reset_failed_empty_bootstrap_sync", reset)
    try:
        await bootstrap_schema(engine)
        await bootstrap_schema(engine)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == _get_head_revision()
        reset.assert_not_called()
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
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda sync_connection: set(sa_inspect(sync_connection).get_table_names()))
            assert not (tables & set(Base.metadata.tables))
        monkeypatch.setattr(bootstrap_module, "_stamp", original_stamp)
        await bootstrap_schema(engine)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == _get_head_revision()
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_empty_bootstrap_cleanup_failure_preserves_stamp_error(
    postgres_database_url: str,
    monkeypatch,
    caplog,
) -> None:
    engine = create_async_engine(postgres_database_url)

    def fail_stamp(*_args, **_kwargs):
        raise RuntimeError("stamp failed")

    def fail_cleanup(_connection):
        raise RuntimeError("sensitive cleanup detail")

    monkeypatch.setattr(bootstrap_module, "_stamp", fail_stamp)
    monkeypatch.setattr(bootstrap_module, "_reset_failed_empty_bootstrap_sync", fail_cleanup)
    try:
        with pytest.raises(RuntimeError, match="stamp failed"):
            await bootstrap_schema(engine)
        assert "sensitive cleanup detail" not in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_empty_bootstrap_cleanup_failure_preserves_cancellation(
    postgres_database_url: str,
    monkeypatch,
) -> None:
    engine = create_async_engine(postgres_database_url)
    started = threading.Event()
    release = threading.Event()

    def slow_stamp(*_args, **_kwargs):
        started.set()
        release.wait(timeout=2)

    def fail_cleanup(_connection):
        raise RuntimeError("sensitive cleanup detail")

    monkeypatch.setattr(bootstrap_module, "_stamp", slow_stamp)
    monkeypatch.setattr(bootstrap_module, "_reset_failed_empty_bootstrap_sync", fail_cleanup)
    task = asyncio.create_task(bootstrap_schema(engine))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_bootstrap_cleanup_waits_through_repeated_cancellation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    class Connection:
        execute = AsyncMock()

        async def run_sync(self, _function):
            started.set()
            await release.wait()
            completed.set()

    class Begin:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return False

    class Engine:
        def begin(self):
            return Begin()

    helper = asyncio.create_task(bootstrap_module._attempt_failed_empty_bootstrap_cleanup(Engine()))
    await started.wait()
    helper.cancel("first cleanup wait cancellation")
    await asyncio.sleep(0)
    helper.cancel("second cleanup wait cancellation")
    await asyncio.sleep(0)

    assert helper.done() is False
    assert completed.is_set() is False
    release.set()
    await helper
    assert completed.is_set() is True
    assert not any(task.get_name() == "deerflow-empty-bootstrap-cleanup" for task in asyncio.all_tasks())

    assert 0 < bootstrap_module._EMPTY_BOOTSTRAP_LOCK_TIMEOUT_MS
    assert bootstrap_module._EMPTY_BOOTSTRAP_LOCK_TIMEOUT_MS <= bootstrap_module._EMPTY_BOOTSTRAP_STATEMENT_TIMEOUT_MS
    assert [str(await_call.args[0]) for await_call in Connection.execute.await_args_list] == [
        "SELECT set_config('lock_timeout', :value, true)",
        "SELECT set_config('statement_timeout', :value, true)",
    ]
    assert [await_call.args[1] for await_call in Connection.execute.await_args_list] == [
        {"value": f"{bootstrap_module._EMPTY_BOOTSTRAP_LOCK_TIMEOUT_MS}ms"},
        {"value": f"{bootstrap_module._EMPTY_BOOTSTRAP_STATEMENT_TIMEOUT_MS}ms"},
    ]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_failed_empty_bootstrap_cleanup_is_bounded_by_database_deadline(
    postgres_database_url: str,
    monkeypatch,
) -> None:
    engine = create_async_engine(postgres_database_url)
    holder_engine = create_async_engine(postgres_database_url)
    probe_engine = create_async_engine(postgres_database_url)
    stamp_started = threading.Event()
    release_stamp = threading.Event()
    stamp_error = RuntimeError("stamp failed")

    def fail_stamp(*_args, **_kwargs):
        stamp_started.set()
        release_stamp.wait(timeout=2)
        raise stamp_error

    monkeypatch.setattr(bootstrap_module, "_stamp", fail_stamp)
    monkeypatch.setattr(bootstrap_module, "_EMPTY_BOOTSTRAP_LOCK_TIMEOUT_MS", 100)
    monkeypatch.setattr(bootstrap_module, "_EMPTY_BOOTSTRAP_STATEMENT_TIMEOUT_MS", 250)
    task = asyncio.create_task(bootstrap_schema(engine))
    try:
        assert await asyncio.to_thread(stamp_started.wait, 1)
        async with holder_engine.begin() as holder:
            await holder.execute(text("LOCK TABLE runs IN ACCESS SHARE MODE"))
            release_stamp.set()
            started = asyncio.get_running_loop().time()
            with pytest.raises(RuntimeError, match="stamp failed") as exc_info:
                await asyncio.wait_for(task, timeout=2)
            assert exc_info.value is stamp_error
            assert asyncio.get_running_loop().time() - started < 1.5
            assert not any(pending.get_name() == "deerflow-empty-bootstrap-cleanup" for pending in asyncio.all_tasks())
            async with probe_engine.connect() as probe:
                assert await probe.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": bootstrap_module._PG_LOCK_KEY}) is True
                await probe.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": bootstrap_module._PG_LOCK_KEY},
                )
    finally:
        release_stamp.set()
        if not task.done():
            task.cancel()
        await engine.dispose()
        await holder_engine.dispose()
        await probe_engine.dispose()
