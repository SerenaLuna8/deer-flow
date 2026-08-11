from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker
from support.private_thread_seed import seed_private_thread_database

import deerflow.persistence.workflows.sql as workflow_persistence_sql
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.workflows.authorization import (
    WorkflowAction,
    WorkflowAuthorizationService,
)
from app.workflows.errors import (
    WorkflowForbidden,
    WorkflowNotFound,
    WorkflowRunRetryForbidden,
)
from app.workflows.private_run_service import WorkflowPrivateRunService
from app.workflows.repository import (
    WorkflowCredentialSlotCreate,
    WorkflowDefinitionCreate,
    WorkflowDraftCASConflict,
    WorkflowDraftUpdate,
    WorkflowExecutionFence,
    WorkflowManualRetryForbidden,
    WorkflowRepository,
    WorkflowRunAdmissionRequest,
    WorkflowRunCreate,
    WorkflowRunEventAppend,
    WorkflowRunIdempotencyConflict,
    WorkflowRunScope,
    WorkflowRunStateConflict,
    WorkflowVersionPublish,
)
from deerflow.persistence.jobs.sql import EnqueueJob, JobRepository, JobScope

pytestmark = pytest.mark.postgres


class _AllowWorkflowRunPolicy:
    def allows(self, _context, action: WorkflowAction) -> bool:
        return action in {
            WorkflowAction.RUN_READ_OWN,
            WorkflowAction.RUN_CANCEL_OWN,
            WorkflowAction.RETRY,
        }


async def _agent_table_counts(session) -> tuple[int, int, int]:
    counts: list[int] = []
    for table in ("runs", "threads_meta", "run_events"):
        counts.append(int(await session.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0))
    return counts[0], counts[1], counts[2]


async def _wait_for_backend_lock(session, backend_pid: int) -> None:
    for _ in range(200):
        wait_event_type = await session.scalar(
            sa.text(
                """SELECT wait_event_type FROM pg_stat_activity
                    WHERE pid=:backend_pid"""
            ),
            {"backend_pid": backend_pid},
        )
        if wait_event_type == "Lock":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Workflow mutation did not reach the expected row-lock wait")


async def _expect_named_constraint(
    session,
    *,
    constraint_name: str,
    statement: str,
    params: dict[str, object],
) -> None:
    with pytest.raises(DBAPIError) as exc_info:
        async with session.begin_nested():
            await session.execute(sa.text(statement), params)
            await session.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert constraint_name in str(exc_info.value)


async def _create_definition(
    factory: async_sessionmaker,
    *,
    project_id: uuid.UUID,
    actor_id: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with factory() as session, session.begin():
        repository = WorkflowRepository(session)
        definition, draft = await repository.create_definition(
            project_id=project_id,
            actor_user_id=actor_id,
            command=WorkflowDefinitionCreate(
                name=f"G13 {uuid.uuid4().hex[:8]}",
                description="repository contract",
                spec_schema_version=1,
                canvas_schema_version=1,
                spec={"nodes": []},
                canvas={"viewport": {}},
                draft_checksum="1" * 64,
            ),
        )
        assert draft.revision == 1
        return definition.workflow_id, draft.workflow_id


def _admission_request(
    *,
    requested_workflow_version_id: uuid.UUID | None,
    inputs: dict[str, object],
    retry_of_run_id: uuid.UUID | None = None,
) -> WorkflowRunAdmissionRequest:
    return WorkflowRunAdmissionRequest(
        requested_workflow_version_id=requested_workflow_version_id,
        inputs=inputs,
        trigger_kind="manual",
        trigger_ref=None,
        retry_of_run_id=retry_of_run_id,
    )


@pytest.mark.asyncio
async def test_workflow_draft_cas_and_publish_are_serialized_without_repository_commit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    project_id = seed.owner_a.project_id
    actor_id = str(seed.owner_a.user_id)
    try:
        workflow_id, _ = await _create_definition(
            seed.factory,
            project_id=project_id,
            actor_id=actor_id,
        )

        async def save(checksum: str):
            async with seed.factory() as session, session.begin():
                return await WorkflowRepository(session).save_draft(
                    project_id=project_id,
                    actor_user_id=actor_id,
                    workflow_id=workflow_id,
                    command=WorkflowDraftUpdate(
                        expected_revision=1,
                        spec_schema_version=1,
                        canvas_schema_version=1,
                        spec={"winner": checksum[0]},
                        canvas={"viewport": {}},
                        draft_checksum=checksum,
                    ),
                )

        save_results = await asyncio.gather(
            save("2" * 64),
            save("3" * 64),
            return_exceptions=True,
        )
        winners = [result for result in save_results if not isinstance(result, BaseException)]
        conflicts = [result for result in save_results if isinstance(result, WorkflowDraftCASConflict)]
        assert len(winners) == 1
        assert len(conflicts) == 1
        winner = winners[0]
        assert winner.revision == 2

        payload_schema_source = {
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
        }
        credential_slot = WorkflowCredentialSlotCreate(
            slot_id="api_token",
            name="API token",
            purpose="http",
            payload_schema=payload_schema_source,
            payload_schema_checksum="7" * 64,
        )
        payload_schema_source["properties"]["token"]["type"] = "number"  # type: ignore[index]

        async def publish():
            async with seed.factory() as session, session.begin():
                return await WorkflowRepository(session).publish_version(
                    project_id=project_id,
                    actor_user_id=actor_id,
                    workflow_id=workflow_id,
                    command=WorkflowVersionPublish(
                        expected_draft_revision=2,
                        expected_draft_checksum=winner.draft_checksum,
                        graph_schema_version=1,
                        canvas_schema_version=1,
                        compiler_contract_version=1,
                        semantic_checksum=winner.draft_checksum,
                        credential_slots=(credential_slot,),
                    ),
                )

        published = await asyncio.gather(publish(), publish())
        assert published[0].version_id == published[1].version_id
        assert published[0].version_number == 1

        async with seed.factory() as session, session.begin():
            repository = WorkflowRepository(session)
            persisted_schema = await session.scalar(
                sa.text(
                    """SELECT payload_schema_json
                         FROM workflow_version_credential_slots
                        WHERE workflow_version_id=:version AND slot_id='api_token'"""
                ),
                {"version": published[0].version_id},
            )
            assert persisted_schema == {
                "type": "object",
                "properties": {"token": {"type": "string"}},
                "required": ["token"],
            }
            for graph_schema_version, canvas_schema_version in ((2, 1), (1, 2)):
                with pytest.raises(WorkflowDraftCASConflict):
                    await repository.publish_version(
                        project_id=project_id,
                        actor_user_id=actor_id,
                        workflow_id=workflow_id,
                        command=WorkflowVersionPublish(
                            expected_draft_revision=winner.revision,
                            expected_draft_checksum=winner.draft_checksum,
                            graph_schema_version=graph_schema_version,
                            canvas_schema_version=canvas_schema_version,
                            compiler_contract_version=1,
                            semantic_checksum=winner.draft_checksum,
                            credential_slots=(credential_slot,),
                        ),
                    )
            assert await repository.get_definition(uuid.uuid4(), workflow_id) is None
            draft_b = await repository.save_draft(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                command=WorkflowDraftUpdate(
                    expected_revision=2,
                    spec_schema_version=1,
                    canvas_schema_version=1,
                    spec={"winner": "b"},
                    canvas={"viewport": {}},
                    draft_checksum="4" * 64,
                ),
            )
            version_b = await repository.publish_version(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                command=WorkflowVersionPublish(
                    expected_draft_revision=draft_b.revision,
                    expected_draft_checksum=draft_b.draft_checksum,
                    graph_schema_version=1,
                    canvas_schema_version=1,
                    compiler_contract_version=1,
                    semantic_checksum=draft_b.draft_checksum,
                ),
            )
            draft_a_again = await repository.save_draft(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                command=WorkflowDraftUpdate(
                    expected_revision=3,
                    spec_schema_version=1,
                    canvas_schema_version=1,
                    spec={"winner": winner.draft_checksum[0]},
                    canvas={"viewport": {}},
                    draft_checksum=winner.draft_checksum,
                ),
            )
            with pytest.raises(WorkflowDraftCASConflict):
                await repository.publish_version(
                    project_id=project_id,
                    actor_user_id=actor_id,
                    workflow_id=workflow_id,
                    command=WorkflowVersionPublish(
                        expected_draft_revision=draft_a_again.revision,
                        expected_draft_checksum=draft_a_again.draft_checksum,
                        graph_schema_version=1,
                        canvas_schema_version=1,
                        compiler_contract_version=1,
                        semantic_checksum=winner.draft_checksum,
                    ),
                )
            definition = await repository.get_definition(project_id, workflow_id)
            assert definition is not None
            assert definition.current_published_version_id == version_b.version_id
            versions = await repository.list_versions(project_id, workflow_id)
            assert [version.version_id for version in versions] == [
                published[0].version_id,
                version_b.version_id,
            ]
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_authored_json_commands_are_frozen_across_repository_lock_wait(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    project_id = seed.owner_a.project_id
    actor_id = str(seed.owner_a.user_id)
    try:
        initial_spec_source = {"nodes": [{"id": "initial", "config": {"value": 1}}]}
        initial_canvas_source = {"viewport": {"selected": ["initial"], "x": 1}}
        create_command = WorkflowDefinitionCreate(
            name=f"G13 Frozen {uuid.uuid4().hex[:8]}",
            description="frozen command material",
            spec_schema_version=1,
            canvas_schema_version=1,
            spec=initial_spec_source,
            canvas=initial_canvas_source,
            draft_checksum="8" * 64,
        )
        initial_spec_source["nodes"][0]["config"]["value"] = 99  # type: ignore[index]
        initial_canvas_source["viewport"]["selected"].append("mutated")  # type: ignore[index]
        async with seed.factory() as session, session.begin():
            _definition, created_draft = await WorkflowRepository(session).create_definition(
                project_id=project_id,
                actor_user_id=actor_id,
                command=create_command,
            )
        assert created_draft.spec == {"nodes": [{"id": "initial", "config": {"value": 1}}]}
        assert created_draft.canvas == {"viewport": {"selected": ["initial"], "x": 1}}

        update_spec_source = {"nodes": [{"id": "updated", "config": {"value": "before"}}]}
        update_canvas_source = {"viewport": {"selected": ["updated"], "x": 2}}
        update_command = WorkflowDraftUpdate(
            expected_revision=1,
            spec_schema_version=1,
            canvas_schema_version=1,
            spec=update_spec_source,
            canvas=update_canvas_source,
            draft_checksum="9" * 64,
        )

        async with seed.factory() as locker, seed.factory() as waiter:
            locker_transaction = await locker.begin()
            waiter_transaction = await waiter.begin()
            save_task: asyncio.Task | None = None
            try:
                await locker.execute(
                    sa.text(
                        """SELECT workflow_id FROM workflow_drafts
                            WHERE workflow_id=:workflow AND project_id=:project
                            FOR UPDATE"""
                    ),
                    {
                        "workflow": created_draft.workflow_id,
                        "project": project_id,
                    },
                )
                waiter_pid = int(await waiter.scalar(sa.text("SELECT pg_backend_pid()")) or 0)
                save_task = asyncio.create_task(
                    WorkflowRepository(waiter).save_draft(
                        project_id=project_id,
                        actor_user_id=actor_id,
                        workflow_id=created_draft.workflow_id,
                        command=update_command,
                    )
                )
                await _wait_for_backend_lock(locker, waiter_pid)
                update_spec_source["nodes"][0]["config"]["value"] = "after"  # type: ignore[index]
                update_canvas_source["viewport"]["selected"].append("mutated")  # type: ignore[index]
                await locker_transaction.commit()
                saved = await save_task
                await waiter_transaction.commit()
            except BaseException:
                if save_task is not None and not save_task.done():
                    save_task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await save_task
                if locker_transaction.is_active:
                    await locker_transaction.rollback()
                if waiter_transaction.is_active:
                    await waiter_transaction.rollback()
                raise

        assert saved.spec == {"nodes": [{"id": "updated", "config": {"value": "before"}}]}
        assert saved.canvas == {"viewport": {"selected": ["updated"], "x": 2}}
        async with seed.factory() as session, session.begin():
            persisted = (
                (
                    await session.execute(
                        sa.text(
                            """SELECT spec_json,canvas_json FROM workflow_drafts
                            WHERE workflow_id=:workflow"""
                        ),
                        {"workflow": created_draft.workflow_id},
                    )
                )
                .mappings()
                .one()
            )
            assert persisted["spec_json"] == saved.spec
            assert persisted["canvas_json"] == saved.canvas
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_owner_private_run_cancel_retry_retention_and_agent_negative_boundary(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    project_id = seed.owner_a.project_id
    owner_id = str(seed.owner_a.user_id)
    scope = WorkflowRunScope(project_id=project_id, owner_user_id=owner_id)
    other_project_id = uuid.uuid4()
    other_membership_id = uuid.uuid4()
    try:
        workflow_id, _ = await _create_definition(
            seed.factory,
            project_id=project_id,
            actor_id=owner_id,
        )
        async with seed.factory() as session, session.begin():
            repository = WorkflowRepository(session)
            draft = await repository.get_draft(project_id, workflow_id)
            assert draft is not None
            version = await repository.publish_version(
                project_id=project_id,
                actor_user_id=owner_id,
                workflow_id=workflow_id,
                command=WorkflowVersionPublish(
                    expected_draft_revision=1,
                    expected_draft_checksum=draft.draft_checksum,
                    graph_schema_version=1,
                    canvas_schema_version=1,
                    compiler_contract_version=1,
                    semantic_checksum=draft.draft_checksum,
                ),
            )

            explicit_admission = _admission_request(
                requested_workflow_version_id=version.version_id,
                inputs={"question": "mismatched-explicit-version"},
            )
            mismatched_explicit = WorkflowRunCreate(
                workflow_id=workflow_id,
                workflow_version_id=version.version_id,
                requested_workflow_version_id=version.version_id,
                inputs={"question": "mismatched-explicit-version"},
                input_digest=explicit_admission.input_digest,
                idempotency_hash="4" * 64,
                admission_request_digest=explicit_admission.digest,
                trigger_kind="manual",
                trigger_ref=None,
                origin_trace_id=f"workflow-g13-{uuid.uuid4()}",
                required_worker_profile_digest=None,
                retry_of_run_id=None,
            )
            # Constructor validation is the public boundary; this forged
            # object proves the repository also fails closed on first stage.
            object.__setattr__(
                mismatched_explicit,
                "workflow_version_id",
                uuid.uuid4(),
            )
            with pytest.raises(
                ValueError,
                match="requested_workflow_version_id",
            ):
                await repository.stage_run(scope, mismatched_explicit)
            assert (
                int(
                    await session.scalar(
                        sa.text(
                            """SELECT count(*) FROM workflow_runs
                                WHERE project_id=:project AND owner_user_id=:owner
                                  AND workflow_id=:workflow"""
                        ),
                        {
                            "project": project_id,
                            "owner": owner_id,
                            "workflow": workflow_id,
                        },
                    )
                    or 0
                )
                == 0
            )

            before_agent_counts = await _agent_table_counts(session)
            staged = await repository.stage_run(
                scope,
                WorkflowRunCreate(
                    workflow_id=workflow_id,
                    workflow_version_id=version.version_id,
                    requested_workflow_version_id=None,
                    inputs={"question": "standalone"},
                    input_digest=_admission_request(
                        requested_workflow_version_id=None,
                        inputs={"question": "standalone"},
                    ).input_digest,
                    idempotency_hash="5" * 64,
                    admission_request_digest=_admission_request(
                        requested_workflow_version_id=None,
                        inputs={"question": "standalone"},
                    ).digest,
                    trigger_kind="manual",
                    trigger_ref=None,
                    origin_trace_id=f"workflow-g13-{uuid.uuid4()}",
                    required_worker_profile_digest=None,
                    retry_of_run_id=None,
                ),
            )
            assert staged.created is True
            run = staged.record
            replay = await repository.stage_run(
                scope,
                WorkflowRunCreate(
                    workflow_id=workflow_id,
                    # A replay of a client request that selected "current"
                    # must return before resolving mutable version/profile
                    # authority, so these server-derived values are ignored.
                    workflow_version_id=uuid.uuid4(),
                    requested_workflow_version_id=None,
                    inputs={"question": "standalone"},
                    input_digest=_admission_request(
                        requested_workflow_version_id=None,
                        inputs={"question": "standalone"},
                    ).input_digest,
                    idempotency_hash="5" * 64,
                    admission_request_digest=_admission_request(
                        requested_workflow_version_id=None,
                        inputs={"question": "standalone"},
                    ).digest,
                    trigger_kind="manual",
                    trigger_ref=None,
                    origin_trace_id=f"workflow-g13-{uuid.uuid4()}",
                    required_worker_profile_digest="f" * 64,
                    retry_of_run_id=None,
                ),
            )
            assert replay.created is False
            assert replay.record.run_id == run.run_id
            with pytest.raises(WorkflowRunIdempotencyConflict):
                await repository.stage_run(
                    scope,
                    WorkflowRunCreate(
                        workflow_id=workflow_id,
                        workflow_version_id=version.version_id,
                        requested_workflow_version_id=version.version_id,
                        inputs={"question": "standalone"},
                        input_digest=_admission_request(
                            requested_workflow_version_id=version.version_id,
                            inputs={"question": "standalone"},
                        ).input_digest,
                        idempotency_hash="5" * 64,
                        admission_request_digest=_admission_request(
                            requested_workflow_version_id=version.version_id,
                            inputs={"question": "standalone"},
                        ).digest,
                        trigger_kind="manual",
                        trigger_ref=None,
                        origin_trace_id=f"workflow-g13-{uuid.uuid4()}",
                        required_worker_profile_digest=None,
                        retry_of_run_id=None,
                    ),
                )
            job_id = await JobRepository(session).enqueue(
                EnqueueJob(
                    job_type="workflow_run",
                    scope=JobScope(project_id, owner_id),
                    idempotency_key="6" * 64,
                    run_id=None,
                    occurrence_id=None,
                    max_attempts=3,
                    origin_trace_id=run.origin_trace_id,
                    workflow_run_id=run.run_id,
                    workflow_epoch=1,
                    required_worker_profile_digest=None,
                )
            )
            run = await repository.attach_initial_job(scope, run.run_id, job_id)
            assert run.current_job_id == job_id

            savepoint = await session.begin_nested()
            try:
                forged = await repository.stage_run(
                    scope,
                    WorkflowRunCreate(
                        workflow_id=workflow_id,
                        workflow_version_id=version.version_id,
                        requested_workflow_version_id=version.version_id,
                        inputs={"question": "forged-job-state"},
                        input_digest=_admission_request(
                            requested_workflow_version_id=version.version_id,
                            inputs={"question": "forged-job-state"},
                        ).input_digest,
                        idempotency_hash="8" * 64,
                        admission_request_digest=_admission_request(
                            requested_workflow_version_id=version.version_id,
                            inputs={"question": "forged-job-state"},
                        ).digest,
                        trigger_kind="manual",
                        trigger_ref=None,
                        origin_trace_id=f"workflow-g13-{uuid.uuid4()}",
                        required_worker_profile_digest=None,
                        retry_of_run_id=None,
                    ),
                )
                forged_job_id = await JobRepository(session).enqueue(
                    EnqueueJob(
                        job_type="workflow_run",
                        scope=JobScope(project_id, owner_id),
                        idempotency_key="9" * 64,
                        run_id=None,
                        occurrence_id=None,
                        max_attempts=3,
                        origin_trace_id=forged.record.origin_trace_id,
                        workflow_run_id=forged.record.run_id,
                        workflow_epoch=1,
                        required_worker_profile_digest=None,
                    )
                )
                await session.execute(
                    sa.text("UPDATE jobs SET status='retry_wait' WHERE id=:job_id"),
                    {"job_id": forged_job_id},
                )
                with pytest.raises(WorkflowRunStateConflict):
                    await repository.attach_initial_job(
                        scope,
                        forged.record.run_id,
                        forged_job_id,
                    )
            finally:
                await savepoint.rollback()
            after_agent_counts = await _agent_table_counts(session)
            assert after_agent_counts == before_agent_counts
            await session.execute(
                sa.text(
                    """INSERT INTO projects
                       (id,slug,display_name,created_by_user_id)
                       VALUES (:id,:slug,'G13 Other Project',:owner)"""
                ),
                {
                    "id": other_project_id,
                    "slug": f"workflow-g13-other-{other_project_id.hex[:12]}",
                    "owner": str(seed.owner_b.user_id),
                },
            )
            await session.execute(
                sa.text(
                    """INSERT INTO project_memberships
                       (id,project_id,user_id,role,status,version)
                       VALUES (:id,:project_id,:user_id,'runner','active',1)"""
                ),
                {
                    "id": other_membership_id,
                    "project_id": other_project_id,
                    "user_id": str(seed.owner_b.user_id),
                },
            )

        authorization = WorkflowAuthorizationService(policy=_AllowWorkflowRunPolicy())
        service = WorkflowPrivateRunService(seed.factory, authorization=authorization)
        visible = await service.get(seed.owner_a, run.run_id)
        assert visible.run_id == run.run_id
        with pytest.raises(WorkflowNotFound):
            await service.get(seed.owner_b, run.run_id)
        other_project_context = PrivateWorkContext.from_project(
            ProjectContext(
                user_id=seed.owner_b.user_id,
                project_id=other_project_id,
                membership_id=other_membership_id,
                role=ProjectRole.RUNNER,
                capabilities=capabilities_for(ProjectRole.RUNNER),
                membership_version=1,
                request_id="req-g13-other-project",
            )
        )
        with pytest.raises(WorkflowNotFound):
            await service.get(other_project_context, run.run_id)

        default_authorized = WorkflowPrivateRunService(seed.factory)
        default_visible = await default_authorized.get(seed.owner_a, run.run_id)
        assert default_visible.run_id == run.run_id

        default_authorization = WorkflowAuthorizationService()
        async with seed.factory() as session, session.begin():
            with pytest.raises(WorkflowForbidden):
                await default_authorization.require(
                    session,
                    seed.owner_b,
                    WorkflowAction.HTTP_WRITE,
                    lock=False,
                )

        cancelled = await service.cancel(seed.owner_a, run.run_id)
        assert cancelled.run.status == "cancelled"
        assert cancelled.cancel_requested is True
        assert cancelled.settled is True
        cancelled_again = await service.cancel(seed.owner_a, run.run_id)
        assert cancelled_again.run.updated_at == cancelled.run.updated_at
        assert cancelled_again.run.completed_at == cancelled.run.completed_at
        retry_source = await service.prepare_retry(seed.owner_a, run.run_id)
        assert retry_source.source_run_id == run.run_id
        assert retry_source.workflow_version_id == version.version_id
        assert retry_source.source_origin_trace_id == run.origin_trace_id

        async with seed.factory() as session, session.begin():
            with pytest.raises(WorkflowRunStateConflict):
                await WorkflowRepository(session).stage_run(
                    scope,
                    WorkflowRunCreate(
                        workflow_id=workflow_id,
                        workflow_version_id=version.version_id,
                        requested_workflow_version_id=version.version_id,
                        inputs={"question": "standalone"},
                        input_digest=_admission_request(
                            requested_workflow_version_id=version.version_id,
                            inputs={"question": "standalone"},
                            retry_of_run_id=run.run_id,
                        ).input_digest,
                        idempotency_hash="a" * 64,
                        admission_request_digest=_admission_request(
                            requested_workflow_version_id=version.version_id,
                            inputs={"question": "standalone"},
                            retry_of_run_id=run.run_id,
                        ).digest,
                        trigger_kind="manual",
                        trigger_ref=None,
                        origin_trace_id=run.origin_trace_id,
                        required_worker_profile_digest=None,
                        retry_of_run_id=run.run_id,
                    ),
                )
            with pytest.raises(WorkflowRunStateConflict):
                await WorkflowRepository(session).stage_run(
                    scope,
                    WorkflowRunCreate(
                        workflow_id=workflow_id,
                        workflow_version_id=version.version_id,
                        requested_workflow_version_id=version.version_id,
                        inputs={"question": "changed-on-retry"},
                        input_digest=_admission_request(
                            requested_workflow_version_id=version.version_id,
                            inputs={"question": "changed-on-retry"},
                            retry_of_run_id=run.run_id,
                        ).input_digest,
                        idempotency_hash="b" * 64,
                        admission_request_digest=_admission_request(
                            requested_workflow_version_id=version.version_id,
                            inputs={"question": "changed-on-retry"},
                            retry_of_run_id=run.run_id,
                        ).digest,
                        trigger_kind="manual",
                        trigger_ref=None,
                        origin_trace_id=f"workflow-g13-{uuid.uuid4()}",
                        required_worker_profile_digest=None,
                        retry_of_run_id=run.run_id,
                    ),
                )

        async with seed.factory() as session, session.begin():
            repository = WorkflowRepository(session)
            unknown = await repository.stage_run(
                scope,
                WorkflowRunCreate(
                    workflow_id=workflow_id,
                    workflow_version_id=version.version_id,
                    requested_workflow_version_id=version.version_id,
                    inputs={"question": "unknown"},
                    input_digest=_admission_request(
                        requested_workflow_version_id=version.version_id,
                        inputs={"question": "unknown"},
                    ).input_digest,
                    idempotency_hash="d" * 64,
                    admission_request_digest=_admission_request(
                        requested_workflow_version_id=version.version_id,
                        inputs={"question": "unknown"},
                    ).digest,
                    trigger_kind="manual",
                    trigger_ref=None,
                    origin_trace_id=f"workflow-g13-{uuid.uuid4()}",
                    required_worker_profile_digest=None,
                    retry_of_run_id=None,
                ),
            )
            unknown_job_id = await JobRepository(session).enqueue(
                EnqueueJob(
                    job_type="workflow_run",
                    scope=JobScope(project_id, owner_id),
                    idempotency_key="e" * 64,
                    run_id=None,
                    occurrence_id=None,
                    max_attempts=3,
                    origin_trace_id=unknown.record.origin_trace_id,
                    workflow_run_id=unknown.record.run_id,
                    workflow_epoch=1,
                    required_worker_profile_digest=None,
                )
            )
            await repository.attach_initial_job(
                scope,
                unknown.record.run_id,
                unknown_job_id,
            )
            worker_id = uuid.uuid4()
            raw_lease_token = "workflow-g13-lease"
            lease_token_hash = hashlib.sha256(raw_lease_token.encode()).hexdigest()
            await session.execute(
                sa.text(
                    """INSERT INTO worker_nodes
                       (id,version,capabilities_json,runtime_profile_digests_json,
                        max_concurrent_jobs)
                       VALUES (:worker,'g13','[]','[]',1)"""
                ),
                {"worker": worker_id},
            )
            await session.execute(
                sa.text(
                    """UPDATE jobs
                       SET status='running',attempt_count=1,
                           lease_owner_id=:worker,lease_token_hash=:token_hash,
                           lease_expires_at=clock_timestamp()+interval '10 minutes',
                           heartbeat_at=clock_timestamp(),started_at=clock_timestamp()
                       WHERE id=:job_id"""
                ),
                {
                    "job_id": unknown_job_id,
                    "worker": worker_id,
                    "token_hash": lease_token_hash,
                },
            )
            await session.execute(
                sa.text(
                    """INSERT INTO job_attempts
                       (id,job_id,attempt_number,worker_id,lease_token_hash)
                       VALUES (:id,:job,1,:worker,:token_hash)"""
                ),
                {
                    "id": uuid.uuid4(),
                    "job": unknown_job_id,
                    "worker": worker_id,
                    "token_hash": lease_token_hash,
                },
            )
            await session.execute(
                sa.text(
                    """UPDATE workflow_runs
                       SET status='running',started_at=clock_timestamp()
                       WHERE id=:run_id"""
                ),
                {"run_id": unknown.record.run_id},
            )

            # PostgreSQL CHECK constraints accept both TRUE and NULL.  Every
            # server-authoritative required field below therefore needs an
            # explicit IS NOT NULL guard, not only a regex/comparison.
            lifecycle_times = (
                await session.scalar(sa.text("SELECT clock_timestamp()+interval '1 second'")),
                await session.scalar(sa.text("SELECT clock_timestamp()+interval '2 seconds'")),
            )
            lifecycle_cases = (
                ("running_started", "running", None, None, None, None),
                ("succeeded_started", "succeeded", None, lifecycle_times[1], {}, None),
                ("succeeded_completed", "succeeded", lifecycle_times[0], None, {}, None),
                ("failed_started", "failed", None, lifecycle_times[1], None, "WORKFLOW_INPUT_INVALID"),
                ("failed_completed", "failed", lifecycle_times[0], None, None, "WORKFLOW_INPUT_INVALID"),
                ("failed_error", "failed", lifecycle_times[0], lifecycle_times[1], None, None),
                (
                    "unknown_error",
                    "side_effect_unknown",
                    lifecycle_times[0],
                    lifecycle_times[1],
                    None,
                    None,
                ),
                ("cancelled_completed", "cancelled", None, None, None, None),
            )
            run_insert = """INSERT INTO workflow_runs
                (id,project_id,owner_user_id,workflow_id,workflow_version_id,
                 status,input_json,input_digest,idempotency_hash,
                 admission_request_digest,trigger_kind,origin_trace_id,
                 required_worker_profile_digest,worker_profile_key,
                 execution_epoch,started_at,completed_at,output_json,error_code)
                VALUES
                (:id,:project,:owner,:workflow,:version,:status,'{}'::jsonb,
                 :input_digest,:idempotency,:admission,'manual',:trace,
                 :profile,:profile_key,1,:started,:completed,
                 CAST(:output_json AS jsonb),:error_code)"""
            for label, status, started_at, completed_at, output_json, error_code in lifecycle_cases:
                await _expect_named_constraint(
                    session,
                    constraint_name="ck_workflow_runs_lifecycle",
                    statement=run_insert,
                    params={
                        "id": uuid.uuid4(),
                        "project": project_id,
                        "owner": owner_id,
                        "workflow": workflow_id,
                        "version": version.version_id,
                        "status": status,
                        "input_digest": "a" * 64,
                        "idempotency": uuid.uuid4().hex * 2,
                        "admission": "b" * 64,
                        "trace": f"g13-null-{label}-{uuid.uuid4()}",
                        "profile": None,
                        "profile_key": "0" * 64,
                        "started": started_at,
                        "completed": completed_at,
                        "output_json": None if output_json is None else "{}",
                        "error_code": error_code,
                    },
                )

            await _expect_named_constraint(
                session,
                constraint_name="ck_workflow_runs_profile_digest",
                statement=run_insert,
                params={
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "owner": owner_id,
                    "workflow": workflow_id,
                    "version": version.version_id,
                    "status": "queued",
                    "input_digest": "a" * 64,
                    "idempotency": uuid.uuid4().hex * 2,
                    "admission": "b" * 64,
                    "trace": f"g13-null-run-profile-{uuid.uuid4()}",
                    "profile": None,
                    "profile_key": "f" * 64,
                    "started": None,
                    "completed": None,
                    "output_json": None,
                    "error_code": None,
                },
            )
            await _expect_named_constraint(
                session,
                constraint_name="ck_jobs_workflow_profile",
                statement="""UPDATE jobs
                    SET required_worker_profile_digest=NULL,
                        workflow_profile_key=:profile_key
                    WHERE id=:job""",
                params={"job": unknown_job_id, "profile_key": "f" * 64},
            )
            # The production BEFORE trigger normally rejects this mismatch
            # first.  Disable only that trigger in the disposable transaction
            # so the regression proves the CHECK itself is fail-closed too.
            await session.execute(sa.text("ALTER TABLE workflow_run_snapshots DISABLE TRIGGER trg_workflow_run_snapshots_profile_key"))
            try:
                await _expect_named_constraint(
                    session,
                    constraint_name="ck_workflow_run_snapshots_profile_digest",
                    statement="""INSERT INTO workflow_run_snapshots
                        (workflow_run_id,project_id,owner_user_id,
                         workflow_version_id,graph_schema_version,
                         compiler_contract_version,semantic_checksum,
                         catalog_generation,required_worker_profile_digest,
                         worker_profile_key,snapshot_checksum)
                        VALUES
                        (:run,:project,:owner,:version,1,1,:semantic,:generation,
                         NULL,:profile_key,:snapshot)""",
                    params={
                        "run": unknown.record.run_id,
                        "project": project_id,
                        "owner": owner_id,
                        "version": version.version_id,
                        "semantic": draft.draft_checksum,
                        "generation": "c" * 64,
                        "profile_key": "f" * 64,
                        "snapshot": "d" * 64,
                    },
                )
            finally:
                await session.execute(sa.text("ALTER TABLE workflow_run_snapshots ENABLE TRIGGER trg_workflow_run_snapshots_profile_key"))
            await _expect_named_constraint(
                session,
                constraint_name="ck_workflow_run_http_snapshots_credential_group",
                statement="""INSERT INTO workflow_run_http_snapshots
                    (workflow_run_id,project_id,owner_user_id,
                     workflow_version_id,node_id,http_method,normalized_origin,
                     endpoint_policy_revision,endpoint_policy_checksum,
                     injection_profile_revision,injection_profile_checksum,
                     egress_profile_digest,timeout_ms,max_request_bytes,
                     max_response_bytes,credential_slot_id,credential_grant_id,
                     credential_id,credential_version_id,payload_schema_checksum)
                    VALUES
                    (:run,:project,:owner,:version,:node,'GET',
                     'https://example.test',1,:endpoint,1,:injection,:egress,
                     1000,1024,1024,'slot',:grant,:credential,:credential_version,
                     NULL)""",
                params={
                    "run": unknown.record.run_id,
                    "project": project_id,
                    "owner": owner_id,
                    "version": version.version_id,
                    "node": uuid.uuid4(),
                    "endpoint": "e" * 64,
                    "injection": "f" * 64,
                    "egress": "1" * 64,
                    "grant": uuid.uuid4(),
                    "credential": uuid.uuid4(),
                    "credential_version": uuid.uuid4(),
                },
            )
            code_lease_insert = """INSERT INTO workflow_code_sandbox_leases
                (id,project_id,owner_user_id,workflow_run_id,node_id,
                 activation_id,activation_attempt,job_id,workflow_epoch,
                 job_attempt_number,worker_id,reconciliation_key_hash,
                 profile_digest,state,execution_lease_token_hash,
                 cleanup_locator_ciphertext,cleanup_deadline,
                 cleanup_handoff_at,cleanup_owner_worker_id,
                 cleanup_lease_token_hash,cleanup_lease_expires_at)
                VALUES
                (:id,:project,:owner,:run,:node,:activation,1,:job,1,1,
                 :worker,:reconciliation,:profile,:state,NULL,:locator,
                 clock_timestamp()+interval '5 minutes',:handoff,:cleanup_owner,
                 NULL,:cleanup_expires)"""
            for state, locator, handoff, cleanup_owner, cleanup_expires in (
                ("provisioning", None, None, None, None),
                ("running", b"locator", None, None, None),
                (
                    "cleanup_pending",
                    b"locator",
                    lifecycle_times[0],
                    worker_id,
                    lifecycle_times[1],
                ),
            ):
                await _expect_named_constraint(
                    session,
                    constraint_name="ck_workflow_code_leases_shape",
                    statement=code_lease_insert,
                    params={
                        "id": uuid.uuid4(),
                        "project": project_id,
                        "owner": owner_id,
                        "run": unknown.record.run_id,
                        "node": uuid.uuid4(),
                        "activation": f"g13-null-{state}-{uuid.uuid4().hex[:8]}",
                        "job": unknown_job_id,
                        "worker": worker_id,
                        "reconciliation": uuid.uuid4().hex * 2,
                        "profile": "0" * 64,
                        "state": state,
                        "locator": locator,
                        "handoff": handoff,
                        "cleanup_owner": cleanup_owner,
                        "cleanup_expires": cleanup_expires,
                    },
                )
            await _expect_named_constraint(
                session,
                constraint_name="ck_workflow_node_effects_state_shape",
                statement="""INSERT INTO workflow_node_effects
                    (id,project_id,owner_user_id,workflow_run_id,node_id,
                     activation_key,operation_key,http_method,status,
                     request_hmac,provider_idempotency_key,dispatch_job_id,
                     dispatch_execution_epoch,dispatch_attempt,
                     dispatch_started_at,safe_error_code)
                    VALUES
                    (:id,:project,:owner,:run,:node,:activation,:operation,
                     'POST','unknown',:hmac,:provider,:job,1,1,
                     clock_timestamp(),NULL)""",
                params={
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "owner": owner_id,
                    "run": unknown.record.run_id,
                    "node": uuid.uuid4(),
                    "activation": f"g13-null-effect-{uuid.uuid4().hex[:8]}",
                    "operation": "2" * 64,
                    "hmac": "3" * 64,
                    "provider": "4" * 64,
                    "job": unknown_job_id,
                },
            )
            session.expire_all()
            fence_values = {
                "scope": scope,
                "run_id": unknown.record.run_id,
                "job_id": unknown_job_id,
                "execution_epoch": 1,
                "job_attempt_number": 1,
                "worker_id": worker_id,
            }
            with pytest.raises(WorkflowRunStateConflict):
                await repository.append_execution_event(
                    WorkflowExecutionFence(
                        **fence_values,
                        lease_token="wrong-token",
                    ),
                    WorkflowRunEventAppend(
                        event_type="workflow.run.started",
                        payload={},
                    ),
                )
            started_event = await repository.append_execution_event(
                WorkflowExecutionFence(
                    **fence_values,
                    lease_token=raw_lease_token,
                ),
                WorkflowRunEventAppend(
                    event_type="workflow.run.started",
                    payload={},
                ),
            )
            assert started_event.seq == 1
            assert started_event.node_id is None
            assert started_event.activation_id is None
            assert started_event.scope_path_hash is None
            assert started_event.iteration_path == ()
            assert started_event.attempt is None

            node_id = uuid.uuid4()
            node_event = await repository.append_execution_event(
                WorkflowExecutionFence(
                    **fence_values,
                    lease_token=raw_lease_token,
                ),
                WorkflowRunEventAppend(
                    event_type="workflow.node.log",
                    payload={
                        "node_type": "python_code",
                        "stream": "stdout",
                        "text": "safe",
                        "truncated": False,
                    },
                    node_id=node_id,
                    activation_id="activation-g13-1",
                    scope_path_hash="1" * 64,
                    iteration_path=(2, 3),
                    attempt=2,
                ),
            )
            assert node_event.seq == 2
            assert node_event.node_id == node_id
            assert node_event.activation_id == "activation-g13-1"
            assert node_event.scope_path_hash == "1" * 64
            assert node_event.iteration_path == (2, 3)
            assert node_event.attempt == 2
            assert node_event.payload == {
                "node_type": "python_code",
                "stream": "stdout",
                "text": "safe",
                "truncated": False,
            }

            payload_source = {
                "node_type": "llm",
                "duration_ms": 3,
                "output_preview": {
                    "format": "json",
                    "text": "{}",
                    "truncated": False,
                    "redacted": True,
                },
            }
            frozen_event = WorkflowRunEventAppend(
                event_type="workflow.node.completed",
                payload=payload_source,
                node_id=uuid.uuid4(),
                activation_id="activation-g13-frozen",
                scope_path_hash="2" * 64,
                iteration_path=(1,),
                attempt=1,
            )
            payload_source["output_preview"]["text"] = "mutated-after-construction"  # type: ignore[index]
            completed_event = await repository.append_execution_event(
                WorkflowExecutionFence(
                    **fence_values,
                    lease_token=raw_lease_token,
                ),
                frozen_event,
            )
            assert completed_event.seq == 3
            persisted_event_payload = await session.scalar(
                sa.text(
                    """SELECT payload FROM workflow_run_events
                        WHERE workflow_run_id=:run_id AND seq=3"""
                ),
                {"run_id": unknown.record.run_id},
            )
            assert persisted_event_payload == {
                "node_type": "llm",
                "duration_ms": 3,
                "output_preview": {
                    "format": "json",
                    "text": "{}",
                    "truncated": False,
                    "redacted": True,
                },
            }

            event_count_before_expiry = int(
                await session.scalar(
                    sa.text("SELECT count(*) FROM workflow_run_events WHERE workflow_run_id=:run_id"),
                    {"run_id": unknown.record.run_id},
                )
                or 0
            )
            assert event_count_before_expiry == 3
            with pytest.raises(ValueError):
                await repository.append_execution_event(
                    WorkflowExecutionFence(
                        **fence_values,
                        lease_token=raw_lease_token,
                    ),
                    WorkflowRunEventAppend(
                        event_type="workflow.node.started",
                        payload={"node_type": "llm"},
                        node_id=uuid.uuid4(),
                        activation_id="invalid activation",
                        scope_path_hash="2" * 64,
                        attempt=1,
                    ),
                )
            assert (
                int(
                    await session.scalar(
                        sa.text("SELECT count(*) FROM workflow_run_events WHERE workflow_run_id=:run_id"),
                        {"run_id": unknown.record.run_id},
                    )
                    or 0
                )
                == event_count_before_expiry
            )
            for activation_id, scope_path_hash, attempt in (
                ("invalid activation", "3" * 64, 1),
                (None, "3" * 64, 1),
                ("activation-valid", None, 1),
                ("activation-valid", "3" * 64, None),
            ):
                with pytest.raises(DBAPIError):
                    async with session.begin_nested():
                        await session.execute(
                            sa.text(
                                """INSERT INTO workflow_run_events
                                   (project_id,owner_user_id,workflow_run_id,
                                    workflow_version_id,seq,event_type,node_id,
                                    activation_id,scope_path_hash,iteration_path,
                                    attempt,payload,occurred_at)
                                   VALUES
                                   (:project,:owner,:run,:version,4,
                                    'workflow.node.started',:node,
                                    :activation,:scope,'{}'::integer[],
                                    :attempt,'{"node_type":"llm"}'::jsonb,
                                    clock_timestamp())"""
                            ),
                            {
                                "project": project_id,
                                "owner": owner_id,
                                "run": unknown.record.run_id,
                                "version": version.version_id,
                                "node": uuid.uuid4(),
                                "activation": activation_id,
                                "scope": scope_path_hash,
                                "attempt": attempt,
                            },
                        )
            assert (
                int(
                    await session.scalar(
                        sa.text("SELECT count(*) FROM workflow_run_events WHERE workflow_run_id=:run_id"),
                        {"run_id": unknown.record.run_id},
                    )
                    or 0
                )
                == event_count_before_expiry
            )
            await session.execute(
                sa.text("UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=:job_id"),
                {"job_id": unknown_job_id},
            )
            monkeypatch.setattr(
                workflow_persistence_sql,
                "_now",
                lambda value=None: value or datetime(2000, 1, 1, tzinfo=UTC),
            )
            with pytest.raises(WorkflowRunStateConflict):
                await repository.append_execution_event(
                    WorkflowExecutionFence(
                        **fence_values,
                        lease_token=raw_lease_token,
                    ),
                    WorkflowRunEventAppend(
                        event_type="workflow.node.started",
                        payload={"node_type": "llm"},
                        node_id=uuid.uuid4(),
                        activation_id="activation-expired",
                        scope_path_hash="2" * 64,
                        attempt=1,
                    ),
                )
            assert (
                int(
                    await session.scalar(
                        sa.text("SELECT count(*) FROM workflow_run_events WHERE workflow_run_id=:run_id"),
                        {"run_id": unknown.record.run_id},
                    )
                    or 0
                )
                == event_count_before_expiry
            )
            await session.execute(
                sa.text(
                    """UPDATE jobs
                       SET status='dead',completed_at=clock_timestamp(),
                           public_error_code='SIDE_EFFECT_STATE_UNKNOWN'
                       WHERE id=:job_id"""
                ),
                {"job_id": unknown_job_id},
            )
            await session.execute(
                sa.text(
                    """UPDATE workflow_runs
                       SET status='side_effect_unknown',
                           completed_at=clock_timestamp(),
                           error_code='SIDE_EFFECT_STATE_UNKNOWN',
                           current_job_id=NULL
                       WHERE id=:run_id"""
                ),
                {"run_id": unknown.record.run_id},
            )

        async with seed.factory() as session, session.begin():
            with pytest.raises(WorkflowManualRetryForbidden):
                await WorkflowRepository(session).stage_run(
                    scope,
                    WorkflowRunCreate(
                        workflow_id=workflow_id,
                        workflow_version_id=version.version_id,
                        requested_workflow_version_id=version.version_id,
                        inputs={"question": "unknown"},
                        input_digest=_admission_request(
                            requested_workflow_version_id=version.version_id,
                            inputs={"question": "unknown"},
                            retry_of_run_id=unknown.record.run_id,
                        ).input_digest,
                        idempotency_hash="f" * 64,
                        admission_request_digest=_admission_request(
                            requested_workflow_version_id=version.version_id,
                            inputs={"question": "unknown"},
                            retry_of_run_id=unknown.record.run_id,
                        ).digest,
                        trigger_kind="manual",
                        trigger_ref=None,
                        origin_trace_id=f"workflow-g13-{uuid.uuid4()}",
                        required_worker_profile_digest=None,
                        retry_of_run_id=unknown.record.run_id,
                    ),
                )
        with pytest.raises(WorkflowRunRetryForbidden):
            await service.prepare_retry(seed.owner_a, unknown.record.run_id)

        async with seed.factory() as session, session.begin():
            repository = WorkflowRepository(session)
            assert (
                await repository.get_run(
                    WorkflowRunScope(project_id=project_id, owner_user_id=str(seed.owner_b.user_id)),
                    run.run_id,
                )
                is None
            )
            assert (
                await repository.get_run(
                    WorkflowRunScope(
                        project_id=uuid.uuid4(),
                        owner_user_id=owner_id,
                    ),
                    run.run_id,
                )
                is None
            )
            report = await repository.retention_report(scope, run.run_id)
            assert report is not None
            assert report.terminal is True
            assert report.ordinary_delete_supported is False
            assert report.epoch_job_count == 1
            assert report.event_count == 1

            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        sa.text("DELETE FROM workflow_runs WHERE id=:run_id"),
                        {"run_id": run.run_id},
                    )
    finally:
        await seed.engine.dispose()
