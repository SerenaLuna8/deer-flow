from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.automations.errors import (
    AutomationActiveRun,
    AutomationConcurrencyLimit,
    AutomationForbidden,
    AutomationNotFound,
)
from app.automations.models import AutomationChanges
from app.automations.occurrences import (
    FRESH_THREAD_NAMESPACE,
    RUN_NAMESPACE,
    AutomationOccurrenceService,
    ManualReservation,
    deterministic_run_id,
    deterministic_thread_id,
    hash_manual_idempotency,
    manual_occurrence_key,
    scheduled_occurrence_key,
)
from app.automations.service import ProjectAutomationService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.scheduled_task_runs import (
    ScheduledTaskRunCreate,
    ScheduledTaskRunRepository,
    ScheduledTaskRunRow,
)
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskCreate,
    ScheduledTaskRecord,
    ScheduledTaskRepository,
    ScheduledTaskRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.private_scope import PrivateResourceScope

NOW = datetime(2026, 7, 16, 10, 30, tzinfo=UTC)
DUE_AT = NOW - timedelta(minutes=5)


@dataclass(frozen=True)
class OccurrenceSeed:
    database: M4ThreadSeed
    reuse_thread_id: str

    @property
    def factory(self):
        return self.database.factory

    @property
    def context(self):
        return self.database.owner_a

    @property
    def scope(self) -> PrivateResourceScope:
        return self.database.owner_a_scope

    async def create_task(
        self,
        *,
        task_id: str | None = None,
        scope: PrivateResourceScope | None = None,
        next_run_at: datetime | None = DUE_AT,
        schedule_type: str = "cron",
        schedule_spec: dict[str, object] | None = None,
        context_mode: str = "fresh_thread_per_run",
        thread_id: str | None = None,
        status: str = "enabled",
    ) -> ScheduledTaskRecord:
        task_id = task_id or f"task-{uuid.uuid4().hex[:20]}"
        task_scope = scope or self.scope
        if schedule_spec is None:
            schedule_spec = {"cron": "0 * * * *"} if schedule_type == "cron" else {"run_at": DUE_AT.isoformat()}
        async with self.factory() as session, session.begin():
            repository = ScheduledTaskRepository(session)
            task = await repository.create(
                task_scope,
                ScheduledTaskCreate(
                    task_id=task_id,
                    thread_id=thread_id,
                    context_mode=context_mode,
                    agent_asset_id=self.database.system_agent_id,
                    agent_scope="system",
                    title="Private automation",
                    prompt="Process private project work.",
                    schedule_type=schedule_type,
                    schedule_spec=schedule_spec,
                    timezone="UTC",
                    next_run_at=next_run_at,
                ),
            )
            if status != "enabled":
                updated = await repository.update(
                    task_scope,
                    task.id,
                    expected_version=task.version,
                    values={
                        "status": status,
                        "next_run_at": next_run_at if status == "enabled" else None,
                    },
                )
                assert updated is not None
                task = updated
            return task

    async def create_occurrence(
        self,
        task: ScheduledTaskRecord,
        *,
        status: str,
        trigger: str = "scheduled",
        manual_hash: str | None = None,
        scheduled_for: datetime = DUE_AT,
        suffix: str | None = None,
        next_attempt_at: datetime | None = None,
    ):
        scope = PrivateResourceScope(
            project_id=str(task.project_id),
            owner_user_id=task.owner_user_id,
            membership_version=1,
        )
        occurrence_id = f"occ-{suffix or uuid.uuid4().hex[:20]}"
        async with self.factory() as session, session.begin():
            occurrence = await ScheduledTaskRunRepository(session).create(
                scope,
                ScheduledTaskRunCreate(
                    occurrence_id=occurrence_id,
                    task_id=task.id,
                    task_version=task.version,
                    occurrence_key=hashlib.sha256(occurrence_id.encode("ascii")).hexdigest(),
                    manual_idempotency_hash=manual_hash,
                    scheduled_for=scheduled_for,
                    trigger=trigger,
                    status=status,
                ),
            )
            if next_attempt_at is not None:
                await session.execute(ScheduledTaskRunRow.__table__.update().where(ScheduledTaskRunRow.id == occurrence.id).values(next_attempt_at=next_attempt_at))
            return occurrence

    async def occurrences(self, task_id: str | None = None) -> tuple[ScheduledTaskRunRow, ...]:
        async with self.factory() as session:
            statement = select(ScheduledTaskRunRow).order_by(ScheduledTaskRunRow.created_at, ScheduledTaskRunRow.id)
            if task_id is not None:
                statement = statement.where(ScheduledTaskRunRow.task_id == task_id)
            return tuple((await session.execute(statement)).scalars())

    async def task(self, task_id: str) -> ScheduledTaskRow:
        async with self.factory() as session:
            return (await session.execute(select(ScheduledTaskRow).where(ScheduledTaskRow.id == task_id))).scalar_one()


@pytest_asyncio.fixture()
async def occurrence_seed(migrated_postgres_database_url: str) -> OccurrenceSeed:
    database = await seed_m4_thread_database(migrated_postgres_database_url)
    reuse_thread_id = f"reuse-{uuid.uuid4().hex[:20]}"
    try:
        async with database.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=database.owner_a_scope,
                thread_id=reuse_thread_id,
                agent=ThreadAgentRef(database.system_agent_id, "system"),
            )
        yield OccurrenceSeed(database=database, reuse_thread_id=reuse_thread_id)
    finally:
        await database.engine.dispose()


def test_occurrence_keys_and_uuid_hashes_are_deterministic_and_utc_canonical() -> None:
    task_id = "task-key"
    scheduled_for = datetime(2026, 7, 16, 18, 30, tzinfo=timezone(timedelta(hours=8)))
    canonical = f"scheduled:{task_id}:{scheduled_for.astimezone(UTC).isoformat()}"
    assert scheduled_occurrence_key(task_id, scheduled_for) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    idempotency_key = uuid.UUID("11111111-1111-4111-8111-111111111111")
    idempotency_hash = hashlib.sha256(str(idempotency_key).encode("ascii")).hexdigest()
    assert hash_manual_idempotency(idempotency_key) == idempotency_hash
    assert manual_occurrence_key(task_id, idempotency_hash) == hashlib.sha256(f"manual:{task_id}:{idempotency_hash}".encode()).hexdigest()


def test_claim_ids_use_fixed_uuid5_namespaces() -> None:
    occurrence_id = "occurrence-stable-id"
    assert FRESH_THREAD_NAMESPACE == uuid.UUID("8bc2f65e-f186-5fb2-a480-7f23125f8005")
    assert RUN_NAMESPACE == uuid.UUID("a58150d1-9869-55b1-8cbe-cd30e6edba05")
    assert deterministic_thread_id(occurrence_id) == str(uuid.uuid5(FRESH_THREAD_NAMESPACE, occurrence_id))
    assert deterministic_run_id(occurrence_id) == str(uuid.uuid5(RUN_NAMESPACE, occurrence_id))


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_two_pollers_reserve_one_occurrence_and_advance_atomically(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    task = await seed.create_task()
    service_a = AutomationOccurrenceService(seed.factory, max_concurrent_runs=3)
    service_b = AutomationOccurrenceService(seed.factory, max_concurrent_runs=3)

    first, second = await asyncio.gather(
        service_a.reserve_due(now=NOW, limit=10),
        service_b.reserve_due(now=NOW, limit=10),
    )

    assert len(first) + len(second) == 1
    rows = await seed.occurrences(task.id)
    assert len(rows) == 1
    assert rows[0].occurrence_key == scheduled_occurrence_key(task.id, DUE_AT)
    assert rows[0].task_version == task.version
    persisted_task = await seed.task(task.id)
    assert persisted_task.version == task.version
    assert persisted_task.next_run_at is not None and persisted_task.next_run_at > NOW


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_once_due_reservation_keeps_one_overdue_history_and_clears_next_run(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    task = await seed.create_task(
        schedule_type="once",
        schedule_spec={"run_at": DUE_AT.isoformat()},
    )

    reserved = await AutomationOccurrenceService(seed.factory, max_concurrent_runs=2).reserve_due(now=NOW, limit=10)

    assert [item.task_id for item in reserved] == [task.id]
    assert reserved[0].scheduled_for == DUE_AT
    assert (await seed.task(task.id)).next_run_at is None


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["cron", "once"])
async def test_scheduled_overlap_settles_terminal_history_and_parent_once(
    occurrence_seed: OccurrenceSeed,
    schedule_type: str,
) -> None:
    seed = occurrence_seed
    task = await seed.create_task(
        schedule_type=schedule_type,
        schedule_spec=({"cron": "0 * * * *"} if schedule_type == "cron" else {"run_at": DUE_AT.isoformat()}),
    )
    await seed.create_occurrence(task, status="running", scheduled_for=DUE_AT - timedelta(hours=1), suffix="running")
    services = (
        AutomationOccurrenceService(seed.factory, max_concurrent_runs=1),
        AutomationOccurrenceService(seed.factory, max_concurrent_runs=1),
    )

    batches = await asyncio.gather(*(service.reserve_due(now=NOW, limit=10) for service in services))

    reserved = tuple(item for batch in batches for item in batch)
    assert len(reserved) == 1 and reserved[0].status == "skipped"
    rows = await seed.occurrences(task.id)
    assert {row.status for row in rows} == {"running", "skipped"}
    skipped = next(row for row in rows if row.status == "skipped")
    assert skipped.error_code == "AUTOMATION_OVERLAP_SKIPPED"
    assert skipped.finished_at == NOW
    persisted = await seed.task(task.id)
    assert persisted.status == ("enabled" if schedule_type == "cron" else "cancelled")
    assert persisted.next_run_at is not None and persisted.next_run_at > NOW if schedule_type == "cron" else persisted.next_run_at is None
    assert persisted.last_run_at == NOW
    assert persisted.last_outcome == "skipped"
    assert persisted.last_error_code == "AUTOMATION_OVERLAP_SKIPPED"
    assert persisted.run_count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_global_cap_leaves_non_overlap_scheduled_definition_due(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    occupied_task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))
    await seed.create_occurrence(occupied_task, status="running", suffix="occupied")
    due_task = await seed.create_task()

    reserved = await AutomationOccurrenceService(seed.factory, max_concurrent_runs=1).reserve_due(now=NOW, limit=10)

    assert reserved == ()
    assert (await seed.task(due_task.id)).next_run_at == DUE_AT
    assert await seed.occurrences(due_task.id) == ()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_manual_idempotency_returns_same_occurrence_concurrently(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))
    key = uuid.UUID("11111111-1111-4111-8111-111111111111")
    service_a = AutomationOccurrenceService(seed.factory, max_concurrent_runs=3)
    service_b = AutomationOccurrenceService(seed.factory, max_concurrent_runs=3)

    first, second = await asyncio.gather(
        service_a.reserve_manual(seed.context, task.id, key, now=NOW),
        service_b.reserve_manual(seed.context, task.id, key, now=NOW),
    )

    assert isinstance(first, ManualReservation)
    assert isinstance(second, ManualReservation)
    assert first.occurrence.id == second.occurrence.id
    assert sorted((first.created, second.created)) == [False, True]
    rows = await seed.occurrences(task.id)
    assert len(rows) == 1
    assert rows[0].manual_idempotency_hash == hash_manual_idempotency(key)
    assert rows[0].occurrence_key == manual_occurrence_key(task.id, rows[0].manual_idempotency_hash)
    assert (await seed.task(task.id)).next_run_at == NOW + timedelta(hours=1)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_manual_replay_precedes_active_and_global_cap_checks(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))
    key = uuid.UUID("22222222-2222-4222-8222-222222222222")
    service = AutomationOccurrenceService(seed.factory, max_concurrent_runs=1)
    first = await service.reserve_manual(seed.context, task.id, key, now=NOW)

    replay = await service.reserve_manual(seed.context, task.id, key, now=NOW + timedelta(minutes=1))

    assert replay.occurrence.id == first.occurrence.id
    assert replay.created is False


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_manual_replay_survives_soft_delete_and_terminal_history(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))
    key = uuid.UUID("33333333-3333-4333-8333-333333333333")
    service = AutomationOccurrenceService(seed.factory, max_concurrent_runs=1)
    first = await service.reserve_manual(seed.context, task.id, key, now=NOW)
    await ProjectAutomationService(seed.factory, clock=lambda: NOW).delete(
        seed.context,
        task.id,
        task.version,
    )

    replay = await service.reserve_manual(seed.context, task.id, key, now=NOW)

    assert replay.occurrence.id == first.occurrence.id
    assert replay.occurrence.status == "cancelled"
    assert replay.created is False
    with pytest.raises(AutomationNotFound):
        await service.reserve_manual(seed.context, task.id, uuid.uuid4(), now=NOW)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_manual_replay_survives_frozen_definition(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))
    key = uuid.UUID("44444444-4444-4444-8444-444444444444")
    service = AutomationOccurrenceService(seed.factory, max_concurrent_runs=1)
    first = await service.reserve_manual(seed.context, task.id, key, now=NOW)
    async with seed.factory() as session, session.begin():
        await session.execute(ScheduledTaskRow.__table__.update().where(ScheduledTaskRow.id == task.id).values(frozen_at=NOW))

    replay = await service.reserve_manual(seed.context, task.id, key, now=NOW)

    assert replay.occurrence.id == first.occurrence.id
    assert replay.created is False
    with pytest.raises(AutomationNotFound):
        await service.reserve_manual(seed.context, task.id, uuid.uuid4(), now=NOW)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_manual_different_key_rejects_active_occurrence_without_skipped_history(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))
    service = AutomationOccurrenceService(seed.factory, max_concurrent_runs=3)
    await service.reserve_manual(seed.context, task.id, uuid.uuid4(), now=NOW)

    with pytest.raises(AutomationActiveRun) as raised:
        await service.reserve_manual(seed.context, task.id, uuid.uuid4(), now=NOW)

    assert raised.value.request_id == seed.context.request_id
    assert len(await seed.occurrences(task.id)) == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_manual_tasks_cannot_oversell_global_cap(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    first_task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))
    second_task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))
    service_a = AutomationOccurrenceService(seed.factory, max_concurrent_runs=1)
    service_b = AutomationOccurrenceService(seed.factory, max_concurrent_runs=1)

    results = await asyncio.gather(
        service_a.reserve_manual(seed.context, first_task.id, uuid.uuid4(), now=NOW),
        service_b.reserve_manual(seed.context, second_task.id, uuid.uuid4(), now=NOW),
        return_exceptions=True,
    )

    assert sum(isinstance(item, ManualReservation) for item in results) == 1
    assert sum(isinstance(item, AutomationConcurrencyLimit) for item in results) == 1
    assert len(await seed.occurrences()) == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_manual_and_scheduled_share_global_cap(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    scheduled_task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))
    await seed.create_occurrence(scheduled_task, status="running", suffix="running")
    manual_task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))

    with pytest.raises(AutomationConcurrencyLimit):
        await AutomationOccurrenceService(seed.factory, max_concurrent_runs=1).reserve_manual(
            seed.context,
            manual_task.id,
            uuid.uuid4(),
            now=NOW,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_manual_reservation_enforces_owner_scope_and_current_capabilities(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    owner_task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))
    service = AutomationOccurrenceService(seed.factory, max_concurrent_runs=3)

    with pytest.raises(AutomationNotFound) as outsider:
        await service.reserve_manual(seed.database.owner_b, owner_task.id, uuid.uuid4(), now=NOW)
    assert outsider.value.request_id == seed.database.owner_b.request_id

    viewer_task = await seed.create_task(
        scope=seed.database.viewer.resource_scope,
        next_run_at=NOW + timedelta(hours=1),
    )
    with pytest.raises(AutomationForbidden) as viewer:
        await service.reserve_manual(seed.database.viewer, viewer_task.id, uuid.uuid4(), now=NOW)
    assert viewer.value.request_id == seed.database.viewer.request_id
    assert await seed.occurrences(viewer_task.id) == ()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_claim_next_uses_skip_locked_lease_without_fk_placeholders(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    future_task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))
    await seed.create_occurrence(
        future_task,
        status="queued",
        suffix="future",
        next_attempt_at=NOW + timedelta(minutes=1),
    )
    eligible_task = await seed.create_task()
    (reserved,) = await AutomationOccurrenceService(seed.factory, max_concurrent_runs=3).reserve_due(now=NOW, limit=10)
    assert reserved.task_id == eligible_task.id

    claimed = await AutomationOccurrenceService(seed.factory, max_concurrent_runs=3).claim_next(
        now=NOW,
        lease_owner="scheduler-a",
        lease_seconds=90,
    )

    assert claimed is not None
    assert claimed.id == reserved.id
    assert claimed.status == "launching"
    assert claimed.launch_attempt_count == 1
    assert claimed.thread_id is None
    assert claimed.run_id is None
    assert claimed.started_at == NOW
    async with seed.factory() as session:
        row = (await session.execute(select(ScheduledTaskRunRow).where(ScheduledTaskRunRow.id == claimed.id))).scalar_one()
        placeholder_thread = await session.scalar(select(ThreadMetaRow.thread_id).where(ThreadMetaRow.thread_id == deterministic_thread_id(claimed.id)))
        placeholder_run = await session.scalar(select(RunRow.run_id).where(RunRow.run_id == deterministic_run_id(claimed.id)))
    assert row.lease_owner == "scheduler-a"
    assert row.lease_expires_at == NOW + timedelta(seconds=90)
    assert row.updated_at == NOW
    assert row.thread_id is None and row.run_id is None
    assert placeholder_thread is None
    assert placeholder_run is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_two_claimers_claim_one_queued_occurrence_once(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    await seed.create_task()
    service_a = AutomationOccurrenceService(seed.factory, max_concurrent_runs=2)
    service_b = AutomationOccurrenceService(seed.factory, max_concurrent_runs=2)
    await service_a.reserve_due(now=NOW, limit=10)

    first, second = await asyncio.gather(
        service_a.claim_next(now=NOW, lease_owner="scheduler-a", lease_seconds=60),
        service_b.claim_next(now=NOW, lease_owner="scheduler-b", lease_seconds=60),
    )

    claimed = [item for item in (first, second) if item is not None]
    assert len(claimed) == 1
    assert claimed[0].launch_attempt_count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_claim_scans_past_locked_oldest_then_claims_it_after_release(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    oldest_task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))
    next_task = await seed.create_task(next_run_at=NOW + timedelta(hours=1))
    service = AutomationOccurrenceService(seed.factory, max_concurrent_runs=2)
    oldest = await seed.create_occurrence(
        oldest_task,
        status="queued",
        scheduled_for=DUE_AT - timedelta(minutes=1),
        suffix="oldest",
    )
    next_eligible = await seed.create_occurrence(
        next_task,
        status="queued",
        scheduled_for=DUE_AT,
        suffix="next",
    )

    async with seed.factory() as lock_session, lock_session.begin():
        await lock_session.execute(select(ScheduledTaskRunRow).where(ScheduledTaskRunRow.id == oldest.id).with_for_update(of=ScheduledTaskRunRow))
        claimed_next = await asyncio.wait_for(
            service.claim_next(
                now=NOW,
                lease_owner="scheduler-skip-locked",
                lease_seconds=60,
            ),
            timeout=1,
        )
        assert claimed_next is not None
        assert claimed_next.id == next_eligible.id

    claimed_oldest = await service.claim_next(
        now=NOW,
        lease_owner="scheduler-after-release",
        lease_seconds=60,
    )

    assert claimed_oldest is not None
    assert claimed_oldest.id == oldest.id
    assert (
        await service.claim_next(
            now=NOW,
            lease_owner="scheduler-no-duplicate",
            lease_seconds=60,
        )
        is None
    )
    rows = await seed.occurrences()
    assert {row.id for row in rows} == {oldest.id, next_eligible.id}
    assert all(row.status == "launching" for row in rows)
    assert all(row.launch_attempt_count == 1 for row in rows)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_claim_reuse_mode_leaves_pointer_for_task6_atomic_backfill(
    occurrence_seed: OccurrenceSeed,
) -> None:
    seed = occurrence_seed
    await seed.create_task(
        context_mode="reuse_thread",
        thread_id=seed.reuse_thread_id,
    )
    service = AutomationOccurrenceService(seed.factory, max_concurrent_runs=2)
    await service.reserve_due(now=NOW, limit=10)

    claimed = await service.claim_next(now=NOW, lease_owner="scheduler", lease_seconds=60)

    assert claimed is not None
    assert claimed.thread_id is None
    assert claimed.run_id is None
    assert (await seed.task(claimed.task_id)).thread_id == seed.reuse_thread_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_claim_wins_and_definition_mutation_waits_then_rejects(
    occurrence_seed: OccurrenceSeed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two real transactions prove claim -> task mutation serialization."""

    seed = occurrence_seed
    task = await seed.create_task()
    occurrence_service = AutomationOccurrenceService(seed.factory, max_concurrent_runs=2)
    await occurrence_service.reserve_due(now=NOW, limit=10)
    definition_service = ProjectAutomationService(seed.factory, clock=lambda: NOW)

    claim_advanced = asyncio.Event()
    mutation_lock_attempted = asyncio.Event()
    release_claim = asyncio.Event()
    original_claim = ScheduledTaskRunRepository.claim
    original_lock_active = ScheduledTaskRepository.lock_active

    async def claim_and_hold_transaction(self, *args, **kwargs):
        result = await original_claim(self, *args, **kwargs)
        assert result is not None and result.status == "launching"
        claim_advanced.set()
        await release_claim.wait()
        return result

    async def observe_mutation_lock(self, *args, **kwargs):
        mutation_lock_attempted.set()
        return await original_lock_active(self, *args, **kwargs)

    monkeypatch.setattr(ScheduledTaskRunRepository, "claim", claim_and_hold_transaction)
    monkeypatch.setattr(ScheduledTaskRepository, "lock_active", observe_mutation_lock)

    claim_task = asyncio.create_task(occurrence_service.claim_next(now=NOW, lease_owner="scheduler", lease_seconds=60))
    await claim_advanced.wait()
    mutation_task = asyncio.create_task(
        definition_service.update(
            seed.context,
            task.id,
            AutomationChanges(expected_version=task.version, title="Must wait"),
        )
    )
    await mutation_lock_attempted.wait()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(mutation_task), timeout=0.1)
    finally:
        release_claim.set()

    claimed = await claim_task
    assert claimed is not None and claimed.status == "launching"
    with pytest.raises(AutomationActiveRun):
        await mutation_task
    persisted_task = await seed.task(task.id)
    assert persisted_task.title == task.title
    assert persisted_task.version == task.version


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_mutation_wins_and_claim_skips_cancelled_occurrence(
    occurrence_seed: OccurrenceSeed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A definition transaction cancels queued work before a concurrent claim."""

    seed = occurrence_seed
    task = await seed.create_task()
    occurrence_service = AutomationOccurrenceService(seed.factory, max_concurrent_runs=2)
    (queued,) = await occurrence_service.reserve_due(now=NOW, limit=10)
    definition_service = ProjectAutomationService(seed.factory, clock=lambda: NOW)

    queued_cancelled = asyncio.Event()
    release_mutation = asyncio.Event()
    original_cancel = ScheduledTaskRunRepository.cancel_queued

    async def cancel_and_hold_transaction(self, *args, **kwargs):
        count = await original_cancel(self, *args, **kwargs)
        assert count == 1
        queued_cancelled.set()
        await release_mutation.wait()
        return count

    monkeypatch.setattr(ScheduledTaskRunRepository, "cancel_queued", cancel_and_hold_transaction)

    mutation_task = asyncio.create_task(definition_service.pause(seed.context, task.id, task.version))
    await queued_cancelled.wait()
    try:
        claimed = await asyncio.wait_for(
            occurrence_service.claim_next(
                now=NOW,
                lease_owner="scheduler",
                lease_seconds=60,
            ),
            timeout=1,
        )
        assert claimed is None
    finally:
        release_mutation.set()

    paused = await mutation_task
    assert paused.status == "paused"
    rows = await seed.occurrences(task.id)
    cancelled = next(row for row in rows if row.id == queued.id)
    assert cancelled.status == "cancelled"
    assert cancelled.error_code == "AUTOMATION_PAUSED"
