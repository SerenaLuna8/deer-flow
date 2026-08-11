"""Real-PostgreSQL acceptance tests for the atomic G10 Workflow schema.

The test module executes only production ``full_schema.sql`` and production
Alembic migrations.  Phase-0 prototype DDL is intentionally not applied here.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from postgres_utils import temporary_postgres_database
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.bootstrap_identities import BUILTIN_MODEL_EMAIL, BUILTIN_MODEL_USER_ID
from app.system_runtime_settings.bootstrap import (
    WORKFLOW_RUNTIME_DEFAULT_POLICY_VERSION_ID,
    SystemRuntimePolicyBootstrapConflict,
    bootstrap_system_runtime_policies,
)
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import (
    RuntimePolicySection,
    default_policy_value,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.system_runtime_settings.validation import canonical_policy_payload
from deerflow.persistence.bootstrap import (
    CURRENT_SCHEMA_REVISION,
    bootstrap_schema,
)
from deerflow.persistence.final_schema_contract import (
    FINAL_M7_CATALOG_SIGNATURE,
    read_m7_catalog_signature,
)
from deerflow.persistence.jobs.sql import JobRepository
from scripts.upgrade_postgres import upgrade_postgres

BACKEND_ROOT = Path(__file__).resolve().parents[1]
BASELINE_SNAPSHOT_PATH = BACKEND_ROOT / "migrations/baseline/full_schema_v5.sql"
V9_RUNTIME_SECTIONS = (
    RuntimePolicySection.AGENT_RUNTIME,
    RuntimePolicySection.AUTH,
    RuntimePolicySection.MEMORY_DOCUMENT,
    RuntimePolicySection.QUOTAS,
)
_RUNTIME_POLICY_ID_NAMESPACE = uuid.UUID("e80287de-83d9-5d3a-a4c8-df0eeaa2a955")
_EMPTY_PROFILE_KEY = "0" * 64

pytestmark = pytest.mark.postgres


@dataclass(frozen=True, slots=True)
class _WorkflowExecution:
    actor_id: str
    project_id: uuid.UUID
    workflow_id: uuid.UUID
    version_id: uuid.UUID
    run_id: uuid.UUID
    job_id: uuid.UUID
    worker_id: uuid.UUID
    profile_digest: str | None
    origin_trace_id: str


async def _seed_workflow_execution(
    connection: AsyncConnection,
    *,
    job_status: str = "running",
    with_attempt: bool = True,
    profile_digest: str | None = "8" * 64,
) -> _WorkflowExecution:
    actor_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    workflow_id = uuid.uuid4()
    version_id = uuid.uuid4()
    run_id = uuid.uuid4()
    job_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    profile_key = profile_digest or _EMPTY_PROFILE_KEY
    trace_id = f"workflow-g10-{uuid.uuid4()}"
    await connection.execute(
        text(
            """INSERT INTO users
               (id,email,system_role,created_at,needs_setup,token_version)
               VALUES (:id,:email,'user',now(),false,0)"""
        ),
        {"id": actor_id, "email": f"{actor_id}@example.com"},
    )
    await connection.execute(
        text(
            """INSERT INTO projects
               (id,slug,display_name,created_by_user_id)
               VALUES (:id,:slug,'Workflow G10',:actor)"""
        ),
        {
            "id": project_id,
            "slug": f"workflow-g10-{project_id.hex[:12]}",
            "actor": actor_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO project_memberships
               (id,project_id,user_id,role,status,version)
               VALUES (:id,:project,:actor,'admin','active',1)"""
        ),
        {"id": uuid.uuid4(), "project": project_id, "actor": actor_id},
    )
    await connection.execute(
        text(
            """INSERT INTO workflow_definitions
               (id,project_id,name,status,revision,created_by,updated_by)
               VALUES (:id,:project,'G10 Workflow','active',1,:actor,:actor)"""
        ),
        {"id": workflow_id, "project": project_id, "actor": actor_id},
    )
    await connection.execute(
        text(
            """INSERT INTO workflow_versions
               (id,workflow_id,project_id,version_number,spec_json,canvas_json,
                semantic_checksum,compiler_contract_version,published_by)
               VALUES (:id,:workflow,:project,1,'{}','{}',:checksum,1,:actor)"""
        ),
        {
            "id": version_id,
            "workflow": workflow_id,
            "project": project_id,
            "checksum": "1" * 64,
            "actor": actor_id,
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
            """INSERT INTO worker_nodes
               (id,version,capabilities_json,runtime_profile_digests_json,
                max_concurrent_jobs)
               VALUES (:id,'g10','[]',CAST(:profiles AS jsonb),1)"""
        ),
        {
            "id": worker_id,
            "profiles": json.dumps([] if profile_digest is None else [profile_digest]),
        },
    )
    # The AsyncConnection does not start its PostgreSQL transaction until the
    # first statement.  A Python timestamp captured before that point can be
    # earlier than the row's database-side ``created_at = now()``, making an
    # otherwise valid running fixture violate the lifecycle CHECK.  Use the
    # authoritative database clock after the transaction is established.
    started_at = await connection.scalar(text("SELECT clock_timestamp()")) if job_status == "running" else None
    lease_expires_at = started_at + timedelta(minutes=10) if with_attempt and started_at is not None else None
    await connection.execute(
        text(
            """INSERT INTO workflow_runs
               (id,project_id,owner_user_id,workflow_id,workflow_version_id,
                status,input_json,input_digest,idempotency_hash,
                admission_request_digest,trigger_kind,
                origin_trace_id,required_worker_profile_digest,
                worker_profile_key,execution_epoch,started_at)
               VALUES (:id,:project,:actor,:workflow,:version,:status,'{}',
                       :input_digest,:idempotency,:admission_digest,'manual',:trace,:profile,
                       :profile_key,1,:started_at)"""
        ),
        {
            "id": run_id,
            "project": project_id,
            "actor": actor_id,
            "workflow": workflow_id,
            "version": version_id,
            "status": "queued" if job_status == "queued" else "running",
            "input_digest": "2" * 64,
            "idempotency": "3" * 64,
            "admission_digest": "4" * 64,
            "trace": trace_id,
            "profile": profile_digest,
            "profile_key": profile_key,
            "started_at": started_at,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO jobs
               (id,job_type,project_id,owner_user_id,workflow_run_id,
                workflow_epoch,required_worker_profile_digest,
                workflow_profile_key,origin_trace_id,idempotency_key,status,
                attempt_count,max_attempts,lease_owner_id,lease_token_hash,
                lease_expires_at,heartbeat_at,started_at)
               VALUES (:id,'workflow_run',:project,:actor,:run,1,:profile,
                       :profile_key,:trace,:key,:status,:attempt_count,3,
                       :lease_owner,:lease_hash,:lease_expires_at,:heartbeat_at,
                       :started_at)"""
        ),
        {
            "id": job_id,
            "project": project_id,
            "actor": actor_id,
            "run": run_id,
            "profile": profile_digest,
            "profile_key": profile_key,
            "trace": trace_id,
            "key": job_id.hex * 2,
            "status": job_status,
            "attempt_count": 1 if with_attempt else 0,
            "lease_owner": worker_id if with_attempt else None,
            "lease_hash": "9" * 64 if with_attempt else None,
            "lease_expires_at": lease_expires_at,
            "heartbeat_at": started_at,
            "started_at": started_at,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO workflow_run_jobs
               (workflow_run_id,execution_epoch,job_id,project_id,
                owner_user_id,worker_profile_key,cause)
               VALUES (:run,1,:job,:project,:actor,:profile_key,'initial')"""
        ),
        {
            "run": run_id,
            "job": job_id,
            "project": project_id,
            "actor": actor_id,
            "profile_key": profile_key,
        },
    )
    await connection.execute(
        text("UPDATE workflow_runs SET current_job_id=:job WHERE id=:run"),
        {"job": job_id, "run": run_id},
    )
    if with_attempt:
        await connection.execute(
            text(
                """INSERT INTO job_attempts
                   (id,job_id,attempt_number,worker_id,lease_token_hash)
                   VALUES (:id,:job,1,:worker,:token_hash)"""
            ),
            {
                "id": uuid.uuid4(),
                "job": job_id,
                "worker": worker_id,
                "token_hash": "9" * 64,
            },
        )
    return _WorkflowExecution(
        actor_id=actor_id,
        project_id=project_id,
        workflow_id=workflow_id,
        version_id=version_id,
        run_id=run_id,
        job_id=job_id,
        worker_id=worker_id,
        profile_digest=profile_digest,
        origin_trace_id=trace_id,
    )


def _baseline_sql() -> str:
    lines = BASELINE_SNAPSHOT_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.startswith("--"):
            return "".join(lines[index:])
    raise AssertionError("baseline snapshot contains no SQL body")


async def _execute_sql_batch(database_url: str, payload: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            raw_connection = await connection.get_raw_connection()
            await raw_connection.driver_connection.execute(payload)
    finally:
        await engine.dispose()


def _run_alembic_upgrade(database_url: str, target: str) -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    config.attributes["sqlalchemy_url"] = database_url
    command.upgrade(config, target)


def _v1_policy_version_id(section: RuntimePolicySection) -> uuid.UUID:
    return uuid.uuid5(
        _RUNTIME_POLICY_ID_NAMESPACE,
        f"{section.value}:version:1",
    )


async def _seed_complete_v9_runtime_catalog(database_url: str) -> None:
    """Seed the exact four-section catalog owned by the released v9 code."""

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET CONSTRAINTS ALL DEFERRED"))
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'user',now(),false,0)"""
                ),
                {
                    "id": str(BUILTIN_MODEL_USER_ID),
                    "email": BUILTIN_MODEL_EMAIL,
                },
            )
            for section in V9_RUNTIME_SECTIONS:
                version_id = _v1_policy_version_id(section)
                await connection.execute(
                    text(
                        """INSERT INTO system_runtime_policies
                           (section,current_version_id,revision,updated_by_user_id)
                           VALUES (:section,:version,1,:actor)"""
                    ),
                    {
                        "section": section.value,
                        "version": version_id,
                        "actor": str(BUILTIN_MODEL_USER_ID),
                    },
                )
            for section in V9_RUNTIME_SECTIONS:
                canonical = canonical_policy_payload(
                    section,
                    default_policy_value(section),
                )
                version_id = _v1_policy_version_id(section)
                await connection.execute(
                    text(
                        """INSERT INTO system_runtime_policy_versions
                           (id,section,version_number,schema_version,value,
                            payload_checksum,created_by_user_id)
                           VALUES (:id,:section,1,:schema_version,
                                   CAST(:value AS jsonb),:checksum,:actor)"""
                    ),
                    {
                        "id": version_id,
                        "section": section.value,
                        "schema_version": canonical.schema_version,
                        "value": json.dumps(canonical.value),
                        "checksum": canonical.checksum,
                        "actor": str(BUILTIN_MODEL_USER_ID),
                    },
                )
    finally:
        await engine.dispose()


async def _runtime_catalog_rows(database_url: str) -> list[object]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return list(
                (
                    await connection.execute(
                        text(
                            """SELECT p.section,p.current_version_id,p.revision,
                                      v.schema_version,v.payload_checksum,v.value
                               FROM system_runtime_policies p
                               JOIN system_runtime_policy_versions v
                                 ON v.section=p.section
                                AND v.id=p.current_version_id
                               ORDER BY p.section"""
                        )
                    )
                ).mappings()
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_published_version_is_immutable_and_active_credential_grant_is_unique(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            execution = await _seed_workflow_execution(connection)

            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """UPDATE workflow_versions
                               SET spec_json=CAST(:changed AS jsonb)
                               WHERE id=:version"""
                        ),
                        {
                            "version": execution.version_id,
                            "changed": json.dumps({"changed": True}),
                        },
                    )

            credential_id = uuid.uuid4()
            credential_version_id = uuid.uuid4()
            slot_checksum = "a" * 64
            await connection.execute(
                text(
                    """INSERT INTO credentials
                       (id,scope,project_id,name,display_name,credential_type,
                        status,is_delete,version,created_by_user_id)
                       VALUES (:id,'project',:project,:name,'Workflow HTTP',
                               'http_bearer','active',false,1,:actor)"""
                ),
                {
                    "id": credential_id,
                    "project": execution.project_id,
                    "name": f"workflow-http-{credential_id.hex[:8]}",
                    "actor": execution.actor_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO credential_versions
                       (id,credential_id,version_number,status,
                        payload_schema_version,payload_schema,created_by_user_id)
                       VALUES (:id,:credential,1,'active',1,'{}',:actor)"""
                ),
                {
                    "id": credential_version_id,
                    "credential": credential_id,
                    "actor": execution.actor_id,
                },
            )
            await connection.execute(
                text(
                    """UPDATE credentials SET current_version_id=:version
                       WHERE id=:credential"""
                ),
                {"version": credential_version_id, "credential": credential_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_version_credential_slots
                       (workflow_version_id,project_id,slot_id,name,purpose,
                        payload_schema_json,payload_schema_checksum,required)
                       VALUES (:version,:project,'http.auth','HTTP auth','header',
                               '{}',:checksum,true)"""
                ),
                {
                    "version": execution.version_id,
                    "project": execution.project_id,
                    "checksum": slot_checksum,
                },
            )
            first_grant_id = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO workflow_credential_grants
                       (id,project_id,workflow_version_id,slot_id,
                        credential_scope,credential_id,credential_version_id,
                        payload_schema_checksum,status,revision,granted_by)
                       VALUES (:id,:project,:version,'http.auth','project',
                               :credential,:credential_version,:checksum,
                               'active',1,:actor)"""
                ),
                {
                    "id": first_grant_id,
                    "project": execution.project_id,
                    "version": execution.version_id,
                    "credential": credential_id,
                    "credential_version": credential_version_id,
                    "checksum": slot_checksum,
                    "actor": execution.actor_id,
                },
            )

            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_credential_grants
                   (id,project_id,workflow_version_id,slot_id,credential_scope,
                    credential_id,credential_version_id,payload_schema_checksum,
                    status,revision,granted_by)
                   VALUES (:id,:project,:version,'http.auth','project',
                           :credential,:credential_version,:checksum,
                           'active',1,:actor)""",
                {
                    "id": uuid.uuid4(),
                    "project": execution.project_id,
                    "version": execution.version_id,
                    "credential": credential_id,
                    "credential_version": credential_version_id,
                    "checksum": slot_checksum,
                    "actor": execution.actor_id,
                },
            )
            await connection.execute(
                text(
                    """UPDATE workflow_credential_grants
                       SET status='revoked',revision=2,revoked_by=:actor,
                           revoked_at=now()
                       WHERE id=:id"""
                ),
                {"id": first_grant_id, "actor": execution.actor_id},
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_credential_grants
                   (id,project_id,workflow_version_id,slot_id,credential_scope,
                    credential_id,credential_version_id,payload_schema_checksum,
                    status,revision,granted_by)
                   VALUES (:id,:project,:version,'http.auth','project',
                           :credential,:credential_version,:checksum,
                           'active',1,:actor)""",
                {
                    "id": uuid.uuid4(),
                    "project": execution.project_id,
                    "version": execution.version_id,
                    "credential": credential_id,
                    "credential_version": credential_version_id,
                    "checksum": "b" * 64,
                    "actor": execution.actor_id,
                },
            )
            replacement_id = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO workflow_credential_grants
                       (id,project_id,workflow_version_id,slot_id,
                        credential_scope,credential_id,credential_version_id,
                        payload_schema_checksum,status,revision,granted_by)
                       VALUES (:id,:project,:version,'http.auth','project',
                               :credential,:credential_version,:checksum,
                               'active',1,:actor)"""
                ),
                {
                    "id": replacement_id,
                    "project": execution.project_id,
                    "version": execution.version_id,
                    "credential": credential_id,
                    "credential_version": credential_version_id,
                    "checksum": slot_checksum,
                    "actor": execution.actor_id,
                },
            )
            assert (
                await connection.scalar(
                    text(
                        """SELECT count(*) FROM workflow_credential_grants
                           WHERE workflow_version_id=:version
                             AND slot_id='http.auth' AND status='active'"""
                    ),
                    {"version": execution.version_id},
                )
                == 1
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_workflow_job_epoch_trace_profile_and_worker_admission_are_fenced(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            execution = await _seed_workflow_execution(
                connection,
                job_status="queued",
                with_attempt=False,
            )

            await _expect_integrity_error(
                connection,
                """INSERT INTO jobs
                   (id,job_type,project_id,owner_user_id,workflow_run_id,
                    workflow_epoch,required_worker_profile_digest,
                    workflow_profile_key,origin_trace_id,idempotency_key,
                    max_attempts)
                   VALUES (:id,'workflow_run',:project,:actor,:run,1,:profile,
                           :profile_key,'wrong-trace',:key,1)""",
                {
                    "id": uuid.uuid4(),
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "profile": execution.profile_digest,
                    "profile_key": execution.profile_digest,
                    "key": "5" * 64,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO jobs
                   (id,job_type,project_id,owner_user_id,run_id,workflow_run_id,
                    workflow_epoch,required_worker_profile_digest,
                    workflow_profile_key,origin_trace_id,idempotency_key,
                    max_attempts)
                   VALUES (:id,'workflow_run',:project,:actor,'hidden-chat-run',
                           :run,1,:profile,:profile_key,:trace,:key,1)""",
                {
                    "id": uuid.uuid4(),
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "profile": execution.profile_digest,
                    "profile_key": execution.profile_digest,
                    "trace": execution.origin_trace_id,
                    "key": "6" * 64,
                },
            )

            await _expect_integrity_error(
                connection,
                """INSERT INTO jobs
                   (id,job_type,project_id,owner_user_id,workflow_run_id,
                    workflow_epoch,required_worker_profile_digest,
                    workflow_profile_key,origin_trace_id,idempotency_key,
                    max_attempts)
                   VALUES (:id,'workflow_run',:project,:actor,:run,2,
                           :profile,:profile_key,:trace,:key,1)""",
                {
                    "id": uuid.uuid4(),
                    "run": execution.run_id,
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "profile": "7" * 64,
                    "profile_key": "7" * 64,
                    "trace": execution.origin_trace_id,
                    "key": "7" * 64,
                },
            )

            stale_future_job_id = uuid.uuid4()
            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """INSERT INTO jobs
                               (id,job_type,project_id,owner_user_id,
                                workflow_run_id,workflow_epoch,
                                required_worker_profile_digest,
                                workflow_profile_key,origin_trace_id,
                                idempotency_key,max_attempts)
                               VALUES (:id,'workflow_run',:project,:actor,
                                       :run,2,:profile,:profile_key,:trace,
                                       :key,1)"""
                        ),
                        {
                            "id": stale_future_job_id,
                            "project": execution.project_id,
                            "actor": execution.actor_id,
                            "run": execution.run_id,
                            "profile": execution.profile_digest,
                            "profile_key": execution.profile_digest,
                            "trace": execution.origin_trace_id,
                            "key": stale_future_job_id.hex * 2,
                        },
                    )
                    await connection.execute(
                        text(
                            """INSERT INTO workflow_run_jobs
                               (workflow_run_id,execution_epoch,job_id,
                                project_id,owner_user_id,worker_profile_key,
                                cause)
                               VALUES (:run,2,:job,:project,:actor,:profile,
                                       'resume')"""
                        ),
                        {
                            "run": execution.run_id,
                            "job": stale_future_job_id,
                            "project": execution.project_id,
                            "actor": execution.actor_id,
                            "profile": execution.profile_digest,
                        },
                    )
                    await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

            resume_job_id = uuid.uuid4()
            await connection.execute(
                text(
                    """UPDATE jobs
                       SET status='cancelled',completed_at=now()
                       WHERE id=:job"""
                ),
                {"job": execution.job_id},
            )
            await connection.execute(
                text(
                    """UPDATE workflow_runs
                       SET execution_epoch=2,current_job_id=NULL
                       WHERE id=:run"""
                ),
                {"run": execution.run_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO jobs
                       (id,job_type,project_id,owner_user_id,workflow_run_id,
                        workflow_epoch,required_worker_profile_digest,
                        workflow_profile_key,origin_trace_id,idempotency_key,
                        max_attempts)
                       VALUES (:id,'workflow_run',:project,:actor,:run,2,
                               :profile,:profile_key,:trace,:key,1)"""
                ),
                {
                    "id": resume_job_id,
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "profile": execution.profile_digest,
                    "profile_key": execution.profile_digest,
                    "trace": execution.origin_trace_id,
                    "key": "8" * 64,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_jobs
                       (workflow_run_id,execution_epoch,job_id,project_id,
                        owner_user_id,worker_profile_key,cause)
                       VALUES (:run,2,:job,:project,:actor,:profile,'resume')"""
                ),
                {
                    "run": execution.run_id,
                    "job": resume_job_id,
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "profile": execution.profile_digest,
                },
            )
            await connection.execute(
                text(
                    """UPDATE workflow_runs SET current_job_id=:job
                       WHERE id=:run"""
                ),
                {"run": execution.run_id, "job": resume_job_id},
            )
            assert (
                await connection.scalar(
                    text(
                        """SELECT count(*) FROM workflow_run_jobs
                           WHERE workflow_run_id=:run"""
                    ),
                    {"run": execution.run_id},
                )
                == 2
            )

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            claim = await JobRepository(session).claim_next(
                worker_id=execution.worker_id,
                capabilities=frozenset({"workflow_run"}),
                lease_seconds=30,
            )
        assert claim is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_attested_profile_digest_array_is_canonical(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            for profiles in (
                {"not": "an array"},
                ["not-a-digest"],
                ["a" * 64, "a" * 64],
                ["B" * 64],
                [1],
            ):
                await _expect_integrity_error(
                    connection,
                    """INSERT INTO worker_nodes
                       (id,version,capabilities_json,
                        runtime_profile_digests_json,max_concurrent_jobs)
                       VALUES (:id,'g10-invalid','[]',CAST(:profiles AS jsonb),1)""",
                    {"id": uuid.uuid4(), "profiles": json.dumps(profiles)},
                )

            valid_worker = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO worker_nodes
                       (id,version,capabilities_json,
                        runtime_profile_digests_json,max_concurrent_jobs)
                       VALUES (:id,'g10-valid','[]',CAST(:profiles AS jsonb),1)"""
                ),
                {
                    "id": valid_worker,
                    "profiles": json.dumps(["a" * 64, "b" * 64]),
                },
            )
            assert await connection.scalar(
                text(
                    """SELECT runtime_profile_digests_json
                           FROM worker_nodes WHERE id=:id"""
                ),
                {"id": valid_worker},
            ) == ["a" * 64, "b" * 64]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_snapshots_bind_exact_version_profile_and_runtime_policy_value(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            execution = await _seed_workflow_execution(connection)
            policy = (
                (
                    await connection.execute(
                        text(
                            """SELECT p.current_version_id,p.revision,
                                  v.schema_version,v.payload_checksum,v.value
                           FROM system_runtime_policies p
                           JOIN system_runtime_policy_versions v
                             ON v.section=p.section
                            AND v.id=p.current_version_id
                           WHERE p.section='workflow_runtime'"""
                        )
                    )
                )
                .mappings()
                .one()
            )

            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """INSERT INTO workflow_run_runtime_policy_snapshots
                               (workflow_run_id,project_id,owner_user_id,section,
                                policy_version_id,revision,schema_version,
                                payload_checksum,value_json)
                               VALUES (:run,:project,:actor,'workflow_runtime',
                                       :version,:revision,:schema_version,
                                       :checksum,'{}')"""
                        ),
                        {
                            "run": execution.run_id,
                            "project": execution.project_id,
                            "actor": execution.actor_id,
                            "version": policy["current_version_id"],
                            "revision": policy["revision"],
                            "schema_version": policy["schema_version"],
                            "checksum": policy["payload_checksum"],
                        },
                    )
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_runtime_policy_snapshots
                       (workflow_run_id,project_id,owner_user_id,section,
                        policy_version_id,revision,schema_version,
                        payload_checksum,value_json)
                       VALUES (:run,:project,:actor,'workflow_runtime',
                               :version,:revision,:schema_version,:checksum,
                               CAST(:value AS jsonb))"""
                ),
                {
                    "run": execution.run_id,
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "version": policy["current_version_id"],
                    "revision": policy["revision"],
                    "schema_version": policy["schema_version"],
                    "checksum": policy["payload_checksum"],
                    "value": json.dumps(policy["value"]),
                },
            )

            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_run_snapshots
                   (workflow_run_id,project_id,owner_user_id,
                    workflow_version_id,graph_schema_version,
                    compiler_contract_version,semantic_checksum,
                    catalog_generation,required_worker_profile_digest,
                    snapshot_checksum)
                   VALUES (:run,:project,:actor,:version,1,1,:semantic,
                           :generation,:wrong_profile,:snapshot)""",
                {
                    "run": execution.run_id,
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "version": execution.version_id,
                    "semantic": "1" * 64,
                    "generation": "5" * 64,
                    "wrong_profile": "7" * 64,
                    "snapshot": "6" * 64,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_snapshots
                       (workflow_run_id,project_id,owner_user_id,
                        workflow_version_id,graph_schema_version,
                        compiler_contract_version,semantic_checksum,
                        catalog_generation,required_worker_profile_digest,
                        snapshot_checksum)
                       VALUES (:run,:project,:actor,:version,1,1,:semantic,
                               :generation,:profile,:snapshot)"""
                ),
                {
                    "run": execution.run_id,
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "version": execution.version_id,
                    "semantic": "1" * 64,
                    "generation": "5" * 64,
                    "profile": execution.profile_digest,
                    "snapshot": "6" * 64,
                },
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_workflow_model_snapshot_credential_closure_is_exact(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            execution = await _seed_workflow_execution(connection)

            model_id = uuid.uuid4()
            model_version_id = uuid.uuid4()
            node_without_credential = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO system_model_configs
                       (id,logical_name,display_name,status,revision,
                        created_by_user_id,updated_by_user_id)
                       VALUES (:id,:name,'Workflow Test Model','active',1,
                               :actor,:actor)"""
                ),
                {
                    "id": model_id,
                    "name": f"workflow-test-{model_id.hex[:8]}",
                    "actor": execution.actor_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO system_model_config_versions
                       (id,model_config_id,version_number,provider_adapter,
                        provider_model,settings,payload_checksum,
                        created_by_user_id)
                       VALUES (:id,:model,1,'openai_compatible','test-model','{}',
                               :checksum,:actor)"""
                ),
                {
                    "id": model_version_id,
                    "model": model_id,
                    "checksum": "a" * 64,
                    "actor": execution.actor_id,
                },
            )
            await connection.execute(
                text(
                    """UPDATE system_model_configs
                       SET current_version_id=:version WHERE id=:model"""
                ),
                {"model": model_id, "version": model_version_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_version_model_refs
                       (workflow_version_id,project_id,node_id,
                        logical_model_name,purpose)
                       VALUES (:version,:project,:node,:name,'primary')"""
                ),
                {
                    "version": execution.version_id,
                    "project": execution.project_id,
                    "node": node_without_credential,
                    "name": f"workflow-test-{model_id.hex[:8]}",
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_model_snapshots
                       (workflow_run_id,project_id,owner_user_id,
                        workflow_version_id,node_id,purpose,logical_model_name,
                        model_config_id,model_config_version_id,payload_checksum)
                       VALUES (:run,:project,:actor,:workflow_version,:node,
                               'primary',:name,:model,:model_version,:checksum)"""
                ),
                {
                    "run": execution.run_id,
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "workflow_version": execution.version_id,
                    "node": node_without_credential,
                    "name": f"workflow-test-{model_id.hex[:8]}",
                    "model": model_id,
                    "model_version": model_version_id,
                    "checksum": "a" * 64,
                },
            )

            incomplete_node = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO workflow_version_model_refs
                       (workflow_version_id,project_id,node_id,
                        logical_model_name,purpose)
                       VALUES (:version,:project,:node,:name,'repair')"""
                ),
                {
                    "version": execution.version_id,
                    "project": execution.project_id,
                    "node": incomplete_node,
                    "name": f"workflow-test-{model_id.hex[:8]}",
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_run_model_snapshots
                   (workflow_run_id,project_id,owner_user_id,
                    workflow_version_id,node_id,purpose,logical_model_name,
                    model_config_id,model_config_version_id,payload_checksum,
                    credential_id)
                   VALUES (:run,:project,:actor,:workflow_version,:node,
                           'repair',:name,:model,:model_version,:checksum,
                           :credential)""",
                {
                    "run": execution.run_id,
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "workflow_version": execution.version_id,
                    "node": incomplete_node,
                    "name": f"workflow-test-{model_id.hex[:8]}",
                    "model": model_id,
                    "model_version": model_version_id,
                    "checksum": "a" * 64,
                    "credential": uuid.uuid4(),
                },
            )

            credential_id = uuid.uuid4()
            credential_version_id = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO credentials
                       (id,scope,project_id,name,display_name,credential_type,
                        status,is_delete,version,created_by_user_id)
                       VALUES (:id,'project',:project,:name,'Model key','api_key',
                               'active',false,1,:actor)"""
                ),
                {
                    "id": credential_id,
                    "project": execution.project_id,
                    "name": f"model-key-{credential_id.hex[:8]}",
                    "actor": execution.actor_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO credential_versions
                       (id,credential_id,version_number,status,
                        payload_schema_version,payload_schema,created_by_user_id)
                       VALUES (:id,:credential,1,'active',1,'{}',:actor)"""
                ),
                {
                    "id": credential_version_id,
                    "credential": credential_id,
                    "actor": execution.actor_id,
                },
            )
            await connection.execute(
                text(
                    """UPDATE credentials SET current_version_id=:version
                       WHERE id=:credential"""
                ),
                {"version": credential_version_id, "credential": credential_id},
            )

            secured_model_id = uuid.uuid4()
            secured_model_version_id = uuid.uuid4()
            secured_model_name = f"workflow-secured-{secured_model_id.hex[:8]}"
            await connection.execute(
                text(
                    """INSERT INTO system_model_configs
                       (id,logical_name,display_name,status,revision,
                        created_by_user_id,updated_by_user_id)
                       VALUES (:id,:name,'Workflow Secured Model','active',1,
                               :actor,:actor)"""
                ),
                {
                    "id": secured_model_id,
                    "name": secured_model_name,
                    "actor": execution.actor_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO system_model_config_versions
                       (id,model_config_id,version_number,provider_adapter,
                        provider_model,settings,credential_id,
                        credential_version_id,credential_env_key,
                        payload_checksum,created_by_user_id)
                       VALUES (:id,:model,1,'openai_compatible','secured-model',
                               '{}',:credential,:credential_version,'API_KEY',
                               :checksum,:actor)"""
                ),
                {
                    "id": secured_model_version_id,
                    "model": secured_model_id,
                    "credential": credential_id,
                    "credential_version": credential_version_id,
                    "checksum": "b" * 64,
                    "actor": execution.actor_id,
                },
            )
            await connection.execute(
                text(
                    """UPDATE system_model_configs
                       SET current_version_id=:version WHERE id=:model"""
                ),
                {
                    "model": secured_model_id,
                    "version": secured_model_version_id,
                },
            )
            secured_node = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO workflow_version_model_refs
                       (workflow_version_id,project_id,node_id,
                        logical_model_name,purpose)
                       VALUES (:version,:project,:node,:name,'secondary')"""
                ),
                {
                    "version": execution.version_id,
                    "project": execution.project_id,
                    "node": secured_node,
                    "name": secured_model_name,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_run_model_snapshots
                   (workflow_run_id,project_id,owner_user_id,
                    workflow_version_id,node_id,purpose,logical_model_name,
                    model_config_id,model_config_version_id,payload_checksum)
                   VALUES (:run,:project,:actor,:workflow_version,:node,
                           'secondary',:name,:model,:model_version,:checksum)""",
                {
                    "run": execution.run_id,
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "workflow_version": execution.version_id,
                    "node": secured_node,
                    "name": secured_model_name,
                    "model": secured_model_id,
                    "model_version": secured_model_version_id,
                    "checksum": "b" * 64,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_model_snapshots
                       (workflow_run_id,project_id,owner_user_id,
                        workflow_version_id,node_id,purpose,logical_model_name,
                        model_config_id,model_config_version_id,payload_checksum,
                        credential_id,credential_version_id,credential_env_key)
                       VALUES (:run,:project,:actor,:workflow_version,:node,
                               'secondary',:name,:model,:model_version,:checksum,
                               :credential,:credential_version,'API_KEY')"""
                ),
                {
                    "run": execution.run_id,
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "workflow_version": execution.version_id,
                    "node": secured_node,
                    "name": secured_model_name,
                    "model": secured_model_id,
                    "model_version": secured_model_version_id,
                    "checksum": "b" * 64,
                    "credential": credential_id,
                    "credential_version": credential_version_id,
                },
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_workflow_run_lifecycle_shape_and_cancel_before_start(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            queued = await _seed_workflow_execution(
                connection,
                job_status="queued",
                with_attempt=False,
                profile_digest=None,
            )
            running = await _seed_workflow_execution(connection)

        async with engine.begin() as connection:
            queued_row = (
                await connection.execute(
                    text(
                        """SELECT status,current_job_id,started_at,completed_at
                           FROM workflow_runs WHERE id=:run"""
                    ),
                    {"run": queued.run_id},
                )
            ).one()
            assert queued_row.status == "queued"
            assert queued_row.current_job_id == queued.job_id
            assert queued_row.started_at is None
            assert queued_row.completed_at is None

            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """UPDATE workflow_runs SET current_job_id=NULL
                               WHERE id=:run"""
                        ),
                        {"run": queued.run_id},
                    )
                    await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """UPDATE workflow_runs
                               SET status='cancelled',current_job_id=NULL,
                                   completed_at=now()
                               WHERE id=:run"""
                        ),
                        {"run": queued.run_id},
                    )
                    await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """UPDATE jobs
                               SET status='cancelled',completed_at=now()
                               WHERE id=:job"""
                        ),
                        {"job": queued.job_id},
                    )
                    await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """UPDATE workflow_run_jobs SET cause=cause
                               WHERE workflow_run_id=:run"""
                        ),
                        {"run": queued.run_id},
                    )

            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """DELETE FROM workflow_run_jobs
                               WHERE workflow_run_id=:run"""
                        ),
                        {"run": queued.run_id},
                    )

            await connection.execute(
                text(
                    """UPDATE jobs
                       SET status='cancelled',completed_at=now()
                       WHERE id=:job"""
                ),
                {"job": queued.job_id},
            )
            await connection.execute(
                text(
                    """UPDATE workflow_runs
                       SET status='cancelled',current_job_id=NULL,
                           completed_at=now(),error_code=NULL
                       WHERE id=:run"""
                ),
                {"run": queued.run_id},
            )
            cancelled = (
                await connection.execute(
                    text(
                        """SELECT status,started_at,completed_at
                           FROM workflow_runs WHERE id=:run"""
                    ),
                    {"run": queued.run_id},
                )
            ).one()
            assert cancelled.status == "cancelled"
            assert cancelled.started_at is None
            assert cancelled.completed_at is not None
            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """UPDATE workflow_runs SET execution_epoch=99
                               WHERE id=:run"""
                        ),
                        {"run": queued.run_id},
                    )
                    await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """UPDATE workflow_runs
                               SET status='running',completed_at=NULL,
                                   started_at=now()
                               WHERE id=:run"""
                        ),
                        {"run": queued.run_id},
                    )

            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """UPDATE workflow_runs
                               SET completed_at=started_at - interval '1 second'
                               WHERE id=:run"""
                        ),
                        {"run": running.run_id},
                    )
            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """UPDATE workflow_runs SET retry_of_run_id=id
                               WHERE id=:run"""
                        ),
                        {"run": running.run_id},
                    )
            await connection.execute(
                text(
                    """UPDATE jobs
                       SET status='succeeded',completed_at=now(),
                           lease_owner_id=NULL,lease_token_hash=NULL,
                           lease_expires_at=NULL,heartbeat_at=NULL
                       WHERE id=:job"""
                ),
                {"job": running.job_id},
            )
            await connection.execute(
                text(
                    """UPDATE workflow_runs
                       SET status='succeeded',output_json='{}',completed_at=now(),
                           current_job_id=NULL,error_code=NULL
                       WHERE id=:run"""
                ),
                {"run": running.run_id},
            )
            assert (
                await connection.scalar(
                    text("SELECT status FROM workflow_runs WHERE id=:run"),
                    {"run": running.run_id},
                )
                == "succeeded"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_workflow_run_state_transition_matrix_and_admission_identity_are_immutable(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url, poolclass=NullPool)

    async def finish_run(
        connection: AsyncConnection,
        execution: _WorkflowExecution,
        terminal_status: str,
    ) -> None:
        job_status = "succeeded" if terminal_status == "succeeded" else ("cancelled" if terminal_status == "cancelled" else "failed")
        await connection.execute(
            text(
                """UPDATE jobs
                   SET status=:job_status,completed_at=clock_timestamp(),
                       lease_owner_id=NULL,lease_token_hash=NULL,
                       lease_expires_at=NULL,heartbeat_at=NULL
                   WHERE id=:job"""
            ),
            {"job": execution.job_id, "job_status": job_status},
        )
        await connection.execute(
            text(
                """UPDATE workflow_runs
                   SET status=CAST(:status AS varchar),current_job_id=NULL,
                       started_at=CASE
                           WHEN CAST(:status AS varchar)='cancelled' THEN started_at
                           ELSE COALESCE(started_at,created_at)
                       END,
                       completed_at=GREATEST(
                           clock_timestamp(),
                           COALESCE(started_at,created_at)
                       ),
                       output_json=CASE
                           WHEN CAST(:status AS varchar)='succeeded'
                               THEN '{}'::jsonb
                           ELSE NULL
                       END,
                       error_code=CASE
                           WHEN CAST(:status AS varchar)='failed'
                               THEN 'WORKFLOW_TEST_FAILURE'
                           WHEN CAST(:status AS varchar)='side_effect_unknown'
                               THEN 'WORKFLOW_SIDE_EFFECT_UNKNOWN'
                           ELSE NULL
                       END
                   WHERE id=:run"""
            ),
            {"run": execution.run_id, "status": terminal_status},
        )

    async def attempt_terminal_transition(
        connection: AsyncConnection,
        execution: _WorkflowExecution,
        target_status: str,
    ) -> None:
        if target_status in {"queued", "running"}:
            replacement_job_id = uuid.uuid4()
            next_epoch = 2
            await connection.execute(
                text(
                    """INSERT INTO jobs
                       (id,job_type,project_id,owner_user_id,workflow_run_id,
                        workflow_epoch,required_worker_profile_digest,
                        workflow_profile_key,origin_trace_id,idempotency_key,
                        status,max_attempts)
                       VALUES (:id,'workflow_run',:project,:actor,:run,
                               :epoch,:profile,:profile_key,:trace,:key,
                               'queued',1)"""
                ),
                {
                    "id": replacement_job_id,
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "epoch": next_epoch,
                    "profile": execution.profile_digest,
                    "profile_key": execution.profile_digest or _EMPTY_PROFILE_KEY,
                    "trace": execution.origin_trace_id,
                    "key": replacement_job_id.hex * 2,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_jobs
                       (workflow_run_id,execution_epoch,job_id,project_id,
                        owner_user_id,worker_profile_key,cause)
                       VALUES (:run,:epoch,:job,:project,:actor,:profile_key,
                               'resume')"""
                ),
                {
                    "run": execution.run_id,
                    "epoch": next_epoch,
                    "job": replacement_job_id,
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "profile_key": execution.profile_digest or _EMPTY_PROFILE_KEY,
                },
            )
            await connection.execute(
                text(
                    """UPDATE workflow_runs
                       SET status=CAST(:status AS varchar),execution_epoch=:epoch,
                           current_job_id=:job,
                           started_at=CASE
                               WHEN CAST(:status AS varchar)='running'
                                   THEN created_at
                               ELSE NULL
                           END,
                           completed_at=NULL,output_json=NULL,error_code=NULL
                       WHERE id=:run"""
                ),
                {
                    "run": execution.run_id,
                    "status": target_status,
                    "epoch": next_epoch,
                    "job": replacement_job_id,
                },
            )
            return

        await connection.execute(
            text(
                """UPDATE workflow_runs
                   SET status=CAST(:status AS varchar),current_job_id=NULL,
                       started_at=CASE
                           WHEN CAST(:status AS varchar)='cancelled' THEN started_at
                           ELSE COALESCE(started_at,created_at)
                       END,
                       completed_at=GREATEST(
                           clock_timestamp(),
                           COALESCE(started_at,created_at)
                       ),
                       output_json=CASE
                           WHEN CAST(:status AS varchar)='succeeded'
                               THEN '{}'::jsonb
                           ELSE NULL
                       END,
                       error_code=CASE
                           WHEN CAST(:status AS varchar)='failed'
                               THEN 'WORKFLOW_TEST_FAILURE'
                           WHEN CAST(:status AS varchar)='side_effect_unknown'
                               THEN 'WORKFLOW_SIDE_EFFECT_UNKNOWN'
                           ELSE NULL
                       END
                   WHERE id=:run"""
            ),
            {"run": execution.run_id, "status": target_status},
        )

    try:
        async with engine.begin() as connection:
            queued_to_running = await _seed_workflow_execution(
                connection,
                job_status="queued",
                with_attempt=False,
            )
            await connection.execute(
                text(
                    """UPDATE workflow_runs
                       SET status='running',started_at=now()
                       WHERE id=:run"""
                ),
                {"run": queued_to_running.run_id},
            )

            queued_to_cancelled = await _seed_workflow_execution(
                connection,
                job_status="queued",
                with_attempt=False,
            )
            await finish_run(connection, queued_to_cancelled, "cancelled")

            for terminal_status in (
                "succeeded",
                "failed",
                "cancelled",
                "side_effect_unknown",
            ):
                running = await _seed_workflow_execution(connection)
                await finish_run(connection, running, terminal_status)

            for forbidden_status in (
                "succeeded",
                "failed",
                "side_effect_unknown",
            ):
                queued = await _seed_workflow_execution(
                    connection,
                    job_status="queued",
                    with_attempt=False,
                )
                with pytest.raises(DBAPIError):
                    async with connection.begin_nested():
                        await finish_run(connection, queued, forbidden_status)
                        await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

            running_to_queued = await _seed_workflow_execution(connection)
            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """UPDATE workflow_runs
                               SET status='queued',started_at=NULL
                               WHERE id=:run"""
                        ),
                        {"run": running_to_queued.run_id},
                    )
                    await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

            statuses = (
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                "side_effect_unknown",
            )
            for source_status in (
                "succeeded",
                "failed",
                "cancelled",
                "side_effect_unknown",
            ):
                source = await _seed_workflow_execution(
                    connection,
                    job_status="queued" if source_status == "cancelled" else "running",
                    with_attempt=source_status != "cancelled",
                )
                await finish_run(connection, source, source_status)
                settlement_update = {
                    "succeeded": (
                        "output_json=CAST(:output AS jsonb)",
                        {"output": json.dumps({"changed": True})},
                    ),
                    "failed": (
                        "error_code='WORKFLOW_CHANGED_FAILURE'",
                        {},
                    ),
                    "cancelled": (
                        "completed_at=completed_at + interval '1 second'",
                        {},
                    ),
                    "side_effect_unknown": (
                        "error_code='WORKFLOW_CHANGED_UNKNOWN'",
                        {},
                    ),
                }[source_status]
                with pytest.raises(DBAPIError):
                    async with connection.begin_nested():
                        await connection.execute(
                            text(
                                f"""UPDATE workflow_runs
                                    SET {settlement_update[0]}
                                    WHERE id=:run"""
                            ),
                            {"run": source.run_id, **settlement_update[1]},
                        )
                        await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                for target_status in statuses:
                    if target_status == source_status:
                        continue
                    with pytest.raises(DBAPIError):
                        async with connection.begin_nested():
                            await attempt_terminal_transition(
                                connection,
                                source,
                                target_status,
                            )
                            await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

            immutable_run = await _seed_workflow_execution(connection)
            alternate_version_id = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO workflow_versions
                       (id,workflow_id,project_id,version_number,spec_json,
                        canvas_json,semantic_checksum,
                        compiler_contract_version,published_by)
                       VALUES (:id,:workflow,:project,2,'{}','{}',:checksum,
                               1,:actor)"""
                ),
                {
                    "id": alternate_version_id,
                    "workflow": immutable_run.workflow_id,
                    "project": immutable_run.project_id,
                    "checksum": "c" * 64,
                    "actor": immutable_run.actor_id,
                },
            )
            alternate_workflow_id = uuid.uuid4()
            alternate_workflow_version_id = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO workflow_definitions
                       (id,project_id,name,status,revision,created_by,updated_by)
                       VALUES (:id,:project,:name,'active',1,:actor,:actor)"""
                ),
                {
                    "id": alternate_workflow_id,
                    "project": immutable_run.project_id,
                    "name": f"Alternate {alternate_workflow_id.hex[:12]}",
                    "actor": immutable_run.actor_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_versions
                       (id,workflow_id,project_id,version_number,spec_json,
                        canvas_json,semantic_checksum,
                        compiler_contract_version,published_by)
                       VALUES (:id,:workflow,:project,1,'{}','{}',:checksum,
                               1,:actor)"""
                ),
                {
                    "id": alternate_workflow_version_id,
                    "workflow": alternate_workflow_id,
                    "project": immutable_run.project_id,
                    "checksum": "9" * 64,
                    "actor": immutable_run.actor_id,
                },
            )
            await connection.execute(
                text(
                    """UPDATE workflow_definitions
                       SET current_published_version_id=:version
                       WHERE id=:workflow"""
                ),
                {
                    "workflow": alternate_workflow_id,
                    "version": alternate_workflow_version_id,
                },
            )
            immutable_updates = (
                (
                    "workflow",
                    "workflow_id=:workflow,workflow_version_id=:version",
                    {
                        "workflow": alternate_workflow_id,
                        "version": alternate_workflow_version_id,
                    },
                ),
                (
                    "input",
                    "input_json=CAST(:input_json AS jsonb),input_digest=:input_digest",
                    {
                        "input_json": json.dumps({"changed": True}),
                        "input_digest": "d" * 64,
                    },
                ),
                (
                    "version",
                    "workflow_version_id=:version",
                    {"version": alternate_version_id},
                ),
                (
                    "trace",
                    "origin_trace_id='changed-server-trace'",
                    {},
                ),
                (
                    "idempotency",
                    "idempotency_hash=:idempotency_hash",
                    {"idempotency_hash": "e" * 64},
                ),
                (
                    "profile",
                    "required_worker_profile_digest=:profile,worker_profile_key=:profile",
                    {"profile": "f" * 64},
                ),
                (
                    "trigger",
                    "trigger_kind='api',trigger_ref='changed-trigger-ref'",
                    {},
                ),
            )
            for _label, assignments, params in immutable_updates:
                with pytest.raises(DBAPIError):
                    async with connection.begin_nested():
                        await connection.execute(
                            text(
                                f"""UPDATE workflow_runs SET {assignments}
                                    WHERE id=:run"""
                            ),
                            {"run": immutable_run.run_id, **params},
                        )
                        await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_effect_and_code_lease_state_uniqueness_and_attempt_fences(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            execution = await _seed_workflow_execution(connection)
            effect_node = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO workflow_node_effects
                       (id,project_id,owner_user_id,workflow_run_id,node_id,
                        activation_key,operation_key,http_method,status,
                        request_hmac,provider_idempotency_key)
                       VALUES (:id,:project,:actor,:run,:node,'activation-1',
                               :operation,'POST','prepared',:request_hmac,
                               :provider_key)"""
                ),
                {
                    "id": uuid.uuid4(),
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "node": effect_node,
                    "operation": "a" * 64,
                    "request_hmac": "b" * 64,
                    "provider_key": "c" * 64,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_node_effects
                   (id,project_id,owner_user_id,workflow_run_id,node_id,
                    activation_key,operation_key,http_method,status,
                    request_hmac,provider_idempotency_key)
                   VALUES (:id,:project,:actor,:run,:node,'activation-1',
                           :operation,'POST','prepared',:request_hmac,
                           :provider_key)""",
                {
                    "id": uuid.uuid4(),
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "node": effect_node,
                    "operation": "d" * 64,
                    "request_hmac": "e" * 64,
                    "provider_key": "f" * 64,
                },
            )

            for index, invalid_outcome in enumerate(
                (
                    {"kind": "success"},
                    {"kind": "http_error"},
                    {"kind": "response_invalid"},
                    {"kind": "unknown"},
                ),
                start=1,
            ):
                await _expect_integrity_error(
                    connection,
                    """INSERT INTO workflow_node_effects
                       (id,project_id,owner_user_id,workflow_run_id,node_id,
                        activation_key,operation_key,http_method,status,
                        request_hmac,provider_idempotency_key,dispatch_job_id,
                        dispatch_execution_epoch,dispatch_attempt,
                        dispatch_started_at,outcome_json,outcome_digest)
                       VALUES (:id,:project,:actor,:run,:node,:activation,
                               :operation,'POST','settled',:request_hmac,
                               :provider_key,:job,1,1,now(),
                               CAST(:outcome AS jsonb),:digest)""",
                    {
                        "id": uuid.uuid4(),
                        "project": execution.project_id,
                        "actor": execution.actor_id,
                        "run": execution.run_id,
                        "node": uuid.uuid4(),
                        "activation": f"invalid-settled-{index}",
                        "operation": f"{index}" * 64,
                        "request_hmac": f"{index + 1}" * 64,
                        "provider_key": f"{index + 2}" * 64,
                        "job": execution.job_id,
                        "outcome": json.dumps(invalid_outcome),
                        "digest": f"{index + 3}" * 64,
                    },
                )

            success_outcome = {
                "kind": "success",
                "response": {
                    "status_code": 200,
                    "headers": [],
                    "body": {"kind": "empty"},
                    "duration_ms": 1,
                    "wire_byte_count": {"value": 0, "relation": "exact"},
                    "decoded_byte_count": {"value": 0, "relation": "exact"},
                    "retained_body_byte_count": 0,
                },
            }
            await connection.execute(
                text(
                    """INSERT INTO workflow_node_effects
                       (id,project_id,owner_user_id,workflow_run_id,node_id,
                        activation_key,operation_key,http_method,status,
                        request_hmac,provider_idempotency_key,dispatch_job_id,
                        dispatch_execution_epoch,dispatch_attempt,
                        dispatch_started_at,outcome_json,outcome_digest)
                       VALUES (:id,:project,:actor,:run,:node,'valid-settled',
                               :operation,'POST','settled',:request_hmac,
                               :provider_key,:job,1,1,now(),
                               CAST(:outcome AS jsonb),:digest)"""
                ),
                {
                    "id": uuid.uuid4(),
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "node": uuid.uuid4(),
                    "operation": "5" * 64,
                    "request_hmac": "6" * 64,
                    "provider_key": "7" * 64,
                    "job": execution.job_id,
                    "outcome": json.dumps(success_outcome),
                    "digest": "8" * 64,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_node_effects
                   (id,project_id,owner_user_id,workflow_run_id,node_id,
                    activation_key,operation_key,http_method,status,
                    request_hmac,provider_idempotency_key,dispatch_job_id,
                    dispatch_execution_epoch,dispatch_attempt,
                    dispatch_started_at,outcome_json,outcome_digest)
                   VALUES (:id,:project,:actor,:run,:node,'wrong-attempt',
                           :operation,'POST','settled',:request_hmac,
                           :provider_key,:job,1,2,now(),
                           CAST(:outcome AS jsonb),:digest)""",
                {
                    "id": uuid.uuid4(),
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "node": uuid.uuid4(),
                    "operation": "9" * 64,
                    "request_hmac": "a" * 64,
                    "provider_key": "b" * 64,
                    "job": execution.job_id,
                    "outcome": json.dumps(success_outcome),
                    "digest": "c" * 64,
                },
            )

            lease_node = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO workflow_code_sandbox_leases
                       (id,project_id,owner_user_id,workflow_run_id,node_id,
                        activation_id,activation_attempt,job_id,workflow_epoch,
                        job_attempt_number,worker_id,reconciliation_key_hash,
                        profile_digest,state,execution_lease_token_hash,
                        cleanup_deadline)
                       VALUES (:id,:project,:actor,:run,:node,'code-activation',1,
                               :job,1,1,:worker,:reconciliation,:profile,
                               'provisioning',:lease_hash,
                               now()+interval '5 minutes')"""
                ),
                {
                    "id": uuid.uuid4(),
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "node": lease_node,
                    "job": execution.job_id,
                    "worker": execution.worker_id,
                    "reconciliation": "d" * 64,
                    "profile": execution.profile_digest,
                    "lease_hash": "e" * 64,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_code_sandbox_leases
                   (id,project_id,owner_user_id,workflow_run_id,node_id,
                    activation_id,activation_attempt,job_id,workflow_epoch,
                    job_attempt_number,worker_id,reconciliation_key_hash,
                    profile_digest,state,execution_lease_token_hash,
                    cleanup_deadline)
                   VALUES (:id,:project,:actor,:run,:node,'code-activation',2,
                           :job,1,1,:worker,:reconciliation,:profile,
                           'provisioning',:lease_hash,
                           now()+interval '5 minutes')""",
                {
                    "id": uuid.uuid4(),
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "node": lease_node,
                    "job": execution.job_id,
                    "worker": execution.worker_id,
                    "reconciliation": "f" * 64,
                    "profile": execution.profile_digest,
                    "lease_hash": "1" * 64,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_code_sandbox_leases
                   (id,project_id,owner_user_id,workflow_run_id,node_id,
                    activation_id,activation_attempt,job_id,workflow_epoch,
                    job_attempt_number,worker_id,reconciliation_key_hash,
                    profile_digest,state,execution_lease_token_hash,
                    cleanup_locator_ciphertext,cleanup_deadline)
                   VALUES (:id,:project,:actor,:run,:node,'locator-too-early',1,
                           :job,1,1,:worker,:reconciliation,:profile,
                           'provisioning',:lease_hash,:locator,
                           now()+interval '5 minutes')""",
                {
                    "id": uuid.uuid4(),
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "node": uuid.uuid4(),
                    "job": execution.job_id,
                    "worker": execution.worker_id,
                    "reconciliation": "2" * 64,
                    "profile": execution.profile_digest,
                    "lease_hash": "3" * 64,
                    "locator": b"must-not-exist-before-acquire",
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_code_sandbox_leases
                   (id,project_id,owner_user_id,workflow_run_id,node_id,
                    activation_id,activation_attempt,job_id,workflow_epoch,
                    job_attempt_number,worker_id,reconciliation_key_hash,
                    profile_digest,state,execution_lease_token_hash,
                    cleanup_deadline)
                   VALUES (:id,:project,:actor,:run,:node,'wrong-profile',1,
                           :job,1,1,:worker,:reconciliation,:profile,
                           'provisioning',:lease_hash,
                           now()+interval '5 minutes')""",
                {
                    "id": uuid.uuid4(),
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "node": uuid.uuid4(),
                    "job": execution.job_id,
                    "worker": execution.worker_id,
                    "reconciliation": "4" * 64,
                    "profile": "7" * 64,
                    "lease_hash": "5" * 64,
                },
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_workflow_events_enforce_closed_types_shapes_attempts_and_terminal(
    migrated_postgres_database_url: str,
) -> None:
    event_types = (
        "workflow.run.started",
        "workflow.node.queued",
        "workflow.node.started",
        "workflow.node.delta",
        "workflow.node.log",
        "workflow.node.completed",
        "workflow.node.failed",
        "workflow.run.completed",
        "workflow.run.failed",
        "workflow.run.cancelled",
        "workflow.run.side_effect_unknown",
    )
    engine = create_async_engine(migrated_postgres_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            for index, event_type in enumerate(event_types, start=1):
                execution = await _seed_workflow_execution(connection)
                is_node = event_type.startswith("workflow.node.")
                await connection.execute(
                    text(
                        """INSERT INTO workflow_run_events
                           (project_id,owner_user_id,workflow_run_id,
                            workflow_version_id,seq,
                            event_type,node_id,activation_id,iteration_path,
                            scope_path_hash,attempt,payload,occurred_at)
                           VALUES (:project,:actor,:run,:version,1,
                                   :event_type,:node,
                                   :activation,CAST(:iteration_path AS integer[]),
                                   :scope,:attempt,'{}',now())"""
                    ),
                    {
                        "project": execution.project_id,
                        "actor": execution.actor_id,
                        "run": execution.run_id,
                        "version": execution.version_id,
                        "event_type": event_type,
                        "node": uuid.uuid4() if is_node else None,
                        "activation": f"activation-{index}" if is_node else None,
                        "iteration_path": [1] if is_node else [],
                        "scope": "a" * 64 if is_node else None,
                        "attempt": 1 if is_node else None,
                    },
                )

            execution = await _seed_workflow_execution(connection)
            node_id = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_events
                       (project_id,owner_user_id,workflow_run_id,
                        workflow_version_id,seq,event_type,node_id,
                        activation_id,scope_path_hash,iteration_path,attempt,
                        payload,occurred_at)
                       VALUES (:project,:actor,:run,:version,1,
                               'workflow.node.started',:node,
                               'monotonic-attempt',:scope,ARRAY[1],2,'{}',now())"""
                ),
                {
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "version": execution.version_id,
                    "node": node_id,
                    "scope": "b" * 64,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_run_events
                   (project_id,owner_user_id,workflow_run_id,
                    workflow_version_id,seq,event_type,node_id,activation_id,
                    scope_path_hash,iteration_path,attempt,payload,occurred_at)
                   VALUES (:project,:actor,:run,:version,2,
                           'workflow.node.started',:node,'monotonic-attempt',
                           :scope,ARRAY[1],1,'{}',now())""",
                {
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "version": execution.version_id,
                    "node": node_id,
                    "scope": "b" * 64,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_run_events
                   (project_id,owner_user_id,workflow_run_id,
                    workflow_version_id,seq,event_type,node_id,activation_id,
                    scope_path_hash,iteration_path,attempt,payload,occurred_at)
                   VALUES (:project,:actor,:run,:version,2,
                           'workflow.node.started',:node,'monotonic-attempt',
                           :scope,ARRAY[0],3,'{}',now())""",
                {
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "version": execution.version_id,
                    "node": node_id,
                    "scope": "b" * 64,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_events
                       (project_id,owner_user_id,workflow_run_id,
                        workflow_version_id,seq,event_type,node_id,
                        activation_id,scope_path_hash,iteration_path,attempt,
                        payload,occurred_at)
                       VALUES (:project,:actor,:run,:version,2,
                               'workflow.node.completed',:node,
                               'monotonic-attempt',:scope,ARRAY[1],3,'{}',now())"""
                ),
                {
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "version": execution.version_id,
                    "node": node_id,
                    "scope": "b" * 64,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_events
                       (project_id,owner_user_id,workflow_run_id,
                        workflow_version_id,seq,event_type,node_id,
                        activation_id,scope_path_hash,iteration_path,attempt,
                        payload,occurred_at)
                       VALUES (:project,:actor,:run,:version,3,
                               'workflow.run.completed',NULL,NULL,NULL,'{}',
                               NULL,'{}',now())"""
                ),
                {
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "version": execution.version_id,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_run_events
                   (project_id,owner_user_id,workflow_run_id,
                    workflow_version_id,seq,event_type,node_id,activation_id,
                    scope_path_hash,iteration_path,attempt,payload,occurred_at)
                   VALUES (:project,:actor,:run,:version,4,
                           'workflow.run.failed',NULL,NULL,NULL,'{}',NULL,
                           '{}',now())""",
                {
                    "project": execution.project_id,
                    "actor": execution.actor_id,
                    "run": execution.run_id,
                    "version": execution.version_id,
                },
            )

            for invalid_kind, invalid_statement in (
                (
                    "unknown_type",
                    """VALUES (:project,:actor,:run,:version,1,
                               'workflow.node.future',:node,'bad-shape',
                               :scope,ARRAY[1],1,'{}',now())""",
                ),
                (
                    "run_with_node_identity",
                    """VALUES (:project,:actor,:run,:version,1,
                               'workflow.run.started',:node,'bad-shape',
                               :scope,ARRAY[1],1,'{}',now())""",
                ),
                (
                    "node_without_activation_identity",
                    """VALUES (:project,:actor,:run,:version,1,
                               'workflow.node.started',NULL,NULL,NULL,'{}',NULL,
                               '{}',now())""",
                ),
            ):
                invalid_run = await _seed_workflow_execution(connection)
                await _expect_integrity_error(
                    connection,
                    """INSERT INTO workflow_run_events
                       (project_id,owner_user_id,workflow_run_id,
                        workflow_version_id,seq,event_type,node_id,
                        activation_id,scope_path_hash,iteration_path,attempt,
                        payload,occurred_at)
                       """
                    + invalid_statement,
                    {
                        "project": invalid_run.project_id,
                        "actor": invalid_run.actor_id,
                        "run": invalid_run.run_id,
                        "version": invalid_run.version_id,
                        "node": uuid.uuid4(),
                        "scope": "c" * 64,
                        "kind": invalid_kind,
                    },
                )

            assert (
                await connection.scalar(
                    text(
                        """SELECT count(*) FROM workflow_run_event_invariants
                           WHERE workflow_run_id=:run"""
                    ),
                    {"run": execution.run_id},
                )
                == 3
            )
            assert (
                await connection.scalar(
                    text(
                        """SELECT count(*) FROM pg_inherits i
                           JOIN pg_class p ON p.oid=i.inhparent
                           WHERE p.relname='workflow_run_events'"""
                    )
                )
                >= 1
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_v9_to_current_upgrade_adds_workflow_policy_and_matches_fresh_catalog(
    postgres_admin_url: str,
) -> None:
    assert CURRENT_SCHEMA_REVISION == "full_schema_v12"
    async with temporary_postgres_database(postgres_admin_url) as database_url:
        await _execute_sql_batch(database_url, _baseline_sql())
        await asyncio.to_thread(_run_alembic_upgrade, database_url, "full_schema_v9")
        await _seed_complete_v9_runtime_catalog(database_url)
        legacy_worker_id = uuid.uuid4()
        legacy_engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with legacy_engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO worker_nodes
                           (id,version,capabilities_json,max_concurrent_jobs)
                           VALUES (:id,'v9-worker','[]',1)"""
                    ),
                    {"id": legacy_worker_id},
                )
        finally:
            await legacy_engine.dispose()
        before = {row["section"]: row["current_version_id"] for row in await _runtime_catalog_rows(database_url)}
        assert set(before) == {section.value for section in V9_RUNTIME_SECTIONS}

        result = await upgrade_postgres(database_url, assume_yes=True)
        assert result.from_revision == "full_schema_v9"
        assert result.to_revision == CURRENT_SCHEMA_REVISION
        assert result.applied is True

        after_rows = await _runtime_catalog_rows(database_url)
        after = {row["section"]: row for row in after_rows}
        assert set(after) == {section.value for section in RuntimePolicySection}
        for section, pointer in before.items():
            assert after[section]["current_version_id"] == pointer
        workflow = after[RuntimePolicySection.WORKFLOW_RUNTIME.value]
        assert workflow["current_version_id"] == WORKFLOW_RUNTIME_DEFAULT_POLICY_VERSION_ID
        assert workflow["revision"] == 1
        assert workflow["schema_version"] == 1
        assert workflow["payload_checksum"] == "4ca136425002aa3a3a2426b4687f2e8091b6e4c23bf1d4db88b952730e1431e4"
        assert workflow["value"]["enabled"] is False
        assert workflow["value"]["admission_enabled"] is False

        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                legacy_worker = (
                    await connection.execute(
                        text(
                            """SELECT runtime_profile_digests_json,
                                      workflow_runtime_policy_section,
                                      workflow_runtime_policy_version_id,
                                      workflow_runtime_policy_revision,
                                      workflow_runtime_policy_schema_version,
                                      workflow_runtime_policy_checksum
                                 FROM worker_nodes WHERE id=:id"""
                        ),
                        {"id": legacy_worker_id},
                    )
                ).one()
                assert legacy_worker.runtime_profile_digests_json == []
                assert all(value is None for value in legacy_worker[1:])
                assert await read_m7_catalog_signature(connection) == FINAL_M7_CATALOG_SIGNATURE
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_fresh_runtime_bootstrap_seeds_five_sections_with_fixed_workflow_v1(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        await bootstrap_schema(engine)
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(engine, expire_on_commit=False)
        assert await bootstrap_system_runtime_policies(factory) == 1
        async with factory() as session, session.begin():
            locked = await SystemRuntimePolicyService.lock_workflow_runtime_policy(session)
        assert locked.policy_version_id == WORKFLOW_RUNTIME_DEFAULT_POLICY_VERSION_ID
        assert locked.revision == 1
        assert locked.schema_version == 1
        assert locked.payload_checksum == ("4ca136425002aa3a3a2426b4687f2e8091b6e4c23bf1d4db88b952730e1431e4")
        assert locked.value.enabled is False
        assert locked.value.admission_enabled is False
        materialized = await SystemRuntimePolicyMaterializer(factory).materialize_workflow_runtime_current()
        assert type(materialized) is type(locked.value)
        assert materialized.enabled is False
        assert materialized.admission_enabled is False
        rows = await _runtime_catalog_rows(postgres_database_url)
        assert {row["section"] for row in rows} == {section.value for section in RuntimePolicySection}
        workflow = next(row for row in rows if row["section"] == RuntimePolicySection.WORKFLOW_RUNTIME.value)
        assert workflow["current_version_id"] == WORKFLOW_RUNTIME_DEFAULT_POLICY_VERSION_ID
        assert workflow["revision"] == 1
        assert workflow["schema_version"] == 1
        assert workflow["payload_checksum"] == "4ca136425002aa3a3a2426b4687f2e8091b6e4c23bf1d4db88b952730e1431e4"
        assert workflow["value"]["enabled"] is False
        assert workflow["value"]["admission_enabled"] is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_complete_runtime_catalog_is_validated_without_overwriting_admin_pointer(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url, poolclass=NullPool)
    replacement_id = uuid.uuid4()
    try:
        canonical = canonical_policy_payload(
            RuntimePolicySection.WORKFLOW_RUNTIME,
            default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME),
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO system_runtime_policy_versions
                       (id,section,version_number,schema_version,value,
                        payload_checksum,supersedes_version_id,created_by_user_id)
                       VALUES (:id,'workflow_runtime',2,1,CAST(:value AS jsonb),
                               :checksum,:supersedes,:actor)"""
                ),
                {
                    "id": replacement_id,
                    "value": json.dumps(canonical.value),
                    "checksum": canonical.checksum,
                    "supersedes": WORKFLOW_RUNTIME_DEFAULT_POLICY_VERSION_ID,
                    "actor": str(BUILTIN_MODEL_USER_ID),
                },
            )
            await connection.execute(
                text(
                    """UPDATE system_runtime_policies
                       SET current_version_id=:id,revision=2
                       WHERE section='workflow_runtime'"""
                ),
                {"id": replacement_id},
            )
            await connection.execute(
                text(
                    """UPDATE system_runtime_policy_catalog_state
                       SET revision=2 WHERE id=1"""
                )
            )

        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(engine, expire_on_commit=False)
        assert await bootstrap_system_runtime_policies(factory) == 2
        row = next(row for row in await _runtime_catalog_rows(migrated_postgres_database_url) if row["section"] == RuntimePolicySection.WORKFLOW_RUNTIME.value)
        assert row["current_version_id"] == replacement_id
        assert row["revision"] == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partial_or_pointer_drift_runtime_catalog_fails_closed(
    postgres_admin_url: str,
) -> None:
    for corruption in (
        "missing_section",
        "pointer_revision_drift",
        "unknown_schema",
        "checksum_drift",
    ):
        async with temporary_postgres_database(postgres_admin_url) as database_url:
            engine = create_async_engine(database_url, poolclass=NullPool)
            try:
                await bootstrap_schema(engine)
                from sqlalchemy.ext.asyncio import async_sessionmaker

                factory = async_sessionmaker(engine, expire_on_commit=False)
                if corruption == "missing_section":
                    await _seed_complete_v9_runtime_catalog(database_url)
                    assert len(await _runtime_catalog_rows(database_url)) == len(V9_RUNTIME_SECTIONS)
                else:
                    await bootstrap_system_runtime_policies(factory)
                    async with engine.begin() as connection:
                        if corruption == "pointer_revision_drift":
                            await connection.execute(
                                text(
                                    """UPDATE system_runtime_policies
                                       SET revision=2
                                       WHERE section='workflow_runtime'"""
                                )
                            )
                        else:
                            canonical = canonical_policy_payload(
                                RuntimePolicySection.WORKFLOW_RUNTIME,
                                default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME),
                            )
                            invalid_id = uuid.uuid4()
                            await connection.execute(
                                text(
                                    """INSERT INTO system_runtime_policy_versions
                                       (id,section,version_number,schema_version,value,
                                        payload_checksum,supersedes_version_id,
                                        created_by_user_id)
                                       VALUES (:id,'workflow_runtime',2,:schema_version,
                                               CAST(:value AS jsonb),:checksum,
                                               :supersedes,:actor)"""
                                ),
                                {
                                    "id": invalid_id,
                                    "schema_version": 99 if corruption == "unknown_schema" else canonical.schema_version,
                                    "value": json.dumps(canonical.value),
                                    "checksum": "f" * 64 if corruption == "checksum_drift" else canonical.checksum,
                                    "supersedes": WORKFLOW_RUNTIME_DEFAULT_POLICY_VERSION_ID,
                                    "actor": str(BUILTIN_MODEL_USER_ID),
                                },
                            )
                            await connection.execute(
                                text(
                                    """UPDATE system_runtime_policies
                                       SET current_version_id=:id,revision=2
                                       WHERE section='workflow_runtime'"""
                                ),
                                {"id": invalid_id},
                            )
                            await connection.execute(
                                text(
                                    """UPDATE system_runtime_policy_catalog_state
                                       SET revision=2 WHERE id=1"""
                                )
                            )
                with pytest.raises(SystemRuntimePolicyBootstrapConflict):
                    await bootstrap_system_runtime_policies(factory)
            finally:
                await engine.dispose()


async def _expect_integrity_error(
    connection: AsyncConnection,
    statement: str,
    params: dict[str, object],
) -> None:

    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(text(statement), params)
            await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
