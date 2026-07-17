from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from support.m4_private_threads import seed_m4_thread_database

from app.audit.service import AuditService
from app.audit.sinks import OperationalAuditSink
from app.automations.dispatcher import AutomationDefinitionRef, AutomationDispatcher
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectLastAdmin
from app.projects.membership_repository import MembershipRepository
from app.projects.membership_service import MembershipService
from app.projects.models import ProjectRole
from app.reliability.execution import PrivateRunJobTerminalPort
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.jobs.sql import JobTerminalEvent
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskCreate,
    ScheduledTaskRepository,
)

NOW = datetime(2026, 7, 16, 18, tzinfo=UTC)


def _keyring() -> AuditHmacKeyring:
    return AuditHmacKeyring(
        active_key_id="audit-v1",
        _keys={"audit-v1": b"1" * 32},
    )


def _project(context) -> ProjectContext:
    return ProjectContext(
        user_id=context.user_id,
        project_id=context.project_id,
        membership_id=context.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=context.membership_version,
        request_id=context.request_id,
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_private_run_admission_writes_audit_in_domain_transaction(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    run_id = str(uuid.uuid4())
    thread_id = f"m6-audit-{uuid.uuid4()}"
    sink = OperationalAuditSink(AuditService(seed.factory, _keyring()))
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )

        admitted = await PrivateRunAdmissionService(
            seed.factory,
            audit=sink,
        ).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(run_id=run_id),
        )

        async with seed.factory() as session:
            row = (
                await session.execute(
                    select(AuditLogRow).where(
                        AuditLogRow.project_id == seed.owner_a.project_id,
                        AuditLogRow.action == "run.admitted",
                    )
                )
            ).scalar_one()
        assert row.job_id == admitted.job.job_id
        assert row.metadata_json == {
            "job_type": "private_run",
            "non_interactive": False,
        }
        assert run_id not in repr(row.__dict__)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_failed_last_admin_removal_writes_no_success_audit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = OperationalAuditSink(AuditService(seed.factory, _keyring()))
    project = _project(seed.owner_a)
    try:
        async with seed.factory() as session:
            service = MembershipService(
                MembershipRepository(session),
                audit=sink,
            )
            with pytest.raises(ProjectLastAdmin):
                await service.remove(
                    project,
                    project.membership_id,
                    project.membership_version,
                )

        async with seed.factory() as session:
            count = await session.scalar(
                select(AuditLogRow.id).where(
                    AuditLogRow.project_id == project.project_id,
                    AuditLogRow.action == "member.removed",
                )
            )
        assert count is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_successful_member_removal_commits_one_safe_audit_row(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = OperationalAuditSink(AuditService(seed.factory, _keyring()))
    project = _project(seed.owner_a)
    try:
        async with seed.factory() as session:
            removed = await MembershipService(
                MembershipRepository(session),
                audit=sink,
            ).remove(
                project,
                seed.owner_b.membership_id,
                seed.owner_b.membership_version,
            )

        async with seed.factory() as session:
            row = (
                await session.execute(
                    select(AuditLogRow).where(
                        AuditLogRow.project_id == project.project_id,
                        AuditLogRow.action == "member.removed",
                    )
                )
            ).scalar_one()
        assert removed.status == "removed"
        assert row.actor_user_id == str(project.user_id)
        assert row.metadata_json == {}
        assert str(removed.user_id) not in repr(row.__dict__)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_scheduled_automation_writes_joint_trigger_and_run_audit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = OperationalAuditSink(AuditService(seed.factory, _keyring()))
    try:
        async with seed.factory() as session, session.begin():
            task = await ScheduledTaskRepository(session).create(
                seed.owner_a_scope,
                ScheduledTaskCreate(
                    task_id=str(uuid.uuid4()),
                    thread_id=None,
                    context_mode="fresh_thread_per_run",
                    agent_asset_id=seed.project_agent_id,
                    agent_scope="project",
                    title="Audit automation",
                    prompt="private prompt must never enter audit",
                    schedule_type="cron",
                    schedule_spec={"cron": "0 * * * *"},
                    timezone="UTC",
                    next_run_at=NOW,
                ),
            )

        admitted = await AutomationDispatcher(
            seed.factory,
            audit=sink,
        ).admit_occurrence(
            AutomationDefinitionRef(
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                task_id=task.id,
                membership_version=seed.owner_a.membership_version,
            ),
            scheduled_for=NOW,
        )

        async with seed.factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLogRow)
                        .where(
                            AuditLogRow.project_id == seed.owner_a.project_id,
                        )
                        .order_by(AuditLogRow.action)
                    )
                )
                .scalars()
                .all()
            )
        assert [row.action for row in rows] == [
            "automation.triggered",
            "run.admitted",
        ]
        assert rows[0].actor_process == "scheduler"
        assert rows[0].metadata_json == {"trigger_kind": "scheduled"}
        assert rows[1].job_id == admitted.job.job_id
        assert rows[1].metadata_json == {
            "job_type": "automation_run",
            "non_interactive": True,
        }
        assert "private prompt" not in repr([row.__dict__ for row in rows])
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_dead_job_audit_contains_codes_not_exception_text(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = OperationalAuditSink(AuditService(seed.factory, _keyring()))
    run_id = str(uuid.uuid4())
    thread_id = f"m6-dead-audit-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(run_id=run_id),
        )

        async with seed.factory() as session, session.begin():
            await PrivateRunJobTerminalPort(audit=sink).job_terminalized(
                session,
                JobTerminalEvent(
                    job_id=admitted.job.job_id,
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    run_id=run_id,
                    occurrence_id=None,
                    job_type="private_run",
                    status="dead",
                    retry_safety="unknown",
                    public_error_code="SIDE_EFFECT_STATE_UNKNOWN",
                    cancel_reason=None,
                    occurred_at=NOW,
                    attempt_count=2,
                ),
            )

        async with seed.factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLogRow).where(
                            AuditLogRow.action.in_(("job.dead", "run.terminal")),
                        )
                    )
                )
                .scalars()
                .all()
            )
        by_action = {row.action: row for row in rows}
        assert by_action["job.dead"].metadata_json == {
            "job_type": "private_run",
            "public_error_code": "SIDE_EFFECT_STATE_UNKNOWN",
            "attempt_count": 2,
            "retry_safety": "unknown",
        }
        assert by_action["run.terminal"].metadata_json == {
            "job_type": "private_run",
            "status": "failed",
            "public_error_code": "SIDE_EFFECT_STATE_UNKNOWN",
        }
        assert "exception" not in repr([row.__dict__ for row in rows]).lower()
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_successful_job_terminal_does_not_duplicate_run_terminal_audit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = OperationalAuditSink(AuditService(seed.factory, _keyring()))
    run_id = str(uuid.uuid4())
    thread_id = f"m6-terminal-audit-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(run_id=run_id),
        )
        async with seed.factory() as session, session.begin():
            await sink.run_terminal(
                session,
                seed.owner_a_scope,
                run_id=run_id,
                job_id=admitted.job.job_id,
                job_type="private_run",
                status="success",
                public_error_code=None,
                request_id="worker-private-run",
            )
            await sink.job_terminalized(
                session,
                JobTerminalEvent(
                    job_id=admitted.job.job_id,
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    run_id=run_id,
                    occurrence_id=None,
                    job_type="private_run",
                    status="succeeded",
                    retry_safety="safe",
                    public_error_code=None,
                    cancel_reason=None,
                    occurred_at=NOW,
                    attempt_count=1,
                ),
            )

        async with seed.factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLogRow).where(
                            AuditLogRow.action == "run.terminal",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].metadata_json == {
            "job_type": "private_run",
            "status": "completed",
        }
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_queued_run_cancel_writes_request_and_terminal_in_same_transaction(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = OperationalAuditSink(AuditService(seed.factory, _keyring()))
    run_id = str(uuid.uuid4())
    thread_id = f"m6-cancel-audit-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(run_id=run_id),
        )

        await PrivateRunService(seed.factory, audit=sink).cancel(
            seed.owner_a,
            thread_id,
            run_id,
        )

        async with seed.factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLogRow)
                        .where(
                            AuditLogRow.project_id == seed.owner_a.project_id,
                        )
                        .order_by(AuditLogRow.occurred_at, AuditLogRow.id)
                    )
                )
                .scalars()
                .all()
            )
        assert [row.action for row in rows] == [
            "run.cancel_requested",
            "run.terminal",
        ]
        assert {row.job_id for row in rows} == {admitted.job.job_id}
        assert rows[1].metadata_json == {
            "job_type": "private_run",
            "status": "cancelled",
        }
    finally:
        await seed.engine.dispose()
