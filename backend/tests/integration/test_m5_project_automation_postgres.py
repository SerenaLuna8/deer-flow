from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
import yaml
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from support.m5_automation import (
    M5_NOW,
    M5App,
    M5Database,
    M5Seed,
    build_m5_app,
    isolated_m5_database,
    seed_m5_database,
)

from app.automations.dispatcher import AutomationDispatcher
from app.automations.errors import AutomationConcurrencyLimit, AutomationUnavailable
from app.automations.occurrences import (
    AutomationOccurrenceService,
    ManualReservation,
    deterministic_thread_id,
)
from app.automations.ownership import AutomationSchedulerOwnership
from app.automations.reconciliation import AutomationReconciler
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.private_work.thread_service import PrivateThreadService
from app.projects.lifecycle_repository import ProjectLifecycleRepository
from app.projects.lifecycle_service import ProjectLifecycleService
from app.projects.membership_repository import MembershipRepository
from app.projects.membership_service import MembershipService
from app.projects.models import ProjectRole
from deerflow.persistence.private_work.model import RunAssetVersionRow
from deerflow.persistence.scheduled_task_runs import (
    ScheduledTaskRunCreate,
    ScheduledTaskRunRepository,
)
from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
from deerflow.persistence.scheduled_tasks import ScheduledTaskRepository
from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
from deerflow.runtime import DisconnectMode, RunRecord, RunStatus

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest_asyncio.fixture()
async def m5_database(postgres_admin_url: str):
    async with isolated_m5_database(postgres_admin_url) as database:
        yield database


@pytest_asyncio.fixture()
async def m5_seed(m5_database: M5Database):
    seed = await seed_m5_database(m5_database)
    try:
        yield seed
    finally:
        await seed.m4.engine.dispose()


@pytest_asyncio.fixture()
async def m5_app(m5_seed: M5Seed):
    app = await build_m5_app(m5_seed)
    try:
        yield app
    finally:
        await app.aclose()


def test_release_workflow_delegates_exact_m1_to_m7_gate_after_hard_fail() -> None:
    workflow_path = Path(__file__).resolve().parents[3] / ".github/workflows/project-foundation-postgres-tests.yml"
    workflow = yaml.load(
        workflow_path.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    # BaseLoader intentionally keeps YAML 1.1 words such as `on` as strings.
    # SafeLoader would silently turn this workflow key into boolean True.
    assert "on" in workflow
    assert workflow["name"] == "M1-M7 PostgreSQL Gates"
    job = workflow["jobs"]["postgres-release-gates"]
    assert job["name"] == "M1-M7 PostgreSQL gates"
    steps = job["steps"]
    step_names = [step.get("name") for step in steps]
    assert step_names.count("Require PostgreSQL test administrator URL") == 1
    gate_name = "Run the fixed M1-M7 PostgreSQL release gate with zero skips"
    assert step_names.count(gate_name) == 1
    hard_fail_index = next(index for index, step in enumerate(steps) if step.get("name") == "Require PostgreSQL test administrator URL")
    gate_index = next(index for index, step in enumerate(steps) if step.get("name") == gate_name)
    assert hard_fail_index < gate_index
    assert 'test -n "${POSTGRES_TEST_URL:-}"' in steps[hard_fail_index]["run"]
    assert steps[gate_index]["run"] == "make test-project-foundation-postgres"


@pytest.mark.asyncio
async def test_project_api_is_project_owner_and_capability_scoped(
    m5_app: M5App,
) -> None:
    seed = m5_app.seed
    owner_project = seed.project_for("owner_a")
    other_project = seed.project_for("owner_a_project_b")
    target = seed.task_for("owner_a")
    other_project_target = seed.task_for("owner_a_project_b")
    owner_b_task = seed.task_for("owner_b")
    viewer_task = seed.task_for("viewer")
    assert seed.actor("owner_a").user_id == seed.actor("owner_a_project_b").user_id
    assert owner_project != other_project

    detail_matrix = (
        (owner_project, target.id, "owner_a", 200),
        (other_project, other_project_target.id, "owner_a_project_b", 200),
        (owner_project, other_project_target.id, "owner_a", 404),
        (other_project, target.id, "owner_a_project_b", 404),
        (owner_project, owner_b_task.id, "owner_b", 200),
        (owner_project, owner_b_task.id, "owner_a", 404),
        (owner_project, target.id, "owner_b", 404),
        (owner_project, viewer_task.id, "viewer", 200),
        (owner_project, target.id, "viewer", 404),
        (owner_project, target.id, "system_admin", 404),
    )
    for project_id, task_id, actor, expected_status in detail_matrix:
        response = await m5_app.request(
            "GET",
            f"/api/projects/{project_id}/automations/{task_id}",
            actor=actor,
        )
        assert response.status_code == expected_status, (project_id, task_id, actor)

    first_page = await m5_app.request(
        "GET",
        f"/api/projects/{owner_project}/automations?limit=1&offset=0",
        actor="owner_a",
    )
    second_page = await m5_app.request(
        "GET",
        f"/api/projects/{owner_project}/automations?limit=1&offset=1",
        actor="owner_a",
    )
    assert first_page.status_code == second_page.status_code == 200
    owner_ids = {
        first_page.json()["items"][0]["id"],
        second_page.json()["items"][0]["id"],
    }
    assert owner_ids == {target.id, seed.task_for("owner_a_secondary").id}
    owner_b_list = await m5_app.request(
        "GET",
        f"/api/projects/{owner_project}/automations",
        actor="owner_b",
    )
    assert [item["id"] for item in owner_b_list.json()["items"]] == [owner_b_task.id]
    other_project_list = await m5_app.request(
        "GET",
        f"/api/projects/{other_project}/automations",
        actor="owner_a_project_b",
    )
    assert [item["id"] for item in other_project_list.json()["items"]] == [other_project_target.id]
    viewer_list = await m5_app.request(
        "GET",
        f"/api/projects/{owner_project}/automations",
        actor="viewer",
    )
    assert [item["id"] for item in viewer_list.json()["items"]] == [viewer_task.id]
    hidden_list = await m5_app.request(
        "GET",
        f"/api/projects/{owner_project}/automations",
        actor="system_admin",
    )
    assert hidden_list.status_code == 404

    history_matrix = (
        (owner_project, target.id, "owner_a", 200),
        (other_project, other_project_target.id, "owner_a_project_b", 200),
        (owner_project, other_project_target.id, "owner_a", 404),
        (owner_project, owner_b_task.id, "owner_a", 404),
        (owner_project, owner_b_task.id, "owner_b", 200),
        (owner_project, viewer_task.id, "viewer", 200),
        (owner_project, target.id, "viewer", 404),
        (owner_project, target.id, "system_admin", 404),
    )
    for project_id, task_id, actor, expected_status in history_matrix:
        response = await m5_app.request(
            "GET",
            f"/api/projects/{project_id}/automations/{task_id}/runs?limit=10&offset=0",
            actor=actor,
        )
        assert response.status_code == expected_status, (project_id, task_id, actor)
        if task_id == target.id and actor == "owner_a":
            assert [item["id"] for item in response.json()["items"]] == [seed.history_record().id]

    reverse_matrix = (
        (owner_project, seed.threads["owner_a"], "owner_a", [target.id]),
        (other_project, seed.threads["owner_a_project_b"], "owner_a_project_b", [other_project_target.id]),
        (owner_project, seed.threads["owner_a_project_b"], "owner_a", []),
        (owner_project, seed.threads["owner_b"], "owner_a", []),
        (owner_project, seed.threads["owner_b"], "owner_b", [owner_b_task.id]),
        (owner_project, seed.threads["viewer"], "viewer", [viewer_task.id]),
        (owner_project, seed.threads["owner_a"], "viewer", []),
    )
    for project_id, thread_id, actor, expected_ids in reverse_matrix:
        response = await m5_app.request(
            "GET",
            f"/api/projects/{project_id}/automations/threads/{thread_id}",
            actor=actor,
        )
        assert response.status_code == 200, (project_id, thread_id, actor)
        assert [item["id"] for item in response.json()["items"]] == expected_ids
    system_reverse = await m5_app.request(
        "GET",
        f"/api/projects/{owner_project}/automations/threads/{seed.threads['owner_a']}",
        actor="system_admin",
    )
    assert system_reverse.status_code == 404

    update_target = await seed.create_task(
        "owner_a",
        task_id="m5-api-owner-update",
        next_run_at=M5_NOW + timedelta(days=1),
    )
    update_matrix = (
        (owner_project, update_target.id, "owner_a", 200),
        (owner_project, other_project_target.id, "owner_a", 404),
        (owner_project, owner_b_task.id, "owner_a", 404),
        (owner_project, viewer_task.id, "viewer", 403),
        (owner_project, target.id, "system_admin", 404),
    )
    for project_id, task_id, actor, expected_status in update_matrix:
        response = await m5_app.request(
            "PATCH",
            f"/api/projects/{project_id}/automations/{task_id}",
            actor=actor,
            json={"expected_version": 1, "title": f"updated-by-{actor}"},
        )
        assert response.status_code == expected_status, (project_id, task_id, actor)
        if task_id == update_target.id:
            assert response.json()["title"] == "updated-by-owner_a"
            assert response.json()["version"] == 2

    delete_target = await seed.create_task(
        "owner_a",
        task_id="m5-api-owner-delete",
        next_run_at=M5_NOW + timedelta(days=1),
    )
    delete_matrix = (
        (owner_project, other_project_target.id, "owner_a", 404),
        (owner_project, owner_b_task.id, "owner_a", 404),
        (owner_project, viewer_task.id, "viewer", 403),
        (owner_project, target.id, "system_admin", 404),
        (owner_project, delete_target.id, "owner_a", 200),
    )
    for project_id, task_id, actor, expected_status in delete_matrix:
        response = await m5_app.request(
            "DELETE",
            f"/api/projects/{project_id}/automations/{task_id}",
            actor=actor,
            json={"expected_version": 1},
        )
        assert response.status_code == expected_status, (project_id, task_id, actor)
    async with seed.factory() as session, session.begin():
        updated = await ScheduledTaskRepository(session).get(
            seed.context("owner_a").resource_scope,
            update_target.id,
        )
        deleted = await session.get(ScheduledTaskRow, delete_target.id)
    assert updated is not None
    assert updated.title == "updated-by-owner_a"
    assert deleted is not None
    assert deleted.project_id == seed.context("owner_a").project_id
    assert deleted.owner_user_id == str(seed.context("owner_a").user_id)
    assert deleted.deleted_at == M5_NOW

    runner_update_target = await seed.create_task(
        "owner_b",
        task_id="m5-api-runner-update",
        next_run_at=M5_NOW + timedelta(days=1),
    )
    runner_delete_target = await seed.create_task(
        "owner_b",
        task_id="m5-api-runner-delete",
        next_run_at=M5_NOW + timedelta(days=1),
    )
    runner_trigger_target = await seed.create_task(
        "owner_b",
        task_id="m5-api-runner-trigger",
        next_run_at=M5_NOW + timedelta(days=1),
    )
    owner_cross_update_target = await seed.create_task(
        "owner_a",
        task_id="m5-api-owner-cross-update",
        next_run_at=M5_NOW + timedelta(days=1),
    )
    owner_cross_delete_target = await seed.create_task(
        "owner_a",
        task_id="m5-api-owner-cross-delete",
        next_run_at=M5_NOW + timedelta(days=1),
    )
    owner_cross_trigger_target = await seed.create_task(
        "owner_a",
        task_id="m5-api-owner-cross-trigger",
        next_run_at=M5_NOW + timedelta(days=1),
    )

    runner_update = await m5_app.request(
        "PATCH",
        f"/api/projects/{owner_project}/automations/{runner_update_target.id}",
        actor="owner_b",
        json={"expected_version": 1, "title": "runner-updated-own-task"},
    )
    assert runner_update.status_code == 200
    assert runner_update.json()["title"] == "runner-updated-own-task"
    assert runner_update.json()["version"] == 2

    runner_delete = await m5_app.request(
        "DELETE",
        f"/api/projects/{owner_project}/automations/{runner_delete_target.id}",
        actor="owner_b",
        json={"expected_version": 1},
    )
    assert runner_delete.status_code == 200

    dispatched_occurrences: list[str] = []
    atomic_dispatcher = AutomationDispatcher(
        seed.factory,
        max_concurrent_runs=3,
    )

    class RecordingDispatcher:
        async def admit_manual(self, *args, **kwargs):
            admitted = await atomic_dispatcher.admit_manual(*args, **kwargs)
            dispatched_occurrences.append(admitted.occurrence.id)
            return admitted

    m5_app.app.state.automation_dispatcher = RecordingDispatcher()
    runner_idempotency_key = uuid.uuid4()
    runner_trigger = await m5_app.request(
        "POST",
        f"/api/projects/{owner_project}/automations/{runner_trigger_target.id}/trigger",
        actor="owner_b",
        headers={"Idempotency-Key": str(runner_idempotency_key)},
    )
    assert runner_trigger.status_code == 200
    assert runner_trigger.json()["automation_id"] == runner_trigger_target.id
    assert runner_trigger.json()["trigger"] == "manual"
    assert runner_trigger.json()["status"] == "running"
    assert dispatched_occurrences == [runner_trigger.json()["id"]]

    cross_update = await m5_app.request(
        "PATCH",
        f"/api/projects/{owner_project}/automations/{owner_cross_update_target.id}",
        actor="owner_b",
        json={"expected_version": 1, "title": "runner-cross-update"},
    )
    cross_delete = await m5_app.request(
        "DELETE",
        f"/api/projects/{owner_project}/automations/{owner_cross_delete_target.id}",
        actor="owner_b",
        json={"expected_version": 1},
    )
    cross_trigger = await m5_app.request(
        "POST",
        f"/api/projects/{owner_project}/automations/{owner_cross_trigger_target.id}/trigger",
        actor="owner_b",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert cross_update.status_code == 404
    assert cross_delete.status_code == 404
    assert cross_trigger.status_code == 404
    assert dispatched_occurrences == [runner_trigger.json()["id"]]

    async with seed.factory() as session:
        repository = ScheduledTaskRepository(session)
        persisted_runner_update = await repository.get(
            seed.context("owner_b").resource_scope,
            runner_update_target.id,
        )
        persisted_runner_delete = await session.get(
            ScheduledTaskRow,
            runner_delete_target.id,
        )
        persisted_runner_occurrence = await session.get(
            ScheduledTaskRunRow,
            runner_trigger.json()["id"],
        )
        persisted_cross_targets = tuple(
            [
                await repository.get(
                    seed.context("owner_a").resource_scope,
                    cross_target.id,
                )
                for cross_target in (
                    owner_cross_update_target,
                    owner_cross_delete_target,
                    owner_cross_trigger_target,
                )
            ]
        )
        cross_occurrence_count = await session.scalar(
            select(func.count())
            .select_from(ScheduledTaskRunRow)
            .where(
                ScheduledTaskRunRow.task_id.in_(
                    (
                        owner_cross_update_target.id,
                        owner_cross_delete_target.id,
                        owner_cross_trigger_target.id,
                    )
                )
            )
        )
    assert persisted_runner_update is not None
    assert persisted_runner_update.project_id == seed.context("owner_b").project_id
    assert persisted_runner_update.owner_user_id == str(seed.context("owner_b").user_id)
    assert persisted_runner_update.title == "runner-updated-own-task"
    assert persisted_runner_update.version == 2
    assert persisted_runner_delete is not None
    assert persisted_runner_delete.project_id == seed.context("owner_b").project_id
    assert persisted_runner_delete.owner_user_id == str(seed.context("owner_b").user_id)
    assert persisted_runner_delete.deleted_at == M5_NOW
    assert persisted_runner_occurrence is not None
    assert persisted_runner_occurrence.project_id == seed.context("owner_b").project_id
    assert persisted_runner_occurrence.owner_user_id == str(seed.context("owner_b").user_id)
    assert persisted_runner_occurrence.task_id == runner_trigger_target.id
    assert persisted_runner_occurrence.trigger == "manual"
    assert persisted_runner_occurrence.status == "running"
    assert persisted_runner_occurrence.job_id is not None
    assert persisted_runner_occurrence.manual_idempotency_hash == hashlib.sha256(str(runner_idempotency_key).encode("ascii")).hexdigest()
    assert persisted_cross_targets == (
        owner_cross_update_target,
        owner_cross_delete_target,
        owner_cross_trigger_target,
    )
    assert cross_occurrence_count == 0

    viewer_trigger = await m5_app.request(
        "POST",
        f"/api/projects/{owner_project}/automations/{viewer_task.id}/trigger",
        actor="viewer",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert viewer_trigger.status_code == 403
    async with seed.factory() as session:
        viewer_occurrence_count = await session.scalar(
            select(func.count())
            .select_from(ScheduledTaskRunRow)
            .where(
                ScheduledTaskRunRow.task_id == viewer_task.id,
            )
        )
    assert viewer_occurrence_count == 0
    assert dispatched_occurrences == [runner_trigger.json()["id"]]


@pytest.mark.asyncio
async def test_scheduler_reservation_and_claim_are_single_winner_real_transactions(
    m5_seed: M5Seed,
) -> None:
    due_tasks = (
        await m5_seed.create_task(
            "owner_a",
            task_id="m5-concurrent-due-a",
            next_run_at=M5_NOW - timedelta(minutes=5),
        ),
        await m5_seed.create_task(
            "project_b_owner",
            task_id="m5-concurrent-due-b",
            next_run_at=M5_NOW - timedelta(minutes=5),
        ),
    )
    services = (
        AutomationOccurrenceService(m5_seed.factory, max_concurrent_runs=1),
        AutomationOccurrenceService(m5_seed.factory, max_concurrent_runs=1),
    )
    reserve_barrier = asyncio.Barrier(2)

    async def reserve(service: AutomationOccurrenceService):
        await asyncio.wait_for(reserve_barrier.wait(), timeout=5)
        return await service.reserve_due(now=M5_NOW, limit=10)

    reserved = await asyncio.wait_for(
        asyncio.gather(*(reserve(service) for service in services)),
        timeout=10,
    )
    assert sum(len(batch) for batch in reserved) == 1

    claim_barrier = asyncio.Barrier(2)

    async def claim(service: AutomationOccurrenceService, owner: str):
        await asyncio.wait_for(claim_barrier.wait(), timeout=5)
        return await service.claim_next(
            now=M5_NOW,
            lease_owner=owner,
            lease_seconds=60,
        )

    claims = await asyncio.wait_for(
        asyncio.gather(
            claim(services[0], "scheduler-a"),
            claim(services[1], "scheduler-b"),
        ),
        timeout=10,
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].task_id in {task.id for task in due_tasks}
    assert winners[0].thread_id is None
    assert winners[0].run_id is None
    async with m5_seed.factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ScheduledTaskRunRow)
            .where(
                ScheduledTaskRunRow.task_id.in_(tuple(task.id for task in due_tasks)),
            )
        )
    assert count == 1


@pytest.mark.asyncio
async def test_manual_idempotency_and_global_cap_are_serialized_across_sessions(
    m5_seed: M5Seed,
) -> None:
    first_task = await m5_seed.create_task(
        "owner_a",
        task_id="m5-manual-idempotency",
        next_run_at=M5_NOW + timedelta(days=1),
    )
    services = (
        AutomationOccurrenceService(m5_seed.factory, max_concurrent_runs=3),
        AutomationOccurrenceService(m5_seed.factory, max_concurrent_runs=3),
    )
    key = uuid.uuid4()
    replay_barrier = asyncio.Barrier(2)

    async def reserve_replay(service: AutomationOccurrenceService):
        await asyncio.wait_for(replay_barrier.wait(), timeout=5)
        return await service.reserve_manual(
            m5_seed.context("owner_a"),
            first_task.id,
            key,
            now=M5_NOW,
        )

    first, second = await asyncio.wait_for(
        asyncio.gather(*(reserve_replay(service) for service in services)),
        timeout=10,
    )
    assert isinstance(first, ManualReservation)
    assert isinstance(second, ManualReservation)
    assert first.occurrence.id == second.occurrence.id
    assert sorted((first.created, second.created)) == [False, True]

    async with m5_seed.factory() as session, session.begin():
        await session.execute(ScheduledTaskRunRow.__table__.update().where(ScheduledTaskRunRow.id == first.occurrence.id).values(status="cancelled", finished_at=M5_NOW, updated_at=M5_NOW))

    cap_tasks = (
        await m5_seed.create_task(
            "owner_a",
            task_id="m5-cap-a",
            next_run_at=M5_NOW + timedelta(days=1),
        ),
        await m5_seed.create_task(
            "owner_b",
            task_id="m5-cap-b",
            next_run_at=M5_NOW + timedelta(days=1),
        ),
    )
    cap_services = (
        AutomationOccurrenceService(m5_seed.factory, max_concurrent_runs=1),
        AutomationOccurrenceService(m5_seed.factory, max_concurrent_runs=1),
    )
    cap_barrier = asyncio.Barrier(2)

    async def reserve_cap(index: int):
        await asyncio.wait_for(cap_barrier.wait(), timeout=5)
        return await cap_services[index].reserve_manual(
            m5_seed.context("owner_a" if index == 0 else "owner_b"),
            cap_tasks[index].id,
            uuid.uuid4(),
            now=M5_NOW,
        )

    outcomes = await asyncio.wait_for(
        asyncio.gather(reserve_cap(0), reserve_cap(1), return_exceptions=True),
        timeout=10,
    )
    assert sum(isinstance(outcome, ManualReservation) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, AutomationConcurrencyLimit) for outcome in outcomes) == 1
    async with m5_seed.factory() as session:
        active_count = await session.scalar(
            select(func.count())
            .select_from(ScheduledTaskRunRow)
            .where(
                ScheduledTaskRunRow.status.in_(("queued", "launching", "running")),
            )
        )
    assert active_count == 1


@pytest.mark.asyncio
async def test_composite_foreign_keys_reject_cross_scope_task_thread_and_run(
    m5_seed: M5Seed,
) -> None:
    owner_task = m5_seed.task_for("owner_a")
    same_owner_other_project_task = m5_seed.task_for("owner_a_project_b")
    owner_context = m5_seed.context("owner_a")
    same_owner_other_project_context = m5_seed.context("owner_a_project_b")
    assert owner_context.user_id == same_owner_other_project_context.user_id
    assert same_owner_other_project_context.user_id == m5_seed.m4.project_b_owner_a.user_id
    assert owner_context.project_id != same_owner_other_project_context.project_id
    assert same_owner_other_project_context.project_id == m5_seed.m4.project_b_owner_a.project_id
    assert owner_task.owner_user_id == same_owner_other_project_task.owner_user_id
    assert owner_task.project_id != same_owner_other_project_task.project_id
    async with m5_seed.factory() as session:
        repository = ScheduledTaskRepository(session)
        assert (
            await repository.get(
                owner_context.resource_scope,
                same_owner_other_project_task.id,
            )
            is None
        )
        visible_other_project_task = await repository.get(
            same_owner_other_project_context.resource_scope,
            same_owner_other_project_task.id,
        )
    assert visible_other_project_task == same_owner_other_project_task

    cross_task_request = ScheduledTaskRunCreate(
        occurrence_id="m5-cross-task",
        task_id=owner_task.id,
        task_version=owner_task.version,
        occurrence_key=hashlib.sha256(b"m5-cross-task").hexdigest(),
        manual_idempotency_hash=None,
        scheduled_for=M5_NOW,
        trigger="scheduled",
        status="queued",
        created_at=M5_NOW,
    )
    for actor_name in ("owner_b", "owner_a_project_b", "project_b_owner"):
        with pytest.raises(IntegrityError):
            async with m5_seed.factory() as session, session.begin():
                await ScheduledTaskRunRepository(session).create(
                    m5_seed.context(actor_name).resource_scope,
                    cross_task_request,
                )

    occurrence = await m5_seed.create_occurrence(
        "owner_a",
        owner_task,
        occurrence_id="m5-constraint-occurrence",
    )
    for actor_name in ("owner_b", "owner_a_project_b"):
        with pytest.raises(IntegrityError):
            async with m5_seed.factory() as session, session.begin():
                await session.execute(ScheduledTaskRunRow.__table__.update().where(ScheduledTaskRunRow.id == occurrence.id).values(thread_id=m5_seed.threads[actor_name]))

    second_thread = str(uuid.uuid4())
    second_run = str(uuid.uuid4())
    async with m5_seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=m5_seed.context("owner_a").resource_scope,
            thread_id=second_thread,
            agent=ThreadAgentRef(m5_seed.m4.project_agent_id, "project"),
        )
        await PrivateRunRepository(session).create(
            scope=m5_seed.context("owner_a").resource_scope,
            thread_id=second_thread,
            request=PrivateRunCreate(run_id=second_run),
        )
        await session.execute(ScheduledTaskRunRow.__table__.update().where(ScheduledTaskRunRow.id == occurrence.id).values(thread_id=m5_seed.threads["owner_a"]))
    with pytest.raises(IntegrityError):
        async with m5_seed.factory() as session, session.begin():
            await session.execute(ScheduledTaskRunRow.__table__.update().where(ScheduledTaskRunRow.id == occurrence.id).values(run_id=second_run))


@pytest.mark.asyncio
async def test_governance_downgrade_remove_leave_and_pending_delete_freeze_automation(
    m5_seed: M5Seed,
) -> None:
    targets = {
        name: m5_seed.task_for(name)
        for name in (
            "owner_a",
            "owner_b",
            "viewer",
            "project_b_owner",
            "owner_a_project_b",
        )
    }
    occurrences = {
        name: await m5_seed.create_occurrence(
            name,
            task,
            occurrence_id=f"m5-governance-{name}",
        )
        for name, task in targets.items()
    }

    async def read_state():
        async with m5_seed.factory() as session:
            task_rows = {row.id: row for row in (await session.execute(select(ScheduledTaskRow).where(ScheduledTaskRow.id.in_(tuple(task.id for task in targets.values()))))).scalars()}
            occurrence_rows = {row.id: row for row in (await session.execute(select(ScheduledTaskRunRow).where(ScheduledTaskRunRow.id.in_(tuple(item.id for item in occurrences.values()))))).scalars()}
        return task_rows, occurrence_rows

    def assert_revoked(name: str, task_rows, occurrence_rows) -> None:
        task = task_rows[targets[name].id]
        occurrence = occurrence_rows[occurrences[name].id]
        assert task.status == "paused", name
        assert task.frozen_at is not None, name
        assert task.next_run_at is None, name
        assert occurrence.status == "cancelled", name
        assert occurrence.error_code == "AUTOMATION_AUTHORIZATION_REVOKED", name

    def assert_not_revoked(name: str, task_rows, occurrence_rows) -> None:
        task = task_rows[targets[name].id]
        occurrence = occurrence_rows[occurrences[name].id]
        assert task.status == "enabled", name
        assert task.frozen_at is None, name
        assert task.next_run_at is not None, name
        assert occurrence.status == "queued", name
        assert occurrence.error_code is None, name

    async with m5_seed.factory() as session:
        await MembershipService(
            MembershipRepository(session),
            clock=lambda: M5_NOW,
        ).change_role(
            m5_seed.project_context("owner_a"),
            m5_seed.context("owner_b").membership_id,
            ProjectRole.VIEWER,
            expected_version=1,
        )
    task_rows, occurrence_rows = await read_state()
    assert_revoked("owner_b", task_rows, occurrence_rows)
    for untouched in ("owner_a", "viewer", "project_b_owner", "owner_a_project_b"):
        assert_not_revoked(untouched, task_rows, occurrence_rows)

    async with m5_seed.factory() as session:
        await MembershipService(
            MembershipRepository(session),
            clock=lambda: M5_NOW + timedelta(minutes=1),
        ).remove(
            m5_seed.project_context("owner_a"),
            m5_seed.context("viewer").membership_id,
            expected_version=1,
        )
    task_rows, occurrence_rows = await read_state()
    for revoked in ("owner_b", "viewer"):
        assert_revoked(revoked, task_rows, occurrence_rows)
    for untouched in ("owner_a", "project_b_owner", "owner_a_project_b"):
        assert_not_revoked(untouched, task_rows, occurrence_rows)

    async with m5_seed.factory() as session:
        await MembershipService(
            MembershipRepository(session),
            clock=lambda: M5_NOW + timedelta(minutes=2),
        ).leave(
            m5_seed.project_context("project_b_owner"),
            expected_version=1,
        )
    task_rows, occurrence_rows = await read_state()
    for revoked in ("owner_b", "viewer", "project_b_owner"):
        assert_revoked(revoked, task_rows, occurrence_rows)
    for untouched in ("owner_a", "owner_a_project_b"):
        assert_not_revoked(untouched, task_rows, occurrence_rows)

    async with m5_seed.factory() as session:
        await ProjectLifecycleService(
            ProjectLifecycleRepository(session),
        ).request_deletion(
            m5_seed.project_context("owner_a"),
            M5_NOW + timedelta(minutes=3),
        )
    task_rows, occurrence_rows = await read_state()
    for revoked in ("owner_a", "owner_b", "viewer", "project_b_owner"):
        assert_revoked(revoked, task_rows, occurrence_rows)
    assert_not_revoked("owner_a_project_b", task_rows, occurrence_rows)


def _runtime_record(admitted) -> RunRecord:
    return RunRecord(
        run_id=admitted.run.run_id,
        thread_id=admitted.run.thread_id,
        assistant_id=admitted.run.assistant_id,
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.continue_,
        metadata=admitted.run.metadata,
        kwargs=admitted.run.kwargs,
        model_name=admitted.run.model_name,
        created_at=admitted.run.created_at.isoformat(),
        updated_at=admitted.run.updated_at.isoformat(),
        scope=admitted.opaque_runtime_scope,
    )


async def _claim_occurrence(
    seed: M5Seed,
    actor_name: str,
    task,
    *,
    occurrence_id: str,
):
    queued = await seed.create_occurrence(
        actor_name,
        task,
        occurrence_id=occurrence_id,
    )
    async with seed.factory() as session, session.begin():
        claimed = await ScheduledTaskRunRepository(session).claim(
            seed.context(actor_name).resource_scope,
            queued.id,
            now=M5_NOW,
            lease_owner="m5-dispatch",
            lease_expires_at=M5_NOW + timedelta(minutes=1),
        )
    assert claimed is not None
    return claimed


@pytest.mark.asyncio
async def test_dispatch_snapshots_completion_and_restart_never_replay_admitted_run(
    m5_seed: M5Seed,
) -> None:
    checkpointer = ProjectScopedCheckpointer(InMemorySaver(), m5_seed.factory)
    thread_service = PrivateThreadService(m5_seed.factory, checkpointer)
    fresh_task = await m5_seed.create_task(
        "owner_a",
        task_id="m5-dispatch-fresh",
        next_run_at=M5_NOW + timedelta(days=1),
    )
    reuse_task = m5_seed.task_for("owner_a")
    fresh_occurrence = await _claim_occurrence(
        m5_seed,
        "owner_a",
        fresh_task,
        occurrence_id="m5-dispatch-fresh-occurrence",
    )
    reuse_occurrence = await _claim_occurrence(
        m5_seed,
        "owner_a",
        reuse_task,
        occurrence_id="m5-dispatch-reuse-occurrence",
    )
    launch_records: list[RunRecord] = []

    async def launch_private_run(**kwargs):
        admitted = await PrivateRunAdmissionService(m5_seed.factory).admit(
            kwargs["context"],
            kwargs["thread_id"],
            PrivateRunCreate(
                run_id=kwargs["run_id"],
                metadata=dict(kwargs["metadata"]),
                kwargs={
                    "input": {"messages": [{"role": "user", "content": kwargs["prompt"]}]},
                    "config": {"context": {"non_interactive": True}},
                },
            ),
        )
        record = _runtime_record(admitted)
        launch_records.append(record)
        return record

    dispatcher = AutomationDispatcher(
        m5_seed.factory,
        thread_service=thread_service,
        launch_private_run=launch_private_run,
        clock=lambda: M5_NOW,
    )
    app = SimpleNamespace(state=SimpleNamespace())
    fresh_result = await dispatcher.dispatch(fresh_occurrence.id, app=app)
    reuse_result = await dispatcher.dispatch(reuse_occurrence.id, app=app)
    assert fresh_result.thread_id == deterministic_thread_id(fresh_occurrence.id)
    assert reuse_result.thread_id == reuse_task.thread_id
    assert len(launch_records) == 2

    async with m5_seed.factory() as session:
        snapshots = (
            await session.execute(
                select(
                    RunAssetVersionRow.run_id,
                    RunAssetVersionRow.asset_id,
                    RunAssetVersionRow.version_id,
                    RunAssetVersionRow.dependency_order,
                ).where(
                    RunAssetVersionRow.run_id.in_((fresh_result.run_id, reuse_result.run_id)),
                    RunAssetVersionRow.dependency_order == 0,
                )
            )
        ).all()
        expected_agent_version = await session.scalar(
            text("SELECT current_published_version_id FROM agents WHERE id=:agent_id"),
            {"agent_id": m5_seed.m4.project_agent_id},
        )
    assert len(snapshots) == 2
    assert {row.asset_id for row in snapshots} == {m5_seed.m4.project_agent_id}
    assert {row.version_id for row in snapshots} == {expected_agent_version}

    async with m5_seed.factory() as session, session.begin():
        assert await PrivateRunRepository(session).update_status(
            scope=m5_seed.context("owner_a").resource_scope,
            run_id=fresh_result.run_id,
            status="success",
        )
    reconciler = AutomationReconciler(m5_seed.factory, clock=lambda: M5_NOW + timedelta(minutes=2))
    await reconciler.handle_run_completion(launch_records[0])
    await reconciler.handle_run_completion(launch_records[0])
    report = await reconciler.reconcile_restart(M5_NOW + timedelta(minutes=3))

    async with m5_seed.factory() as session:
        fresh_row = await ScheduledTaskRunRepository(session).get(
            m5_seed.context("owner_a").resource_scope,
            fresh_occurrence.id,
        )
        reuse_row = await ScheduledTaskRunRepository(session).get(
            m5_seed.context("owner_a").resource_scope,
            reuse_occurrence.id,
        )
        parent = await ScheduledTaskRepository(session).get(
            m5_seed.context("owner_a").resource_scope,
            fresh_task.id,
        )
        restart_run = await PrivateRunRepository(session).get(
            scope=m5_seed.context("owner_a").resource_scope,
            run_id=reuse_result.run_id,
        )
    assert fresh_row is not None and fresh_row.status == "success"
    assert parent is not None and parent.run_count == 1
    assert reuse_row is not None and reuse_row.status == "running"
    assert restart_run is not None and restart_run.status == "pending"
    assert report.interrupted == 0
    assert report.unchanged == 1
    assert len(launch_records) == 2


@pytest.mark.asyncio
async def test_m6_head_keeps_m4_ready_and_scheduler_ownership_is_exclusive(
    m5_database: M5Database,
) -> None:
    async with m5_database.factory() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == "0001_project_saas_baseline"
    assert m5_database.database_name.startswith("deerflow_test_")

    first_engine = create_async_engine(m5_database.url)
    second_engine = create_async_engine(m5_database.url)
    first = AutomationSchedulerOwnership(first_engine)
    second = AutomationSchedulerOwnership(second_engine)
    try:
        await first.acquire()
        await first.verify()
        with pytest.raises(AutomationUnavailable):
            await second.acquire()
        await first.release()
        await second.acquire()
        await second.verify()
    finally:
        await first.release()
        await second.release()
        await first_engine.dispose()
        await second_engine.dispose()
