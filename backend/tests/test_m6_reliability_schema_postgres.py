from __future__ import annotations

import asyncio
import importlib
import inspect
import uuid

import pytest
from alembic import command as alembic_command
from sqlalchemy import CheckConstraint, UniqueConstraint, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine

from deerflow.persistence import models as _models  # noqa: F401
from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import _get_alembic_config

M6_TABLES = {
    "thread_event_sequences",
    "jobs",
    "job_attempts",
    "dead_jobs",
    "worker_nodes",
    "project_quotas",
    "project_usage_counters",
    "project_usage_ledger",
    "audit_logs",
    "deletion_tombstones",
    "restore_proofs",
    "reliability_migration_runs",
    "reliability_migration_ledger",
    "reliability_cutover_state",
}


def test_m6_tables_are_registered_in_final_metadata() -> None:
    assert M6_TABLES <= set(Base.metadata.tables)


def test_m6_job_catalog_pins_authority_and_lease_constraints() -> None:
    jobs = Base.metadata.tables["jobs"]
    assert {
        "job_type",
        "project_id",
        "owner_user_id",
        "run_id",
        "automation_occurrence_id",
        "predecessor_dead_job_id",
        "idempotency_key",
        "status",
        "attempt_count",
        "max_attempts",
        "lease_owner_id",
        "lease_token_hash",
        "lease_expires_at",
        "heartbeat_at",
        "retry_safety",
    } <= set(jobs.columns.keys())
    assert {constraint.name for constraint in jobs.constraints if isinstance(constraint, CheckConstraint)} >= {
        "ck_jobs_type",
        "ck_jobs_status",
        "ck_jobs_retry_safety",
        "ck_jobs_authority_shape",
    }
    assert any(isinstance(constraint, UniqueConstraint) and tuple(column.name for column in constraint.columns) == ("job_type", "idempotency_key") for constraint in jobs.constraints)
    assert any(isinstance(constraint, UniqueConstraint) and tuple(column.name for column in constraint.columns) == ("predecessor_dead_job_id",) for constraint in jobs.constraints)
    assert {index.name for index in jobs.indexes} >= {"ix_jobs_claim", "ix_jobs_active_lease"}


def test_m6_thread_event_sequence_catalog_pins_deletion_stable_cursor() -> None:
    sequences = Base.metadata.tables["thread_event_sequences"]
    assert set(sequences.primary_key.columns.keys()) == {
        "project_id",
        "owner_user_id",
        "thread_id",
    }
    assert "high_watermark" in sequences.columns
    assert {constraint.name for constraint in sequences.constraints if isinstance(constraint, CheckConstraint)} >= {"ck_thread_event_sequences_high_watermark"}


def test_m6_existing_rows_receive_nullable_job_authority_columns() -> None:
    runs = Base.metadata.tables["runs"]
    occurrences = Base.metadata.tables["scheduled_task_runs"]
    for column in (
        "job_id",
        "execution_lease_token_hash",
        "execution_lease_expires_at",
        "execution_heartbeat_at",
        "execution_started_at",
        "cancel_requested_at",
        "cancel_reason",
    ):
        assert runs.c[column].nullable is True
    assert occurrences.c.job_id.nullable is True


def test_m6_revision_chain_and_probe_first_finalize() -> None:
    expand = importlib.import_module("deerflow.persistence.migrations.versions.0014_project_reliability_expand")
    finalize = importlib.import_module("deerflow.persistence.migrations.versions.0015_project_reliability_finalize")
    assert expand.down_revision == "0013_project_automation_finalize"
    assert finalize.down_revision == "0014_project_reliability_expand"
    source = inspect.getsource(finalize.upgrade)
    assert source.index("_assert_finalize_ready") < source.index("op.create_foreign_key")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_empty_bootstrap_head_contains_m6_catalog_and_triggers(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            tables = await connection.run_sync(lambda sync: set(sa_inspect(sync).get_table_names()))
            triggers = set(
                (
                    await connection.execute(
                        text(
                            """SELECT trigger_name FROM information_schema.triggers
                            WHERE event_object_schema=current_schema()
                              AND trigger_name IN
                                ('trg_project_usage_ledger_append_only',
                                 'trg_audit_logs_append_only',
                                 'trg_dead_jobs_append_only')"""
                        )
                    )
                ).scalars()
            )
        assert revision == "0015_project_reliability_finalize"
        assert M6_TABLES <= tables
        assert triggers == {
            "trg_project_usage_ledger_append_only",
            "trg_audit_logs_append_only",
            "trg_dead_jobs_append_only",
        }
    finally:
        await engine.dispose()


async def _seed_finalize_evidence(database_url: str) -> None:
    migration_run_id = uuid.uuid4()
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO reliability_migration_runs
                    (id,mode,status,source_fingerprint,backup_proof_digest,
                     source_row_count,source_probe_complete,
                     active_run_probe_complete,started_at,completed_at)
                    VALUES (:id,'execute','completed',:digest,:digest,0,true,true,now(),now())"""
                ),
                {"id": migration_run_id, "digest": "a" * 64},
            )
            for domain in ("jobs", "quotas", "audit", "stream", "recovery"):
                await connection.execute(
                    text(
                        """INSERT INTO reliability_migration_ledger
                        (migration_run_id,domain,source_digest,target_digest,
                         source_row_count,target_row_count,status,completed_at)
                        VALUES (:id,:domain,:digest,:digest,0,0,'complete',now())"""
                    ),
                    {"id": migration_run_id, "domain": domain, "digest": "b" * 64},
                )
            await connection.execute(
                text(
                    """INSERT INTO reliability_cutover_state
                    (id,stage,migration_run_id,source_probe_complete,
                     active_run_probe_complete,quota_backfill_probe_complete,
                     job_relation_probe_complete,audit_trigger_probe_complete,
                     stream_probe_complete,recovery_probe_complete,
                     final_schema_probe_complete,updated_at)
                    VALUES (1,'migration_ready',:id,true,true,true,true,true,true,true,false,now())"""
                ),
                {"id": migration_run_id},
            )
    finally:
        await engine.dispose()


async def _upgrade_to_m6_expand(engine) -> None:
    cfg = _get_alembic_config(engine)
    await asyncio.to_thread(
        alembic_command.upgrade,
        cfg,
        "0008_project_private_work_expand",
    )
    private_finalize = importlib.import_module("deerflow.persistence.migrations.versions.0009_project_private_work_finalize")
    migration_run_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO private_work_migration_runs
                (id,mode,status,source_fingerprint,owner_map_digest,
                 legacy_source_probe_complete,checkpoint_marker_probe_complete,
                 cross_scope_probe_complete,completed_at)
                VALUES (:id,'execute','completed',:digest,:digest,true,true,true,now())"""
            ),
            {"id": migration_run_id, "digest": "c" * 64},
        )
        await connection.execute(
            text(
                """INSERT INTO private_work_migration_ledger
                (migration_run_id,domain,source_key_hash,source_fingerprint,
                 target_digest,status,row_count,byte_count)
                VALUES (:run_id,:domain,:digest,:digest,:digest,'complete',0,0)"""
            ),
            [
                {
                    "run_id": migration_run_id,
                    "domain": domain,
                    "digest": f"{index:064x}",
                }
                for index, domain in enumerate(
                    sorted(private_finalize.FINALIZE_LEDGER_DOMAINS),
                    start=1,
                )
            ],
        )
    await asyncio.to_thread(
        alembic_command.upgrade,
        cfg,
        "0011_private_artifact_tombstone",
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO private_work_cutover_state
                (id,stage,migration_run_id,empty_domain_probe_complete,
                 checkpoint_marker_probe_complete,cutover_at,updated_at)
                VALUES (1,'cutover_complete',:id,false,true,now(),now())"""
            ),
            {"id": migration_run_id},
        )
    await asyncio.to_thread(
        alembic_command.upgrade,
        cfg,
        "0014_project_reliability_expand",
    )


def _table_catalog(sync_connection, table_name: str) -> dict[str, set[object]]:
    inspector = sa_inspect(sync_connection)
    return {
        "columns": {column["name"] for column in inspector.get_columns(table_name)},
        "checks": {constraint["name"] for constraint in inspector.get_check_constraints(table_name)},
        "uniques": {tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(table_name)},
        "foreign_keys": {
            (
                tuple(constraint["constrained_columns"]),
                constraint["referred_table"],
                tuple(constraint["referred_columns"]),
            )
            for constraint in inspector.get_foreign_keys(table_name)
        },
        "indexes": {index["name"] for index in inspector.get_indexes(table_name) if not index.get("duplicates_constraint")},
    }


def _metadata_catalog(table_name: str) -> dict[str, set[object]]:
    table = Base.metadata.tables[table_name]
    return {
        "columns": set(table.columns.keys()),
        "checks": {constraint.name for constraint in table.constraints if isinstance(constraint, CheckConstraint)},
        "uniques": {tuple(column.name for column in constraint.columns) for constraint in table.constraints if isinstance(constraint, UniqueConstraint)},
        "foreign_keys": {
            (
                tuple(element.parent.name for element in constraint.elements),
                constraint.elements[0].target_fullname.split(".", maxsplit=1)[0],
                tuple(element.target_fullname.split(".", maxsplit=1)[1] for element in constraint.elements),
            )
            for constraint in table.foreign_key_constraints
        },
        "indexes": {index.name for index in table.indexes},
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_staged_migration_reaches_final_schema_after_probe_evidence(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await _upgrade_to_m6_expand(engine)
        await engine.dispose()
        await _seed_finalize_evidence(postgres_database_url)
        engine = create_async_engine(postgres_database_url)
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(alembic_command.upgrade, cfg, "head")

        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            run_foreign_keys = await connection.run_sync(lambda sync: {item["name"] for item in sa_inspect(sync).get_foreign_keys("runs")})
            occurrence_foreign_keys = await connection.run_sync(lambda sync: {item["name"] for item in sa_inspect(sync).get_foreign_keys("scheduled_task_runs")})
            marker = (
                await connection.execute(
                    text(
                        """SELECT stage,final_schema_probe_complete,
                        schema_revision,cutover_at
                        FROM reliability_cutover_state WHERE id=1"""
                    )
                )
            ).one()
            staged_catalog = {table_name: await connection.run_sync(lambda sync, name=table_name: _table_catalog(sync, name)) for table_name in sorted(M6_TABLES)}
        assert revision == "0015_project_reliability_finalize"
        assert "fk_runs_job" in run_foreign_keys
        assert "fk_scheduled_task_runs_job" in occurrence_foreign_keys
        assert marker.stage == "cutover_complete"
        assert marker.final_schema_probe_complete is True
        assert marker.schema_revision == "0015_project_reliability_finalize"
        assert marker.cutover_at is not None
        assert staged_catalog == {table_name: _metadata_catalog(table_name) for table_name in sorted(M6_TABLES)}
    finally:
        await engine.dispose()
