from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.audit.models import resolve_system_audit_context
from app.audit.service import AuditService
from app.reliability.owner_refs import AuditHmacKeyring
from app.reliability.workers import WorkerRegistry
from app.system_runtime_settings.bootstrap import (
    WORKFLOW_RUNTIME_DEFAULT_POLICY_VERSION_ID,
    bootstrap_system_runtime_policies,
)
from app.system_runtime_settings.errors import (
    SystemRuntimePolicyConflict,
    SystemRuntimePolicyStorageUnavailable,
    SystemRuntimePolicyUnavailable,
)
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import (
    LockedWorkflowRuntimePolicy,
    RuntimePolicySection,
    default_policy_value,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.system_runtime_settings.workflow_runtime import (
    WorkflowRuntimeConvergence,
    WorkflowRuntimeMaterializedIdentity,
)
from app.workflows.runtime_policy import (
    WorkflowRuntimeAdminPolicyV1,
    WorkflowRuntimePolicyUpdateRequestV1,
)

pytestmark = pytest.mark.postgres
_EMPTY_PROFILE_KEY = "0" * 64


class _BarrierWorkflowRuntimeConvergence(WorkflowRuntimeConvergence):
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__()
        self._entered = entered
        self._release = release

    async def project_in_session(
        self,
        session: AsyncSession,
        locked: LockedWorkflowRuntimePolicy,
    ) -> WorkflowRuntimeAdminPolicyV1:
        self._entered.set()
        await self._release.wait()
        return await super().project_in_session(session, locked)


class _UnavailableWorkflowRuntimeConvergence(WorkflowRuntimeConvergence):
    async def project_in_session(
        self,
        session: AsyncSession,
        locked: LockedWorkflowRuntimePolicy,
    ) -> WorkflowRuntimeAdminPolicyV1:
        raise RuntimeError("workflow runtime projection unavailable")


def _enabled_policy_payload() -> dict[str, object]:
    payload = default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME).model_dump(mode="json")
    payload["enabled"] = True
    payload["admission_enabled"] = True
    return payload


def _builder_only_policy_payload() -> dict[str, object]:
    payload = default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME).model_dump(mode="json")
    payload["enabled"] = True
    payload["admission_enabled"] = False
    return payload


async def _worker_runtime_identity(
    connection: AsyncConnection,
    worker_id: uuid.UUID,
):
    return (
        await connection.execute(
            text(
                """SELECT runtime_profile_digests_json,
                          workflow_runtime_policy_section,
                          workflow_runtime_policy_version_id,
                          workflow_runtime_policy_revision,
                          workflow_runtime_policy_schema_version,
                          workflow_runtime_policy_checksum,
                          heartbeat_at
                     FROM worker_nodes
                    WHERE id=:id"""
            ),
            {"id": worker_id},
        )
    ).one()


async def _seed_queued_workflow_run(
    connection: AsyncConnection,
    *,
    owner_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    project_id = uuid.uuid4()
    workflow_id = uuid.uuid4()
    version_id = uuid.uuid4()
    run_id = uuid.uuid4()
    job_id = uuid.uuid4()
    trace_id = f"workflow-g11-{uuid.uuid4()}"
    await connection.execute(
        text(
            """INSERT INTO projects
               (id,slug,display_name,created_by_user_id)
               VALUES (:id,:slug,'Workflow G11',:owner)"""
        ),
        {
            "id": project_id,
            "slug": f"workflow-g11-{project_id.hex[:12]}",
            "owner": str(owner_id),
        },
    )
    await connection.execute(
        text(
            """INSERT INTO project_memberships
               (id,project_id,user_id,role,status,version)
               VALUES (:id,:project,:owner,'admin','active',1)"""
        ),
        {"id": uuid.uuid4(), "project": project_id, "owner": str(owner_id)},
    )
    await connection.execute(
        text(
            """INSERT INTO workflow_definitions
               (id,project_id,name,status,revision,created_by,updated_by)
               VALUES (:id,:project,'G11 Workflow','active',1,:owner,:owner)"""
        ),
        {
            "id": workflow_id,
            "project": project_id,
            "owner": str(owner_id),
        },
    )
    await connection.execute(
        text(
            """INSERT INTO workflow_versions
               (id,workflow_id,project_id,version_number,spec_json,canvas_json,
                semantic_checksum,compiler_contract_version,published_by)
               VALUES (:id,:workflow,:project,1,'{}','{}',:checksum,1,:owner)"""
        ),
        {
            "id": version_id,
            "workflow": workflow_id,
            "project": project_id,
            "checksum": "1" * 64,
            "owner": str(owner_id),
        },
    )
    await connection.execute(
        text(
            """UPDATE workflow_definitions
                  SET current_published_version_id=:version
                WHERE id=:workflow"""
        ),
        {"version": version_id, "workflow": workflow_id},
    )
    await connection.execute(
        text(
            """INSERT INTO workflow_runs
               (id,project_id,owner_user_id,workflow_id,workflow_version_id,
                status,input_json,input_digest,idempotency_hash,
                admission_request_digest,trigger_kind,
                origin_trace_id,worker_profile_key,execution_epoch)
               VALUES (:id,:project,:owner,:workflow,:version,'queued','{}',
                       :input_digest,:idempotency,:admission_digest,'manual',
                       :trace,:profile,1)"""
        ),
        {
            "id": run_id,
            "project": project_id,
            "owner": str(owner_id),
            "workflow": workflow_id,
            "version": version_id,
            "input_digest": "2" * 64,
            "idempotency": "3" * 64,
            "admission_digest": "4" * 64,
            "trace": trace_id,
            "profile": _EMPTY_PROFILE_KEY,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO jobs
               (id,job_type,project_id,owner_user_id,workflow_run_id,
                workflow_epoch,workflow_profile_key,origin_trace_id,
                idempotency_key,status,attempt_count,max_attempts)
               VALUES (:id,'workflow_run',:project,:owner,:run,1,:profile,
                       :trace,:key,'queued',0,3)"""
        ),
        {
            "id": job_id,
            "project": project_id,
            "owner": str(owner_id),
            "run": run_id,
            "profile": _EMPTY_PROFILE_KEY,
            "trace": trace_id,
            "key": job_id.hex * 2,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO workflow_run_jobs
               (workflow_run_id,execution_epoch,job_id,project_id,
                owner_user_id,worker_profile_key,cause)
               VALUES (:run,1,:job,:project,:owner,:profile,'initial')"""
        ),
        {
            "run": run_id,
            "job": job_id,
            "project": project_id,
            "owner": str(owner_id),
            "profile": _EMPTY_PROFILE_KEY,
        },
    )
    await connection.execute(
        text("UPDATE workflow_runs SET current_job_id=:job WHERE id=:run"),
        {"job": job_id, "run": run_id},
    )
    return project_id, run_id


@pytest.mark.anyio
async def test_builder_only_policy_is_gateway_effective_without_worker_or_handler(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    try:
        await bootstrap_system_runtime_policies(factory)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {
                    "id": str(admin_id),
                    "email": f"workflow-g11-builder-{admin_id}@example.com",
                },
            )
        service = SystemRuntimePolicyService(
            factory,
            AuditService(
                factory,
                AuditHmacKeyring(
                    active_key_id="workflow-g11-builder-v1",
                    _keys={"workflow-g11-builder-v1": b"b" * 32},
                ),
            ),
        )
        context = resolve_system_audit_context(
            SimpleNamespace(id=admin_id, system_role="system_admin"),
            request_id=str(uuid.uuid4()),
        )

        projection = await service.update_workflow_runtime_policy(
            context,
            WorkflowRuntimePolicyUpdateRequestV1(
                expected_revision=1,
                value=_builder_only_policy_payload(),
            ),
        )

        assert projection.stored.value.enabled is True
        assert projection.stored.value.admission_enabled is False
        assert projection.effective is not None
        assert projection.effective.revision == projection.stored.revision
        assert projection.pending_roles == ()
        assert projection.readiness.code == "WORKFLOW_RUNTIME_READY"
        assert projection.readiness.admission_ready is False
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_worker_registry_register_and_heartbeat_atomically_publish_current_exact_identity(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    try:
        await bootstrap_system_runtime_policies(factory)
        registry = WorkerRegistry(factory, version="g11-register")
        await registry.register(
            worker_id,
            frozenset(),
            2,
            runtime_profile_digests=frozenset({"a" * 64}),
        )
        async with engine.connect() as connection:
            initial = await _worker_runtime_identity(connection, worker_id)
        assert initial.runtime_profile_digests_json == ["a" * 64]
        assert initial.workflow_runtime_policy_section == "workflow_runtime"
        assert initial.workflow_runtime_policy_version_id == WORKFLOW_RUNTIME_DEFAULT_POLICY_VERSION_ID
        assert initial.workflow_runtime_policy_revision == 1
        assert initial.workflow_runtime_policy_schema_version == 1
        assert initial.workflow_runtime_policy_checksum == "4ca136425002aa3a3a2426b4687f2e8091b6e4c23bf1d4db88b952730e1431e4"
        assert initial.heartbeat_at is not None

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {
                    "id": str(admin_id),
                    "email": f"workflow-g11-worker-{admin_id}@example.com",
                },
            )
        service = SystemRuntimePolicyService(
            factory,
            AuditService(
                factory,
                AuditHmacKeyring(
                    active_key_id="workflow-g11-worker-v1",
                    _keys={"workflow-g11-worker-v1": b"w" * 32},
                ),
            ),
        )
        context = resolve_system_audit_context(
            SimpleNamespace(id=admin_id, system_role="system_admin"),
            request_id=str(uuid.uuid4()),
        )
        updated = await service.update_workflow_runtime_policy(
            context,
            WorkflowRuntimePolicyUpdateRequestV1(
                expected_revision=1,
                value=_enabled_policy_payload(),
            ),
        )
        assert updated.effective is not None
        assert updated.effective.revision == 2
        assert updated.pending_roles == ("worker",)

        async with engine.connect() as connection:
            before_heartbeat = await _worker_runtime_identity(connection, worker_id)
        assert before_heartbeat.workflow_runtime_policy_revision == 1

        # A new registry instance models a Worker restart. Its first heartbeat
        # converges from the PostgreSQL current pointer without process cache.
        restarted_registry = WorkerRegistry(factory, version="g11-restarted")
        assert await restarted_registry.heartbeat(
            worker_id,
            runtime_profile_digests=frozenset({"b" * 64}),
        )
        async with engine.connect() as connection:
            converged = await _worker_runtime_identity(connection, worker_id)
        assert converged.runtime_profile_digests_json == ["b" * 64]
        assert converged.workflow_runtime_policy_version_id == updated.stored.policy_version_id
        assert converged.workflow_runtime_policy_revision == updated.stored.revision
        assert converged.workflow_runtime_policy_schema_version == updated.stored.schema_version
        assert converged.workflow_runtime_policy_checksum == updated.stored.payload_checksum
        assert converged.heartbeat_at >= initial.heartbeat_at

        # A new Gateway service instance reads the durable Worker row directly;
        # G32's hard gate still keeps admission pending.
        restarted_gateway = SystemRuntimePolicyService(
            factory,
            AuditService(
                factory,
                AuditHmacKeyring(
                    active_key_id="workflow-g11-gateway-v1",
                    _keys={"workflow-g11-gateway-v1": b"g" * 32},
                ),
            ),
        )
        restarted_projection = await restarted_gateway.read_workflow_runtime_policy(context)
        assert restarted_projection.effective is not None
        assert restarted_projection.effective.revision == updated.stored.revision
        assert restarted_projection.pending_roles == ("worker",)
        assert restarted_projection.readiness.admission_ready is False

        async def unavailable_materialization(*_args, **_kwargs):
            raise SystemRuntimePolicyUnavailable

        monkeypatch.setattr(
            SystemRuntimePolicyMaterializer,
            "materialize_workflow_runtime_current_locked_in_session",
            unavailable_materialization,
        )
        with pytest.raises(SystemRuntimePolicyUnavailable):
            await restarted_registry.heartbeat(
                worker_id,
                runtime_profile_digests=frozenset({"c" * 64}),
            )
        async with engine.connect() as connection:
            after_failure = await _worker_runtime_identity(connection, worker_id)
        assert after_failure == converged
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_worker_policy_identity_is_all_null_or_exact_fk_and_has_fresh_index(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.connect() as connection:
            current = (
                await connection.execute(
                    text(
                        """SELECT p.current_version_id,p.revision,
                                  v.schema_version,v.payload_checksum
                             FROM system_runtime_policies p
                             JOIN system_runtime_policy_versions v
                               ON v.section=p.section
                              AND v.id=p.current_version_id
                            WHERE p.section='workflow_runtime'"""
                    )
                )
            ).one()

        for columns, values in (
            (
                "workflow_runtime_policy_section,workflow_runtime_policy_version_id,workflow_runtime_policy_revision,workflow_runtime_policy_schema_version,workflow_runtime_policy_checksum",
                "NULL,:version,:revision,:schema_version,:checksum",
            ),
            (
                "workflow_runtime_policy_section",
                "'workflow_runtime'",
            ),
            (
                "workflow_runtime_policy_section,workflow_runtime_policy_version_id,workflow_runtime_policy_revision,workflow_runtime_policy_schema_version,workflow_runtime_policy_checksum",
                "'workflow_runtime',:version,:wrong_revision,:schema_version,:checksum",
            ),
            (
                "workflow_runtime_policy_section,workflow_runtime_policy_version_id,workflow_runtime_policy_revision,workflow_runtime_policy_schema_version,workflow_runtime_policy_checksum",
                "'workflow_runtime',:version,:revision,:schema_version,:wrong_checksum",
            ),
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            f"""INSERT INTO worker_nodes
                                (id,version,capabilities_json,
                                 runtime_profile_digests_json,
                                 max_concurrent_jobs,{columns})
                                VALUES (:id,'g11-invalid','[]','[]'::jsonb,1,{values})"""
                        ),
                        {
                            "id": uuid.uuid4(),
                            "version": current.current_version_id,
                            "revision": current.revision,
                            "wrong_revision": current.revision + 1,
                            "schema_version": current.schema_version,
                            "checksum": current.payload_checksum,
                            "wrong_checksum": "f" * 64,
                        },
                    )

        null_identity_worker = uuid.uuid4()
        exact_identity_worker = uuid.uuid4()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO worker_nodes
                       (id,version,capabilities_json,
                        runtime_profile_digests_json,max_concurrent_jobs)
                       VALUES (:id,'g11-null','[]','[]'::jsonb,1)"""
                ),
                {"id": null_identity_worker},
            )
            await connection.execute(
                text(
                    """INSERT INTO worker_nodes
                       (id,version,capabilities_json,
                        runtime_profile_digests_json,
                        workflow_runtime_policy_section,
                        workflow_runtime_policy_version_id,
                        workflow_runtime_policy_revision,
                        workflow_runtime_policy_schema_version,
                        workflow_runtime_policy_checksum,
                        max_concurrent_jobs)
                       VALUES (:id,'g11-exact','[]','[]'::jsonb,
                               'workflow_runtime',:version,:revision,
                               :schema_version,:checksum,1)"""
                ),
                {
                    "id": exact_identity_worker,
                    "version": current.current_version_id,
                    "revision": current.revision,
                    "schema_version": current.schema_version,
                    "checksum": current.payload_checksum,
                },
            )
            null_row = await _worker_runtime_identity(connection, null_identity_worker)
            exact_row = await _worker_runtime_identity(connection, exact_identity_worker)
            index_count = await connection.scalar(
                text(
                    """SELECT count(*) FROM pg_indexes
                        WHERE schemaname=current_schema()
                          AND tablename='worker_nodes'
                          AND indexname='ix_worker_nodes_workflow_runtime_identity_fresh'"""
                )
            )
            match_type = await connection.scalar(
                text(
                    """SELECT confmatchtype::text
                         FROM pg_constraint
                        WHERE conrelid='worker_nodes'::regclass
                          AND conname='fk_worker_nodes_workflow_runtime_identity'"""
                )
            )
        assert all(value is None for value in null_row[1:6])
        assert exact_row.workflow_runtime_policy_version_id == current.current_version_id
        assert exact_row.workflow_runtime_policy_revision == current.revision
        assert index_count == 1
        assert match_type == "f"
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_worker_registry_uses_only_postgres_clock_and_draining_worker_cannot_revive(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    worker_id = uuid.uuid4()
    registry = WorkerRegistry(factory, version="g11-database-clock")
    try:
        await bootstrap_system_runtime_policies(factory)
        with pytest.raises(TypeError):
            await registry.register(
                worker_id,
                frozenset(),
                1,
                runtime_profile_digests=frozenset(),
                now=datetime.now(UTC) + timedelta(days=365),
            )

        async with engine.connect() as connection:
            registered_after = await connection.scalar(text("SELECT statement_timestamp()"))
        await registry.register(
            worker_id,
            frozenset(),
            1,
            runtime_profile_digests=frozenset(),
        )
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """SELECT started_at,heartbeat_at,draining
                             FROM worker_nodes WHERE id=:id"""
                    ),
                    {"id": worker_id},
                )
            ).one()
            registered_before = await connection.scalar(text("SELECT statement_timestamp()"))
        assert registered_after <= row.started_at == row.heartbeat_at <= registered_before
        assert row.draining is False

        async with engine.connect() as connection:
            draining_after = await connection.scalar(text("SELECT statement_timestamp()"))
        assert await registry.mark_draining(worker_id)
        async with engine.connect() as connection:
            drained = (
                await connection.execute(
                    text("SELECT heartbeat_at,draining FROM worker_nodes WHERE id=:id"),
                    {"id": worker_id},
                )
            ).one()
            draining_before = await connection.scalar(text("SELECT statement_timestamp()"))
        assert draining_after <= drained.heartbeat_at <= draining_before
        assert drained.draining is True
        assert not await registry.heartbeat(
            worker_id,
            runtime_profile_digests=frozenset(),
        )
        async with engine.connect() as connection:
            after_rejected_heartbeat = await connection.scalar(
                text("SELECT heartbeat_at FROM worker_nodes WHERE id=:id"),
                {"id": worker_id},
            )
        assert after_rejected_heartbeat == drained.heartbeat_at
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_admin_workflow_runtime_projection_stays_pending_without_installed_handler(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    convergence = WorkflowRuntimeConvergence()
    try:
        await bootstrap_system_runtime_policies(factory)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {
                    "id": str(admin_id),
                    "email": f"workflow-g11-{admin_id}@example.com",
                },
            )

        service = SystemRuntimePolicyService(
            factory,
            AuditService(
                factory,
                AuditHmacKeyring(
                    active_key_id="workflow-g11-test-v1",
                    _keys={"workflow-g11-test-v1": b"g" * 32},
                ),
            ),
            workflow_runtime_convergence=convergence,
        )
        context = resolve_system_audit_context(
            SimpleNamespace(id=admin_id, system_role="system_admin"),
            request_id=str(uuid.uuid4()),
        )

        initial_catalog = await service.list_policies(context)
        initial = initial_catalog.workflow_runtime
        assert initial is not None
        assert initial.stored.value.enabled is False
        assert initial.effective is not None
        assert initial.effective.revision == 1
        assert initial.pending_roles == ()
        assert initial.readiness.code == "WORKFLOW_RUNTIME_DISABLED"

        updated = await service.update_workflow_runtime_policy(
            context,
            WorkflowRuntimePolicyUpdateRequestV1(
                expected_revision=1,
                value=_enabled_policy_payload(),
            ),
        )
        assert updated.catalog_revision == 2
        assert updated.stored.revision == 2
        assert updated.effective is not None
        assert updated.effective.revision == updated.stored.revision
        assert updated.effective.payload_checksum == updated.stored.payload_checksum
        assert updated.pending_roles == ("worker",)
        assert updated.readiness.code == "WORKFLOW_RUNTIME_PENDING"
        assert updated.readiness.admission_ready is False

        desired = WorkflowRuntimeMaterializedIdentity(
            policy_version_id=updated.stored.policy_version_id,
            revision=updated.stored.revision,
            schema_version=updated.stored.schema_version,
            payload_checksum=updated.stored.payload_checksum,
        )
        # Gateway-effective identity alone is insufficient: a live Worker row
        # must independently advertise exact identity and job capability.
        still_pending = await service.read_workflow_runtime_policy(context)
        assert still_pending.pending_roles == ("worker",)

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO worker_nodes
                       (id,version,capabilities_json,runtime_profile_digests_json,
                        workflow_runtime_policy_section,
                        workflow_runtime_policy_version_id,
                        workflow_runtime_policy_revision,
                        workflow_runtime_policy_schema_version,
                        workflow_runtime_policy_checksum,
                        max_concurrent_jobs,heartbeat_at)
                       VALUES (:id,'g11',CAST(:capabilities AS json),
                               CAST(:profiles AS jsonb),'workflow_runtime',
                               :policy_version,:revision,:schema_version,
                               :checksum,1,now())"""
                ),
                {
                    "id": worker_id,
                    "capabilities": json.dumps(["workflow_run"]),
                    "profiles": json.dumps(["f" * 64]),
                    "policy_version": desired.policy_version_id,
                    "revision": desired.revision,
                    "schema_version": desired.schema_version,
                    "checksum": desired.payload_checksum,
                },
            )

        async with factory() as session, session.begin():
            statements: list[str] = []

            def capture_statement(
                _connection,
                _cursor,
                statement: str,
                _parameters,
                _context,
                _executemany,
            ) -> None:
                statements.append(statement)

            event.listen(
                engine.sync_engine,
                "before_cursor_execute",
                capture_statement,
            )
            try:
                candidate_profiles = await convergence._fresh_exact_worker_profiles(
                    session,
                    desired=desired,
                )
                assert candidate_profiles == (frozenset({"f" * 64}),)
            finally:
                event.remove(
                    engine.sync_engine,
                    "before_cursor_execute",
                    capture_statement,
                )
            worker_queries = [statement for statement in statements if "worker_nodes" in statement]
            assert len(worker_queries) == 1
            assert "statement_timestamp" in worker_queries[0]

        forged = await service.read_workflow_runtime_policy(context)
        assert forged.stored.revision == 2
        assert forged.effective is not None
        assert forged.effective.revision == forged.stored.revision
        assert forged.pending_roles == ("worker",)
        assert forged.readiness.code == "WORKFLOW_RUNTIME_PENDING"
        assert forged.readiness.admission_ready is False

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE worker_nodes
                          SET heartbeat_at=statement_timestamp() + interval '10 minutes'
                        WHERE id=:id"""
                ),
                {"id": worker_id},
            )
        async with factory() as session, session.begin():
            assert not await convergence._fresh_exact_worker_profiles(
                session,
                desired=desired,
            )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE worker_nodes
                          SET heartbeat_at=now() - interval '10 minutes'
                        WHERE id=:id"""
                ),
                {"id": worker_id},
            )
        async with factory() as session, session.begin():
            assert not await convergence._fresh_exact_worker_profiles(
                session,
                desired=desired,
            )
        stale = await service.read_workflow_runtime_policy(context)
        assert stale.effective is not None
        assert stale.effective.revision == stale.stored.revision
        assert stale.pending_roles == ("worker",)
        assert stale.readiness.admission_ready is False

        async with factory() as session:
            audit = (
                await session.execute(
                    text(
                        """SELECT metadata_json,outcome,request_id
                         FROM audit_logs
                        WHERE action='system_setting.updated'
                          AND metadata_json->>'section'='workflow_runtime'"""
                    )
                )
            ).one()
        assert audit.metadata_json == {
            "section": "workflow_runtime",
            "previous_revision": 1,
            "revision": 2,
            "effect_scope": "new_workflow_runs",
        }
        assert audit.outcome == "success"
        assert audit.request_id is not None
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_concurrent_workflow_runtime_cas_returns_its_own_atomic_projection(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    projection_entered = asyncio.Event()
    release_projection = asyncio.Event()
    first: asyncio.Task[object] | None = None
    second: asyncio.Task[object] | None = None
    try:
        await bootstrap_system_runtime_policies(factory)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {
                    "id": str(admin_id),
                    "email": f"workflow-g11-race-{admin_id}@example.com",
                },
            )

        service = SystemRuntimePolicyService(
            factory,
            AuditService(
                factory,
                AuditHmacKeyring(
                    active_key_id="workflow-g11-race-v1",
                    _keys={"workflow-g11-race-v1": b"r" * 32},
                ),
            ),
            workflow_runtime_convergence=_BarrierWorkflowRuntimeConvergence(
                projection_entered,
                release_projection,
            ),
        )
        contexts = tuple(
            resolve_system_audit_context(
                SimpleNamespace(id=admin_id, system_role="system_admin"),
                request_id=str(uuid.uuid4()),
            )
            for _ in range(2)
        )
        request = WorkflowRuntimePolicyUpdateRequestV1(
            expected_revision=1,
            value=_enabled_policy_payload(),
        )

        first = asyncio.create_task(service.update_workflow_runtime_policy(contexts[0], request))
        await asyncio.wait_for(projection_entered.wait(), timeout=5)
        second = asyncio.create_task(service.update_workflow_runtime_policy(contexts[1], request))
        await asyncio.sleep(0.05)
        assert not second.done()

        release_projection.set()
        first_result, second_result = await asyncio.gather(
            first,
            second,
            return_exceptions=True,
        )
        assert isinstance(first_result, WorkflowRuntimeAdminPolicyV1)
        assert first_result.stored.revision == 2
        assert first_result.stored.value.enabled is True
        assert first_result.effective is not None
        assert first_result.effective.revision == first_result.stored.revision
        assert first_result.pending_roles == ("worker",)
        assert isinstance(second_result, SystemRuntimePolicyConflict)

        async with factory() as session:
            version_count = await session.scalar(
                text(
                    """SELECT count(*)
                         FROM system_runtime_policy_versions
                        WHERE section='workflow_runtime'"""
                )
            )
            audit_count = await session.scalar(
                text(
                    """SELECT count(*)
                         FROM audit_logs
                        WHERE action='system_setting.updated'
                          AND metadata_json->>'section'='workflow_runtime'"""
                )
            )
        assert version_count == 2
        assert audit_count == 1
    finally:
        release_projection.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await engine.dispose()


@pytest.mark.anyio
async def test_workflow_runtime_projection_failure_rolls_back_version_pointer_and_audit(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    try:
        await bootstrap_system_runtime_policies(factory)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {
                    "id": str(admin_id),
                    "email": f"workflow-g11-failure-{admin_id}@example.com",
                },
            )

        service = SystemRuntimePolicyService(
            factory,
            AuditService(
                factory,
                AuditHmacKeyring(
                    active_key_id="workflow-g11-failure-v1",
                    _keys={"workflow-g11-failure-v1": b"f" * 32},
                ),
            ),
            workflow_runtime_convergence=_UnavailableWorkflowRuntimeConvergence(),
        )
        context = resolve_system_audit_context(
            SimpleNamespace(id=admin_id, system_role="system_admin"),
            request_id=str(uuid.uuid4()),
        )

        with pytest.raises(SystemRuntimePolicyStorageUnavailable):
            await service.update_workflow_runtime_policy(
                context,
                WorkflowRuntimePolicyUpdateRequestV1(
                    expected_revision=1,
                    value=_enabled_policy_payload(),
                ),
            )

        async with factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT p.revision,p.current_version_id,s.revision AS catalog_revision
                             FROM system_runtime_policies AS p
                             JOIN system_runtime_policy_catalog_state AS s ON s.id=1
                            WHERE p.section='workflow_runtime'"""
                    )
                )
            ).one()
            version_count = await session.scalar(
                text(
                    """SELECT count(*)
                         FROM system_runtime_policy_versions
                        WHERE section='workflow_runtime'"""
                )
            )
            audit_count = await session.scalar(
                text(
                    """SELECT count(*)
                         FROM audit_logs
                        WHERE action='system_setting.updated'
                          AND metadata_json->>'section'='workflow_runtime'"""
                )
            )
        assert state.revision == 1
        assert state.catalog_revision == 1
        assert state.current_version_id == WORKFLOW_RUNTIME_DEFAULT_POLICY_VERSION_ID
        assert version_count == 1
        assert audit_count == 0
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_workflow_run_policy_snapshot_keeps_exact_old_policy_after_current_pointer_moves(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    convergence = WorkflowRuntimeConvergence()
    try:
        await bootstrap_system_runtime_policies(factory)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {
                    "id": str(admin_id),
                    "email": f"workflow-g11-snapshot-{admin_id}@example.com",
                },
            )

        service = SystemRuntimePolicyService(
            factory,
            AuditService(
                factory,
                AuditHmacKeyring(
                    active_key_id="workflow-g11-snapshot-v1",
                    _keys={"workflow-g11-snapshot-v1": b"s" * 32},
                ),
            ),
            workflow_runtime_convergence=convergence,
        )
        context = resolve_system_audit_context(
            SimpleNamespace(id=admin_id, system_role="system_admin"),
            request_id=str(uuid.uuid4()),
        )
        enabled = await service.update_workflow_runtime_policy(
            context,
            WorkflowRuntimePolicyUpdateRequestV1(
                expected_revision=1,
                value=_enabled_policy_payload(),
            ),
        )
        desired = WorkflowRuntimeMaterializedIdentity(
            policy_version_id=enabled.stored.policy_version_id,
            revision=enabled.stored.revision,
            schema_version=enabled.stored.schema_version,
            payload_checksum=enabled.stored.payload_checksum,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO worker_nodes
                       (id,version,capabilities_json,runtime_profile_digests_json,
                        workflow_runtime_policy_section,
                        workflow_runtime_policy_version_id,
                        workflow_runtime_policy_revision,
                        workflow_runtime_policy_schema_version,
                        workflow_runtime_policy_checksum,
                        max_concurrent_jobs,heartbeat_at)
                       VALUES (:id,'g11',CAST(:capabilities AS json),
                               CAST(:profiles AS jsonb),'workflow_runtime',
                               :policy_version,:revision,:schema_version,
                               :checksum,1,now())"""
                ),
                {
                    "id": worker_id,
                    "capabilities": json.dumps(["workflow_run"]),
                    "profiles": json.dumps([]),
                    "policy_version": desired.policy_version_id,
                    "revision": desired.revision,
                    "schema_version": desired.schema_version,
                    "checksum": desired.payload_checksum,
                },
            )
            project_id, run_id = await _seed_queued_workflow_run(
                connection,
                owner_id=admin_id,
            )
            corrupt_project_id, corrupt_run_id = await _seed_queued_workflow_run(
                connection,
                owner_id=admin_id,
            )

        async with factory() as session, session.begin():
            with pytest.raises(SystemRuntimePolicyUnavailable):
                await service.lock_workflow_runtime_for_admission(session)

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_runtime_policy_snapshots
                       (workflow_run_id,project_id,owner_user_id,section,
                        policy_version_id,revision,schema_version,
                        payload_checksum,value_json)
                       VALUES (:run,:project,:owner,'workflow_runtime',
                               :policy_version,:revision,:schema_version,
                               :checksum,CAST(:value AS jsonb))"""
                ),
                {
                    "run": run_id,
                    "project": project_id,
                    "owner": str(admin_id),
                    "policy_version": enabled.stored.policy_version_id,
                    "revision": enabled.stored.revision,
                    "schema_version": enabled.stored.schema_version,
                    "checksum": enabled.stored.payload_checksum,
                    "value": json.dumps(
                        enabled.stored.value.model_dump(mode="json"),
                        sort_keys=True,
                    ),
                },
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO workflow_run_runtime_policy_snapshots
                           (workflow_run_id,project_id,owner_user_id,section,
                            policy_version_id,revision,schema_version,
                            payload_checksum,value_json)
                           VALUES (:run,:project,:owner,'workflow_runtime',
                                   :policy_version,:revision,:schema_version,
                                   :checksum,CAST(:value AS jsonb))"""
                    ),
                    {
                        "run": corrupt_run_id,
                        "project": corrupt_project_id,
                        "owner": str(admin_id),
                        "policy_version": enabled.stored.policy_version_id,
                        "revision": enabled.stored.revision,
                        "schema_version": enabled.stored.schema_version,
                        "checksum": enabled.stored.payload_checksum,
                        "value": json.dumps(
                            default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME).model_dump(mode="json"),
                            sort_keys=True,
                        ),
                    },
                )

        disabled_value = default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME)
        moved = await service.update_workflow_runtime_policy(
            context,
            WorkflowRuntimePolicyUpdateRequestV1(
                expected_revision=2,
                value=disabled_value.model_dump(mode="json"),
            ),
        )
        assert moved.stored.revision == 3

        materializer = SystemRuntimePolicyMaterializer(factory)
        frozen = await materializer.materialize_workflow_run_snapshot(
            project_id=project_id,
            owner_user_id=str(admin_id),
            workflow_run_id=run_id,
        )
        current = await materializer.materialize_workflow_runtime_current_locked()

        assert frozen.revision == 2
        assert frozen.policy_version_id == desired.policy_version_id
        assert frozen.value.enabled is True
        assert frozen.value.admission_enabled is True
        assert current.revision == 3
        assert current.value.enabled is False

        with pytest.raises(SystemRuntimePolicyUnavailable):
            await materializer.materialize_workflow_run_snapshot(
                project_id=corrupt_project_id,
                owner_user_id=str(admin_id),
                workflow_run_id=corrupt_run_id,
            )
    finally:
        await engine.dispose()
