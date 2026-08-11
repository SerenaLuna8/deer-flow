"""G05 disposable-PostgreSQL review of the high-risk Workflow DDL shapes.

This is deliberately not the production migration.  G10 owns the atomic ORM,
full-schema and Alembic change.  The prototype proves that the circular
WorkflowRun/Job fence, effect recovery shape, lease uniqueness, event ledger
and system-policy extension can be represented by the current PostgreSQL
target before that release unit is authored.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE_SQL = BACKEND_ROOT / "tests" / "fixtures" / "workflow_schema_g05_prototype.sql"
BASELINE_SNAPSHOT_PATH = BACKEND_ROOT / "migrations" / "baseline" / "full_schema_v5.sql"
WORKFLOW_NODE_EFFECTS_SQL = BACKEND_ROOT / "tests" / "fixtures" / "workflow_node_effects_g04.sql"
WORKFLOW_NODE_EFFECTS_MARKER = "-- WORKFLOW_NODE_EFFECTS_G04_DDL"
WORKFLOW_CODE_LEASES_SQL = BACKEND_ROOT / "tests" / "fixtures" / "workflow_code_sandbox_leases_g03.sql"
WORKFLOW_CODE_LEASES_MARKER = "-- WORKFLOW_CODE_SANDBOX_LEASES_G03_DDL"

pytestmark = pytest.mark.postgres


async def _apply_prototype(database_url: str) -> None:
    await _execute_sql_batch(database_url, _baseline_sql())
    await asyncio.to_thread(_run_alembic_upgrade, database_url, "full_schema_v9")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        payload = _prototype_payload()
        async with engine.connect() as connection:
            raw_connection = await connection.get_raw_connection()
            await raw_connection.driver_connection.execute(payload)
    finally:
        await engine.dispose()


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


def _prototype_payload() -> str:
    payload = PROTOTYPE_SQL.read_text(encoding="utf-8")
    if payload.count(WORKFLOW_NODE_EFFECTS_MARKER) != 1:
        raise RuntimeError("G05 Workflow effect DDL marker is invalid")
    if payload.count(WORKFLOW_CODE_LEASES_MARKER) != 1:
        raise RuntimeError("G05 Workflow Code lease DDL marker is invalid")
    payload = payload.replace(
        WORKFLOW_NODE_EFFECTS_MARKER,
        WORKFLOW_NODE_EFFECTS_SQL.read_text(encoding="utf-8"),
    )
    return payload.replace(
        WORKFLOW_CODE_LEASES_MARKER,
        WORKFLOW_CODE_LEASES_SQL.read_text(encoding="utf-8"),
    )


async def _expect_integrity_error(connection: AsyncConnection, statement: str, params: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(text(statement), params)


@pytest.mark.asyncio
async def test_current_head_and_disposable_prototype_are_explicit(
    postgres_database_url: str,
) -> None:
    assert CURRENT_SCHEMA_REVISION == "full_schema_v12"
    payload = _prototype_payload()
    assert "G05 DISPOSABLE PROTOTYPE" in payload
    assert "CREATE TABLE workflow_node_effects" in payload
    assert "uq_workflow_node_effects_activation" in payload
    assert "dispatch_lease_token_hash" in payload
    assert "reconciliation_key_hash" in payload
    assert "uq_workflow_code_leases_open_activation" in payload
    assert "UPDATE alembic_version" not in payload
    assert "full_schema_v10" not in payload
    assert "full_schema_v12" not in payload

    await _apply_prototype(postgres_database_url)

    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            marker = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert marker == "full_schema_v9"
            tables = set(
                (
                    await connection.execute(
                        text(
                            """SELECT tablename
                               FROM pg_tables
                               WHERE schemaname = current_schema()
                                 AND tablename LIKE 'workflow_%'"""
                        )
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()

    assert {
        "workflow_definitions",
        "workflow_versions",
        "workflow_runs",
        "workflow_run_jobs",
        "workflow_node_effects",
        "workflow_code_sandbox_leases",
        "workflow_run_event_invariants",
        "workflow_run_events",
        "workflow_run_events_default",
    } <= tables


@pytest.mark.asyncio
async def test_workflow_run_job_epoch_trace_and_authority_fences(
    postgres_database_url: str,
) -> None:
    await _apply_prototype(postgres_database_url)
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    actor_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    workflow_id = uuid.uuid4()
    version_id = uuid.uuid4()
    workflow_run_id = uuid.uuid4()
    job_id = uuid.uuid4()
    origin_trace_id = f"workflow-trace-{uuid.uuid4()}"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {"id": actor_id, "email": f"{actor_id}@example.com"},
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                       (id,slug,display_name,created_by_user_id)
                       VALUES (:id,:slug,'Workflow G05',:actor)"""
                ),
                {"id": project_id, "slug": f"workflow-g05-{project_id.hex[:12]}", "actor": actor_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                       (id,project_id,user_id,role,status,version)
                       VALUES (:id,:project,:actor,'admin','active',1)"""
                ),
                {"id": membership_id, "project": project_id, "actor": actor_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_definitions
                       (id,project_id,name,status,revision,created_by,updated_by)
                       VALUES (:id,:project,'G05 Workflow','active',1,:actor,:actor)"""
                ),
                {"id": workflow_id, "project": project_id, "actor": actor_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_versions
                       (id,workflow_id,version_number,spec_json,canvas_json,
                        semantic_checksum,compiler_contract_version,published_by)
                       VALUES (:id,:workflow,1,'{}','{}',:checksum,1,:actor)"""
                ),
                {"id": version_id, "workflow": workflow_id, "checksum": "a" * 64, "actor": actor_id},
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
                        status,input_json,input_digest,idempotency_hash,trigger_kind,
                        origin_trace_id,execution_epoch)
                       VALUES (:id,:project,:actor,:workflow,:version,'queued','{}',
                               :input_digest,:idempotency,'manual',:trace,1)"""
                ),
                {
                    "id": workflow_run_id,
                    "project": project_id,
                    "actor": actor_id,
                    "workflow": workflow_id,
                    "version": version_id,
                    "input_digest": "b" * 64,
                    "idempotency": "c" * 64,
                    "trace": origin_trace_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO jobs
                       (id,job_type,project_id,owner_user_id,workflow_run_id,
                        workflow_epoch,origin_trace_id,idempotency_key,max_attempts)
                       VALUES (:id,'workflow_run',:project,:actor,:run,1,:trace,
                               :idempotency,3)"""
                ),
                {
                    "id": job_id,
                    "project": project_id,
                    "actor": actor_id,
                    "run": workflow_run_id,
                    "trace": origin_trace_id,
                    "idempotency": "d" * 64,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_jobs
                       (workflow_run_id,execution_epoch,job_id,project_id,owner_user_id,cause)
                       VALUES (:run,1,:job,:project,:actor,'initial')"""
                ),
                {
                    "run": workflow_run_id,
                    "job": job_id,
                    "project": project_id,
                    "actor": actor_id,
                },
            )
            await connection.execute(
                text("UPDATE workflow_runs SET current_job_id=:job WHERE id=:run"),
                {"job": job_id, "run": workflow_run_id},
            )
            worker_id = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO worker_nodes
                       (id,version,capabilities_json,max_concurrent_jobs)
                       VALUES (:id,'g05','[]',1)"""
                ),
                {"id": worker_id},
            )
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

            await _expect_integrity_error(
                connection,
                """INSERT INTO jobs
                   (id,job_type,project_id,owner_user_id,run_id,workflow_run_id,
                    workflow_epoch,origin_trace_id,idempotency_key,max_attempts)
                   VALUES (:id,'workflow_run',:project,:actor,'hidden-thread-run',:run,
                           1,:trace,:key,1)""",
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "actor": actor_id,
                    "run": workflow_run_id,
                    "trace": origin_trace_id,
                    "key": "e" * 64,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO jobs
                   (id,job_type,project_id,owner_user_id,workflow_run_id,
                    workflow_epoch,origin_trace_id,idempotency_key,max_attempts)
                   VALUES (:id,'workflow_run',:project,:actor,:run,1,'wrong-trace',:key,1)""",
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "actor": actor_id,
                    "run": workflow_run_id,
                    "key": "f" * 64,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_run_jobs
                   (workflow_run_id,execution_epoch,job_id,project_id,owner_user_id,cause)
                   VALUES (:run,2,:job,:project,:actor,'resume')""",
                {
                    "run": workflow_run_id,
                    "job": job_id,
                    "project": project_id,
                    "actor": actor_id,
                },
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_effect_lease_event_and_policy_invariants_and_claim_plan(
    postgres_database_url: str,
) -> None:
    await _apply_prototype(postgres_database_url)
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    actor_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    workflow_id = uuid.uuid4()
    version_id = uuid.uuid4()
    workflow_run_id = uuid.uuid4()
    origin_trace_id = f"workflow-trace-{uuid.uuid4()}"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {"id": actor_id, "email": f"{actor_id}@example.com"},
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                       (id,slug,display_name,created_by_user_id)
                       VALUES (:id,:slug,'Workflow G05',:actor)"""
                ),
                {"id": project_id, "slug": f"workflow-g05-{project_id.hex[:12]}", "actor": actor_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                       (id,project_id,user_id,role,status,version)
                       VALUES (:id,:project,:actor,'admin','active',1)"""
                ),
                {"id": membership_id, "project": project_id, "actor": actor_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_definitions
                       (id,project_id,name,status,revision,created_by,updated_by)
                       VALUES (:id,:project,'G05 Workflow','active',1,:actor,:actor)"""
                ),
                {"id": workflow_id, "project": project_id, "actor": actor_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_versions
                       (id,workflow_id,version_number,spec_json,canvas_json,
                        semantic_checksum,compiler_contract_version,published_by)
                       VALUES (:id,:workflow,1,'{}','{}',:checksum,1,:actor)"""
                ),
                {"id": version_id, "workflow": workflow_id, "checksum": "1" * 64, "actor": actor_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_runs
                       (id,project_id,owner_user_id,workflow_id,workflow_version_id,
                        status,input_json,input_digest,idempotency_hash,trigger_kind,
                        origin_trace_id,execution_epoch)
                       VALUES (:id,:project,:actor,:workflow,:version,'running','{}',
                               :input_digest,:idempotency,'manual',:trace,1)"""
                ),
                {
                    "id": workflow_run_id,
                    "project": project_id,
                    "actor": actor_id,
                    "workflow": workflow_id,
                    "version": version_id,
                    "input_digest": "2" * 64,
                    "idempotency": "3" * 64,
                    "trace": origin_trace_id,
                },
            )
            job_id = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO jobs
                       (id,job_type,project_id,owner_user_id,workflow_run_id,
                        workflow_epoch,origin_trace_id,idempotency_key,status,
                        attempt_count,max_attempts)
                       VALUES (:id,'workflow_run',:project,:actor,:run,1,:trace,
                               :key,'running',1,3)"""
                ),
                {
                    "id": job_id,
                    "project": project_id,
                    "actor": actor_id,
                    "run": workflow_run_id,
                    "trace": origin_trace_id,
                    "key": "e" * 64,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_jobs
                       (workflow_run_id,execution_epoch,job_id,project_id,
                        owner_user_id,cause)
                       VALUES (:run,1,:job,:project,:actor,'initial')"""
                ),
                {
                    "run": workflow_run_id,
                    "job": job_id,
                    "project": project_id,
                    "actor": actor_id,
                },
            )
            await connection.execute(
                text("UPDATE workflow_runs SET current_job_id=:job WHERE id=:run"),
                {"job": job_id, "run": workflow_run_id},
            )
            worker_id = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO worker_nodes
                       (id,version,capabilities_json,max_concurrent_jobs)
                       VALUES (:id,'g05-code','[]',1)"""
                ),
                {"id": worker_id},
            )
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
            effect_node = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO workflow_node_effects
                       (id,project_id,owner_user_id,workflow_run_id,node_id,
                        activation_key,operation_key,http_method,status,
                        request_hmac,provider_idempotency_key)
                       VALUES (:id,:project,:actor,:run,:node,'activation-1',
                               :operation,'POST','prepared',:hmac,:provider_key)"""
                ),
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "actor": actor_id,
                    "run": workflow_run_id,
                    "node": effect_node,
                    "operation": "4" * 64,
                    "hmac": "4" * 64,
                    "provider_key": "a" * 64,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_node_effects
                   (id,project_id,owner_user_id,workflow_run_id,node_id,
                    activation_key,operation_key,http_method,status,request_hmac,
                    provider_idempotency_key)
                   VALUES (:id,:project,:actor,:run,:node,'activation-1',
                           :operation,'POST','prepared',:hmac,:provider_key)""",
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "actor": actor_id,
                    "run": workflow_run_id,
                    "node": effect_node,
                    "operation": "8" * 64,
                    "hmac": "8" * 64,
                    "provider_key": "b" * 64,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_node_effects
                   (id,project_id,owner_user_id,workflow_run_id,node_id,
                    activation_key,operation_key,http_method,status,request_hmac,
                    provider_idempotency_key)
                   VALUES (:id,:project,:actor,:run,:node,'activation-2',
                           :operation,'POST','settled',:hmac,:provider_key)""",
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "actor": actor_id,
                    "run": workflow_run_id,
                    "node": uuid.uuid4(),
                    "operation": "5" * 64,
                    "hmac": "5" * 64,
                    "provider_key": "c" * 64,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_node_effects
                       (id,project_id,owner_user_id,workflow_run_id,node_id,
                        activation_key,operation_key,http_method,status,
                        request_hmac,provider_idempotency_key,dispatch_job_id,
                        dispatch_execution_epoch,dispatch_attempt,
                        dispatch_started_at,outcome_json,outcome_digest)
                       VALUES (:id,:project,:actor,:run,:node,'activation-3',
                               :operation,'POST','settled',:hmac,:provider_key,
                               :job,1,1,now(),CAST(:outcome AS jsonb),:digest)"""
                ),
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "actor": actor_id,
                    "run": workflow_run_id,
                    "node": uuid.uuid4(),
                    "operation": "6" * 64,
                    "hmac": "6" * 64,
                    "provider_key": "d" * 64,
                    "job": job_id,
                    "outcome": json.dumps({"kind": "http_error", "status_code": 409}),
                    "digest": "7" * 64,
                },
            )
            for index, invalid_outcome in enumerate(("null", "1", "[]"), start=1):
                await _expect_integrity_error(
                    connection,
                    """INSERT INTO workflow_node_effects
                       (id,project_id,owner_user_id,workflow_run_id,node_id,
                        activation_key,operation_key,http_method,status,
                        request_hmac,provider_idempotency_key,dispatch_job_id,
                        dispatch_execution_epoch,dispatch_attempt,
                        dispatch_started_at,outcome_json,outcome_digest)
                       VALUES (:id,:project,:actor,:run,:node,:activation,
                               :operation,'POST','settled',:hmac,:provider_key,
                               :job,1,1,now(),CAST(:outcome AS jsonb),:digest)""",
                    {
                        "id": uuid.uuid4(),
                        "project": project_id,
                        "actor": actor_id,
                        "run": workflow_run_id,
                        "node": uuid.uuid4(),
                        "activation": f"invalid-outcome-{index}",
                        "operation": f"{index}" * 64,
                        "hmac": f"{index}" * 64,
                        "provider_key": f"{index + 3}" * 64,
                        "job": job_id,
                        "outcome": invalid_outcome,
                        "digest": f"{index + 6}" * 64,
                    },
                )

            lease_node = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO workflow_code_sandbox_leases
                       (id,project_id,owner_user_id,workflow_run_id,node_id,
                        activation_id,activation_attempt,job_id,workflow_epoch,
                        job_attempt_number,worker_id,reconciliation_key_hash,
                        profile_digest,state,execution_lease_token_hash,cleanup_deadline)
                       VALUES (:id,:project,:actor,:run,:node,'activation-code',1,
                               :job,1,1,:worker,:reconciliation,:profile,
                               'provisioning',:fence,now()+interval '5 minutes')"""
                ),
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "actor": actor_id,
                    "run": workflow_run_id,
                    "node": lease_node,
                    "job": job_id,
                    "worker": worker_id,
                    "reconciliation": "7" * 64,
                    "profile": "8" * 64,
                    "fence": "9" * 64,
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_code_sandbox_leases
                   (id,project_id,owner_user_id,workflow_run_id,node_id,
                    activation_id,activation_attempt,job_id,workflow_epoch,
                    job_attempt_number,worker_id,reconciliation_key_hash,
                    profile_digest,state,execution_lease_token_hash,cleanup_deadline)
                   VALUES (:id,:project,:actor,:run,:node,'activation-code',1,
                           :job,1,1,:worker,:reconciliation,:profile,
                           'provisioning',:fence,now()+interval '5 minutes')""",
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "actor": actor_id,
                    "run": workflow_run_id,
                    "node": lease_node,
                    "job": job_id,
                    "worker": worker_id,
                    "reconciliation": "7" * 64,
                    "profile": "8" * 64,
                    "fence": "a" * 64,
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
                   VALUES (:id,:project,:actor,:run,:node,'provisioning-with-locator',1,
                           :job,1,1,:worker,:reconciliation,:profile,
                           'provisioning',:fence,:locator,
                           now()+interval '5 minutes')""",
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "actor": actor_id,
                    "run": workflow_run_id,
                    "node": uuid.uuid4(),
                    "job": job_id,
                    "worker": worker_id,
                    "reconciliation": "7" * 64,
                    "profile": "8" * 64,
                    "fence": "b" * 64,
                    "locator": b"must-not-exist-before-acquire",
                },
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_code_sandbox_leases
                   (id,project_id,owner_user_id,workflow_run_id,node_id,
                    activation_id,activation_attempt,job_id,workflow_epoch,
                    job_attempt_number,worker_id,reconciliation_key_hash,
                    profile_digest,state,execution_lease_token_hash,cleanup_deadline)
                   VALUES (:id,:project,:actor,:run,:node,'running-without-locator',1,
                           :job,1,1,:worker,:reconciliation,:profile,
                           'running',:fence,now()+interval '5 minutes')""",
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "actor": actor_id,
                    "run": workflow_run_id,
                    "node": uuid.uuid4(),
                    "job": job_id,
                    "worker": worker_id,
                    "reconciliation": "7" * 64,
                    "profile": "8" * 64,
                    "fence": "c" * 64,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_code_sandbox_leases
                       (id,project_id,owner_user_id,workflow_run_id,node_id,
                        activation_id,activation_attempt,job_id,workflow_epoch,
                        job_attempt_number,worker_id,reconciliation_key_hash,
                        profile_digest,state,execution_lease_token_hash,
                        cleanup_locator_ciphertext,cleanup_deadline)
                       VALUES (:id,:project,:actor,:run,:node,'running-with-locator',1,
                               :job,1,1,:worker,:reconciliation,:profile,
                               'running',:fence,:locator,
                               now()+interval '5 minutes')"""
                ),
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "actor": actor_id,
                    "run": workflow_run_id,
                    "node": uuid.uuid4(),
                    "job": job_id,
                    "worker": worker_id,
                    "reconciliation": "7" * 64,
                    "profile": "8" * 64,
                    "fence": "d" * 64,
                    "locator": b"opaque-cleanup-locator",
                },
            )

            await connection.execute(
                text(
                    """INSERT INTO workflow_run_event_invariants
                       (workflow_run_id,next_seq) VALUES (:run,1)"""
                ),
                {"run": workflow_run_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_events
                       (workflow_run_id,seq,event_type,payload)
                       VALUES (:run,1,'workflow.run.started','{}')"""
                ),
                {"run": workflow_run_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_events
                       (workflow_run_id,seq,event_type,payload)
                       VALUES (:run,2,'workflow.run.completed','{}')"""
                ),
                {"run": workflow_run_id},
            )
            await _expect_integrity_error(
                connection,
                """INSERT INTO workflow_run_events
                   (workflow_run_id,seq,event_type,payload)
                   VALUES (:run,3,'workflow.run.failed','{}')""",
                {"run": workflow_run_id},
            )

            policy_version_id = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO system_runtime_policies
                       (section,current_version_id,revision,updated_by_user_id)
                       VALUES ('workflow_runtime',:version,1,:actor)"""
                ),
                {"version": policy_version_id, "actor": actor_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO system_runtime_policy_versions
                       (id,section,version_number,schema_version,value,
                        payload_checksum,created_by_user_id)
                       VALUES (:version,'workflow_runtime',1,1,'{}',:checksum,:actor)"""
                ),
                {"version": policy_version_id, "checksum": "b" * 64, "actor": actor_id},
            )

            await connection.execute(text("SET LOCAL enable_seqscan = off"))
            plan = (
                await connection.execute(
                    text(
                        """EXPLAIN (FORMAT JSON)
                           SELECT id
                           FROM jobs
                           WHERE job_type='workflow_run'
                             AND status='queued'
                             AND required_worker_profile_digest IS NULL
                           ORDER BY priority DESC, created_at, id
                           LIMIT 1"""
                    )
                )
            ).scalar_one()
            assert "ix_jobs_workflow_claim" in json.dumps(plan)
    finally:
        await engine.dispose()
