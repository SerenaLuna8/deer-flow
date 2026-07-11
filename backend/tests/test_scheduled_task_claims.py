import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.engine import close_engine, get_session_factory, init_engine_from_config
from deerflow.persistence.scheduled_tasks import ScheduledTaskRepository

_DATABASE_URL: str | None = None

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("_postgres_database")]


@pytest_asyncio.fixture()
async def _postgres_database(migrated_postgres_database_url):
    global _DATABASE_URL
    _DATABASE_URL = migrated_postgres_database_url
    try:
        yield
    finally:
        await close_engine()
        _DATABASE_URL = None


def _database_config() -> DatabaseConfig:
    assert _DATABASE_URL is not None
    return DatabaseConfig(url=_DATABASE_URL)


@pytest.mark.asyncio
async def test_claim_due_tasks_claims_only_due_rows(tmp_path):
    await init_engine_from_config(_database_config())
    sf = get_session_factory()
    assert sf is not None
    repo = ScheduledTaskRepository(sf)

    due = datetime.now(UTC) - timedelta(minutes=1)
    future = datetime.now(UTC) + timedelta(hours=1)

    await repo.create(
        task_id="due-1",
        user_id="user-1",
        thread_id="thread-1",
        context_mode="reuse_thread",
        assistant_id="lead_agent",
        title="Due",
        prompt="Prompt",
        schedule_type="cron",
        schedule_spec={"cron": "0 9 * * *"},
        timezone="UTC",
        next_run_at=due,
    )
    await repo.create(
        task_id="future-1",
        user_id="user-1",
        thread_id="thread-1",
        context_mode="reuse_thread",
        assistant_id="lead_agent",
        title="Future",
        prompt="Prompt",
        schedule_type="cron",
        schedule_spec={"cron": "0 9 * * *"},
        timezone="UTC",
        next_run_at=future,
    )

    claimed = await repo.claim_due_tasks(
        now=datetime.now(UTC),
        lease_owner="worker-1",
        lease_seconds=120,
        limit=10,
    )
    assert [task["id"] for task in claimed] == ["due-1"]

    await close_engine()


@pytest.mark.asyncio
async def test_claim_reclaims_task_stuck_in_running_with_expired_lease(tmp_path):
    """A task whose claiming process died mid-dispatch must stay reclaimable.

    Regression for the lease dead-end bug: claim flips status to ``running``,
    and the old claim query only selected ``status == 'enabled'``, so a crash
    between claim and dispatch left the task permanently un-triggerable.
    """
    await init_engine_from_config(_database_config())
    sf = get_session_factory()
    assert sf is not None
    repo = ScheduledTaskRepository(sf)

    now = datetime.now(UTC)
    due = now - timedelta(minutes=5)

    await repo.create(
        task_id="stuck-1",
        user_id="user-1",
        thread_id="thread-1",
        context_mode="reuse_thread",
        assistant_id="lead_agent",
        title="Stuck",
        prompt="Prompt",
        schedule_type="cron",
        schedule_spec={"cron": "0 9 * * *"},
        timezone="UTC",
        next_run_at=due,
    )

    first_claim = await repo.claim_due_tasks(
        now=now,
        lease_owner="dead-worker",
        lease_seconds=60,
        limit=10,
    )
    assert first_claim[0]["id"] == "stuck-1"
    assert first_claim[0]["status"] == "running"

    # Simulate the claiming process dying: lease expires, status stays "running".
    expired_now = now + timedelta(seconds=120)
    reclaimed = await repo.claim_due_tasks(
        now=expired_now,
        lease_owner="new-worker",
        lease_seconds=60,
        limit=10,
    )
    assert [task["id"] for task in reclaimed] == ["stuck-1"]
    assert reclaimed[0]["lease_owner"] == "new-worker"

    await close_engine()


@pytest.mark.asyncio
async def test_claim_skips_task_with_active_lease(tmp_path):
    """A task whose lease has not expired must not be reclaimed."""
    await init_engine_from_config(_database_config())
    sf = get_session_factory()
    assert sf is not None
    repo = ScheduledTaskRepository(sf)

    now = datetime.now(UTC)
    due = now - timedelta(minutes=5)

    await repo.create(
        task_id="active-1",
        user_id="user-1",
        thread_id="thread-1",
        context_mode="reuse_thread",
        assistant_id="lead_agent",
        title="Active",
        prompt="Prompt",
        schedule_type="cron",
        schedule_spec={"cron": "0 9 * * *"},
        timezone="UTC",
        next_run_at=due,
    )

    await repo.claim_due_tasks(
        now=now,
        lease_owner="worker-1",
        lease_seconds=300,
        limit=10,
    )

    # Lease still valid — second claim within the same process must not re-grab it.
    reclaimed = await repo.claim_due_tasks(
        now=now + timedelta(seconds=10),
        lease_owner="worker-2",
        lease_seconds=300,
        limit=10,
    )
    assert reclaimed == []

    await close_engine()


@pytest.mark.asyncio
async def test_two_repositories_claim_due_set_without_overlap() -> None:
    assert _DATABASE_URL is not None
    first_engine = create_async_engine(_DATABASE_URL, poolclass=NullPool)
    second_engine = create_async_engine(_DATABASE_URL, poolclass=NullPool)
    first = ScheduledTaskRepository(async_sessionmaker(first_engine, expire_on_commit=False))
    second = ScheduledTaskRepository(async_sessionmaker(second_engine, expire_on_commit=False))
    now = datetime.now(UTC)
    expected = {f"due-{index}" for index in range(8)}
    try:
        for task_id in sorted(expected):
            await first.create(
                task_id=task_id,
                user_id="user-1",
                thread_id="thread-1",
                context_mode="reuse_thread",
                assistant_id="lead_agent",
                title=task_id,
                prompt="Prompt",
                schedule_type="cron",
                schedule_spec={"cron": "0 9 * * *"},
                timezone="UTC",
                next_run_at=now - timedelta(minutes=1),
            )

        first_claim, second_claim = await asyncio.gather(
            first.claim_due_tasks(now=now, lease_owner="worker-1", lease_seconds=120, limit=4),
            second.claim_due_tasks(now=now, lease_owner="worker-2", lease_seconds=120, limit=4),
        )
        first_ids = {task["id"] for task in first_claim}
        second_ids = {task["id"] for task in second_claim}
        assert first_ids.isdisjoint(second_ids)
        assert first_ids | second_ids == expected
    finally:
        await first_engine.dispose()
        await second_engine.dispose()
