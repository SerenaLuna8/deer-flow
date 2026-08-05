from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_memory_pr2_contract import (
    MEMORY_JOB_TYPES,
    MEMORY_V2_FOREIGN_KEYS,
    MEMORY_V2_TABLES,
)

import deerflow.persistence.models  # noqa: F401
from app.reliability.workers import WorkerRegistry
from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import M7RecreateRequired, classify_database
from deerflow.persistence.jobs.sql import EnqueueJob, JobRepository, JobScope

SCHEMA_PARITY_TABLES = MEMORY_V2_TABLES | {
    "jobs",
    "job_attempts",
    "run_runtime_policy_snapshots",
}


def _reflect_schema_contract(sync_connection) -> dict[str, dict[str, object]]:
    inspector = inspect(sync_connection)
    reflected: dict[str, dict[str, object]] = {}
    for table_name in sorted(SCHEMA_PARITY_TABLES):
        checks = {item["name"] for item in inspector.get_check_constraints(table_name)}
        foreign_keys = {
            (
                tuple(item["constrained_columns"]),
                item["referred_table"],
                tuple(item["referred_columns"]),
            )
            for item in inspector.get_foreign_keys(table_name)
        }
        unique_constraints = {(item["name"], tuple(item["column_names"])) for item in inspector.get_unique_constraints(table_name)}
        index_names = {item["name"] for item in inspector.get_indexes(table_name) if not item.get("duplicates_constraint")}
        reflected[table_name] = {
            "columns": {item["name"]: item["nullable"] for item in inspector.get_columns(table_name)},
            "checks": checks,
            "foreign_keys": foreign_keys,
            "unique_constraints": unique_constraints,
            "indexes": index_names,
        }
    return reflected


@pytest.mark.asyncio
async def test_memory_v2_full_schema_contract(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.connect() as connection:
            marker = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            tables = frozenset(
                str(value)
                for value in (
                    await connection.execute(
                        text(
                            """SELECT tablename FROM pg_tables
                            WHERE schemaname=current_schema()
                              AND tablename = ANY(CAST(:tables AS text[]))"""
                        ),
                        {"tables": sorted(MEMORY_V2_TABLES)},
                    )
                ).scalars()
            )
            job_checks = {
                str(name): str(definition)
                for name, definition in (
                    await connection.execute(
                        text(
                            """SELECT conname, pg_get_constraintdef(oid)
                            FROM pg_constraint
                            WHERE conrelid='jobs'::regclass
                              AND conname IN (
                                  'ck_jobs_type',
                                  'ck_jobs_authority_shape',
                                  'ck_jobs_memory_namespace'
                              )
                            ORDER BY conname"""
                        )
                    )
                )
            }
            scoped_memory_fks = {
                str(name): str(definition)
                for name, definition in (
                    await connection.execute(
                        text(
                            """SELECT c.conname, pg_get_constraintdef(c.oid)
                            FROM pg_constraint c
                            JOIN pg_class child ON child.oid=c.conrelid
                            JOIN pg_class parent ON parent.oid=c.confrelid
                            WHERE c.contype='f'
                              AND child.relname = ANY(CAST(:tables AS text[]))
                              AND parent.relname = ANY(CAST(:tables AS text[]))"""
                        ),
                        {"tables": sorted(MEMORY_V2_TABLES)},
                    )
                )
            }
            reflected_contract = await connection.run_sync(_reflect_schema_contract)

        assert marker == "full_schema_v3"
        assert tables == MEMORY_V2_TABLES
        assert set(job_checks) == {
            "ck_jobs_type",
            "ck_jobs_authority_shape",
            "ck_jobs_memory_namespace",
        }
        for job_type in MEMORY_JOB_TYPES:
            assert job_type in job_checks["ck_jobs_type"]
            assert job_type in job_checks["ck_jobs_authority_shape"]
            assert job_type in job_checks["ck_jobs_memory_namespace"]
        assert "namespace" in job_checks["ck_jobs_authority_shape"]
        assert "namespace" in job_checks["ck_jobs_memory_namespace"]
        assert set(scoped_memory_fks) == MEMORY_V2_FOREIGN_KEYS
        assert all("project_id" in definition and "owner_user_id" in definition and "namespace" in definition for definition in scoped_memory_fks.values())
        for table_name in sorted(SCHEMA_PARITY_TABLES):
            orm_table = Base.metadata.tables[table_name]
            assert reflected_contract[table_name]["columns"] == {column.name: column.nullable for column in orm_table.columns}
            assert reflected_contract[table_name]["checks"] == {constraint.name for constraint in orm_table.constraints if isinstance(constraint, CheckConstraint)}
            assert reflected_contract[table_name]["foreign_keys"] == {
                (
                    tuple(element.parent.name for element in constraint.elements),
                    constraint.referred_table.name,
                    tuple(element.column.name for element in constraint.elements),
                )
                for constraint in orm_table.constraints
                if isinstance(constraint, ForeignKeyConstraint)
            }
            assert reflected_contract[table_name]["unique_constraints"] == {
                (
                    constraint.name,
                    tuple(column.name for column in constraint.columns),
                )
                for constraint in orm_table.constraints
                if isinstance(constraint, UniqueConstraint)
            }
            assert reflected_contract[table_name]["indexes"] == {index.name for index in orm_table.indexes}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_memory_jobs_preserve_owner_and_namespace_through_claim(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'user',now(),false,0)"""
                ),
                {
                    "id": str(owner_id),
                    "email": f"{owner_id}@example.com",
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                    (id,slug,display_name,created_by_user_id)
                    VALUES (:id,:slug,'Memory Jobs',:owner)"""
                ),
                {
                    "id": project_id,
                    "slug": f"memory-jobs-{project_id.hex[:12]}",
                    "owner": str(owner_id),
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                    (id,project_id,user_id,role,status,version)
                    VALUES (:id,:project,:user,'admin','active',1)"""
                ),
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "user": str(owner_id),
                },
            )

        await WorkerRegistry(factory, version="memory-pr2-test").register(
            worker_id,
            MEMORY_JOB_TYPES,
            len(MEMORY_JOB_TYPES),
        )

        namespaces = {job_type: f"namespace-{job_type}" for job_type in MEMORY_JOB_TYPES}
        async with factory() as session, session.begin():
            repository = JobRepository(session)
            for job_type in sorted(MEMORY_JOB_TYPES):
                await repository.enqueue(
                    EnqueueJob(
                        job_type=job_type,
                        scope=JobScope(project_id, str(owner_id)),
                        idempotency_key=hashlib.sha256(job_type.encode()).hexdigest(),
                        run_id=None,
                        occurrence_id=None,
                        max_attempts=3,
                        namespace=namespaces[job_type],
                        memory_retention_cutoff_at=(datetime.now(UTC) - timedelta(days=30) if job_type == "memory_retention_purge" else None),
                    )
                )

        for job_type in sorted(MEMORY_JOB_TYPES):
            async with factory() as session, session.begin():
                claim = await JobRepository(session).claim_next(
                    worker_id=worker_id,
                    capabilities=frozenset({job_type}),
                    lease_seconds=60,
                )
                assert claim is not None
                assert claim.job_type == job_type
                assert claim.scope == JobScope(project_id, str(owner_id))
                assert claim.namespace == namespaces[job_type]
                assert claim.run_id is None
                assert claim.occurrence_id is None
                assert claim.origin_trace_id is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported_marker", ("full_schema_v1", "full_schema_unknown"))
async def test_unsupported_marker_is_rejected_without_mutation(
    migrated_postgres_database_url: str,
    unsupported_marker: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num=:marker"),
                {"marker": unsupported_marker},
            )

        async with engine.connect() as connection:
            with pytest.raises(M7RecreateRequired):
                await classify_database(connection)
            marker = await connection.scalar(text("SELECT version_num FROM alembic_version"))

        assert marker == unsupported_marker
    finally:
        await engine.dispose()
