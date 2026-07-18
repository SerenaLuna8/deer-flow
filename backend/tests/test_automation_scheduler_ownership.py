from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.automations.errors import AutomationUnavailable
from app.automations.ownership import (
    AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY,
    AutomationSchedulerOwnership,
)
from app.automations.reconciliation import ReconciliationReport
from app.scheduler.app import SchedulerApp
from app.scheduler.service import AutomationSchedulerService


@pytest.mark.asyncio
async def test_uncertain_acquire_invalidates_or_unlocks_before_connection_return() -> None:
    class UncertainConnection:
        def __init__(self) -> None:
            self.scalar_calls = 0
            self.execute_calls = 0
            self.closed = False

        async def execute(self, _statement, _parameters):
            self.execute_calls += 1
            raise SQLAlchemyError("ownership result lost")

        async def scalar(self, _statement, _parameters):
            self.scalar_calls += 1
            return False

        async def commit(self) -> None:
            return None

        async def invalidate(self) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    connection = UncertainConnection()

    class Engine:
        async def connect(self):
            return connection

    ownership = AutomationSchedulerOwnership(Engine())  # type: ignore[arg-type]

    with pytest.raises(AutomationUnavailable):
        await ownership.acquire()

    assert connection.execute_calls == 1
    assert connection.scalar_calls == 1
    assert connection.closed is True
    assert ownership.is_acquired is False


@pytest.mark.asyncio
async def test_raw_driver_acquire_failure_uses_stable_error_and_closes() -> None:
    class RawDisconnectConnection:
        def __init__(self) -> None:
            self.invalidated = False
            self.closed = False

        async def execute(self, _statement, _parameters):
            raise RuntimeError("raw driver disconnect with private detail")

        async def scalar(self, _statement, _parameters):
            raise RuntimeError("raw driver disconnect with private detail")

        async def invalidate(self) -> None:
            self.invalidated = True

        async def close(self) -> None:
            self.closed = True

    connection = RawDisconnectConnection()

    class Engine:
        async def connect(self):
            return connection

    ownership = AutomationSchedulerOwnership(Engine())  # type: ignore[arg-type]

    with pytest.raises(AutomationUnavailable):
        await ownership.acquire()

    assert connection.invalidated is True
    assert connection.closed is True
    assert ownership.is_acquired is False


@pytest.mark.asyncio
async def test_release_contains_raw_driver_disconnect_without_leaking_detail(
    caplog,
) -> None:
    class RawDisconnectConnection:
        def __init__(self) -> None:
            self.invalidated = False
            self.closed = False

        async def scalar(self, _statement, _parameters):
            raise RuntimeError("raw driver disconnect with private detail")

        async def invalidate(self) -> None:
            self.invalidated = True

        async def close(self) -> None:
            self.closed = True

    connection = RawDisconnectConnection()
    ownership = AutomationSchedulerOwnership(object())  # type: ignore[arg-type]
    ownership._connection = connection  # type: ignore[assignment]
    ownership._backend_pid = 123

    await ownership.release()

    assert connection.invalidated is True
    assert connection.closed is True
    assert ownership.is_acquired is False
    assert "private detail" not in caplog.text


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scheduler_ownership_is_exclusive_until_held_connection_releases(
    migrated_postgres_database_url: str,
) -> None:
    first_engine = create_async_engine(migrated_postgres_database_url)
    second_engine = create_async_engine(migrated_postgres_database_url)
    first = AutomationSchedulerOwnership(first_engine)
    second = AutomationSchedulerOwnership(second_engine)
    try:
        await first.acquire()

        with pytest.raises(AutomationUnavailable) as captured:
            await second.acquire()

        assert captured.value.request_id == "automation-scheduler-ownership"
        assert first.is_acquired is True
        assert second.is_acquired is False

        await first.release()
        await second.acquire()
        assert second.is_acquired is True
    finally:
        await first.release()
        await second.release()
        await first_engine.dispose()
        await second_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scheduler_ownership_context_releases_after_startup_exception(
    migrated_postgres_database_url: str,
) -> None:
    first_engine = create_async_engine(migrated_postgres_database_url)
    second_engine = create_async_engine(migrated_postgres_database_url)
    first = AutomationSchedulerOwnership(first_engine)
    second = AutomationSchedulerOwnership(second_engine)
    try:
        with pytest.raises(RuntimeError, match="startup failed"):
            async with first.hold():
                assert first.is_acquired is True
                raise RuntimeError("startup failed")

        assert first.is_acquired is False
        await second.acquire()
        assert second.is_acquired is True
    finally:
        await first.release()
        await second.release()
        await first_engine.dispose()
        await second_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scheduler_ownership_heartbeat_preserves_single_lock_count(
    migrated_postgres_database_url: str,
) -> None:
    owner_engine = create_async_engine(migrated_postgres_database_url)
    observer_engine = create_async_engine(migrated_postgres_database_url)
    ownership = AutomationSchedulerOwnership(owner_engine)
    try:
        await ownership.acquire()
        backend_pid = ownership.backend_pid
        assert isinstance(backend_pid, int)

        async def lock_count() -> int:
            async with observer_engine.connect() as connection:
                return int(
                    await connection.scalar(
                        text(
                            """SELECT count(*) FROM pg_locks
                            WHERE pid=:backend_pid
                              AND locktype='advisory'
                              AND granted"""
                        ),
                        {"backend_pid": backend_pid},
                    )
                    or 0
                )

        assert await lock_count() == 1
        for _ in range(5):
            await ownership.verify()
        assert await lock_count() == 1
        assert ownership.is_acquired is True
        assert ownership.is_lost is False
    finally:
        await ownership.release()
        await owner_engine.dispose()
        await observer_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scheduler_ownership_backend_termination_is_permanent_loss(
    migrated_postgres_database_url: str,
) -> None:
    first_engine = create_async_engine(migrated_postgres_database_url)
    second_engine = create_async_engine(migrated_postgres_database_url)
    first = AutomationSchedulerOwnership(first_engine)
    second = AutomationSchedulerOwnership(second_engine)
    try:
        await first.acquire()
        backend_pid = first.backend_pid
        assert isinstance(backend_pid, int)
        async with second_engine.connect() as connection:
            target_database = await connection.scalar(
                text("SELECT datname FROM pg_stat_activity WHERE pid=:backend_pid"),
                {"backend_pid": backend_pid},
            )
            assert isinstance(target_database, str)
            assert target_database.startswith("deerflow_test_")
            terminated = await connection.scalar(
                text("SELECT pg_terminate_backend(:backend_pid)"),
                {"backend_pid": backend_pid},
            )
            assert terminated is True

        with pytest.raises(AutomationUnavailable) as captured:
            await first.verify()

        assert captured.value.request_id == "automation-scheduler-ownership"
        assert first.is_lost is True
        assert first.is_acquired is False

        await second.acquire()
        assert second.is_acquired is True
        with pytest.raises(AutomationUnavailable):
            await first.acquire()
        assert first.is_acquired is False
    finally:
        await first.release()
        await second.release()
        await first_engine.dispose()
        await second_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scheduler_ownership_reconnect_or_missing_lock_is_never_recovered(
    migrated_postgres_database_url: str,
) -> None:
    for failure_mode in ("invalidated", "unlocked"):
        first_engine = create_async_engine(migrated_postgres_database_url)
        second_engine = create_async_engine(migrated_postgres_database_url)
        first = AutomationSchedulerOwnership(first_engine)
        second = AutomationSchedulerOwnership(second_engine)
        try:
            await first.acquire()
            connection = first._connection
            assert connection is not None
            if failure_mode == "invalidated":
                await connection.invalidate()
            else:
                assert (
                    await connection.scalar(
                        text("SELECT pg_advisory_unlock(:lock_key)"),
                        {
                            "lock_key": AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY,
                        },
                    )
                    is True
                )
                await connection.commit()

            with pytest.raises(AutomationUnavailable):
                await first.verify()

            assert first.is_lost is True
            assert first.is_acquired is False
            await second.acquire()
            with pytest.raises(AutomationUnavailable):
                await first.acquire()
        finally:
            await first.release()
            await second.release()
            await first_engine.dispose()
            await second_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_running_scheduler_fail_stops_before_reserve_after_backend_termination(
    migrated_postgres_database_url: str,
) -> None:
    first_engine = create_async_engine(migrated_postgres_database_url)
    second_engine = create_async_engine(migrated_postgres_database_url)
    first = AutomationSchedulerOwnership(first_engine)
    second = AutomationSchedulerOwnership(second_engine)
    first_reserve = asyncio.Event()

    class Occurrences:
        def __init__(self) -> None:
            self.due_count = 0

        async def due_definitions_in_session(self, _session, **_kwargs):
            self.due_count += 1
            first_reserve.set()
            return ()

    occurrences = Occurrences()
    dispatcher = SimpleNamespace(admit_occurrence_in_session=AsyncMock())
    reconciler = SimpleNamespace(reconcile_admitted_runs=AsyncMock(return_value=ReconciliationReport()))
    service = AutomationSchedulerService(
        occurrences=occurrences,
        dispatcher=dispatcher,
        reconciler=reconciler,
        max_concurrent_runs=3,
        ownership=first,
    )
    scheduler = SchedulerApp(
        enabled=True,
        ownership=first,
        service=service,
        session_factory=async_sessionmaker(first_engine, expire_on_commit=False),
        poll_interval_seconds=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(stop))
    try:
        await asyncio.wait_for(first_reserve.wait(), timeout=1)
        backend_pid = first.backend_pid
        assert isinstance(backend_pid, int)

        async with second_engine.connect() as connection:
            target_database = await connection.scalar(
                text("SELECT datname FROM pg_stat_activity WHERE pid=:backend_pid"),
                {"backend_pid": backend_pid},
            )
            assert isinstance(target_database, str)
            assert target_database.startswith("deerflow_test_")
            assert (
                await connection.scalar(
                    text("SELECT pg_terminate_backend(:backend_pid)"),
                    {"backend_pid": backend_pid},
                )
                is True
            )
        due_count_after_termination = occurrences.due_count

        await asyncio.wait_for(task, timeout=1)
        due_count_at_loss = occurrences.due_count

        assert first.is_lost is True
        assert due_count_after_termination >= 1
        assert due_count_at_loss == due_count_after_termination
        dispatcher.admit_occurrence_in_session.assert_not_awaited()

        await second.acquire()
        assert second.is_acquired is True
        with pytest.raises(AutomationUnavailable):
            await scheduler.run(asyncio.Event())
        await asyncio.sleep(0.1)
        assert occurrences.due_count == due_count_at_loss
    finally:
        stop.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await first.release()
        await second.release()
        await first_engine.dispose()
        await second_engine.dispose()
