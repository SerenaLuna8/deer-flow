from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.automations.occurrences import (
    AutomationOccurrenceService,
    deterministic_run_id,
    deterministic_thread_id,
)
from app.automations.reconciliation import AutomationReconciler
from app.automations.service import ProjectAutomationService
from app.private_work.authorization import (
    AUTHORIZATION_REVOKED_REASON,
    PrivateRunAuthorizationService,
)
from app.private_work.retention import PrivateWorkRetentionService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.membership_repository import MembershipRepository
from app.projects.membership_service import MembershipService
from app.projects.models import ProjectRole
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.scheduled_task_runs import (
    ScheduledTaskRunCreate,
    ScheduledTaskRunRepository,
)
from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskCreate,
    ScheduledTaskRepository,
)
from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
RESTORED_AT = NOW + timedelta(minutes=15)
AUTOMATION_AUTHORIZATION_REVOKED = "AUTOMATION_AUTHORIZATION_REVOKED"


@dataclass(frozen=True)
class ActiveScenario:
    task_id: str
    occurrence_id: str
    thread_id: str
    run_id: str


@pytest_asyncio.fixture()
async def retention_seed(
    migrated_postgres_database_url: str,
) -> M4ThreadSeed:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield seed
    finally:
        await seed.engine.dispose()


async def _create_task(
    seed: M4ThreadSeed,
    *,
    context=None,
    task_id: str | None = None,
):
    context = context or seed.owner_a
    async with seed.factory() as session, session.begin():
        return await ScheduledTaskRepository(session).create(
            context.resource_scope,
            ScheduledTaskCreate(
                task_id=task_id or f"retention-task-{uuid.uuid4().hex[:16]}",
                thread_id=None,
                context_mode="fresh_thread_per_run",
                agent_asset_id=(seed.project_b_agent_id if context.project_id == seed.project_b_owner_a.project_id else seed.project_agent_id),
                agent_scope="project",
                title="Private retained automation",
                prompt="Process the retained private project work.",
                schedule_type="cron",
                schedule_spec={"cron": "0 * * * *"},
                timezone="UTC",
                next_run_at=NOW,
            ),
        )


async def _create_queued_occurrence(
    seed: M4ThreadSeed,
    task,
    *,
    context=None,
    occurrence_id: str | None = None,
):
    context = context or seed.owner_a
    async with seed.factory() as session, session.begin():
        return await ScheduledTaskRunRepository(session).create(
            context.resource_scope,
            ScheduledTaskRunCreate(
                occurrence_id=occurrence_id or f"retention-occurrence-{uuid.uuid4().hex[:12]}",
                task_id=task.id,
                task_version=task.version,
                occurrence_key=uuid.uuid4().hex + uuid.uuid4().hex,
                manual_idempotency_hash=None,
                scheduled_for=NOW,
                trigger="scheduled",
                status="queued",
                created_at=NOW,
            ),
        )


async def _create_active_scenario(
    seed: M4ThreadSeed,
    *,
    context,
) -> ActiveScenario:
    task = await _create_task(seed, context=context)
    occurrence = await _create_queued_occurrence(seed, task, context=context)
    thread_id = deterministic_thread_id(occurrence.id)
    run_id = deterministic_run_id(occurrence.id)
    metadata = {
        "scheduled_task_id": task.id,
        "scheduled_task_run_id": occurrence.id,
        "scheduled_trigger": occurrence.trigger,
    }
    async with seed.factory() as session, session.begin():
        occurrences = ScheduledTaskRunRepository(session)
        claimed = await occurrences.claim(
            context.resource_scope,
            occurrence.id,
            now=NOW,
            lease_owner="retention-test",
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        assert claimed is not None
        await PrivateThreadRepository(session).create(
            scope=context.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(task.agent_asset_id, task.agent_scope),
            metadata=metadata,
        )
        await PrivateRunRepository(session).create(
            scope=context.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(
                run_id=run_id,
                status="pending",
                metadata=metadata,
            ),
        )
        running = await occurrences.mark_running(
            context.resource_scope,
            occurrence.id,
            thread_id=thread_id,
            run_id=run_id,
            started_at=NOW,
            updated_at=NOW,
        )
        assert running is not None
    return ActiveScenario(task.id, occurrence.id, thread_id, run_id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_freeze_pauses_scoped_definitions_and_cancels_only_queued_occurrences(
    retention_seed: M4ThreadSeed,
) -> None:
    seed = retention_seed
    task = await _create_task(seed, task_id="retention-target")
    queued = await _create_queued_occurrence(
        seed,
        task,
        occurrence_id="retention-target-queued",
    )
    other_owner = await _create_task(
        seed,
        context=seed.owner_b,
        task_id="retention-other-owner",
    )
    other_project = await _create_task(
        seed,
        context=seed.project_b_owner_a,
        task_id="retention-other-project",
    )

    async with seed.factory() as session, session.begin():
        expected_updated_at = await session.scalar(select(func.now()))
        change = await PrivateWorkRetentionService.freeze_owner(
            session,
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            now=NOW,
        )

    assert change.automation_ids == (task.id,)
    assert change.occurrence_ids == (queued.id,)
    async with seed.factory() as session:
        task_rows = {row.id: row for row in (await session.execute(select(ScheduledTaskRow).where(ScheduledTaskRow.id.in_((task.id, other_owner.id, other_project.id))))).scalars()}
        occurrence = (await session.execute(select(ScheduledTaskRunRow).where(ScheduledTaskRunRow.id == queued.id))).scalar_one()

    target = task_rows[task.id]
    assert target.status == "paused"
    assert target.next_run_at is None
    assert target.frozen_at == NOW
    assert target.version == task.version + 1
    assert target.updated_at == expected_updated_at
    assert task_rows[other_owner.id].status == "enabled"
    assert task_rows[other_owner.id].frozen_at is None
    assert task_rows[other_project.id].status == "enabled"
    assert task_rows[other_project.id].frozen_at is None
    assert occurrence.status == "cancelled"
    assert occurrence.error_code == AUTOMATION_AUTHORIZATION_REVOKED
    assert occurrence.error_message is None
    assert occurrence.finished_at == NOW


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_restore_unfreezes_without_resuming_then_explicit_resume_replans(
    retention_seed: M4ThreadSeed,
) -> None:
    seed = retention_seed
    active = await _create_task(seed, task_id="retention-restorable")
    deleted = await _create_task(seed, task_id="retention-deleted")
    async with seed.factory() as session, session.begin():
        frozen = await PrivateWorkRetentionService.freeze_owner(
            session,
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            now=NOW,
        )
        assert frozen.automation_ids == (deleted.id, active.id)
    async with seed.factory() as session, session.begin():
        await session.execute(ScheduledTaskRow.__table__.update().where(ScheduledTaskRow.id == deleted.id).values(deleted_at=RESTORED_AT))
    async with seed.factory() as session, session.begin():
        expected_updated_at = await session.scalar(select(func.now()))
        restored = await PrivateWorkRetentionService.restore_owner(
            session,
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            now=RESTORED_AT,
        )

    assert restored.automation_ids == (active.id,)
    assert restored.occurrence_ids == ()
    async with seed.factory() as session:
        active_row = (await session.execute(select(ScheduledTaskRow).where(ScheduledTaskRow.id == active.id))).scalar_one()
        deleted_row = (await session.execute(select(ScheduledTaskRow).where(ScheduledTaskRow.id == deleted.id))).scalar_one()
    assert active_row.status == "paused"
    assert active_row.frozen_at is None
    assert active_row.next_run_at is None
    assert active_row.updated_at == expected_updated_at
    assert active_row.version == active.version + 1
    assert deleted_row.frozen_at == NOW

    resumed = await ProjectAutomationService(
        seed.factory,
        clock=lambda: RESTORED_AT,
    ).resume(seed.owner_a, active.id, active_row.version)
    assert resumed.status == "enabled"
    assert resumed.next_run_at == datetime(2026, 7, 16, 11, 0, tzinfo=UTC)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_freeze_wins_concurrent_claim_before_commit(
    retention_seed: M4ThreadSeed,
) -> None:
    seed = retention_seed
    task = await _create_task(seed, task_id="retention-claim-race")
    queued = await _create_queued_occurrence(
        seed,
        task,
        occurrence_id="retention-claim-race-queued",
    )
    frozen_uncommitted = asyncio.Event()
    release_freeze = asyncio.Event()

    async def freeze_and_hold_transaction():
        async with seed.factory() as session, session.begin():
            change = await PrivateWorkRetentionService.freeze_owner(
                session,
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                now=NOW,
            )
            frozen_uncommitted.set()
            await release_freeze.wait()
            return change

    freeze_task = asyncio.create_task(freeze_and_hold_transaction())
    await asyncio.wait_for(frozen_uncommitted.wait(), timeout=5)
    claimed = None
    try:
        claimed = await asyncio.wait_for(
            AutomationOccurrenceService(
                seed.factory,
                max_concurrent_runs=2,
            ).claim_next(
                now=NOW,
                lease_owner="concurrent-claim",
                lease_seconds=60,
            ),
            timeout=5,
        )
    finally:
        release_freeze.set()
    change = await asyncio.wait_for(freeze_task, timeout=5)

    assert claimed is None
    assert change.automation_ids == (task.id,)
    assert change.occurrence_ids == (queued.id,)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_viewer_downgrade_preserves_readable_definition_and_active_run_settles_safely(
    retention_seed: M4ThreadSeed,
) -> None:
    seed = retention_seed
    scenario = await _create_active_scenario(seed, context=seed.owner_b)
    actor_context = ProjectContext(
        user_id=seed.owner_a.user_id,
        project_id=seed.owner_a.project_id,
        membership_id=seed.owner_a.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=seed.owner_a.membership_version,
        request_id="retention-viewer-downgrade",
    )
    async with seed.factory() as session:
        await MembershipService(
            MembershipRepository(session),
            clock=lambda: NOW,
        ).change_role(
            actor_context,
            seed.owner_b.membership_id,
            ProjectRole.VIEWER,
            expected_version=seed.owner_b.membership_version,
        )

    async with seed.factory() as session, session.begin():
        run = (
            await session.execute(
                select(RunRow).where(
                    RunRow.run_id == scenario.run_id,
                    RunRow.project_id == seed.owner_b.project_id,
                    RunRow.owner_user_id == str(seed.owner_b.user_id),
                )
            )
        ).scalar_one()
        task = await ScheduledTaskRepository(session).get(
            seed.owner_b.resource_scope,
            scenario.task_id,
        )
        occurrence = await ScheduledTaskRunRepository(session).get(
            seed.owner_b.resource_scope,
            scenario.occurrence_id,
        )
    assert run.authorization_cancel_requested_at == NOW
    assert run.authorization_cancel_reason == AUTHORIZATION_REVOKED_REASON
    assert task is not None and task.status == "paused"
    assert task.frozen_at is None
    assert task.next_run_at is None
    assert occurrence is not None and occurrence.status == "running"

    async with seed.factory() as session, session.begin():
        assert await PrivateRunRepository(session).update_status(
            scope=seed.owner_b.resource_scope,
            run_id=scenario.run_id,
            status="success",
        )
    await AutomationReconciler(
        seed.factory,
        clock=lambda: NOW + timedelta(minutes=1),
    ).handle_run_completion(SimpleNamespace(run_id=scenario.run_id))

    async with seed.factory() as session, session.begin():
        settled_run = await PrivateRunRepository(session).get(
            scope=seed.owner_b.resource_scope,
            run_id=scenario.run_id,
        )
        settled_task = await ScheduledTaskRepository(session).get(
            seed.owner_b.resource_scope,
            scenario.task_id,
        )
        settled_occurrence = await ScheduledTaskRunRepository(session).get(
            seed.owner_b.resource_scope,
            scenario.occurrence_id,
        )
    assert settled_run is not None and settled_run.status == "interrupted"
    assert settled_run.error == AUTHORIZATION_REVOKED_REASON
    assert settled_occurrence is not None
    assert settled_occurrence.status == "interrupted"
    assert settled_occurrence.error_code == "AUTOMATION_RUN_INTERRUPTED"
    assert settled_task is not None
    assert settled_task.status == "paused"
    assert settled_task.frozen_at is None
    assert settled_task.run_count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_governance_transaction_rolls_back_run_and_automation_changes_together(
    retention_seed: M4ThreadSeed,
) -> None:
    seed = retention_seed
    task = await _create_task(seed, task_id="retention-atomic-rollback")
    queued = await _create_queued_occurrence(
        seed,
        task,
        occurrence_id="retention-atomic-rollback-queued",
    )

    with pytest.raises(RuntimeError, match="abort governance transaction"):
        async with seed.factory() as session, session.begin():
            await PrivateRunAuthorizationService.mark_revoked(
                session,
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                now=NOW,
            )
            await PrivateWorkRetentionService.freeze_owner(
                session,
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                now=NOW,
            )
            raise RuntimeError("abort governance transaction")

    async with seed.factory() as session, session.begin():
        persisted_task = await ScheduledTaskRepository(session).get(
            seed.owner_a.resource_scope,
            task.id,
        )
        occurrence = await ScheduledTaskRunRepository(session).get(
            seed.owner_a.resource_scope,
            queued.id,
        )
    assert persisted_task is not None
    assert persisted_task.status == "enabled"
    assert persisted_task.frozen_at is None
    assert persisted_task.next_run_at == NOW
    assert persisted_task.version == task.version
    assert occurrence is not None and occurrence.status == "queued"
