from __future__ import annotations

import uuid
from copy import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from support.m4_private_threads import seed_m4_thread_database

import app.audit.sinks as audit_sinks
from app.audit.models import (
    AuditAuthorityRejected,
    AuditProcess,
    resolve_system_audit_context,
)
from app.audit.service import (
    AuditService,
    _bind_gateway_audit_process,
    _bind_operator_audit_process,
    _bind_process_audit_for_test,
    _bind_recovery_audit_process,
    _bind_scheduler_audit_process,
    _bind_worker_audit_process,
)
from app.audit.sinks import OperationalAuditSink, SystemJobAuditSink
from app.automations.dispatcher import AutomationDefinitionRef, AutomationDispatcher
from app.automations.models import AutomationChanges, AutomationCreate
from app.automations.service import ProjectAutomationService
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectLastAdmin
from app.projects.invitation_repository import InvitationRepository
from app.projects.invitation_service import InvitationService
from app.projects.lifecycle_repository import ProjectLifecycleRepository
from app.projects.lifecycle_service import ProjectLifecycleService
from app.projects.membership_repository import MembershipRepository
from app.projects.membership_service import MembershipService
from app.projects.models import CreateProject, ProjectChanges, ProjectRole
from app.projects.repository import ProjectRepository
from app.projects.service import ProjectService
from app.quotas.models import (
    EffectiveQuotaLimits,
    ProjectQuotaLimits,
    ProjectQuotaPolicy,
)
from app.reliability.execution import (
    AgentExecutionResult,
    PrivateRunJobHandler,
    PrivateRunJobTerminalPort,
)
from app.reliability.owner_refs import AuditHmacKeyring
from app.worker.service import JobLeaseAuthority
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.jobs.model import DeadJobRow, JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import (
    JobRepository,
    JobRequeueForbidden,
    JobTerminalEvent,
)
from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
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


def _operational_sink(
    service: AuditService,
    process: AuditProcess,
) -> OperationalAuditSink:
    bind = {
        AuditProcess.GATEWAY: _bind_gateway_audit_process,
        AuditProcess.WORKER: _bind_worker_audit_process,
        AuditProcess.SCHEDULER: _bind_scheduler_audit_process,
    }[process]
    return OperationalAuditSink(service, process_context=bind(service))


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


async def _seed_safe_dead_job(seed) -> uuid.UUID:
    predecessor_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            JobRow(
                id=predecessor_id,
                job_type="retention_purge",
                project_id=seed.owner_a.project_id,
                owner_user_id=None,
                run_id=None,
                automation_occurrence_id=None,
                predecessor_dead_job_id=None,
                idempotency_key=uuid.uuid4().hex * 2,
                status="dead",
                attempt_count=1,
                max_attempts=1,
                retry_safety="safe",
                public_error_code="PURGE_FAILED",
            )
        )
        await session.flush()
        session.add(
            DeadJobRow(
                job_id=predecessor_id,
                project_id=seed.owner_a.project_id,
                owner_ref_key_id=None,
                owner_ref_hmac=None,
                job_type="retention_purge",
                attempt_count=1,
                retry_safety="safe",
                public_error_code="PURGE_FAILED",
                dead_at=NOW,
            )
        )
        await session.flush()
    return predecessor_id


@pytest.mark.postgres
@pytest.mark.anyio
async def test_private_run_admission_writes_audit_in_domain_transaction(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    run_id = str(uuid.uuid4())
    thread_id = f"m6-audit-{uuid.uuid4()}"
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.GATEWAY,
    )
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
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.GATEWAY,
    )
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
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.GATEWAY,
    )
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
async def test_same_role_change_is_idempotent_without_audit_or_version_change(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.GATEWAY,
    )
    project = _project(seed.owner_a)
    try:
        async with seed.factory() as session:
            unchanged = await MembershipService(
                MembershipRepository(session),
                audit=sink,
            ).change_role(
                project,
                seed.owner_b.membership_id,
                ProjectRole.RUNNER,
                seed.owner_b.membership_version,
            )

        assert unchanged.version == seed.owner_b.membership_version
        async with seed.factory() as session:
            assert (
                await session.scalar(
                    select(AuditLogRow.id).where(
                        AuditLogRow.action == "member.role_changed",
                    )
                )
                is None
            )
    finally:
        await seed.engine.dispose()


class _FailingProjectAudit:
    async def project_created(self, *_args, **_kwargs) -> None:
        raise RuntimeError("governance audit unavailable")


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_create_update_audit_and_hook_rollback(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.GATEWAY,
    )
    slug = f"audit-project-{uuid.uuid4().hex[:12]}"
    rolled_back_slug = f"audit-rollback-{uuid.uuid4().hex[:12]}"
    try:
        async with seed.factory() as session:
            service = ProjectService(ProjectRepository(session), audit=sink)
            context = await service.create(
                seed.owner_a.user_id,
                CreateProject(slug, "Audit project", "private description", "A"),
                "project-create-audit",
            )
            await service.update(
                context,
                ProjectChanges(display_name="Updated audit project"),
            )

        with pytest.raises(RuntimeError, match="governance audit unavailable"):
            async with seed.factory() as session:
                await ProjectService(
                    ProjectRepository(session),
                    audit=_FailingProjectAudit(),
                ).create(
                    seed.owner_a.user_id,
                    CreateProject(
                        rolled_back_slug,
                        "Rollback project",
                        "private rollback description",
                        "R",
                    ),
                    "project-create-rollback",
                )

        async with seed.factory() as session:
            rows = (await session.execute(select(AuditLogRow).where(AuditLogRow.project_id == context.project_id).order_by(AuditLogRow.occurred_at, AuditLogRow.id))).scalars().all()
            rolled_back = await session.scalar(
                text("SELECT id FROM projects WHERE slug=:slug"),
                {"slug": rolled_back_slug},
            )
        assert [row.action for row in rows] == ["project.created", "project.updated"]
        assert all(row.metadata_json == {} for row in rows)
        assert all(row.actor_user_id == str(seed.owner_a.user_id) for row in rows)
        assert rolled_back is None
        assert "private description" not in repr([row.__dict__ for row in rows])
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_lifecycle_audits_all_existing_mutations(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.GATEWAY,
    )
    context = _project(seed.owner_a)
    try:
        async with seed.factory() as session:
            service = ProjectLifecycleService(
                ProjectLifecycleRepository(session),
                audit=sink,
            )
            await service.request_deletion(context, NOW)
            await service.restore(
                context.user_id,
                context.project_id,
                "project-recovered-audit",
                NOW + timedelta(seconds=1),
            )
            await service.suspend(context, NOW + timedelta(seconds=2))
            await service.resume(
                context.user_id,
                context.project_id,
                "project-resumed-audit",
                NOW + timedelta(seconds=3),
            )

        async with seed.factory() as session:
            rows = (await session.execute(select(AuditLogRow).where(AuditLogRow.project_id == context.project_id).order_by(AuditLogRow.occurred_at, AuditLogRow.id))).scalars().all()
        assert [row.action for row in rows] == [
            "project.deletion_requested",
            "project.recovered",
            "project.suspended",
            "project.resumed",
        ]
        assert all(row.metadata_json == {} for row in rows)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_invitation_and_member_join_audits_are_allowlisted(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.GATEWAY,
    )
    context = _project(seed.owner_a)
    member_id = uuid.uuid4()
    member_email = f"{member_id}@example.com"
    try:
        async with seed.engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'user',now(),false,0)"""
                ),
                {"id": str(member_id), "email": member_email},
            )
        async with seed.factory() as session:
            service = InvitationService(
                InvitationRepository(session),
                audit=sink,
            )
            revoked = await service.create(
                context,
                "revoked@example.com",
                ProjectRole.VIEWER,
                NOW,
            )
            await service.revoke(
                context,
                revoked.invitation.id,
                revoked.invitation.version,
                NOW + timedelta(seconds=1),
            )
            redeemable = await service.create(
                context,
                member_email,
                ProjectRole.EDITOR,
                NOW + timedelta(seconds=2),
            )
            claim = await service.claim(
                redeemable.token,
                NOW + timedelta(seconds=3),
            )
            await service.redeem(
                member_id,
                member_email,
                claim,
                NOW + timedelta(seconds=4),
                request_id="invitation-redeemed-audit",
            )

        async with seed.factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLogRow)
                        .where(
                            AuditLogRow.project_id == context.project_id,
                            AuditLogRow.action.in_(
                                (
                                    "invitation.created",
                                    "invitation.revoked",
                                    "invitation.redeemed",
                                    "member.joined",
                                )
                            ),
                        )
                        .order_by(AuditLogRow.occurred_at, AuditLogRow.id)
                    )
                )
                .scalars()
                .all()
            )
        assert [row.action for row in rows] == [
            "invitation.created",
            "invitation.revoked",
            "invitation.created",
            "invitation.redeemed",
            "member.joined",
        ]
        assert [row.metadata_json for row in rows] == [
            {"role": "viewer"},
            {},
            {"role": "editor"},
            {"role": "editor"},
            {"role": "editor"},
        ]
        encoded = repr([row.__dict__ for row in rows])
        assert member_email not in encoded
        assert redeemable.token not in encoded
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_automation_definition_mutations_write_safe_audit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.GATEWAY,
    )
    try:
        service = ProjectAutomationService(
            seed.factory,
            lambda: NOW,
            min_once_delay_seconds=0,
            audit=sink,
        )
        created = await service.create(
            seed.owner_a,
            AutomationCreate(
                title="Private automation title",
                prompt="Private automation prompt",
                context_mode="fresh_thread_per_run",
                thread_id=None,
                agent_asset_id=seed.project_agent_id,
                agent_scope="project",
                schedule_type="cron",
                schedule_spec={"cron": "0 * * * *"},
                timezone="UTC",
            ),
        )
        updated = await service.update(
            seed.owner_a,
            created.id,
            AutomationChanges(
                expected_version=created.version,
                title="Updated private title",
            ),
        )
        await service.delete(seed.owner_a, updated.id, updated.version)

        async with seed.factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLogRow)
                        .where(
                            AuditLogRow.project_id == seed.owner_a.project_id,
                            AuditLogRow.action.in_(
                                (
                                    "automation.created",
                                    "automation.updated",
                                    "automation.deleted",
                                )
                            ),
                        )
                        .order_by(AuditLogRow.occurred_at, AuditLogRow.id)
                    )
                )
                .scalars()
                .all()
            )
        assert [row.action for row in rows] == [
            "automation.created",
            "automation.updated",
            "automation.deleted",
        ]
        assert all(row.metadata_json == {} for row in rows)
        encoded = repr([row.__dict__ for row in rows])
        assert "Private automation" not in encoded
        assert "Updated private" not in encoded
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_scheduled_automation_writes_joint_trigger_and_run_audit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.SCHEDULER,
    )
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
async def test_gateway_bound_sink_cannot_mint_scheduler_audit_actor(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.GATEWAY,
    )
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
                    title="Process-bound audit",
                    prompt="private prompt",
                    schedule_type="cron",
                    schedule_spec={"cron": "0 * * * *"},
                    timezone="UTC",
                    next_run_at=NOW,
                ),
            )

        with pytest.raises(AuditAuthorityRejected):
            await AutomationDispatcher(
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
            assert await session.scalar(select(ScheduledTaskRunRow.id)) is None
            assert await session.scalar(select(AuditLogRow.id)) is None
    finally:
        await seed.engine.dispose()


def test_process_audit_registry_binds_service_to_one_role() -> None:
    service = AuditService(None, _keyring())

    gateway_context = _bind_gateway_audit_process(service)

    assert gateway_context.process is AuditProcess.GATEWAY
    assert not hasattr(service, "bind_gateway_process")
    assert _bind_gateway_audit_process(service) is gateway_context
    with pytest.raises(AuditAuthorityRejected):
        _bind_worker_audit_process(service)
    with pytest.raises(AuditAuthorityRejected):
        _bind_scheduler_audit_process(service)


def test_process_audit_context_rejects_copy_replacement_and_cross_service_use() -> None:
    gateway_service = AuditService(None, _keyring())
    gateway_context = _bind_gateway_audit_process(gateway_service)
    worker_service = AuditService(None, _keyring())
    worker_context = _bind_process_audit_for_test(
        worker_service,
        AuditProcess.WORKER,
    )

    OperationalAuditSink(
        gateway_service,
        process_context=gateway_context,
    )
    with pytest.raises(AuditAuthorityRejected):
        OperationalAuditSink(
            gateway_service,
            process_context=copy(gateway_context),
        )
    with pytest.raises(AuditAuthorityRejected):
        OperationalAuditSink(
            gateway_service,
            process_context=worker_context,
        )

    object.__setattr__(gateway_context, "process", AuditProcess.WORKER)
    with pytest.raises(AuditAuthorityRejected):
        OperationalAuditSink(
            gateway_service,
            process_context=gateway_context,
        )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_quota_policy_audit_contract_persists_only_allowlisted_governance_values(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.GATEWAY,
    )
    policy = ProjectQuotaPolicy(
        configured=ProjectQuotaLimits(
            member_limit=7,
            storage_bytes_limit=None,
            concurrent_run_limit=2,
            mcp_calls_daily_limit=900,
        ),
        effective=EffectiveQuotaLimits(
            member_limit=7,
            storage_bytes_limit=5_368_709_120,
            concurrent_run_limit=2,
            mcp_calls_daily_limit=900,
        ),
        version=3,
    )
    try:
        async with seed.factory() as session, session.begin():
            await sink.quota_policy_updated(session, seed.owner_a, policy)

        async with seed.factory() as session:
            row = (
                await session.execute(
                    select(AuditLogRow).where(
                        AuditLogRow.action == "quota.policy_updated",
                    )
                )
            ).scalar_one()
        assert row.actor_user_id == str(seed.owner_a.user_id)
        assert row.metadata_json == {
            "member_limit": 7,
            "concurrent_run_limit": 2,
            "mcp_calls_daily_limit": 900,
            "version": 3,
        }
        assert "effective" not in repr(row.__dict__).lower()
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_trusted_operation_audit_contracts_allowlist_recovery_metadata(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink_type = audit_sinks.TrustedOperationAuditSink
    operator_service = AuditService(seed.factory, _keyring())
    operator = sink_type(
        operator_service,
        process_context=_bind_operator_audit_process(operator_service),
    )
    recovery_service = AuditService(seed.factory, _keyring())
    recovery = sink_type(
        recovery_service,
        process_context=_bind_recovery_audit_process(recovery_service),
    )
    backup_id = uuid.uuid4()
    restore_id = uuid.uuid4()
    purge_id = uuid.uuid4()
    try:
        async with seed.factory() as session, session.begin():
            await operator.backup_created(
                session,
                backup_id=backup_id,
                table_count=18,
                tombstone_high_watermark=41,
                request_id="operator backup request",
            )
            await operator.restore_started(
                session,
                restore_id=restore_id,
                table_count=18,
                tombstones_replayed=0,
                request_id="operator restore request",
            )
            await recovery.restore_completed(
                session,
                restore_id=restore_id,
                table_count=18,
                tombstones_replayed=2,
                request_id="recovery restore request",
            )
            await recovery.recovery_drill_completed(
                session,
                restore_id=restore_id,
                table_count=18,
                tombstones_replayed=2,
                request_id="recovery drill request",
            )
            await recovery.purge_completed(
                session,
                purge_id=purge_id,
                project_id=None,
                resource_kind="account",
                purged_count=9,
                request_id="recovery purge request",
            )

        async with seed.factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLogRow).order_by(
                            AuditLogRow.occurred_at,
                            AuditLogRow.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert [row.action for row in rows] == [
            "backup.created",
            "restore.started",
            "restore.completed",
            "recovery.drill_completed",
            "purge.completed",
        ]
        assert rows[0].metadata_json == {
            "table_count": 18,
            "tombstone_high_watermark": 41,
        }
        assert rows[-1].metadata_json == {
            "resource_kind": "account",
            "purged_count": 9,
        }
        encoded = repr([row.__dict__ for row in rows])
        for forbidden in (
            str(backup_id),
            str(restore_id),
            str(purge_id),
            "operator backup request",
            "operator restore request",
            "recovery restore request",
            "recovery drill request",
            "recovery purge request",
        ):
            assert forbidden not in encoded
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_system_requeue_sink_binds_admin_and_successor_authority(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    predecessor_id = await _seed_safe_dead_job(seed)
    context = resolve_system_audit_context(
        SimpleNamespace(
            id=seed.owner_a.user_id,
            system_role="system_admin",
        ),
        request_id="system-requeue",
    )
    sink = SystemJobAuditSink(
        AuditService(seed.factory, _keyring()),
        context,
    )
    try:
        async with seed.factory() as session, session.begin():
            monkeypatch.setattr(
                session,
                "get",
                lambda *_args, **_kwargs: pytest.fail("system requeue audit must not load a naked Job row"),
            )
            successor_id = await JobRepository(session).requeue_safe_system(
                seed.owner_a.project_id,
                predecessor_id,
                idempotency_key="a" * 64,
                max_attempts=3,
                request_id=context.request_id,
                audit_port=sink,
            )

        async with seed.factory() as session:
            row = (
                await session.execute(
                    select(AuditLogRow).where(
                        AuditLogRow.action == "job.requeued",
                    )
                )
            ).scalar_one()
        assert row.actor_user_id == str(context.user_id)
        assert row.actor_platform_role == "system_admin"
        assert row.project_id == seed.owner_a.project_id
        assert row.job_id == successor_id
        assert row.metadata_json == {
            "job_type": "retention_purge",
            "attempt_count": 0,
            "retry_safety": "safe",
        }
        assert str(predecessor_id) not in repr(row.__dict__)
        assert row.target_ref_hmac != str(successor_id)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_system_requeue_rejects_cross_project_dead_job_scope(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    predecessor_id = await _seed_safe_dead_job(seed)
    context = resolve_system_audit_context(
        SimpleNamespace(
            id=seed.owner_a.user_id,
            system_role="system_admin",
        ),
        request_id="system-requeue-mismatch",
    )
    sink = SystemJobAuditSink(
        AuditService(seed.factory, _keyring()),
        context,
    )
    try:
        with pytest.raises(JobRequeueForbidden):
            async with seed.factory() as session, session.begin():
                await JobRepository(session).requeue_safe_system(
                    seed.project_b_owner_a.project_id,
                    predecessor_id,
                    idempotency_key="b" * 64,
                    max_attempts=3,
                    request_id=context.request_id,
                    audit_port=sink,
                )

        async with seed.factory() as session:
            assert await session.scalar(select(AuditLogRow.id)) is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_dead_job_audit_contains_codes_not_exception_text(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.WORKER,
    )
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
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.WORKER,
    )
    run_id = str(uuid.uuid4())
    thread_id = f"m6-terminal-audit-{uuid.uuid4()}"
    worker_id = uuid.uuid4()
    try:
        async with seed.factory() as session, session.begin():
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="audit-test",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=1,
                )
            )
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
            repository = JobRepository(session)
            claim = await repository.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=90,
            )
            assert claim is not None
            assert claim.job_id == admitted.job.job_id
            assert await repository.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )

        class Executor:
            async def execute(self, _execution, _authority):
                return AgentExecutionResult.succeeded()

        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
            audit=sink,
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()
        await settlement.commit()

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
    sink = _operational_sink(
        AuditService(seed.factory, _keyring()),
        AuditProcess.GATEWAY,
    )
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
        assert rows[1].actor_process == "gateway"
    finally:
        await seed.engine.dispose()
