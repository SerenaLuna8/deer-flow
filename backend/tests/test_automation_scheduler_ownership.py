from __future__ import annotations

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from app.automations.errors import AutomationUnavailable
from app.automations.ownership import AutomationSchedulerOwnership


@pytest.mark.asyncio
async def test_uncertain_acquire_invalidates_or_unlocks_before_connection_return() -> None:
    class UncertainConnection:
        def __init__(self) -> None:
            self.scalar_calls = 0
            self.closed = False

        async def scalar(self, _statement, _parameters):
            self.scalar_calls += 1
            if self.scalar_calls == 1:
                raise SQLAlchemyError("ownership result lost")
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

    assert connection.scalar_calls == 2
    assert connection.closed is True
    assert ownership.is_acquired is False


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
